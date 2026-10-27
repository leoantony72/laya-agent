"""Pure-Python PDF skill tests (no PyMuPDF / native DLLs)."""

from __future__ import annotations

import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.pdf_utils import extract_text as extract_pdf_text
from winbrow.skill_scripts import execute_skill_script_tool
from winbrow.web_tools import _pdf_to_text


def _run(spec: dict) -> dict:
    result = execute_skill_script_tool(
        "run_skill_script",
        {"skill": "pdf", "script": "pdf_tool.py", "spec": spec},
    )
    assert result.get("success"), result
    return result


def _create(path: Path, title: str, content: str) -> dict:
    return _run({
        "operation": "create",
        "output_path": str(path),
        "title": title,
        "content": content,
    })


def test_create_extract_unicode_and_web_fetch_helper(tmp_path):
    output = tmp_path / "report.pdf"
    result = _create(
        output,
        "Quarterly report",
        "First line with café and rupee \u20b9.\n\nSecond paragraph.",
    )

    assert Path(result["path"]) == output
    assert output.is_file()
    text = PdfReader(output).pages[0].extract_text()
    assert "Quarterly report" in text
    assert "café" in text
    assert "\u20b9" in text

    raw_text = _pdf_to_text(output.read_bytes())
    assert "Quarterly report" in raw_text
    assert "Second paragraph." in raw_text


def test_file_like_source_survives_extractor_fallback(tmp_path, monkeypatch):
    source = tmp_path / "stream.pdf"
    _create(source, "Stream", "Fallback text survives.")

    def failing_extract(stream, **kwargs):
        stream.read(16)
        raise RuntimeError("pdfminer unavailable")

    from pdfminer import high_level

    monkeypatch.setattr(high_level, "extract_text", failing_extract)
    with open(source, "rb") as handle:
        text = extract_pdf_text(handle, pages=[0])
    assert "Fallback text survives." in text


def test_merge_and_split(tmp_path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _create(first, "First", "Alpha")
    _create(second, "Second", "Beta")

    merged = tmp_path / "merged.pdf"
    _run({
        "operation": "merge",
        "inputs": [str(first), str(second)],
        "output_path": str(merged),
    })
    assert len(PdfReader(merged).pages) == 2

    split_dir = tmp_path / "split"
    result = _run({
        "operation": "split",
        "path": str(merged),
        "pages": "1-2",
        "output_dir": str(split_dir),
    })
    paths = sorted(Path(item) for item in result["paths"])
    assert [path.name for path in paths] == [
        "merged_page_1.pdf",
        "merged_page_2.pdf",
    ]
    assert all(path.is_file() for path in paths)


def test_rotate_watermark_encrypt_and_page_range(tmp_path):
    source = tmp_path / "source.pdf"
    _create(source, "Source", "Rotate me")

    rotated = tmp_path / "rotated.pdf"
    _run({
        "operation": "rotate",
        "path": str(source),
        "pages": "1",
        "angle": 90,
        "output_path": str(rotated),
    })
    assert PdfReader(rotated).pages[0].get("/Rotate") == 90

    watermarked = tmp_path / "watermarked.pdf"
    _run({
        "operation": "watermark",
        "path": str(source),
        "text": "CONFIDENTIAL",
        "output_path": str(watermarked),
    })
    assert "CONFIDENTIAL" in PdfReader(watermarked).pages[0].extract_text()

    encrypted = tmp_path / "encrypted.pdf"
    _run({
        "operation": "encrypt",
        "path": str(source),
        "user_password": "secret",
        "output_path": str(encrypted),
    })
    reader = PdfReader(encrypted)
    assert reader.is_encrypted
    assert reader.decrypt("secret")
    assert "Rotate me" in reader.pages[0].extract_text()

    second = tmp_path / "second.pdf"
    _create(second, "Second", "Page two text")
    merged = tmp_path / "merged.pdf"
    _run({
        "operation": "merge",
        "inputs": [str(source), str(second)],
        "output_path": str(merged),
    })
    result = _run({
        "operation": "extract_text",
        "path": str(merged),
        "pages": "2",
    })
    assert "--- Page 2 ---" in result["output"]
    assert "Page two text" in result["output"]
    assert "Rotate me" not in result["output"]
