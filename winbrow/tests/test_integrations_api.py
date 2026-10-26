"""REST surface for the Integrations panel (no network, no startup hooks)."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def test_integrations_list_and_save_masks_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_INTEGRATIONS_PATH", str(tmp_path / "integrations.json"))
    client = TestClient(server.app)

    resp = client.get("/api/integrations")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["providers"]) == 8
    assert any(item["id"] == "gemini" for item in data["providers"])

    resp = client.post("/api/integrations/gemini", json={"values": {"api_key": "AIza-secret"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"]["configured"] is True
    assert "AIza-secret" not in resp.text

    resp = client.get("/api/integrations")
    assert "AIza-secret" not in resp.text
    gemini = next(item for item in resp.json()["providers"] if item["id"] == "gemini")
    assert gemini["fields"][0]["has_value"] is True
    assert gemini["fields"][0]["value"] == "••••"

    resp = client.delete("/api/integrations/gemini")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_unknown_integration_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_INTEGRATIONS_PATH", str(tmp_path / "integrations.json"))
    client = TestClient(server.app)

    assert client.post("/api/integrations/nope", json={"values": {}}).status_code == 404
    assert client.post("/api/integrations/nope/test").status_code == 404
    assert client.delete("/api/integrations/nope").status_code == 404


def test_ui_exposes_integrations_panel():
    client = TestClient(server.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="integrationsBtn"' in resp.text
    assert 'id="integrationsModal"' in resp.text
    assert "openIntegrations" in resp.text

