"""Inbound sync: watch .note files and ingest them into Paperless-ngx."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from watchfiles import Change, awatch

from .config import Settings
from .converter import get_pdf_for_note
from .db import get_ingested_mtime, record_ingestion
from .paperless import PaperlessClient, PaperlessError

log = structlog.get_logger()


async def _process_note(
    note_path: Path,
    settings: Settings,
    client: PaperlessClient,
    tag_id: int,
) -> None:
    """Convert a single .note file and upload it to Paperless if new or modified."""
    try:
        stat = note_path.stat()
    except FileNotFoundError:
        log.warning("note_disappeared", note=note_path.name)
        return

    mtime_ns = stat.st_mtime_ns
    stored_mtime = get_ingested_mtime(settings.state_db, str(note_path))

    if stored_mtime == mtime_ns:
        log.debug("note_unchanged_skip", note=note_path.name)
        return

    is_update = stored_mtime is not None
    log.info(
        "processing_note",
        note=note_path.name,
        action="update" if is_update else "new",
    )

    # Conversion is CPU-bound — run in a thread pool to avoid blocking the loop
    loop = asyncio.get_running_loop()
    try:
        pdf_data: bytes = await loop.run_in_executor(
            None,
            get_pdf_for_note,
            note_path,
            settings.notelib_convert_dir,
        )
    except RuntimeError as exc:
        log.error("conversion_failed", note=note_path.name, error=str(exc))
        return

    filename = f"{note_path.stem}.pdf"
    try:
        doc_id = await client.upload_document(pdf_data, filename, [tag_id])
    except PaperlessError as exc:
        log.error("upload_failed", note=note_path.name, error=str(exc))
        return

    record_ingestion(settings.state_db, str(note_path), mtime_ns, doc_id)
    log.info(
        "note_ingested",
        note=note_path.name,
        doc_id=doc_id,
        action="update" if is_update else "new",
    )


async def _scan_existing(
    settings: Settings, client: PaperlessClient, tag_id: int
) -> None:
    """On startup, ingest any notes that were missed while the service was down."""
    note_dir = settings.supernote_note_dir
    if not note_dir.is_dir():
        log.warning("note_dir_missing", path=str(note_dir))
        return

    notes = sorted(note_dir.glob("*.note"))
    if not notes:
        log.info("no_notes_found", path=str(note_dir))
        return

    log.info("startup_scan", count=len(notes), path=str(note_dir))
    for note_path in notes:
        await _process_note(note_path, settings, client, tag_id)


async def run_inbound_watcher(settings: Settings, client: PaperlessClient) -> None:
    """
    Main inbound loop.

    1. Resolve the inbound tag ID (fails loudly if the tag doesn't exist).
    2. Scan for any notes missed while offline.
    3. Watch for new / modified .note files continuously.
    """
    tag_id = await client.get_tag_id(settings.inbound_tag)
    if tag_id is None:
        raise RuntimeError(
            f"Inbound tag '{settings.inbound_tag}' not found in Paperless. "
            "Create it in the Paperless UI before starting."
        )
    log.info("inbound_tag_resolved", tag=settings.inbound_tag, id=tag_id)

    await _scan_existing(settings, client, tag_id)

    log.info("watching_note_dir", path=str(settings.supernote_note_dir))
    async for changes in awatch(settings.supernote_note_dir):
        for change_type, path_str in changes:
            path = Path(path_str)
            if path.suffix.lower() != ".note":
                continue
            if change_type in (Change.added, Change.modified):
                await _process_note(path, settings, client, tag_id)
