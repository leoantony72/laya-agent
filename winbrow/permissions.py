"""
Tiered Permissions — the single gate in front of every executor (M3)
=====================================================================
Every action funnels through `PermissionManager.gate()` — no exceptions:
  • registry tools (T0/T1 auto, T2 remembered-confirm, T3 always-confirm)
  • computer atoms / adapter ops (classified by name)
  • learned/generated PowerShell scripts (script-pattern classification)
  • installer ops (always T3)

Tiers (from UPGRADE_PLAN §6):
  T0 readable    — read-only sensing                       → auto
  T1 reversible  — open app/file/folder, volume, focus     → auto
  T2 user-data   — file writes outside temp, close windows → confirm ONCE (remembered)
  T3 system      — installs, uninstalls, shutdown-class    → confirm EVERY time + blocklist denied

Confirm channels:
  • interactive: `use_pending_*` by the server (UI/tap + /api/confirm)
  • programmatic caller: install a confirm_fn (voice/CLI) — returns bool
  • tests/e2e opt-in bypass: WINBROW_BYPASS_CONFIRM=1
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("winbrow.permissions")

# --------------------------------------------------------------------------- tiers

T0, T1, T2, T3 = "t0", "t1", "t2", "t3"
TIER_ORDER = {T0: 0, T1: 1, T2: 2, T3: 3}

PERMISSIONS_FILENAME = "permissions.json"
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "tools"

PENDING_TTL_S = 900.0          # a pending confirmation stays valid 15 minutes

# Tool-name → tier. Everything not listed defaults via _default_tier().
TOOL_TIERS: Dict[str, str] = {
    # T0 — read-only sensing
    "screen_read_text": T0, "current_time": T0, "network_status": T0,
    "battery_health": T0, "clipboard_inspect": T0, "vscode_list_extensions": T0,
    "aftereffects_detect": T0, "take_screenshot": T0, "app_search": T0,
    "read_document": T0,
    "integration_status": T0,
    "web_search": T0,
    "web_fetch": T0,
    # T1 — reversible
    "computer_click": T1, "computer_click_text": T1, "computer_type": T1,
    "computer_key": T1, "computer_scroll": T1, "computer_paste": T1,
    "focus_window": T1, "wait_for_window": T1, "window_action": T1,  # 'close' overridden dynamically
    "volume_set_level": T1, "system_volume": T1, "mute": T1, "media_playback": T1,
    "app_focus_or_launch": T1, "open_common_app": T1, "open_file": T1,
    "open_folder": T1, "close_folder_window": T2,
    "laya_ultrafast_browser": T1, "take_screenshot_b64": T0,
    "browser_navigate": T1, "browser_interact": T1, "browser_wait": T1,
    "browser_upload_file": T1,
    "browser_read_page": T0, "browser_read_full": T0,
    "browser_get_accessibility_tree": T0,
    "delegate_task": T1,
    "vscode_open": T1, "vscode_install_extension": T2, "vscode_run_command": T1,
    "notepad_open": T1, "paint_open_image": T1, "office_open_doc": T1,
    "quick_note": T1,
    # T2 — user-data writes
    "notepad_save_as": T2, "paint_save_copy": T2, "paint_invert_colors": T2,
    "photoshop_save_as": T2, "photoshop_desaturate": T2, "photoshop_open_image": T1,
    "office_save_as_pdf": T2, "file_dialog_save": T2,
    "create_text_file": T2, "download_file": T2,
    # Image search/download writes into Pictures (or the requested folder);
    # applying a wallpaper is reversible.
    "download_images": T2, "download_wallpaper": T2, "set_wallpaper": T1,
    # Bundled skill helpers may write user files; scripts are allowlisted but
    # the tool still runs user-visible work, so keep it at the write tier.
    "run_skill_script": T2,
    # Shell execution — base T2; gate() raises to T3 for system-class commands.
    "run_command": T2,
    # T3 — system
    "app_install": T3, "app_uninstall": T3, "app_upgrade_all": T3,
    "toggle_dark_mode": T1, "windows_settings": T1,
}

# Frozen snapshot of names classified *explicitly in code* — tool_tier()
# grows TOOL_TIERS in-memory (setdefault cache), so the gate's lifting
# decision must check this snapshot, not the mutated dict.
_CURATED_TIERS = frozenset(TOOL_TIERS.keys())


def _default_tier(tool_name: str) -> str:
    """Classify tools that aren't in the table."""
    n = (tool_name or "").lower()
    if any(k in n for k in ("install", "uninstall", "shutdown", "reboot", "registry", "service", "disk")):
        return T3
    if any(k in n for k in ("save", "write", "export", "delete", "remove", "close", "move", "copy", "paste", "learn",
                            "edit", "creat", "commit")):
        return T2
    if any(k in n for k in ("screenshot", "read", "list", "status", "time", "battery", "network", "detect", "show", "get")):
        return T0
    return T1


