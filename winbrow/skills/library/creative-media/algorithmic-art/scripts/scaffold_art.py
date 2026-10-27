"""Scaffold an interactive algorithmic-art folder.

Creates:
  <output_dir>/index.html    viewer (p5.js + auto-built controls)
  <output_dir>/sketch.js     algorithm placeholder (overwrite this)
  <output_dir>/philosophy.md algorithmic philosophy (overwrite this)

The model writes sketch.js and philosophy.md afterwards, then opens index.html.
"""

from __future__ import annotations

from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_DIR / "templates"


def main() -> None:
    spec = load_spec()
    output_dir = resolve_path(
        spec.get("output_dir") or spec.get("path") or "algorithmic-art"
    )
    title = str(spec.get("title") or "Algorithmic Art").strip() or "Algorithmic Art"
    philosophy = str(spec.get("philosophy") or "").strip()
    overwrite = bool(spec.get("overwrite", False))

    viewer = TEMPLATES / "viewer.html"
    generator = TEMPLATES / "generator_template.js"
    if not viewer.is_file() or not generator.is_file():
        fail("Bundled templates are missing; reinstall the algorithmic-art skill.")

    output_dir.mkdir(parents=True, exist_ok=True)
    index = output_dir / "index.html"
    sketch = output_dir / "sketch.js"
    philosophy_path = output_dir / "philosophy.md"

    for path in (index, sketch):
        if path.exists() and not overwrite:
            fail(
                f"{path} already exists. Pass overwrite=true only after "
                "confirming the user wants to replace the artwork."
            )

    html = viewer.read_text(encoding="utf-8").replace(
        "<title>Algorithmic Art</title>", f"<title>{title}</title>"
    )
    index.write_text(html, encoding="utf-8")
    sketch.write_text(generator.read_text(encoding="utf-8"), encoding="utf-8")
    paths = [str(index), str(sketch)]
    if philosophy:
        philosophy_path.write_text(philosophy + "\n", encoding="utf-8")
        paths.append(str(philosophy_path))

    emit(
        True,
        (
            f"Scaffolded algorithmic art at {output_dir}\n"
            + "\n".join(f"- {path}" for path in paths)
            + "\nOverwrite sketch.js with the algorithm, then open index.html."
        ),
        path=output_dir,
        paths=paths,
    )


if __name__ == "__main__":
    main()
