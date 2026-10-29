"""Optional LaTeX backend: source wrapping and graceful unavailability."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.layout.latex import (
    LatexUnavailable,
    latex_available,
    find_latex,
    render_latex,
    wrap_document,
)


def test_body_gets_paper_preamble():
    source = wrap_document("\\section{Experience}\nBuilt things.")
    assert "\\documentclass" in source
    assert "geometry" in source
    assert "\\section{Experience}" in source
    assert source.rstrip().endswith("\\end{document}")


def test_complete_document_is_not_wrapped():
    original = "\\documentclass{article}\\begin{document}Hi\\end{document}"
    assert wrap_document(original) == original


def test_empty_source_stays_empty():
    assert wrap_document("") == ""


def test_availability_is_boolean():
    assert isinstance(latex_available(), bool)
    if find_latex() is None:
        assert latex_available() is False


def test_render_without_engine_fails_clearly(tmp_path):
    if find_latex() is not None:
        pytest.skip("This machine has a TeX engine")
    with pytest.raises(LatexUnavailable) as exc:
        render_latex("Hello", tmp_path / "out.pdf")
    assert "MiKTeX" in str(exc.value) or "TeX Live" in str(exc.value)