def tool_tier(tool_name: str) -> str:
    low = (tool_name or "").lower()
    if low in TOOL_TIERS:
        return TOOL_TIERS[low]
    t = _default_tier(low)
    TOOL_TIERS.setdefault(low, t)     # cache; table grows in-memory only
    return t


# --------------------------------------------------------------------------- action key

def action_key(tool_name: str, args: dict) -> str:
    """Stable fingerprint of (tool, normalized args) for allowlisting."""
    norm = {str(k).lower(): str(v).strip().lower()[:160] for k, v in (args or {}).items()}
    blob = f"{(tool_name or '').lower()}|{json.dumps(norm, sort_keys=True)}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- pending store

def _emit_confirm(pending: "PendingAction") -> None:
    """Tell the island/dashboard that a decision is waiting on the user."""
    try:
        from .events import emit
        emit("confirm.required", dict(pending.to_dict()))
    except Exception:
        pass


@dataclass
class PendingAction:
    id: str
    tier: str
    tool: str
    args: dict
    utterance: str
    session_id: str
    created: float
    status: str = "pending"          # pending | approved | denied

    def to_dict(self) -> dict:
        return {
            "confirmation_id": self.id, "tier": self.tier, "tool": self.tool,
            "args": self.args, "utterance": self.utterance,
            "session_id": self.session_id, "status": self.status,
            "message": f"Allow {self.tier.upper()} action '{self.tool}'?",
            "created": self.created,
            "expires_at": self.created + PENDING_TTL_S,
        }


# --------------------------------------------------------------------------- manager

ConfirmFn = Callable[[dict], "Optional[Awaitable[bool]]"]


