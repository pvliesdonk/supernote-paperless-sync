"""Async Paperless-ngx API client."""

import asyncio
from urllib.parse import urljoin

import httpx
import structlog

log = structlog.get_logger()


class PaperlessError(Exception):
    """Raised when the Paperless API returns an unexpected response."""


class PaperlessClient:
    """Thin async wrapper around the Paperless-ngx REST API."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json; version=9",
            },
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0),
            follow_redirects=True,
        )

    def _url(self, path: str) -> str:
        return urljoin(self._base + "/", path.lstrip("/"))

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    async def get_tag_id(self, name: str) -> int | None:
        """Return the ID of a tag by name, or None if it doesn't exist."""
        url = self._url("/api/tags/")
        while url:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            for tag in data["results"]:
                if tag["name"].lower() == name.lower():
                    return int(tag["id"])
            url = data.get("next")
        return None

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def upload_document(
        self,
        pdf_data: bytes,
        filename: str,
        tag_ids: list[int],
    ) -> int:
        """
        Upload a PDF to Paperless-ngx.

        Args:
            pdf_data: Raw PDF bytes.
            filename: Filename shown in Paperless (used as initial title).
            tag_ids: List of tag IDs to apply immediately.

        Returns:
            The Paperless document ID once the task completes.
        """
        files = {"document": (filename, pdf_data, "application/pdf")}
        # httpx data= accepts Mapping[str, str] or sequence of (str, str) pairs
        data: list[tuple[str, str]] = [("tags", str(tid)) for tid in tag_ids]

        resp = await self._client.post(
            self._url("/api/documents/post_document/"),
            files=files,
            data=data,  # type: ignore[arg-type]  # httpx accepts list[tuple[str,str]]
        )
        if resp.status_code != 200:
            raise PaperlessError(f"Upload failed: {resp.status_code} {resp.text[:200]}")

        task_id = resp.text.strip().strip('"')
        log.debug("upload_task_queued", task_id=task_id, filename=filename)
        return await self._wait_for_task(task_id)

    async def _wait_for_task(self, task_id: str, timeout: int = 180) -> int:
        """Poll the tasks endpoint until the document is processed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(2)
            resp = await self._client.get(
                self._url("/api/tasks/"),
                params={"task_id": task_id},
            )
            resp.raise_for_status()
            tasks = resp.json()
            if not tasks:
                continue
            task = tasks[0]
            status = task.get("status")
            if status == "SUCCESS":
                doc_id = task.get("related_document")
                if doc_id is None:
                    raise PaperlessError(
                        f"Task {task_id} succeeded but has no document ID"
                    )
                return int(doc_id)
            if status == "FAILURE":
                raise PaperlessError(f"Task {task_id} failed: {task.get('result')}")
        raise PaperlessError(f"Task {task_id} timed out after {timeout}s")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def list_documents_by_tag(self, tag_id: int) -> list[dict]:
        """Return all documents tagged with the given tag ID."""
        results: list[dict] = []
        url = self._url(f"/api/documents/?tags__id__in={tag_id}&page_size=100")
        while url:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data["results"])
            url = data.get("next")
        return results

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download_document(self, doc_id: int) -> tuple[bytes, str]:
        """
        Download the original document file.

        Returns:
            Tuple of (file bytes, suggested filename).
        """
        resp = await self._client.get(self._url(f"/api/documents/{doc_id}/download/"))
        resp.raise_for_status()

        # Extract filename from Content-Disposition if present
        filename = f"document_{doc_id}.pdf"
        cd = resp.headers.get("content-disposition", "")
        if "filename=" in cd:
            part = cd.split("filename=")[-1].strip().strip('"').strip("'")
            if part:
                filename = part

        return resp.content, filename

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
