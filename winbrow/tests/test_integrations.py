"""Integration storage, masking, and status tests (no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow import integrations


def test_secret_is_masked_and_roundtrips(tmp_path, monkeypatch):
    store = tmp_path / "integrations.json"
    monkeypatch.setenv("WINBROW_INTEGRATIONS_PATH", str(store))

    status = integrations.save_integration("gemini", {"api_key": "AIza-test-secret"})
    assert status["configured"] is True
    assert status["fields"][0]["value"] == "••••"
    assert "AIza-test-secret" not in json.dumps(status)

    assert integrations.get_secret("gemini", "api_key") == "AIza-test-secret"
    raw = store.read_text(encoding="utf-8")
    assert "AIza-test-secret" not in raw


def test_empty_secret_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_INTEGRATIONS_PATH", str(tmp_path / "integrations.json"))
    integrations.save_integration("outline", {
        "base_url": "https://wiki.example.com", "token": "ol_api_secret",
    })
    integrations.save_integration("outline", {"token": "", "base_url": "https://wiki2.example.com"})

    assert integrations.get_secret("outline", "token") == "ol_api_secret"
    assert integrations.get_secret("outline", "base_url") == "https://wiki2.example.com"


def test_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_INTEGRATIONS_PATH", str(tmp_path / "integrations.json"))
    integrations.save_integration("gemini", {"api_key": "stored"})
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert integrations.get_secret("gemini", "api_key") == "from-env"


def test_clear_and_unknown_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_INTEGRATIONS_PATH", str(tmp_path / "integrations.json"))
    integrations.save_integration("pixelbin", {"api_key": "pb"})
    assert integrations.clear_integration("pixelbin") is True
    assert integrations.clear_integration("pixelbin") is False
    try:
        integrations.save_integration("nope", {})
    except KeyError:
        pass
    else:
        raise AssertionError("unknown provider should raise KeyError")


def test_agent_status_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_INTEGRATIONS_PATH", str(tmp_path / "integrations.json"))
    out = integrations.execute_integration_tool("integration_status", {"provider": "gemini"})
    assert out["success"] is True
    assert out["configured"] is False
    assert "Integrations" in out["output"]

    integrations.save_integration("gemini", {"api_key": "AIza-x"})
    out = integrations.execute_integration_tool("integration_status", {"provider": "gemini"})
    assert out["configured"] is True