class PermissionManager:
    """Tier gate + remembered allowlist (T2) + pending confirmations."""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            env_dir = os.environ.get("WINBROW_PERMISSIONS_DIR")
            path = (Path(env_dir) / PERMISSIONS_FILENAME) if env_dir else (DEFAULT_DIR / PERMISSIONS_FILENAME)
        self.path = Path(path)
        self._allowed: Dict[str, dict] = self._load()          # action_key → {tier, ts, label}
        self._pending: Dict[str, PendingAction] = {}
        self._confirm_fn: Optional[ConfirmFn] = None

    # ------------------------------------------------------------------ IO
    def _load(self) -> Dict[str, dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return {k: v for k, v in json.load(f).items() if isinstance(v, dict)}
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning(f"Corrupt permissions file ignored: {e}")
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._allowed, f, indent=2)
        os.replace(tmp, self.path)

    def reset_in_memory(self) -> None:
        self._allowed = self._load()

    # ------------------------------------------------------------------ hooks
    def set_confirm_fn(self, fn: Optional[ConfirmFn]) -> None:
        """Programmatic confirmation (voice/CLI). fn(dict) -> bool (or awaitable)."""
        self._confirm_fn = fn

    @property
    def bypass(self) -> bool:
        return os.environ.get("WINBROW_BYPASS_CONFIRM", "") in ("1", "true", "True")

    # ------------------------------------------------------------------ THE gate
    async def gate(
        self,
        tool_name: str,
        args: dict,
        utterance: str = "",
        session_id: str = "default",
        tool: Any = None,
        script: str = "",
    ) -> Tuple[bool, Optional[str], Optional[dict]]:
        """THE single entry point (policy.gate per plan §6).

        Returns (allowed, reason, pending_dict).
            allowed=True            → execute immediately
            allowed=False + pending → permission_required; call /api/confirm
            allowed=False + reason  → DENIED outright (blocklist)
        """
        tier = tool_tier(tool_name)
        # 'close window' is T2 per plan; other window actions stay T1
        if tool_name == "window_action" and str((args or {}).get("action", "")).lower() == "close":
            tier = T2
        # Raw shell commands: hard-deny blocked patterns, raise the tier for
        # system-class content, otherwise stay at the curated T2 baseline.
        if (tool_name or "").lower() == "run_command":
            command = str((args or {}).get("command", ""))
            from .policy import PolicyError, script_tier, validate_script_safety
            try:
                validate_script_safety(command)
            except PolicyError as e:
                return (False, f"blocked by policy: {e}", None)
            cmd_tier = script_tier(command)
            if TIER_ORDER[cmd_tier] > TIER_ORDER[tier]:
                tier = cmd_tier
        # Learned tools run user/agent-authored scripts — never below T2.
        # impl-executed tools (MCP bridge/app native) carry no authored
        # PowerShell, so the floor does not apply to them.
        if tool is not None and getattr(tool, "is_learned", False) \
                and getattr(tool, "impl", None) is None \
                and TIER_ORDER[tier] < TIER_ORDER[T2]:
            tier = T2
        # Rendered script can raise the tier (system-class content → T3), but
        # only for tools tiered by heuristics/learned/generated work — curated
        # TOOL_TIERS entries are trusted as classified (a "screenshot" that
        # saves a PNG must not require T2 approval; that kills voice UX).
        if script and (tool_name or "").lower() not in _CURATED_TIERS:
            from .policy import script_tier
            s_tier = script_tier(script)
            if TIER_ORDER[s_tier] > TIER_ORDER[tier]:
                tier = s_tier

        akey = action_key(tool_name, args)

        if self.bypass:
            return (True, None, None)
        if os.environ.get("WINBROW_AUTO_APPROVE_T2", "").lower() in ("1", "true") and tier == T2:
            self.remember(akey, tier, tool_name)
            log.info(f"Auto-approved T2 action '{tool_name}' via WINBROW_AUTO_APPROVE_T2")
            return (True, None, None)
        if TIER_ORDER[tier] <= TIER_ORDER[T1]:
            return (True, None, None)

        # T2: remembered approval allows instant replay
        if tier == T2 and akey in self._allowed:
            return (True, None, None)

        # 1. interactive confirm_fn (voice / CLI in-process)
        if self._confirm_fn is not None:
            try:
                res = self._confirm_fn(self._desc(tool_name, args, tier, utterance))
                if hasattr(res, "__await__"):
                    res = await res
                if res:
                    if tier == T2:
                        self.remember(akey, tier, tool_name)
                    log.info(f"Allowed {tier} '{tool_name}' via confirm hook")
                    return (True, None, None)
                return (False, "denied by user", None)
            except Exception as e:
                log.warning(f"Confirm hook failed ({e}); falling back to pending flow")

        # 2. pending-flow (server / headless): create or reuse a pending record
        existing = self._find_any(tool_name, akey)
        if existing is not None:
            if existing.status == "approved":
                return (True, None, None)       # /api/confirm approved it already
            if existing.status == "denied":
                return (False, "denied by user", None)
            if existing.status == "pending":
                _emit_confirm(existing)
                return (False, None, existing.to_dict())

        p = PendingAction(
            id=uuid.uuid4().hex[:12], tier=tier, tool=tool_name,
            args=dict(args or {}), utterance=utterance or "", session_id=session_id,
            created=time.time(),
        )
        self._pending[p.id] = p
        self._prune_pending()
        log.info(f"Permission {tier.upper()} needed for '{tool_name}': pending {p.id}")
        _emit_confirm(p)
        return (False, None, p.to_dict())

    def _desc(self, tool_name: str, args: dict, tier: str, utterance: str) -> dict:
        return {
            "tool": tool_name, "tier": tier, "args": args,
            "utterance": utterance,
            "message": f"Allow {tier.upper()} action '{tool_name}'?",
        }

    # ------------------------------------------------------------------ pending mgmt
    def _prune_pending(self) -> None:
        now = time.time()
        for pid in list(self._pending.keys()):
            if now - self._pending[pid].created > PENDING_TTL_S:
                del self._pending[pid]

    def _find_pending(self, tool_name: str, akey: str) -> Optional[PendingAction]:
        for p in self._pending.values():
            if action_key(p.tool, p.args) == akey and p.status == "pending":
                return p
        return None

    def _find_any(self, tool_name: str, akey: str) -> Optional[PendingAction]:
        for p in self._pending.values():
            if action_key(p.tool, p.args) == akey:
                return p
        return None

    def get_pending(self, confirmation_id: str) -> Optional[PendingAction]:
        return self._pending.get(confirmation_id)

    def resolve_pending(self, confirmation_id: str, approve: bool, remember: bool = False) -> Optional[PendingAction]:
        p = self._pending.get(confirmation_id)
        if not p:
            return None
        p.status = "approved" if approve else "denied"
        if approve and remember and p.tier == T2:
            self.remember(action_key(p.tool, p.args), p.tier, p.tool)
        return p

    def pending_list(self) -> List[dict]:
        self._prune_pending()
        return [p.to_dict() for p in self._pending.values() if p.status == "pending"]

    # clean up one-shot system actions after every confirmation
    def consume(self, tool_name: str, args: dict) -> None:
        akey = action_key(tool_name, args)
        for pid, p in list(self._pending.items()):
            if action_key(p.tool, p.args) == akey:
                del self._pending[pid]

    # ------------------------------------------------------------------ allowlist
    def remember(self, akey: str, tier: str, tool_name: str) -> None:
        if tier != T2:
            return  # only T2 approvals are remembered (T3 asks every time)
        self._allowed[akey] = {"tier": tier, "tool": tool_name, "ts": time.time()}
        self._save()

    def forget_all(self) -> int:
        n = len(self._allowed)
        self._allowed = {}
        self._save()
        return n

    def list_allowed(self) -> List[dict]:
        return [{"key": k, **v} for k, v in self._allowed.items()]


# singleton
_pm: Optional[PermissionManager] = None


def get_permissions() -> PermissionManager:
    global _pm
    if _pm is None:
        _pm = PermissionManager()
    return _pm


def reset_permissions() -> PermissionManager:
    global _pm
    _pm = PermissionManager()
    return _pm
