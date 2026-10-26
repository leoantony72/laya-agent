"""Generate or edit images with the Gemini image model."""

from __future__ import annotations

import base64
import os

from winbrow.skill_script_utils import emit, ensure_parent, fail, integration_secret, load_spec, require_module, resolve_path

httpx = require_module("httpx", "httpx")


def generate(spec: dict) -> None:
    key = str(
        spec.get("api_key")
        or integration_secret("gemini", "api_key")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    ).strip()
    if not key:
        fail(
            "Gemini is not configured. Add an API key in the Integrations panel "
            "or set GEMINI_API_KEY."
        )
    prompt = str(spec.get("prompt") or "").strip()
    if not prompt:
        fail("An image prompt is required.")
    output = resolve_path(spec.get("output_path") or spec.get("path") or "")
    if output.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        output = output.with_suffix(".png")
    ensure_parent(output)

    model = str(spec.get("model") or "gemini-2.5-flash-image").strip()
    parts: list[dict] = [{"text": prompt}]
    input_path = spec.get("input_path")
    if input_path:
        source = resolve_path(input_path)
        if not source.is_file():
            fail(f"Input image not found: {source}")
        mime = "image/png" if source.suffix.lower() == ".png" else "image/jpeg"
        parts.append({
            "inlineData": {
                "mimeType": mime,
                "data": base64.b64encode(source.read_bytes()).decode("ascii"),
            }
        })
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    aspect = str(spec.get("aspect_ratio") or "").strip()
    if aspect:
        payload["generationConfig"]["imageConfig"] = {"aspectRatio": aspect}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        response = httpx.post(
            url, params={"key": key}, json=payload,
            timeout=float(spec.get("timeout") or 120),
        )
    except Exception as exc:
        fail(f"Gemini image request failed: {exc}")
    if response.status_code >= 400:
        fail(f"Gemini returned HTTP {response.status_code}: {response.text[:500]}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        fail("Gemini returned no image candidate. The prompt may have been blocked.")
    image_parts = [
        part for part in (candidates[0].get("content") or {}).get("parts") or []
        if part.get("inlineData")
    ]
    if not image_parts:
        text = " ".join(
            str(part.get("text") or "") for part in (candidates[0].get("content") or {}).get("parts") or []
        ).strip()
        fail(f"Gemini did not return an image. {text[:300]}")

    inline = image_parts[0]["inlineData"]
    raw = base64.b64decode(inline.get("data") or "")
    output.write_bytes(raw)
    emit(True, f"Saved image: {output}", path=output, data={
        "mime_type": inline.get("mimeType"), "bytes": len(raw), "model": model,
    })


def main() -> None:
    generate(load_spec())


if __name__ == "__main__":
    main()
