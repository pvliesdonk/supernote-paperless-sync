"""Inbound pipeline: watch Supernote Note/ directory and ingest .note files into Paperless.

The heavy work (conversion + HTTP upload) runs inside asyncio.to_thread() so
the async event loop stays unblocked.  The sync PaperlessClient is safe to use
there because supernotelib's multiprocessing imports can't corrupt the thread's
own execution context.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI
from watchfiles import Change, awatch

from .config import Settings
from .converter import get_pdf_for_note
from .db import (
    get_ingested_doc_id,
    get_ingested_hash,
    get_ingested_mtime,
    record_ingestion,
)
from .ocr import embed_text_layer, ocr_pdf, suggest_metadata
from .paperless import PaperlessClient

log = logging.getLogger(__name__)


def _mtime_to_date(path: Path) -> str:
    """Return the file mtime as a ``YYYY-MM-DD`` string."""
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_note_date(note_path: Path) -> str:
    """Extract creation date from filename or fall back to mtime.

    Supports filenames like ``20240315_143022.note`` (YYYYMMDD_HHMMSS).
    """
    m = re.match(
        r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$", note_path.stem
    )
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    return _mtime_to_date(note_path)


def _derive_correspondent(note_path: Path, override: str | None) -> str:
    """Derive the correspondent name from the note's account directory.

    The Supernote Note directory lives at
    ``…/<account>@<domain>/Supernote/Note/<file>.note``.
    We walk up three levels to find the account directory name.

    Args:
        note_path: Absolute path to the ``.note`` file.
        override: If non-empty, return this instead.

    Returns:
        Correspondent name string.
    """
    if override:
        return override
    # note_path.parent = Note/
    # note_path.parent.parent = Supernote/
    # note_path.parent.parent.parent = <account@domain>/
    try:
        return note_path.parent.parent.parent.name
    except Exception:
        return "Supernote"


def _ingest_note_sync(
    note_path: Path,
    settings: Settings,
    client: PaperlessClient,
    llm_client: OpenAI,
    inbound_tag_id: int,
    completion_tag_id: int,
    superseded_tag_id: int,
    document_type_id: int | None,
) -> str:
    """Convert a .note file and upload it to Paperless (fully synchronous).

    Returns a status string: 'skipped', 'ingested', or 'updated'.
    Runs inside asyncio.to_thread() — no async I/O allowed here.
    """
    # --- Change detection: mtime first (fast), then hash (content guard) ---
    mtime_ns = note_path.stat().st_mtime_ns
    stored_mtime = get_ingested_mtime(settings.state_db, str(note_path))
    old_doc_id = get_ingested_doc_id(settings.state_db, str(note_path))

    if stored_mtime == mtime_ns:
        # File not touched since last ingest — nothing to do.
        return "skipped"

    # mtime changed (or never ingested): convert and compare content hash to guard
    # against spurious mtime updates where content is actually unchanged.
    pdf_bytes = get_pdf_for_note(note_path, settings.notelib_convert_dir)
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    stored_hash = get_ingested_hash(settings.state_db, str(note_path))

    if content_hash == stored_hash:
        # Content unchanged — record new mtime and skip upload.
        record_ingestion(
            settings.state_db, str(note_path), mtime_ns, old_doc_id, content_hash
        )
        log.debug("mtime_changed_content_unchanged note=%s", note_path.name)
        return "skipped"

    is_update = old_doc_id is not None

    log.info("converting_note note=%s update=%s", note_path.name, is_update)

    # --- OCR ---
    ocr_text = ocr_pdf(pdf_bytes, llm_client, settings.vision_llm_model)
    log.debug("ocr_done note=%s chars=%d", note_path.name, len(ocr_text))

    # --- Metadata suggestion ---
    meta = suggest_metadata(
        ocr_text, note_path.name, llm_client, settings.llm_model
    )
    suggested_title: str = meta["title"]
    suggested_tag_names: list[str] = meta["tags"]

    # --- Embed text layer ---
    pdf_with_text = embed_text_layer(pdf_bytes, ocr_text)

    # --- Resolve tag IDs ---
    # Do NOT include inbound_tag (e.g. paperless-gpt-ocr-auto) in the upload —
    # that would trigger the paperless-gpt → docling-md pipeline which would
    # overwrite our OCR content with base64 image markdown.
    # We only apply the completion tag + LLM-suggested tags.
    suggested_tag_ids = [client.get_or_create_tag(t) for t in suggested_tag_names]
    upload_tag_ids = list({completion_tag_id, *suggested_tag_ids})

    # --- Derive correspondent ---
    correspondent_name = _derive_correspondent(
        note_path, settings.inbound_correspondent_override
    )
    correspondent_id = client.get_or_create_correspondent(correspondent_name)

    # --- Creation date from filename ---
    created_date = _parse_note_date(note_path)

    # --- Upload ---
    doc_id = client.upload_document(
        pdf_with_text,
        filename=f"{note_path.stem}.pdf",
        tag_ids=upload_tag_ids,
        correspondent_id=correspondent_id,
        document_type_id=document_type_id,
        created_date=created_date,
    )

    # --- Patch content and title ---
    client.patch_document(doc_id, {"content": ocr_text, "title": suggested_title})

    # --- Mark old document superseded (if update) ---
    if is_update and old_doc_id is not None:
        try:
            old_doc = client.get_document(old_doc_id)
            old_tags = list(set(old_doc.get("tags", [])) | {superseded_tag_id})
            # Preserve original created date on the old document (no-op, just add tag)
            client.patch_document(old_doc_id, {"tags": old_tags})
            log.info("marked_superseded old_doc_id=%d", old_doc_id)
        except Exception:
            log.warning(
                "failed_to_mark_superseded old_doc_id=%d", old_doc_id, exc_info=True
            )

    mtime_ns = note_path.stat().st_mtime_ns
    record_ingestion(settings.state_db, str(note_path), mtime_ns, doc_id, content_hash)
    log.info(
        "ingested_note note=%s doc_id=%d update=%s created=%s title=%r",
        note_path.name,
        doc_id,
        is_update,
        created_date,
        suggested_title,
    )
    return "updated" if is_update else "ingested"


async def _process_note(
    note_path: Path,
    settings: Settings,
    client: PaperlessClient,
    llm_client: OpenAI,
    inbound_tag_id: int,
    completion_tag_id: int,
    superseded_tag_id: int,
    document_type_id: int | None,
) -> None:
    """Process one .note file asynchronously (dispatches sync work to thread)."""
    try:
        status = await asyncio.to_thread(
            _ingest_note_sync,
            note_path,
            settings,
            client,
            llm_client,
            inbound_tag_id,
            completion_tag_id,
            superseded_tag_id,
            document_type_id,
        )
        if status != "skipped":
            log.info("process_note_done note=%s status=%s", note_path.name, status)
    except Exception as exc:
        log.error("ingest_failed note=%s error=%s", note_path.name, exc, exc_info=True)


async def _scan_existing(
    settings: Settings,
    client: PaperlessClient,
    llm_client: OpenAI,
    inbound_tag_id: int,
    completion_tag_id: int,
    superseded_tag_id: int,
    document_type_id: int | None,
) -> None:
    """Ingest any .note files that haven't been ingested yet (startup catch-up)."""
    note_dir = settings.supernote_note_dir
    notes = list(note_dir.glob("*.note"))
    log.info("startup_scan count=%d", len(notes))
    for note_path in notes:
        await _process_note(
            note_path,
            settings,
            client,
            llm_client,
            inbound_tag_id,
            completion_tag_id,
            superseded_tag_id,
            document_type_id,
        )


