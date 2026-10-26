"""Build Slack-sized animated GIFs from text and composable motion primitives."""

from __future__ import annotations

import math
import os
from pathlib import Path

from winbrow.skill_script_utils import emit, ensure_parent, fail, load_spec, require_module, resolve_path

Image = require_module("PIL.Image", "Pillow")
ImageDraw = require_module("PIL.ImageDraw", "Pillow")
ImageFont = require_module("PIL.ImageFont", "Pillow")

_FONTS = {
    "sans": ["arialbd.ttf", "segoeuib.ttf", "calibrib.ttf"],
    "serif": ["georgiab.ttf", "timesbd.ttf"],
    "mono": ["consolab.ttf", "courbd.ttf"],
}


def _font(size: int, family: str = "sans"):
    for filename in _FONTS.get(family, _FONTS["sans"]):
        path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / filename
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), max(8, int(size)))
            except Exception:
                continue
    return ImageFont.load_default()


def _text_layer(text: str, size: int, color: str, family: str, canvas_size):
    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(size, family)
    bbox = draw.multiline_textbbox((0, 0), text, font=font, align="center")
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    position = ((canvas_size[0] - width) // 2, (canvas_size[1] - height) // 2)
    draw.multiline_text(position, text, font=font, fill=color, align="center")
    return layer


def _frames_from_text(spec: dict, width: int, height: int):
    animation = spec.get("animation") or {}
    effect = str(animation.get("type") or "pulse").lower()
    text = str(animation.get("text") or spec.get("text") or "Hello")
    color = str(animation.get("color") or "#ffffff")
    family = str(animation.get("font") or "sans")
    background = str(spec.get("background") or "#1f2937")
    base_size = int(animation.get("size") or min(width, height) // 5)
    count = max(2, min(int(spec.get("frames") or animation.get("frames") or 18), 60))
    frames = []
    for index in range(count):
        phase = index / count
        canvas = Image.new("RGBA", (width, height), background)
        if effect == "pulse":
            size = int(base_size * (1.0 + 0.12 * math.sin(phase * math.tau)))
            layer = _text_layer(text, size, color, family, (width, height))
            canvas.alpha_composite(layer)
        elif effect == "shake":
            offset = int(width * 0.04 * math.sin(phase * math.tau * 3))
            layer = _text_layer(text, base_size, color, family, (width, height))
            canvas.alpha_composite(layer, (offset, 0))
        elif effect == "slide":
            layer = _text_layer(text, base_size, color, family, (width, height))
            offset = int((width + layer.width) * phase) - layer.width
            canvas.alpha_composite(layer, (offset, 0))
        elif effect == "zoom":
            size = int(base_size * (0.55 + 0.6 * phase))
            layer = _text_layer(text, size, color, family, (width, height))
            canvas.alpha_composite(layer)
        elif effect == "spin":
            layer = _text_layer(text, base_size, color, family, (width, height))
            rotated = layer.rotate(360 * phase, resample=Image.BICUBIC, expand=False)
            canvas.alpha_composite(rotated)
        elif effect == "bounce":
            layer = _text_layer(text, base_size, color, family, (width, height))
            offset = int(abs(math.sin(phase * math.pi)) * height * 0.18)
            canvas.alpha_composite(layer, (0, -offset))
        else:
            fail(f"Unknown animation '{effect}'. Use pulse, shake, slide, zoom, spin, or bounce.")
        frames.append(canvas.convert("P", palette=Image.ADAPTIVE, colors=int(spec.get("colors") or 128)))
    return frames


def _frames_from_images(spec: dict, width: int, height: int):
    paths = spec.get("image_paths") or spec.get("frames") or []
    if not isinstance(paths, list) or not paths:
        fail("Provide animation text or a list of image_paths.")
    frames = []
    for item in paths:
        path = resolve_path(item)
        image = Image.open(path).convert("RGB").resize((width, height), Image.LANCZOS)
        frames.append(image.convert("P", palette=Image.ADAPTIVE, colors=int(spec.get("colors") or 128)))
    return frames


def build(spec: dict) -> None:
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() != ".gif":
        output = output.with_suffix(".gif")
    ensure_parent(output)
    width = int(spec.get("width") or 480)
    height = int(spec.get("height") or 480)
    duration = int(spec.get("duration_ms") or spec.get("duration") or 80)
    loop = int(spec.get("loop") or 0)
    max_bytes = int(spec.get("max_bytes") or (64 * 1024 if spec.get("emoji") else 2 * 1024 * 1024))

    frames = _frames_from_images(spec, width, height) if spec.get("image_paths") else _frames_from_text(spec, width, height)
    if not frames:
        fail("No frames were generated.")

    attempts = [
        (len(frames), int(spec.get("colors") or 128)),
        (max(2, len(frames) // 2), 64),
        (max(2, len(frames) // 3), 32),
    ]
    last_size = 0
    for frame_count, colors in attempts:
        selected = frames if frame_count == len(frames) else frames[:frame_count]
        if colors < 128:
            selected = [frame.quantize(colors=colors, method=Image.MEDIANCUT) for frame in selected]
        selected[0].save(
            str(output), save_all=True, append_images=selected[1:],
            duration=duration, loop=loop, optimize=True, disposal=2,
        )
        last_size = output.stat().st_size
        if last_size <= max_bytes:
            break
    if last_size > max_bytes:
        fail(
            f"GIF is {last_size} bytes, above the {max_bytes}-byte limit even "
            "after reducing colors and frames. Reduce dimensions or text size."
        )
    emit(True, f"Created GIF: {output}", path=output, data={
        "bytes": last_size, "frames": len(selected), "width": width, "height": height,
    })


def main() -> None:
    build(load_spec())


if __name__ == "__main__":
    main()
