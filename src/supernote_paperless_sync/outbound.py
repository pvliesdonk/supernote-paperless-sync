"""Outbound sync: export tagged Paperless documents to Supernote Document folder."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import structlog

from .config import Settings
from .db import (
    get_exported_doc_ids,
    get_exported_path,
    record_export,
    remove_export_record,
)
from .paperless import PaperlessClient, PaperlessError

log = structlog.get_logger()

_INVALID_CHARS = '<>:"/\\|?*'


def _safe_filename(title: str, ext: str) -> str:
    """
    Convert a Paperless document title to a safe filename for the Supernote.

    Args:
        title: Raw document title from Paperless.
        ext: File extension including the leading dot (e.g. '.pdf').

    Returns:
        A filename safe for FAT/exFAT filesystems used by Supernote.
    """
    stem = title
    for ch in _INVALID_CHARS:
        stem = stem.replace(ch, "_")
    # Collapse multiple underscores, strip leading/trailing whitespace/dots
    stem = stem.strip(" .")[:180]
    return f"{stem}{ext}"


async def _export_document(
    doc: dict,
    settings: Settings,
    client: PaperlessClient,
) -> None:
    """Download a Paperless document and write it to the Supernote Document folder."""
    doc_id: int = doc["id"]
    title: str = doc.get("title") or f"document_{doc_id}"

    try:
        content, original_filename = await client.download_document(doc_id)
    except PaperlessError as exc:
        log.error("download_failed", doc_id=doc_id, error=str(exc))
        return

    ext = Path(original_filename).suffix or ".pdf"
    filename = _safe_filename(title, ext)

    dest_dir = settings.supernote_doc_dir / settings.outbound_subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    # Handle filename collision by appending the doc ID
    if dest_path.exists():
        stem = Path(filename).stem
        filename = f"{stem}_{doc_id}{ext}"
        dest_path = dest_dir / filename

    dest_path.write_bytes(content)

    checksum = hashlib.sha256(content).hexdigest()[:16]
    record_export(settings.state_db, doc_id, str(dest_path), checksum)
    log.info("document_exported", doc_id=doc_id, title=title, filename=filename)


async def _remove_document(doc_id: int, settings: Settings) -> None:
    """
    Delete an exported document from Supernote when its tag was removed.

    Safety: only deletes files that live inside the managed subfolder.
    """
    path_str = get_exported_path(settings.state_db, doc_id)
    if not path_str:
        remove_export_record(settings.state_db, doc_id)
        return

    path = Path(path_str)
    managed_dir = settings.supernote_doc_dir / settings.outbound_subfolder

    # Guard: refuse to delete files outside the managed subfolder
    try:
        path.relative_to(managed_dir)
    except ValueError:
        log.warning(
            "remove_outside_managed_dir",
            doc_id=doc_id,
            path=path_str,
            managed=str(managed_dir),
        )
        remove_export_record(settings.state_db, doc_id)
        return

    if path.exists():
        path.unlink()
        log.info("document_removed", doc_id=doc_id, path=path.name)
    else:
        log.warning("remove_file_not_found", doc_id=doc_id, path=path_str)

    remove_export_record(settings.state_db, doc_id)


async def _sync_once(
    tag_id: int,
    settings: Settings,
    client: PaperlessClient,
) -> None:
    """Run one outbound sync cycle."""
    try:
        tagged_docs = await client.list_documents_by_tag(tag_id)
    except PaperlessError as exc:
        log.error("list_tagged_failed", error=str(exc))
        return

    tagged_ids = {doc["id"] for doc in tagged_docs}
    exported_ids = get_exported_doc_ids(settings.state_db)

    new_ids = tagged_ids - exported_ids
    stale_ids = exported_ids - tagged_ids

    if new_ids:
        log.info("outbound_new_docs", count=len(new_ids))
    if stale_ids:
        log.info("outbound_stale_docs", count=len(stale_ids))

    # Export new documents
    doc_map = {doc["id"]: doc for doc in tagged_docs}
    for doc_id in new_ids:
        await _export_document(doc_map[doc_id], settings, client)

    # Remove documents whose tag was removed
    for doc_id in stale_ids:
        await _remove_document(doc_id, settings)


async def run_outbound_sync(settings: Settings, client: PaperlessClient) -> None:
    """
    Main outbound loop.

    1. Resolve the outbound tag ID (fails loudly if the tag doesn't exist).
    2. Poll Paperless every poll_interval seconds, syncing tagged documents.
    """
    tag_id = await client.get_tag_id(settings.outbound_tag)
    if tag_id is None:
        raise RuntimeError(
            f"Outbound tag '{settings.outbound_tag}' not found in Paperless. "
            "Create it in the Paperless UI before starting."
        )
    log.info("outbound_tag_resolved", tag=settings.outbound_tag, id=tag_id)

    while True:
        await _sync_once(tag_id, settings, client)
        await asyncio.sleep(settings.poll_interval)