async def run_inbound_watcher(settings: Settings, client: PaperlessClient) -> None:
    """Main inbound coroutine: resolves tag and metadata IDs, scans existing notes, watches for changes."""
    llm_client = OpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
    )

    inbound_tag_id = client.get_tag_id(settings.inbound_tag)
    if inbound_tag_id is None:
        raise RuntimeError(
            f"Inbound tag '{settings.inbound_tag}' not found in Paperless — create it first"
        )
    log.info("inbound_tag_resolved name=%s id=%d", settings.inbound_tag, inbound_tag_id)

    completion_tag_id = client.get_or_create_tag(settings.inbound_completion_tag)
    log.info(
        "completion_tag_resolved name=%s id=%d",
        settings.inbound_completion_tag,
        completion_tag_id,
    )

    superseded_tag_id = client.get_or_create_tag(settings.superseded_tag)
    log.info(
        "superseded_tag_resolved name=%s id=%d",
        settings.superseded_tag,
        superseded_tag_id,
    )

    document_type_id: int | None = None
    if settings.inbound_document_type:
        document_type_id = client.get_or_create_document_type(
            settings.inbound_document_type
        )
        log.info(
            "document_type_resolved name=%s id=%d",
            settings.inbound_document_type,
            document_type_id,
        )

    await _scan_existing(
        settings,
        client,
        llm_client,
        inbound_tag_id,
        completion_tag_id,
        superseded_tag_id,
        document_type_id,
    )

    log.info("watching_notes directory=%s", settings.supernote_note_dir)
    async for changes in awatch(settings.supernote_note_dir):
        for change_type, path_str in changes:
            path = Path(path_str)
            if path.suffix != ".note":
                continue
            if change_type in (Change.added, Change.modified):
                await _process_note(
                    path,
                    settings,
                    client,
                    llm_client,
                    inbound_tag_id,
                    completion_tag_id,
                    superseded_tag_id,
                    document_type_id,
                )
