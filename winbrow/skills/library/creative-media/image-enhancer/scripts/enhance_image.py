"""Enhance, resize, crop, and convert images with Pillow."""

from __future__ import annotations

from pathlib import Path

from winbrow.skill_script_utils import emit, ensure_parent, fail, load_spec, require_module, resolve_path

Image = require_module("PIL.Image", "Pillow")
ImageEnhance = require_module("PIL.ImageEnhance", "Pillow")
ImageFilter = require_module("PIL.ImageFilter", "Pillow")


def _open(path: Path):
    if not path.is_file():
        fail(f"Image not found: {path}")
    try:
        return Image.open(path).convert("RGB")
    except Exception as exc:
        fail(f"Could not open image: {exc}")


def _enhance_one(image, spec: dict):
    scale = float(spec.get("scale") or 1.0)
    if scale != 1.0:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )
    sharpen = float(spec.get("sharpen") or 1.0)
    if sharpen != 1.0:
        image = ImageEnhance.Sharpness(image).enhance(sharpen)
    contrast = float(spec.get("contrast") or 1.0)
    if contrast != 1.0:
        image = ImageEnhance.Contrast(image).enhance(contrast)
    color = float(spec.get("color") or 1.0)
    if color != 1.0:
        image = ImageEnhance.Color(image).enhance(color)
    brightness = float(spec.get("brightness") or 1.0)
    if brightness != 1.0:
        image = ImageEnhance.Brightness(image).enhance(brightness)
    if spec.get("unsharp", True):
        image = image.filter(ImageFilter.UnsharpMask(
            radius=float(spec.get("radius") or 1.2),
            percent=int(spec.get("percent") or 120),
            threshold=int(spec.get("threshold") or 3),
        ))
    return image


def single(spec: dict) -> None:
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    image = _open(path)
    operation = str(spec.get("operation") or "enhance").lower()
    if operation == "resize":
        width = int(spec.get("width") or image.width)
        height = int(spec.get("height") or 0)
        if not height:
            height = max(1, int(image.height * width / max(1, image.width)))
        image = image.resize((width, height), Image.LANCZOS)
    elif operation == "crop":
        box = spec.get("box") or []
        if len(box) != 4:
            fail("crop needs box=[left, top, right, bottom].")
        image = image.crop(tuple(int(value) for value in box))
    elif operation == "enhance":
        image = _enhance_one(image, spec)
    else:
        fail(f"Unknown image operation '{operation}'. Use enhance, resize, or crop.")

    output = resolve_path(spec.get("output_path") or (path.parent / f"{path.stem}_enhanced.png"))
    ensure_parent(output)
    fmt = output.suffix.lower().lstrip(".") or "png"
    if fmt == "jpg":
        fmt = "jpeg"
    image.save(str(output), format=fmt.upper(), quality=int(spec.get("quality") or 95))
    emit(True, f"Saved image: {output}", path=output, data={
        "width": image.width, "height": image.height,
    })


def batch(spec: dict) -> None:
    directory = resolve_path(spec.get("input_dir") or spec.get("path") or "")
    if not directory.is_dir():
        fail(f"Input folder not found: {directory}")
    output_dir = resolve_path(spec.get("output_dir") or (directory / "enhanced"))
    output_dir.mkdir(parents=True, exist_ok=True)
    extensions = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    written = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        image = _enhance_one(Image.open(path).convert("RGB"), spec)
        target = output_dir / f"{path.stem}_enhanced.png"
        image.save(str(target), "PNG")
        written.append(str(target))
    if not written:
        fail("No supported images found in the folder.")
    emit(True, f"Enhanced {len(written)} image(s).", paths=written)


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "enhance").lower()
    if operation == "batch":
        batch(spec)
    else:
        single(spec)


if __name__ == "__main__":
    main()
