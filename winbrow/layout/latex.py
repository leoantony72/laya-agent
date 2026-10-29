"""Optional LaTeX backend for paper documents (resume, cover letter, official).

LaTeX is not bundled with WinBrow. When a TeX engine is installed, callers can
compile a .tex source to PDF here; otherwise the measured fpdf2 engine remains
the default. Keeping this optional avoids a hard dependency on MiKTeX/TeX Live.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

_ENGINES = ("pdflatex", "xelatex", "lualatex", "latexmk")


class LatexUnavailable(RuntimeError):
    """No LaTeX engine is installed."""


def find_latex() -> Optional[str]:
    for name in _ENGINES:
        found = shutil.which(name)
        if found:
            return found
    return None


def latex_available() -> bool:
    return find_latex() is not None


def latex_install_hint() -> str:
    return (
        "LaTeX engine not found. Install MiKTeX (https://miktex.org) or "
        "TeX Live (https://tug.org/texlive), or use the measured PDF engine "
        "(operation='create' with document_kind='resume')."
    )


_DEFAULT_PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage[a4paper,margin=0.7in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{titlesec}
\setlist[itemize]{leftmargin=1.2em,itemsep=2pt,topsep=2pt}
\titleformat{\section}{\large\bfseries}{}{0em}{}[\vspace{-0.35em}\rule{\linewidth}{0.4pt}]
\titlespacing*{\section}{0pt}{8pt}{4pt}
\pagestyle{empty}
\begin{document}
"""


def wrap_document(source: str) -> str:
    """Wrap a LaTeX body in a sane paper-document preamble when needed."""
    text = str(source or "").strip()
    if not text:
        return text
    if "\\documentclass" in text:
        return text
    return _DEFAULT_PREAMBLE + text + "\n\\end{document}\n"


def render_latex(
    source: str,
    output_path: str | Path,
    *,
    timeout: int = 120,
) -> dict:
    """Compile LaTeX source to ``output_path``. Raises LatexUnavailable."""
    engine = find_latex()
    if engine is None:
        raise LatexUnavailable(latex_install_hint())
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.gettempdir()) / f"winbrow-latex-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=True)
    tex_path = work / "document.tex"
    tex_path.write_text(wrap_document(source), encoding="utf-8")
    engine_name = Path(engine).stem.lower()
    if engine_name == "latexmk":
        command = [
            engine, "-pdf", "-interaction=nonstopmode", "-halt-on-error",
            f"-outdir={work}", str(tex_path),
        ]
    else:
        command = [
            engine,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-output-directory", str(work),
            str(tex_path),
        ]
    try:
        proc = subprocess.run(
            command, cwd=str(work), capture_output=True, timeout=timeout,
            text=True, errors="replace",
        )
        produced = work / "document.pdf"
        if proc.returncode != 0 or not produced.is_file():
            detail = (proc.stdout or "")[-1200:] + (proc.stderr or "")[-600:]
            raise RuntimeError(
                f"LaTeX compilation failed ({engine}): {detail.strip()}"
            )
        shutil.copy2(produced, target)
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "engine": engine,
        }
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LaTeX compilation timed out after {timeout}s.") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
