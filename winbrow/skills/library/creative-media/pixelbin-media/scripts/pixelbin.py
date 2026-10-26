"""Thin authenticated Pixelbin client for uploads, transforms, and API calls."""

from __future__ import annotations

import os

from winbrow.skill_script_utils import emit, ensure_parent, fail, integration_secret, load_spec, require_module, resolve_path

requests = require_module("requests", "requests")


def _key(spec: dict) -> str:
    key = str(
        spec.get("api_key")
        or integration_secret("pixelbin", "api_key")
        or os.environ.get("PIXELBIN_API_KEY")
        or ""
    ).strip()
    if not key:
        fail(
            "Pixelbin is not configured. Add an API key in the Integrations "
            "panel or set PIXELBIN_API_KEY."
        )
    return key


def _base(spec: dict) -> str:
    return str(spec.get("base_url") or "https://api.pixelbin.io").rstrip("/")


def upload(spec: dict) -> None:
    key = _key(spec)
    path = resolve_path(spec.get("path") or spec.get("input_path") or "")
    if not path.is_file():
        fail(f"File not found: {path}")
    url = f"{_base(spec)}/v1/upload"
    try:
        with path.open("rb") as handle:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, handle)},
                data={"path": str(spec.get("remote_path") or "")},
                timeout=90,
            )
    except Exception as exc:
        fail(f"Pixelbin upload failed: {exc}")
    if response.status_code >= 400:
        fail(f"Pixelbin returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:2000]}
    emit(True, "Uploaded to Pixelbin.", data=data)


def request(spec: dict) -> None:
    key = _key(spec)
    path = str(spec.get("path") or spec.get("endpoint") or "").strip()
    if not path.startswith("/") or ".." in path:
        fail("endpoint must start with '/' and must not contain '..'.")
    method = str(spec.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        fail(f"Unsupported method '{method}'.")
    try:
        response = requests.request(
            method,
            f"{_base(spec)}{path}",
            headers={"Authorization": f"Bearer {key}"},
            json=spec.get("json"),
            params=spec.get("params"),
            timeout=float(spec.get("timeout") or 60),
        )
    except Exception as exc:
        fail(f"Pixelbin request failed: {exc}")
    if response.status_code >= 400:
        fail(f"Pixelbin returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:4000]}
    emit(True, f"Pixelbin {method} {path} succeeded.", data=data)


def transform(spec: dict) -> None:
    cloud = str(spec.get("cloud") or "").strip()
    zone = str(spec.get("zone") or "").strip()
    source_path = str(spec.get("remote_path") or spec.get("path") or "").lstrip("/")
    transformations = str(spec.get("transformations") or "").strip("/")
    if not cloud or not zone or not source_path:
        fail("cloud, zone, and remote_path are required for a transform URL.")
    url = f"https://cdn.pixelbin.io/v2/{cloud}/{zone}"
    if transformations:
        url += f"/{transformations}"
    url += f"/{source_path}"
    output = resolve_path(spec.get("output_path") or "")
    ensure_parent(output)
    try:
        response = requests.get(url, timeout=float(spec.get("timeout") or 90))
    except Exception as exc:
        fail(f"Pixelbin transform failed: {exc}")
    if response.status_code >= 400:
        fail(f"Pixelbin CDN returned HTTP {response.status_code}: {response.text[:300]}")
    output.write_bytes(response.content)
    emit(True, f"Saved transformed media: {output}", path=output, data={
        "bytes": len(response.content), "url": url,
    })


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "request").lower()
    handlers = {"upload": upload, "request": request, "transform": transform}
    handler = handlers.get(operation)
    if handler is None:
        fail(f"Unknown Pixelbin operation '{operation}'. Use upload, transform, or request.")
    handler(spec)


if __name__ == "__main__":
    main()
