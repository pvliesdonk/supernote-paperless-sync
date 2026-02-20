"""Outbound pipeline: sync Paperless documents tagged 'send-to-supernote' to Supernote.

Polls Paperless on an interval.  Downloads and writes to Document/Paperless/.
If the tag is removed, the file is deleted from Supernote.

All Paperless API calls are sync (httpx.Client) and run inside asyncio.to_thread()
to keep the event loop free without touching asyncio internals from the thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from .config import Settings
from .db import (
    get_exported_doc_ids,
    get_exported_path,
    record_export,
    remove_export_record,
)
from .paperless import PaperlessClient

log = logging.getLogger(__name__)

_INVALID_CHARS = '<>:"/\\|?*'


def _safe_filename(title: str, ext: str) -> str:
    """Convert a Paperless document title to a safe filename."""
    for ch in _INVALID_CHARS:
        title = title.replace(ch, "_")
    title = title.strip()[:180]
    return f"{title}{ext}"


def _export_document_sync(
    doc: dict,
    settings: Settings,
    client: PaperlessClient,
) -> None:
    """Download one document from Paperless and write to Supernote (sync)."""
    doc_id: int = doc["id"]
    title: str = doc.get("title") or f"document_{doc_id}"

    content, original_filename = client.download_document(doc_id)

    ext = Path(original_filename).suffix if original_filename else ".pdf"
    filename = _safe_filename(title, ext)

    dest_dir = settings.supernote_doc_dir / settings.outbound_subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / filename
    # Handle collision: append doc_id to stem
    if dest_path.exists():
        stem = dest_path.stem
        dest_path = dest_dir / f"{stem}_{doc_id}{ext}"

    dest_path.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()[:16]

    record_export(settings.state_db, doc_id, str(dest_path), checksum)
    log.info("exported_document doc_id=%d path=%s", doc_id, dest_path.name)


def _remove_document_sync(doc_id: int, settings: Settings) -> None:
    """Delete a previously exported document from Supernote (sync)."""
    path_str = get_exported_path(settings.state_db, doc_id)
    if not path_str:
        remove_export_record(settings.state_db, doc_id)
        return

    path = Path(path_str)

    # Safety: only delete files inside the managed subfolder
    managed_dir = settings.supernote_doc_dir / settings.outbound_subfolder
    try:
        path.relative_to(managed_dir)
    except ValueError:
        log.warning(
            "remove_skipped_unsafe doc_id=%d path=%s not_under=%s",
            doc_id,
            path,
            managed_dir,
        )
        remove_export_record(settings.state_db, doc_id)
        return

    if path.exists():
        path.unlink()
        log.info("removed_document doc_id=%d path=%s", doc_id, path.name)
    else:
        log.warning("remove_missing doc_id=%d path=%s", doc_id, path)

    remove_export_record(settings.state_db, doc_id)


def _sync_once(
    settings: Settings,
    client: PaperlessClient,
    tag_id: int,
) -> tuple[int, int]:
    """One outbound sync pass: export new docs, remove stale ones (sync).

    Returns (exported_count, removed_count).
    """
    tagged_docs = client.list_documents_by_tag(tag_id)
    tagged_ids = {doc["id"] for doc in tagged_docs}
    exported_ids = get_exported_doc_ids(settings.state_db)

    exported = 0
    removed = 0

    for doc in tagged_docs:
        if doc["id"] not in exported_ids:
            try:
                _export_document_sync(doc, settings, client)
                exported += 1
            except Exception as exc:
                log.error(
                    "export_failed doc_id=%d error=%s", doc["id"], exc, exc_info=True
                )

    for doc_id in exported_ids:
        if doc_id not in tagged_ids:
            try:
                _remove_document_sync(doc_id, settings)
                removed += 1
            except Exception as exc:
                log.error(
                    "remove_failed doc_id=%d error=%s", doc_id, exc, exc_info=True
                )

    return exported, removed


async def run_outbound_sync(settings: Settings, client: PaperlessClient) -> None:
    """Main outbound coroutine: polls Paperless every poll_interval seconds."""
    tag_id = client.get_tag_id(settings.outbound_tag)
    if tag_id is None:
        raise RuntimeError(
            f"Outbound tag '{settings.outbound_tag}' not found in Paperless — create it first"
        )

    log.info(
        "outbound_sync_started tag=%s id=%d interval=%ds",
        settings.outbound_tag,
        tag_id,
        settings.poll_interval,
    )

    while True:
        try:
            exported, removed = await asyncio.to_thread(
                _sync_once, settings, client, tag_id
            )
            if exported or removed:
                log.info("outbound_sync_done exported=%d removed=%d", exported, removed)
        except Exception as exc:
            log.error("outbound_sync_error error=%s", exc, exc_info=True)

        await asyncio.sleep(settings.poll_interval)
