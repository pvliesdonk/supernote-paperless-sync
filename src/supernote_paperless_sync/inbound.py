"""Inbound pipeline: watch Supernote Note/ directory and ingest .note files into Paperless.

The heavy work (conversion + HTTP upload) runs inside asyncio.to_thread() so
the async event loop stays unblocked.  The sync PaperlessClient is safe to use
there because supernotelib's multiprocessing imports can't corrupt the thread's
own execution context.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from watchfiles import awatch, Change

from .config import Settings
from .converter import get_pdf_for_note
from .db import get_ingested_mtime, record_ingestion
from .paperless import PaperlessClient

log = logging.getLogger(__name__)


def _mtime_to_date(path: Path) -> str:
    """Return the file mtime as a ``YYYY-MM-DD`` string."""
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")


def _ingest_note_sync(
    note_path: Path,
    settings: Settings,
    client: PaperlessClient,
    tag_id: int,
    correspondent_id: int | None,
    document_type_id: int | None,
) -> str:
    """Convert a .note file and upload it to Paperless (fully synchronous).

    Returns a status string: 'skipped', 'ingested', or 'updated'.
    Runs inside asyncio.to_thread() — no async I/O allowed here.
    """
    mtime_ns = note_path.stat().st_mtime_ns
    existing_mtime = get_ingested_mtime(settings.state_db, str(note_path))

    if existing_mtime == mtime_ns:
        return "skipped"

    is_update = existing_mtime is not None
    log.info("converting_note note=%s update=%s", note_path.name, is_update)

    pdf_data = get_pdf_for_note(note_path, settings.notelib_convert_dir)
    created_date = _mtime_to_date(note_path)

    doc_id = client.upload_document(
        pdf_data,
        filename=f"{note_path.stem}.pdf",
        tag_ids=[tag_id],
        correspondent_id=correspondent_id,
        document_type_id=document_type_id,
        created_date=created_date,
    )

    record_ingestion(settings.state_db, str(note_path), mtime_ns, doc_id)
    log.info(
        "ingested_note note=%s doc_id=%d update=%s created=%s",
        note_path.name, doc_id, is_update, created_date,
    )
    return "updated" if is_update else "ingested"


async def _process_note(
    note_path: Path,
    settings: Settings,
    client: PaperlessClient,
    tag_id: int,
    correspondent_id: int | None,
    document_type_id: int | None,
) -> None:
    """Process one .note file asynchronously (dispatches sync work to thread)."""
    try:
        status = await asyncio.to_thread(
            _ingest_note_sync, note_path, settings, client,
            tag_id, correspondent_id, document_type_id,
        )
        if status != "skipped":
            log.info("process_note_done note=%s status=%s", note_path.name, status)
    except Exception as exc:
        log.error("ingest_failed note=%s error=%s", note_path.name, exc, exc_info=True)


async def _scan_existing(
    settings: Settings,
    client: PaperlessClient,
    tag_id: int,
    correspondent_id: int | None,
    document_type_id: int | None,
) -> None:
    """Ingest any .note files that haven't been ingested yet (startup catch-up)."""
    note_dir = settings.supernote_note_dir
    notes = list(note_dir.glob("*.note"))
    log.info("startup_scan count=%d", len(notes))
    for note_path in notes:
        await _process_note(note_path, settings, client, tag_id, correspondent_id, document_type_id)


async def run_inbound_watcher(settings: Settings, client: PaperlessClient) -> None:
    """Main inbound coroutine: resolves tag and metadata IDs, scans existing notes, watches for changes."""
    tag_id = client.get_tag_id(settings.inbound_tag)
    if tag_id is None:
        raise RuntimeError(
            f"Inbound tag '{settings.inbound_tag}' not found in Paperless — create it first"
        )
    log.info("inbound_tag_resolved name=%s id=%d", settings.inbound_tag, tag_id)

    correspondent_id: int | None = None
    if settings.inbound_correspondent:
        correspondent_id = client.get_or_create_correspondent(settings.inbound_correspondent)
        log.info("correspondent_resolved name=%s id=%d", settings.inbound_correspondent, correspondent_id)

    document_type_id: int | None = None
    if settings.inbound_document_type:
        document_type_id = client.get_or_create_document_type(settings.inbound_document_type)
        log.info("document_type_resolved name=%s id=%d", settings.inbound_document_type, document_type_id)

    await _scan_existing(settings, client, tag_id, correspondent_id, document_type_id)

    log.info("watching_notes directory=%s", settings.supernote_note_dir)
    async for changes in awatch(settings.supernote_note_dir):
        for change_type, path_str in changes:
            path = Path(path_str)
            if path.suffix != ".note":
                continue
            if change_type in (Change.added, Change.modified):
                await _process_note(path, settings, client, tag_id, correspondent_id, document_type_id)
