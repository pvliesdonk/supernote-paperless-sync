"""OCR and metadata helpers using PyMuPDF and an OpenAI-compatible vision model."""

from __future__ import annotations

import base64
import io
import logging

import fitz  # PyMuPDF
from openai import OpenAI

log = logging.getLogger(__name__)

_OCR_SYSTEM = (
    "You are a precise transcription assistant. "
    "Transcribe the handwritten content of this document page exactly. "
    "Preserve structure, headings, lists, and diagrams as ASCII art where possible. "
    "Output only the transcribed text — no commentary."
)

_METADATA_SYSTEM = (
    "You are a document metadata assistant. "
    "Given a document's transcribed content and original filename, suggest a concise title "
    "and up to 5 relevant tags. "
    "Respond with JSON only: {\"title\": \"...\", \"tags\": [\"...\", ...]}"
)


def pdf_to_page_images(pdf_bytes: bytes, dpi: int = 150) -> list[bytes]:
    """Convert each page of a PDF to PNG bytes at the given DPI.

    Args:
        pdf_bytes: Raw PDF bytes.
        dpi: Render resolution in dots per inch.

    Returns:
        List of PNG bytes, one per page.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images: list[bytes] = []
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    for page in doc:
        pix = page.get_pixmap(matrix=matrix)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def ocr_pdf(pdf_bytes: bytes, client: OpenAI, model: str) -> str:
    """OCR a PDF using a vision LLM, returning the full transcribed text.

    Sends each page as a base64-encoded image and concatenates results.

    Args:
        pdf_bytes: Raw PDF bytes.
        client: Configured OpenAI client.
        model: Vision model name (e.g. "gpt-4o").

    Returns:
        Transcribed text with pages separated by double newlines.
    """
    page_images = pdf_to_page_images(pdf_bytes)
    log.info("ocr_start pages=%d model=%s", len(page_images), model)
    page_texts: list[str] = []
    for i, img_bytes in enumerate(page_images, 1):
        b64 = base64.b64encode(img_bytes).decode()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _OCR_SYSTEM},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ],
            temperature=0,
        )
        text = resp.choices[0].message.content or ""
        page_texts.append(text.strip())
        log.debug("ocr_page page=%d/%d chars=%d", i, len(page_images), len(text))
    return "\n\n".join(page_texts)


def suggest_metadata(
    content: str, filename: str, client: OpenAI, model: str
) -> dict:
    """Suggest a title and tags for a document.

    Args:
        content: Transcribed document text.
        filename: Original filename (used as context).
        client: Configured OpenAI client.
        model: Model name for metadata suggestion.

    Returns:
        Dict with keys ``title`` (str) and ``tags`` (list[str]).
    """
    import json

    prompt = f"Filename: {filename}\n\nContent:\n{content[:4000]}"
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _METADATA_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("metadata_json_parse_failed raw=%r", raw[:200])
        result = {}
    title = result.get("title") or filename
    tags = result.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return {"title": str(title), "tags": [str(t) for t in tags]}


_SUMMARY_SYSTEM = (
    "You are a document summarization assistant. "
    "Given a document's transcribed text and its title, write a concise summary "
    "that captures the key points, important details, dates, and named entities. "
    "Be concise — aim for a few sentences to a short paragraph. "
    "Output only the summary text — no commentary, no headers."
)


def summarize_text(ocr_text: str, title: str, client: OpenAI, model: str) -> str:
    """Generate a concise summary of a document's OCR text.

    Args:
        ocr_text: Full transcribed text of the document.
        title: Document title (used for context in the prompt).
        client: Configured OpenAI client.
        model: Model name for summarization.

    Returns:
        Summary string. Returns an empty string if ocr_text is blank or
        if the LLM call fails.
    """
    if not ocr_text or not ocr_text.strip():
        return ""
    prompt = f'Document title: "{title}"\n\nContent:\n{ocr_text[:8000]}'
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        log.warning("summarize_text_failed title=%r", title, exc_info=True)
        return ""


def embed_text_layer(pdf_bytes: bytes, text: str) -> bytes:
    """Insert an invisible text overlay into a PDF for searchability.

    Splits the OCR text proportionally across pages by character count and
    inserts it as white-on-white text using PyMuPDF's textbox insertion.
    The original visual appearance is preserved.

    Args:
        pdf_bytes: Raw PDF bytes to augment.
        text: Full OCR text to embed (split across pages).

    Returns:
        Modified PDF bytes with invisible text layer.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n_pages = len(doc)
    if n_pages == 0 or not text.strip():
        doc.close()
        return pdf_bytes

    # Split text proportionally across pages
    chars_per_page = max(1, len(text) // n_pages)
    for i, page in enumerate(doc):
        start = i * chars_per_page
        end = start + chars_per_page if i < n_pages - 1 else len(text)
        page_text = text[start:end]
        if not page_text.strip():
            continue
        rect = page.rect
        # White text — invisible but indexable by PDF viewers and Paperless
        page.insert_textbox(
            rect,
            page_text,
            fontsize=6,
            color=(1, 1, 1),  # white
            overlay=False,
        )

    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    return buf.getvalue()
