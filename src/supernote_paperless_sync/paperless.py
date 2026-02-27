"""Synchronous Paperless-ngx API client.

Uses httpx.Client (sync) so it is safe to call from asyncio.to_thread() without
triggering sniffio / anyio backend-detection issues caused by supernotelib's
multiprocessing imports corrupting asyncio contextvars.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API_VERSION_HEADER = "application/json; version=9"


class PaperlessError(RuntimeError):
    """Raised when the Paperless API returns an error."""


class PaperlessClient:
    """Synchronous Paperless-ngx REST API client."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={
                "Authorization": f"Token {token}",
                "Accept": _API_VERSION_HEADER,
            },
            timeout=60.0,
        )

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def get_tag_id(self, name: str) -> int | None:
        """Return the Paperless tag ID for *name*, or None if not found."""
        url = f"{self._base}/api/tags/"
        while url:
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for tag in data.get("results", []):
                if tag["name"] == name:
                    return int(tag["id"])
            url = data.get("next")
        return None

    def get_or_create_tag(self, name: str) -> int:
        """Return the ID of tag *name*, creating it if absent."""
        tag_id = self.get_tag_id(name)
        if tag_id is not None:
            return tag_id
        resp = self._client.post(f"{self._base}/api/tags/", json={"name": name})
        resp.raise_for_status()
        created = resp.json()
        log.info("created_tag name=%s id=%d", name, created["id"])
        return int(created["id"])

    # ------------------------------------------------------------------
    # Custom fields
    # ------------------------------------------------------------------

    def get_or_create_custom_field(self, name: str, data_type: str = "string") -> int:
        """Return the ID of a custom field, creating it if absent.

        Args:
            name: Field name as shown in Paperless UI.
            data_type: Paperless data type string (e.g. ``"string"``, ``"url"``,
                ``"date"``, ``"integer"``, ``"float"``, ``"boolean"``).

        Returns:
            Custom field ID.
        """
        url: str | None = f"{self._base}/api/custom_fields/"
        while url:
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for field in data.get("results", []):
                if field["name"].lower() == name.lower():
                    return int(field["id"])
            url = data.get("next")
        resp = self._client.post(
            f"{self._base}/api/custom_fields/",
            json={"name": name, "data_type": data_type},
        )
        resp.raise_for_status()
        created = resp.json()
        log.info("created_custom_field name=%s id=%d", name, created["id"])
        return int(created["id"])

    # ------------------------------------------------------------------
    # Documents — get and patch
    # ------------------------------------------------------------------

    def get_document(self, doc_id: int) -> dict:
        """Fetch document metadata from ``GET /api/documents/{doc_id}/``."""
        resp = self._client.get(f"{self._base}/api/documents/{doc_id}/")
        resp.raise_for_status()
        return resp.json()

    def patch_document(self, doc_id: int, fields: dict) -> dict:
        """Update document fields via ``PATCH /api/documents/{doc_id}/``."""
        resp = self._client.patch(
            f"{self._base}/api/documents/{doc_id}/", json=fields
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Documents — upload
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Correspondents
    # ------------------------------------------------------------------

    def get_or_create_correspondent(self, name: str) -> int:
        """Return the ID of correspondent *name*, creating it if absent."""
        url = f"{self._base}/api/correspondents/"
        while url:
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("results", []):
                if item["name"] == name:
                    return int(item["id"])
            url = data.get("next")
        resp = self._client.post(f"{self._base}/api/correspondents/", json={"name": name})
        resp.raise_for_status()
        created = resp.json()
        log.info("created_correspondent name=%s id=%d", name, created["id"])
        return int(created["id"])

    # ------------------------------------------------------------------
    # Document types
    # ------------------------------------------------------------------

    def get_or_create_document_type(self, name: str) -> int:
        """Return the ID of document type *name*, creating it if absent."""
        url = f"{self._base}/api/document_types/"
        while url:
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("results", []):
                if item["name"] == name:
                    return int(item["id"])
            url = data.get("next")
        resp = self._client.post(f"{self._base}/api/document_types/", json={"name": name})
        resp.raise_for_status()
        created = resp.json()
        log.info("created_document_type name=%s id=%d", name, created["id"])
        return int(created["id"])

    # ------------------------------------------------------------------
    # Documents — upload
    # ------------------------------------------------------------------

    def upload_document(
        self,
        pdf_data: bytes,
        filename: str,
        tag_ids: list[int],
        correspondent_id: int | None = None,
        document_type_id: int | None = None,
        created_date: str | None = None,
    ) -> int:
        """Upload *pdf_data* to Paperless and return the new document ID.

        Blocks until the Paperless task queue confirms the document is created.

        Args:
            pdf_data: Raw PDF bytes.
            filename: Filename for the uploaded document.
            tag_ids: List of tag IDs to assign.
            correspondent_id: Optional correspondent ID to assign.
            document_type_id: Optional document type ID to assign.
            created_date: Optional creation date as ``YYYY-MM-DD`` string.
        """
        # Build multipart manually: httpx requires all fields in `files` when
        # mixing file upload with repeated form fields (list[tuple] in data=
        # breaks h11 when combined with files=).
        multipart: list[tuple[str, Any]] = [
            ("document", (filename, pdf_data, "application/pdf")),
        ]
        for tid in tag_ids:
            multipart.append(("tags", (None, str(tid), "text/plain")))
        if correspondent_id is not None:
            multipart.append(("correspondent", (None, str(correspondent_id), "text/plain")))
        if document_type_id is not None:
            multipart.append(("document_type", (None, str(document_type_id), "text/plain")))
        if created_date is not None:
            multipart.append(("created", (None, created_date, "text/plain")))
        resp = self._client.post(
            f"{self._base}/api/documents/post_document/",
            files=multipart,  # type: ignore[arg-type]
        )
        resp.raise_for_status()
        task_id: str = resp.json()
        log.debug("upload_task_queued task_id=%s", task_id)
        return self._wait_for_task(task_id)

    def _wait_for_task(self, task_id: str, timeout: float = 180.0) -> int:
        """Poll /api/tasks/ until the task succeeds; return document ID."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.get(
                f"{self._base}/api/tasks/",
                params={"task_id": task_id},
            )
            resp.raise_for_status()
            tasks = resp.json()
            if tasks:
                task = tasks[0]
                status = task.get("status")
                if status == "SUCCESS":
                    doc_id = task.get("related_document")
                    if doc_id is None:
                        raise PaperlessError(
                            f"Task {task_id} succeeded but no document ID"
                        )
                    return int(doc_id)
                if status in ("FAILURE", "REVOKED"):
                    raise PaperlessError(f"Task {task_id} failed: {task.get('result')}")
            time.sleep(2.0)
        raise PaperlessError(f"Task {task_id} did not complete within {timeout}s")

    # ------------------------------------------------------------------
    # Documents — query and download
    # ------------------------------------------------------------------

    def list_documents_by_tag(self, tag_id: int) -> list[dict]:
        """Return all documents tagged with *tag_id*."""
        docs: list[dict] = []
        url = f"{self._base}/api/documents/?tags__id__in={tag_id}&page_size=100"
        while url:
            resp = self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            docs.extend(data.get("results", []))
            url = data.get("next")
        return docs

    def download_document(self, doc_id: int) -> tuple[bytes, str]:
        """Download the original file for *doc_id*.

        Returns (content_bytes, filename).
        """
        resp = self._client.get(f"{self._base}/api/documents/{doc_id}/download/")
        resp.raise_for_status()
        # Extract filename from Content-Disposition header
        cd = resp.headers.get("content-disposition", "")
        filename = f"document_{doc_id}.pdf"
        for part in cd.split(";"):
            part = part.strip()
            if part.startswith("filename="):
                filename = part[9:].strip('"')
                break
        return resp.content, filename

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()
