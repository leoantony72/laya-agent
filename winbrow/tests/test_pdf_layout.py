"""Measured PDF layout: overflow, pagination, tables, images, validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.skill_scripts import execute_skill_script_tool


def _run(spec: dict, timeout: int = 120) -> dict:
    result = execute_skill_script_tool(
        "run_skill_script",
        {"skill": "pdf", "script": "pdf_tool.py", "spec": spec, "timeout": timeout},
    )
    assert result.get("success"), result.get("output")
    return result


@pytest.fixture(scope="module")
def image(tmp_path_factory) -> Path:
    pytest.importorskip("PIL")
    from PIL import Image

    target = tmp_path_factory.mktemp("pdf-assets") / "photo.png"
    Image.new("RGB", (1400, 800), (217, 119, 87)).save(target)
    return target


def test_dense_report_no_overflow_and_renders_every_page(tmp_path, image):
    output = tmp_path / "report.pdf"
    paragraph = (
        "The layout engine must measure every line, keep it inside the content "
        "area, and split long paragraphs across pages instead of leaving a "
        "stranded heading and a large blank block. "
    ) * 24
    table_rows = "\n".join(
        f"| Region {index} | ${index}.2M | {index}% |" for index in range(1, 10)
    )
    content = f"""# Annual Report

{paragraph}

URL: https://example.com/reports/2026/a/very/long/path/that/must/wrap/inside/the/page/without/overflowing

## Metrics

| Region | Revenue | Growth |
|---|---|---|
{table_rows}

- First bullet with a long sentence that must wrap inside the hanging indent.
- Second bullet.

## Code

```python
def hello(name):
    return f"Hello, {{name}}"
```

> A quote block with a tinted background and a left accent bar.

![A sample image]({image})

### Conclusion

Final paragraph.
"""
    result = _run({
        "operation": "create",
        "output_path": str(output),
        "title": "Annual Report",
        "subtitle": "Regional performance",
        "author": "WinBrow",
        "footer": "Confidential",
        "theme": "corporate",
        "page_size": "a4",
        "content": content,
        "validate": True,
    })
    validation = result["data"]["validation"]
    assert validation["ok"], validation["errors"]
    assert validation["pages"] >= 2
    assert len(validation["rendered"]) == validation["pages"]
    assert result["data"]["page_size"] == [595.28, 841.89]
    text = Path(validation["rendered"][0]).read_bytes()
    assert text.startswith(b"\x89PNG")


def test_landscape_letter_uses_requested_dimensions(tmp_path):
    output = tmp_path / "wide.pdf"
    result = _run({
        "operation": "create",
        "output_path": str(output),
        "title": "Wide Report",
        "page_size": "letter",
        "orientation": "landscape",
        "theme": "editorial",
        "content": "# Wide\n\n" + ("This paragraph should use the landscape width. " * 20),
    })
    assert result["data"]["page_size"] == [792.0, 612.0]
    validation = result["data"]["validation"]
    assert validation["ok"], validation["errors"]


def test_large_table_repeats_header_across_pages(tmp_path):
    output = tmp_path / "table.pdf"
    rows = "\n".join(
        f"| Item {index} | Description {index} | ${index},200 |" for index in range(1, 70)
    )
    content = (
        "# Inventory\n\n| Item | Description | Price |\n|---|---|---|\n"
        + rows
    )
    result = _run({
        "operation": "create",
        "output_path": str(output),
        "title": "Inventory",
        "content": content,
        "validate": True,
    })
    validation = result["data"]["validation"]
    assert validation["ok"], validation["errors"]
    assert validation["pages"] >= 2
    from pypdf import PdfReader

    full_text = "\n".join(page.extract_text() or "" for page in PdfReader(output).pages)
    assert full_text.count("Item") >= 2  # header repeated
    assert "Item 69" in full_text


def test_missing_image_fails_without_placeholder(tmp_path):
    result = execute_skill_script_tool(
        "run_skill_script",
        {"skill": "pdf", "script": "pdf_tool.py", "spec": {
            "operation": "create",
            "output_path": str(tmp_path / "bad.pdf"),
            "content": "# Bad\n\nText\n\n![missing](does-not-exist.png)",
        }, "timeout": 90},
    )
    assert result["success"] is False
    assert "not found" in result["output"].lower()


def test_validate_operation_reports_geometry(tmp_path):
    output = tmp_path / "simple.pdf"
    _run({
        "operation": "create",
        "output_path": str(output),
        "title": "Simple",
        "content": "One paragraph.",
        "validate": False,
    })
    result = _run({
        "operation": "validate",
        "path": str(output),
        "render_dir": str(tmp_path / "pages"),
    })
    assert result["data"]["ok"] is True
    assert result["data"]["pages"] == 1
    assert len(result["data"]["rendered"]) == 1
