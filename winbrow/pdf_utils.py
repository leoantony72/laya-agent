"""Pure-Python PDF text helpers.

PyMuPDF is deliberately not required here. Its native ``_mupdf.pyd`` can be
blocked by Windows Smart App Control, and the resulting import error is easy to
misdiagnose as a missing package. ``pdfminer.six`` (layout-aware extraction)
and ``pypdf`` (metadata, page operations, and a fallback extractor) are pure
Python and work in the same environments.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Sequence


class PdfError(RuntimeError):
    """A PDF could not be opened, decrypted, or read."""


Source = str | Path | bytes | bytearray | memoryview | BinaryIO


def _source_arg(source: Source) -> Any:
    if isinstance(source, (str, Path)):
        return str(source)
    if isinstance(source, (bytes, bytearray, memoryview)):
        return io.BytesIO(bytes(source))
    return source


def _page_numbers(pages: Sequence[int] | None) -> list[int] | None:
    if pages is None:
        return None
    return sorted({int(page) for page in pages})


def _snapshot_stream(source: BinaryIO) -> bytes | None:
    """Copy a file-like PDF so each extractor starts from the same position."""
    try:
        position = source.tell()
    except Exception:
        position = None
    try:
        return bytes(source.read())
    except Exception:
        return None
    finally:
        if position is not None:
            try:
                source.seek(position)
            except Exception:
                pass


def open_reader(source: Source, *, password: str = ""):
    """Open a PDF with pypdf, decrypting it when needed.

    ``source`` may be a path, raw bytes, or a binary file-like object.
    Raises ``PdfError`` with a user-facing message on failure.
    """
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - dependency is in requirements
        raise PdfError(f"pypdf is not installed: {exc}") from exc

    try:
        reader = PdfReader(_source_arg(source))
    except Exception as exc:
        raise PdfError(f"Could not open PDF: {exc}") from exc

    if reader.is_encrypted:
        try:
            result = reader.decrypt(password or "")
        except Exception as exc:
            raise PdfError(
                "PDF is encrypted and could not be decrypted: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not result:
            raise PdfError("PDF is password-protected. Ask the user for the password.")
    return reader


def extract_pages(source: Source, *, password: str = "") -> list[str]:
    """Return extracted text for every page, using pypdf."""
    reader = open_reader(source, password=password)
    try:
        return [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise PdfError(f"Could not extract PDF text: {exc}") from exc


def extract_text(
    source: Source,
    *,
    pages: Sequence[int] | None = None,
    password: str = "",
) -> str:
    """Extract text from a PDF path, bytes, or file-like object.

    ``pdfminer.six`` is tried first because it preserves reading order better
    than pypdf on complex layouts. pypdf is the fallback when pdfminer is
    unavailable or returns nothing. ``pages`` is zero-based.
    """
    page_numbers = _page_numbers(pages)
    errors: list[str] = []
    stream_data = (
        _snapshot_stream(source)
        if not isinstance(source, (str, Path, bytes, bytearray, memoryview))
        else None
    )

    def fresh_source() -> Any:
        if stream_data is not None:
            return io.BytesIO(stream_data)
        return _source_arg(source)

    try:
        from pdfminer.high_level import extract_text as pdfminer_extract

        kwargs: dict[str, Any] = {}
        if page_numbers is not None:
            kwargs["page_numbers"] = page_numbers
        if password:
            kwargs["password"] = password
        text = pdfminer_extract(fresh_source(), **kwargs)
        if text and text.strip():
            # pdfminer separates pages with a form feed.
            return text.replace("\x0c", "\n").strip()
    except Exception as exc:
        errors.append(f"pdfminer: {type(exc).__name__}: {exc}")

    try:
        reader = open_reader(fresh_source(), password=password)
        indices: Iterable[int] = (
            page_numbers if page_numbers is not None else range(len(reader.pages))
        )
        chunks: list[str] = []
        for index in indices:
            if 0 <= index < len(reader.pages):
                chunks.append(reader.pages[index].extract_text() or "")
        return "\n".join(chunks).strip()
    except Exception as exc:
        errors.append(f"pypdf: {type(exc).__name__}: {exc}")

    raise PdfError("; ".join(errors) or "Could not extract PDF text.")


def page_count(source: Source, *, password: str = "") -> int:
    """Return the number of pages in a PDF."""
    return len(open_reader(source, password=password).pages)
