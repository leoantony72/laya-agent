"""Search and manage documents in an Outline wiki."""

from __future__ import annotations

import os

from winbrow.skill_script_utils import emit, fail, integration_secret, load_spec, require_module

requests = require_module("requests", "requests")


def _config(spec: dict) -> tuple[str, str]:
    base = str(
        spec.get("base_url")
        or integration_secret("outline", "base_url")
        or os.environ.get("OUTLINE_API_URL")
        or ""
    ).rstrip("/")
    token = str(
        spec.get("token")
        or integration_secret("outline", "token")
        or os.environ.get("OUTLINE_API_TOKEN")
        or ""
    )
    if not base or not token:
        fail(
            "Outline is not configured. Add the base URL and API token in the "
            "Integrations panel or set OUTLINE_API_URL and OUTLINE_API_TOKEN."
        )
    if base.endswith("/api"):
        base = base[:-4]
    return base, token


def _request(spec: dict, endpoint: str, payload: dict):
    base, token = _config(spec)
    try:
        response = requests.post(
            f"{base}/api/{endpoint}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=float(spec.get("timeout") or 45),
        )
    except Exception as exc:
        fail(f"Outline request failed: {exc}")
    if response.status_code >= 400:
        fail(f"Outline returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except Exception:
        fail(f"Outline returned non-JSON: {response.text[:300]}")


def search(spec: dict) -> None:
    data = _request(spec, "documents.search", {
        "query": str(spec.get("query") or ""),
        "limit": int(spec.get("limit") or 10),
    })
    emit(True, "Outline search complete.", data=data)


def info(spec: dict) -> None:
    document_id = str(spec.get("document_id") or "")
    if not document_id:
        fail("document_id is required.")
    data = _request(spec, "documents.info", {"id": document_id})
    emit(True, "Outline document loaded.", data=data)


def collections(spec: dict) -> None:
    data = _request(spec, "collections.list", {"limit": int(spec.get("limit") or 50)})
    emit(True, "Outline collections listed.", data=data)


def create(spec: dict) -> None:
    title = str(spec.get("title") or "").strip()
    if not title:
        fail("title is required.")
    payload = {
        "title": title,
        "text": str(spec.get("text") or ""),
        "publish": bool(spec.get("publish", True)),
    }
    if spec.get("collection_id"):
        payload["collectionId"] = str(spec["collection_id"])
    data = _request(spec, "documents.create", payload)
    emit(True, "Outline document created.", data=data)


def update(spec: dict) -> None:
    document_id = str(spec.get("document_id") or "")
    if not document_id:
        fail("document_id is required.")
    payload = {"id": document_id}
    if spec.get("title"):
        payload["title"] = str(spec["title"])
    if spec.get("text") is not None:
        payload["text"] = str(spec["text"])
    data = _request(spec, "documents.update", payload)
    emit(True, "Outline document updated.", data=data)


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "search").lower()
    handlers = {
        "search": search, "info": info, "get": info,
        "collections": collections, "create": create, "update": update,
    }
    handler = handlers.get(operation)
    if handler is None:
        fail(f"Unknown Outline operation '{operation}'.")
    handler(spec)


if __name__ == "__main__":
    main()
