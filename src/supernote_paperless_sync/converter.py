"""Convert .note files to PDF.

Strategy (hybrid):
1. Look for a pre-converted PDF produced by the notelib sidecar in the
   convert/ directory. This is free — notelib already did the work.
2. If no notelib PDF is found, fall back to supernotelib (runs in-process).
"""

from __future__ import annotations

import time
from pathlib import Path

import structlog

log = structlog.get_logger()

# Maximum age difference (seconds) between a .note mtime and a notelib PDF
# for us to consider the PDF a match.
_NOTELIB_WINDOW_S = 300


def _find_notelib_pdf(note_path: Path, convert_dir: Path) -> bytes | None:
    """
    Search convert_dir for a PDF likely generated from note_path by notelib.

    Notelib names converted files with an internal hash (e.g.
    ``{user_id}_{file_id}_{size}_{hash}.pdf``), so we can't match by name.
    Instead we look for PDFs whose mtime is close to the note's mtime —
    notelib converts immediately after sync, so the timestamps should be
    within a few minutes of each other.

    Returns raw PDF bytes if a candidate is found, else None.
    """
    if not convert_dir.is_dir():
        return None

    note_mtime = note_path.stat().st_mtime
    candidates: list[tuple[float, Path]] = []

    for pdf in convert_dir.glob("*.pdf"):
        pdf_mtime = pdf.stat().st_mtime
        delta = abs(pdf_mtime - note_mtime)
        # PDF must be newer than (or very close to) the note
        if pdf_mtime >= note_mtime - 5 and delta <= _NOTELIB_WINDOW_S:
            candidates.append((delta, pdf))

    if not candidates:
        return None

    # Pick the PDF whose mtime is closest to the note's mtime
    candidates.sort(key=lambda x: x[0])
    best = candidates[0][1]
    log.debug(
        "notelib_pdf_candidate", pdf=best.name, delta_s=round(candidates[0][0], 1)
    )
    return best.read_bytes()


def _convert_with_supernotelib(note_path: Path) -> bytes:
    """
    Convert a .note file to PDF bytes using supernotelib.

    Uses raster mode (default) which works on all device types without
    requiring the potrace/cairo system libraries.

    Important: we iterate pages individually instead of passing page_number=-1
    to PdfConverter.convert(), because -1 triggers ProcessPoolExecutor which
    forks child processes and corrupts asyncio's event loop state.
    """
    import io

    import supernotelib as sn
    from supernotelib.converter import ImageConverter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    t0 = time.monotonic()
    with note_path.open("rb") as fh:
        notebook = sn.load(fh)

    total = notebook.get_total_pages()
    img_converter = ImageConverter(notebook)

    buf = io.BytesIO()
    c = None
    for i in range(total):
        img = img_converter.convert(i)
        if img is None:
            continue
        w, h = img.size
        if c is None:
            c = canvas.Canvas(buf, pagesize=(w, h))
        else:
            c.setPageSize((w, h))
        c.drawImage(ImageReader(img), 0, 0, w, h)
        c.showPage()

    if c is not None:
        c.save()

    pdf_bytes = buf.getvalue()
    elapsed = time.monotonic() - t0
    log.debug(
        "supernotelib_converted",
        note=note_path.name,
        pages=total,
        elapsed_s=round(elapsed, 2),
    )
    return pdf_bytes


def get_pdf_for_note(note_path: Path, convert_dir: Path) -> bytes:
    """
    Return PDF bytes for the given .note file.

    Tries notelib's pre-converted output first; falls back to supernotelib.

    Args:
        note_path: Absolute path to the .note file.
        convert_dir: Path to the notelib convert/ directory.

    Returns:
        Raw PDF bytes.

    Raises:
        RuntimeError: If conversion fails with both methods.
    """
    pdf = _find_notelib_pdf(note_path, convert_dir)
    if pdf is not None:
        log.info("using_notelib_pdf", note=note_path.name, size_kb=len(pdf) // 1024)
        return pdf

    log.info("falling_back_to_supernotelib", note=note_path.name)
    try:
        pdf = _convert_with_supernotelib(note_path)
        log.info("supernotelib_ok", note=note_path.name, size_kb=len(pdf) // 1024)
        return pdf
    except Exception as exc:
        raise RuntimeError(f"Failed to convert {note_path.name}: {exc}") from exc
