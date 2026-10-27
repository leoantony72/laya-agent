"""
Integration registry and secure local storage
=============================================
The converted skills need optional third-party services (Gemini, Google,
PostgreSQL, Outline, n8n, Pixelbin, Mercury, NotebookLM). This module owns the
single provider registry used by both the REST API / Integrations panel and the
agent's ``integration_status`` tool.

Secrets are stored outside the skill folders in ``data/integrations.json`` and
are encrypted with Windows DPAPI when available. Every read endpoint masks
secret values; environment variables always win over stored values.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import socket
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .registry import ArgSpec, Tool

log = logging.getLogger("winbrow.integrations")

INTEGRATION_IMPL = "integration"
DEFAULT_STORE = Path(__file__).resolve().parent.parent / "data" / "integrations.json"
_LOCK = Lock()


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {
        "label": "Gemini",
        "category": "AI & Research",
        "description": "Powers deep-research and image generation.",
        "used_by": ["deep-research", "imagen"],
        "fields": [
            {"key": "api_key", "label": "API key", "secret": True, "required": True,
             "placeholder": "AIza...", "help": "https://aistudio.google.com/apikey"},
            {"key": "model", "label": "Text model", "secret": False, "required": False,
             "placeholder": "gemini-2.5-flash", "default": "gemini-2.5-flash"},
        ],
    },
    "postgres": {
        "label": "PostgreSQL",
        "category": "Data",
        "description": "Read-only SQL access for the postgres skill.",
        "used_by": ["postgres"],
        "fields": [
            {"key": "connections_json", "label": "Connections JSON", "secret": True,
             "required": True, "multiline": True,
             "placeholder": '{"databases":[{"name":"local","host":"localhost","port":5432,"database":"app","user":"readonly","password":"...","sslmode":"prefer"}]}',
             "help": "https://www.postgresql.org/docs/current/libpq-connect.html"},
            {"key": "connections_path", "label": "Or path to connections file", "secret": False,
             "required": False, "placeholder": "C:\\Users\\me\\.winbrow\\postgres.json"},
        ],
    },
    "notebooklm": {
        "label": "NotebookLM",
        "category": "Research",
        "description": "Browser-driven; no API key. Sign in in the controlled browser.",
        "used_by": ["notebooklm"],
        "fields": [],
        "manual": True,
    },
    "google": {
        "label": "Google Workspace",
        "category": "Productivity",
        "description": "Gmail, Calendar, Drive, Docs, Sheets, Slides, and Chat.",
        "used_by": ["google-workspace"],
        "fields": [
            {"key": "credentials_path", "label": "OAuth client JSON path", "secret": False,
             "required": True, "placeholder": "C:\\Users\\me\\.winbrow\\google_credentials.json",
             "help": "https://developers.google.com/workspace/guides/create-credentials"},
            {"key": "token_path", "label": "Token path", "secret": False, "required": False,
             "placeholder": "C:\\Users\\me\\.winbrow\\google_token.json"},
        ],
        "oauth": True,
    },
    "outline": {
        "label": "Outline",
        "category": "Collaboration",
        "description": "Team wiki search, reading, and document updates.",
        "used_by": ["outline"],
        "fields": [
            {"key": "base_url", "label": "Base URL", "secret": False, "required": True,
             "placeholder": "https://wiki.example.com"},
            {"key": "token", "label": "API token", "secret": True, "required": True,
             "placeholder": "ol_api_...", "help": "https://docs.getoutline.com/s/guide"},
        ],
    },
    "n8n": {
        "label": "n8n",
        "category": "Automation",
        "description": "Workflow design and execution against an n8n instance.",
        "used_by": ["n8n-workflows"],
        "fields": [
            {"key": "base_url", "label": "Base URL", "secret": False, "required": True,
             "placeholder": "https://n8n.example.com"},
            {"key": "api_key", "label": "API key", "secret": True, "required": True,
             "placeholder": "n8n_api_...", "help": "https://docs.n8n.io/api/"},
        ],
    },
    "mercury": {
        "label": "Mercury MCP",
        "category": "Collaboration",
        "description": "Agent teammate messaging, threads, tasks, and automations.",
        "used_by": ["mercury"],
        "fields": [
            {"key": "mcp_url", "label": "MCP URL", "secret": False, "required": True,
             "placeholder": "https://mercury.example.com/mcp"},
            {"key": "token", "label": "Token", "secret": True, "required": False,
             "placeholder": "optional bearer token"},
        ],
    },
    "pixelbin": {
        "label": "Pixelbin",
        "category": "Media",
        "description": "Image/video generation, transforms, and CDN delivery.",
        "used_by": ["pixelbin-media"],
        "fields": [
            {"key": "api_key", "label": "API key", "secret": True, "required": True,
             "placeholder": "pb_...", "help": "https://www.pixelbin.io/"},
            {"key": "cloud", "label": "Cloud name", "secret": False, "required": False,
             "placeholder": "my-cloud"},
            {"key": "zone", "label": "Zone", "secret": False, "required": False,
             "placeholder": "my-zone"},
        ],
    },
}

ENV_KEYS = {
    ("gemini", "api_key"): ("GEMINI_API_KEY",),
    ("outline", "base_url"): ("OUTLINE_API_URL",),
    ("outline", "token"): ("OUTLINE_API_TOKEN",),
    ("n8n", "base_url"): ("N8N_BASE_URL",),
    ("n8n", "api_key"): ("N8N_API_KEY",),
    ("mercury", "mcp_url"): ("MERCURY_MCP_URL",),
    ("mercury", "token"): ("MERCURY_MCP_TOKEN",),
    ("pixelbin", "api_key"): ("PIXELBIN_API_KEY",),
    ("google", "credentials_path"): ("GOOGLE_OAUTH_CLIENT",),
    ("google", "token_path"): ("GOOGLE_OAUTH_TOKEN",),
}

DEPENDENCIES = [
    {"module": "curl_cffi", "package": "curl_cffi", "used_by": ["web-search"]},
    {"module": "trafilatura", "package": "trafilatura", "used_by": ["web-fetch"]},
    {"module": "pypdf", "package": "pypdf", "used_by": ["pdf", "web-fetch"]},
    {"module": "fpdf", "package": "fpdf2", "used_by": ["pdf"]},
    {"module": "pdfminer", "package": "pdfminer.six", "used_by": ["pdf", "invoice-organizer", "web-fetch"]},
    {"module": "pdfplumber", "package": "pdfplumber", "used_by": ["pdf"]},
    {"module": "pypdfium2", "package": "pypdfium2", "used_by": ["pdf"]},
    {"module": "docx", "package": "python-docx", "used_by": ["docx"]},
    {"module": "pptx", "package": "python-pptx", "used_by": ["pptx"]},
    {"module": "PIL", "package": "Pillow", "used_by": ["pdf", "pptx", "image-search"]},
    {"module": "yt_dlp", "package": "yt-dlp", "used_by": ["video-downloader", "youtube-transcript"]},
    {"module": "psycopg2", "package": "psycopg2-binary", "used_by": ["postgres"]},
    {"module": "googleapiclient", "package": "google-api-python-client", "used_by": ["google-workspace"]},
    {"module": "google_auth_oauthlib", "package": "google-auth-oauthlib", "used_by": ["google-workspace"]},
    {"module": "google.auth", "package": "google-auth", "used_by": ["google-workspace"]},
]


# ---------------------------------------------------------------------------
# Secure storage
# ---------------------------------------------------------------------------

def _store_path() -> Path:
    override = os.environ.get("WINBROW_INTEGRATIONS_PATH")
    return Path(override).expanduser().resolve() if override else DEFAULT_STORE


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(text: str) -> str:
    if os.name != "nt":
        return "b64:" + base64.b64encode(text.encode("utf-8")).decode("ascii")
    try:
        raw = text.encode("utf-8")
        buffer = ctypes.create_string_buffer(raw)
        blob_in = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        if crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out),
        ):
            encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    except Exception as exc:
        log.warning(f"DPAPI protect failed; using local encoding: {exc}")
    return "b64:" + base64.b64encode(text.encode("utf-8")).decode("ascii")


def _dpapi_unprotect(value: str) -> str:
    if not value:
        return ""
    if value.startswith("b64:"):
        return base64.b64decode(value[4:]).decode("utf-8")
    if not value.startswith("dpapi:"):
        return value
    if os.name != "nt":
        return ""
    try:
        raw = base64.b64decode(value[6:])
        buffer = ctypes.create_string_buffer(raw)
        blob_in = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        if crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out),
        ):
            decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return decrypted.decode("utf-8")
    except Exception as exc:
        log.warning(f"DPAPI unprotect failed: {exc}")
    return ""


def _load_store() -> dict:
    path = _store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "providers": {}}
    except Exception as exc:
        log.warning(f"Corrupt integrations store ignored: {exc}")
        return {"version": 1, "providers": {}}
    if not isinstance(data, dict):
        return {"version": 1, "providers": {}}
    data.setdefault("providers", {})
    return data


def _save_store(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    if os.name == "nt":
        try:
            os.system(f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%":F >nul 2>&1')
        except Exception:
            pass


def _provider_values(provider: str) -> dict[str, str]:
    spec = PROVIDERS.get(provider)
    if spec is None:
        return {}
    stored = _load_store().get("providers", {}).get(provider, {})
    raw = stored.get("values") or {}
    values: dict[str, str] = {}
    for field in spec.get("fields", []):
        key = field["key"]
        if field.get("secret"):
            values[key] = _dpapi_unprotect(str(raw.get(key) or ""))
        else:
            values[key] = str(raw.get(key) or "")
    return values


def get_secret(provider: str, key: str, default: str = "") -> str:
    """Return an env value first, then the decrypted stored value."""
    for env_name in ENV_KEYS.get((provider, key), ()):
        value = os.environ.get(env_name)
        if value:
            return value
    return _provider_values(provider).get(key) or default


def _configured(provider: str) -> bool:
    spec = PROVIDERS[provider]
    if spec.get("manual"):
        return True
    values = _provider_values(provider)
    for field in spec.get("fields", []):
        if field.get("required") and not str(values.get(field["key"]) or "").strip():
            return False
    return any(str(value).strip() for value in values.values())


def _last_test(provider: str) -> dict:
    stored = _load_store().get("providers", {}).get(provider, {})
    return stored.get("last_test") or {}


def _save_test(provider: str, ok: bool, message: str) -> None:
    with _LOCK:
        data = _load_store()
        entry = data.setdefault("providers", {}).setdefault(provider, {})
        entry["last_test"] = {"ok": bool(ok), "message": message, "ts": time.time()}
        _save_store(data)


def _mask(value: str) -> str:
    return "••••" if value else ""


def provider_status(provider: str) -> dict:
    spec = PROVIDERS[provider]
    values = _provider_values(provider)
    fields = []
    for field in spec.get("fields", []):
        key = field["key"]
        env_value = any(os.environ.get(name) for name in ENV_KEYS.get((provider, key), ()))
        value = values.get(key, "")
        fields.append({
            "key": key,
            "label": field.get("label", key),
            "secret": bool(field.get("secret")),
            "required": bool(field.get("required")),
            "multiline": bool(field.get("multiline")),
            "placeholder": field.get("placeholder", ""),
            "help": field.get("help", ""),
            "value": _mask(value) if field.get("secret") else value,
            "has_value": bool(value) or env_value,
            "source": "environment" if env_value else ("stored" if value else ""),
        })
    return {
        "id": provider,
        "label": spec.get("label", provider),
        "category": spec.get("category", ""),
        "description": spec.get("description", ""),
        "used_by": spec.get("used_by", []),
        "manual": bool(spec.get("manual")),
        "oauth": bool(spec.get("oauth")),
        "configured": _configured(provider),
        "last_test": _last_test(provider),
        "fields": fields,
    }


def list_integrations() -> dict:
    return {
        "providers": [provider_status(provider) for provider in PROVIDERS],
        "dependencies": dependency_status(),
    }


def save_integration(provider: str, values: dict[str, Any]) -> dict:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    values = values or {}
    spec = PROVIDERS[provider]
    with _LOCK:
        data = _load_store()
        entry = data.setdefault("providers", {}).setdefault(provider, {})
        stored = entry.setdefault("values", {})
        for field in spec.get("fields", []):
            key = field["key"]
            if key not in values:
                continue
            value = str(values.get(key) or "")
            if field.get("secret"):
                if value and value != "••••":
                    stored[key] = _dpapi_protect(value)
            else:
                stored[key] = value
        entry["updated"] = time.time()
        # A changed configuration invalidates the previous probe result.
        entry.pop("last_test", None)
        _save_store(data)
    return provider_status(provider)


def clear_integration(provider: str) -> bool:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    with _LOCK:
        data = _load_store()
        entry = data.get("providers", {}).get(provider)
        if not entry:
            return False
        data["providers"].pop(provider, None)
        _save_store(data)
    return True


def dependency_status() -> list[dict]:
    import importlib.util
    result = []
    for item in DEPENDENCIES:
        installed = importlib.util.find_spec(item["module"]) is not None
        result.append({**item, "installed": installed})
    return result


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _http_json(method: str, url: str, *, headers: dict | None = None,
               payload: dict | None = None, timeout: float = 15) -> tuple[int, Any]:
    import requests
    response = requests.request(
        method, url, headers=headers or {}, json=payload, timeout=timeout,
    )
    try:
        data = response.json()
    except Exception:
        data = response.text[:1000]
    return response.status_code, data


def _test_gemini(values: dict) -> tuple[bool, str]:
    key = get_secret("gemini", "api_key")
    if not key:
        return False, "Add a Gemini API key."
    status, data = _http_json(
        "GET", "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
    )
    if status == 200:
        count = len(data.get("models", [])) if isinstance(data, dict) else 0
        return True, f"Gemini reachable ({count} models)."
    return False, f"Gemini returned HTTP {status}: {str(data)[:200]}"


def _test_outline(values: dict) -> tuple[bool, str]:
    base = get_secret("outline", "base_url").rstrip("/")
    token = get_secret("outline", "token")
    if not base or not token:
        return False, "Add the Outline base URL and API token."
    status, data = _http_json(
        "POST", f"{base}/api/collections.list",
        headers={"Authorization": f"Bearer {token}"}, payload={"limit": 1},
    )
    return status < 400, f"Outline returned HTTP {status}." if status >= 400 else "Outline reachable."


def _test_n8n(values: dict) -> tuple[bool, str]:
    base = get_secret("n8n", "base_url").rstrip("/")
    key = get_secret("n8n", "api_key")
    if not base or not key:
        return False, "Add the n8n base URL and API key."
    status, data = _http_json(
        "GET", f"{base}/api/v1/workflows?limit=1",
        headers={"X-N8N-API-KEY": key},
    )
    return status < 400, f"n8n returned HTTP {status}." if status >= 400 else "n8n reachable."


def _test_pixelbin(values: dict) -> tuple[bool, str]:
    key = get_secret("pixelbin", "api_key")
    if not key:
        return False, "Add a Pixelbin API key."
    status, data = _http_json(
        "GET", "https://api.pixelbin.io/v1/",
        headers={"Authorization": f"Bearer {key}"}, timeout=10,
    )
    if status in (401, 403):
        return False, "Pixelbin rejected the key."
    if status == 404:
        return True, "Pixelbin is reachable; the probe endpoint returned 404."
    return status < 500, f"Pixelbin returned HTTP {status}."


def _test_postgres(values: dict) -> tuple[bool, str]:
    try:
        import psycopg2
    except Exception:
        return False, "Install psycopg2-binary to test PostgreSQL."
    connections = get_secret("postgres", "connections_json")
    path = get_secret("postgres", "connections_path")
    if connections:
        try:
            data = json.loads(connections)
        except Exception as exc:
            return False, f"Connections JSON is invalid: {exc}"
    elif path:
        try:
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"Could not read connections file: {exc}"
    else:
        dsn = os.environ.get("WINBROW_POSTGRES_DSN", "")
        if not dsn:
            return False, "Add a PostgreSQL connection."
        data = {"databases": [{"dsn": dsn}]}
    databases = data.get("databases") if isinstance(data, dict) else data
    if not isinstance(databases, list) or not databases:
        return False, "No database entries found."
    entry = databases[0]
    try:
        if entry.get("dsn"):
            conn = psycopg2.connect(entry["dsn"], connect_timeout=6)
        else:
            conn = psycopg2.connect(
                host=entry.get("host"), port=entry.get("port", 5432),
                dbname=entry.get("database") or entry.get("dbname"),
                user=entry.get("user"), password=entry.get("password"),
                sslmode=entry.get("sslmode", "prefer"), connect_timeout=6,
            )
        with conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        conn.close()
        return True, "PostgreSQL connection succeeded."
    except Exception as exc:
        return False, f"PostgreSQL connection failed: {str(exc)[:200]}"


def _test_google(values: dict) -> tuple[bool, str]:
    token = get_secret("google", "token_path") or str(
        Path.home() / ".winbrow" / "google_token.json"
    )
    if not token or not Path(token).expanduser().is_file():
        return False, "Google is not connected. Save the OAuth client and click Connect."
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(token)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return bool(creds.valid), "Google token is valid." if creds.valid else "Google token is not valid."
    except Exception as exc:
        return False, f"Google token check failed: {str(exc)[:200]}"


def _test_mercury(values: dict) -> tuple[bool, str]:
    url = get_secret("mercury", "mcp_url")
    if not url:
        return False, "Add the Mercury MCP URL."
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False, "The MCP URL has no host."
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=5):
            return True, f"Mercury host {host}:{port} is reachable."
    except Exception as exc:
        return False, f"Mercury host is unreachable: {exc}"


def _test_notebooklm(values: dict) -> tuple[bool, str]:
    return True, "Open NotebookLM in the controlled browser and sign in manually."


PROBES: dict[str, Callable[[dict], tuple[bool, str]]] = {
    "gemini": _test_gemini,
    "postgres": _test_postgres,
    "notebooklm": _test_notebooklm,
    "google": _test_google,
    "outline": _test_outline,
    "n8n": _test_n8n,
    "mercury": _test_mercury,
    "pixelbin": _test_pixelbin,
}


def test_integration(provider: str) -> dict:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    try:
        ok, message = PROBES[provider]({})
    except Exception as exc:
        ok, message = False, f"Test failed: {exc}"
    _save_test(provider, ok, message)
    return {"ok": ok, "message": message, "status": provider_status(provider)}


def google_oauth() -> dict:
    """Run the installed-app OAuth flow. Call from a worker thread."""
    credentials_path = get_secret("google", "credentials_path")
    token_path = get_secret("google", "token_path") or str(
        Path.home() / ".winbrow" / "google_token.json"
    )
    if not credentials_path or not Path(credentials_path).expanduser().is_file():
        return {"ok": False, "message": "Save a valid OAuth client JSON path first."}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(
            str(Path(credentials_path).expanduser()),
            [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/presentations",
                "https://www.googleapis.com/auth/chat.messages",
            ],
        )
        creds = flow.run_local_server(port=0, open_browser=True)
        target = Path(token_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(creds.to_json(), encoding="utf-8")
        if not get_secret("google", "token_path"):
            save_integration("google", {"token_path": str(target)})
        _save_test("google", True, "Google authorization saved.")
        return {"ok": True, "message": f"Google token saved to {target}."}
    except Exception as exc:
        _save_test("google", False, f"Google OAuth failed: {exc}")
        return {"ok": False, "message": f"Google OAuth failed: {exc}"}


# ---------------------------------------------------------------------------
# Agent tool
# ---------------------------------------------------------------------------

def execute_integration_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name != "integration_status":
        return {"success": False, "output": f"Unknown integration op '{name}'"}
    provider = str(args.get("provider") or "").strip().lower()
    if provider not in PROVIDERS:
        return {
            "success": False,
            "output": f"Unknown integration '{provider}'. Available: {', '.join(PROVIDERS)}",
        }
    status = provider_status(provider)
    if status["configured"]:
        message = (
            f"{status['label']} is configured. "
            f"Last test: {status['last_test'].get('message') or 'not tested'}."
        )
    else:
        message = (
            f"{status['label']} is not configured. Ask the user to open "
            "Integrations in the WinBrow UI and add the required fields."
        )
    return {"success": True, "output": message, "configured": status["configured"]}


def get_integration_tools() -> list[Tool]:
    return [
        Tool(
            name="integration_status",
            description=(
                "Check whether an optional integration (gemini, postgres, "
                "notebooklm, google, outline, n8n, mercury, pixelbin) is "
                "configured. Use before a gated skill so you can give the user "
                "the exact setup path instead of failing mid-task."
            ),
            script="",
            impl=INTEGRATION_IMPL,
            args=[
                ArgSpec(
                    name="provider", kind="text",
                    instructions="Integration id, e.g. gemini or google.",
                ),
            ],
            speak="result",
            examples=["is Gemini configured", "check the Postgres integration"],
        ),
    ]
