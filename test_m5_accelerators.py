"""
M5 — Optional accelerators (observe service, cloud-VLM pixel tier)
==================================================================
Rung-4 VLM grounding is opt-in + budget-capped; the observe service is
LLM-free pattern matching over episodic memory.
All external calls at unit level: fake store duck-types MemoryStore's
get_recent(kind=), HTTP mocked with AsyncMock/SideEffect.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["WINBROW_TTS"] = "0"
os.environ["WINBROW_NORMALIZE"] = "0"
os.environ["WINBROW_CLOUD_VISION"] = "0"   # off by default in tests

from winbrow.observe import (Observer, Suggestion, DismissStore,             # noqa: E402
                             K_FILE_OPEN, K_FOLDER_OPEN, K_APP_LAUNCH,
                             K_WEB_SEARCH)
from winbrow.memory.store import MemoryStore                                # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeEntry:
    def __init__(self, kind, ts, label="", path="", app="", content=""):
        self.kind = kind
        self.timestamp = ts
        self.label = label
        self.path = path
        self.app = app
        self.content = content
        self.metadata = {}


class FakeStore:
    def __init__(self):
        self.entries: list = []

    def record(self, kind, ts=None, label="", path="", app="", content="", **kw):
        self.entries.append(FakeEntry(kind, ts if ts is not None else time.time(),
                                      label=label, path=path, app=app,
                                      content=content))

    def get_recent(self, limit=200, kind=None):
        out = [e for e in self.entries if kind is None or e.kind == kind]
        out.sort(key=lambda e: -e.timestamp)
        return out[:limit]


class TestDismissStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m5_dismiss_"))

    def test_dismiss_roundtrip(self):
        d = DismissStore(self.tmp / "d.json")
        d.dismiss("a:123")
        self.assertTrue(d.dismissed("a:123"))
        self.assertFalse(d.dismissed("b:456"))
        # persistence across instances
        d2 = DismissStore(self.tmp / "d.json")
        self.assertTrue(d2.dismissed("a:123"))

    def test_cooldown_expiry(self):
        d = DismissStore(self.tmp / "d.json")
        d._data["a:1"] = time.time() - 13 * 3600   # older than cooldown
        self.assertFalse(d.dismissed("a:1"))


class TestObserver(unittest.TestCase):
    def _observer(self, store: FakeStore) -> Observer:
        tmp = Path(tempfile.mkdtemp(prefix="m5_obs_"))
        return Observer(store=store, dismiss_path=tmp / "d.json")

    def test_repeat_file_open(self):
        st = FakeStore()
        now = time.time()
        st.record(K_FILE_OPEN, ts=now - 3600, label="report.pdf", path=r"D:\docs\report.pdf")
        st.record(K_FILE_OPEN, ts=now - 86400, label="report.pdf", path=r"D:\docs\report.pdf")
        obs = self._observer(st)
        sugs = obs.refresh()
        rep = [s for s in sugs if s.kind == "repeat_open" and "report" in s.title.lower()]
        self.assertTrue(rep, f"expected repeat_open suggestion, got {sugs}")
        self.assertIn("report.pdf", rep[0].utterance)
        self.assertGreaterEqual(rep[0].score, 2.0)

    def test_single_open_no_suggestion(self):
        st = FakeStore()
        st.record(K_FILE_OPEN, ts=time.time(), label="once.pdf", path=r"D:\x\once.pdf")
        obs = self._observer(st)
        sugs = obs.refresh()
        self.assertFalse(any(s.kind == "repeat_open" and "once" in s.title.lower()
                             for s in sugs))

    def test_old_events_ignored(self):
        st = FakeStore()
        st.record(K_FILE_OPEN, ts=time.time() - 30 * 86400,   # 30 days old
                  label="old.pdf", path=r"D:\x\old.pdf")
        st.record(K_FILE_OPEN, ts=time.time() - 30 * 86400, label="old.pdf", path=r"D:\x\old.pdf")
        obs = self._observer(st)
        sugs = obs.refresh()
        self.assertFalse(any(s.kind == "repeat_open" for s in sugs))

    def test_folder_open_included(self):
        st = FakeStore()
        now = time.time()
        st.record(K_FOLDER_OPEN, ts=now - 100, label="Downloads",
                  path=r"C:\Users\X\Downloads")
        st.record(K_FOLDER_OPEN, ts=now - 90000, label="Downloads",
                  path=r"C:\Users\X\Downloads")
        obs = self._observer(st)
        sugs = obs.refresh()
        rep = [s for s in sugs if s.kind == "repeat_open"]
        self.assertTrue(rep, "repeat folder open should suggest")

    def test_app_pattern_three_distinct_days(self):
        st = FakeStore()
        now = time.time()
        # same app on 3 distinct days (yesterday, 2d, 3d ago)
        st.record(K_APP_LAUNCH, ts=now - 86400, app="vscode", label="vscode")
        st.record(K_APP_LAUNCH, ts=now - 2 * 86400, app="vscode", label="vscode")
        st.record(K_APP_LAUNCH, ts=now - 3 * 86400, app="vscode", label="vscode")
        st.record(K_APP_LAUNCH, ts=now - 100, app="paint", label="paint")
        obs = self._observer(st)
        sugs = obs.refresh()
        apps = [s for s in sugs if s.kind == "app_pattern"]
        self.assertTrue(any("vscode" in s.target.lower() for s in apps))
        self.assertFalse(any("paint" in s.target.lower() for s in apps))

    def test_search_repeat(self):
        st = FakeStore()
        now = time.time()
        st.record(K_WEB_SEARCH, ts=now - 500, label="cheap flights",
                  content="cheap flights")
        st.record(K_WEB_SEARCH, ts=now - 90000, label="cheap flights",
                  content="cheap flights")
        obs = self._observer(st)
        sugs = obs.refresh()
        se = [s for s in sugs if s.kind == "search_repeat"]
        self.assertTrue(se, "repeated search should suggest")
        self.assertIn("cheap flights", se[0].utterance.lower())

    def test_dismissal_hidden_until_cooldown(self):
        st = FakeStore()
        now = time.time()
        st.record(K_FILE_OPEN, ts=now - 10, label="report.pdf", path=r"D:\report.pdf")
        st.record(K_FILE_OPEN, ts=now - 86400, label="report.pdf", path=r"D:\report.pdf")
        obs = self._observer(st)
        sugs = obs.refresh()
        target = [s for s in sugs if s.kind == "repeat_open"][0]
        obs.dismissals.dismiss(target.id)
        again = obs.refresh()
        self.assertFalse(any(s.id == target.id for s in again))

    def test_real_memory_store_integration(self):
        """End-to-end against the real SQLite MemoryStore."""
        tmp = Path(tempfile.mkdtemp(prefix="m5_store_"))
        real = MemoryStore(db_path=tmp / "mem.db", chroma_path=None)
        now = time.time()
        real.record_file_open(r"D:\docs\wedding.jpg", label="wedding.jpg")
        real.record_file_open(r"D:\docs\wedding.jpg", label="wedding.jpg")
        obs = Observer(store=real, dismiss_path=tmp / "d.json")
        sugs = obs.refresh()
        rep = [s for s in sugs if s.kind == "repeat_open" and "wedding" in s.title.lower()]
        self.assertTrue(rep, f"real store should find repeat opens, got {[(s.kind, s.title) for s in sugs]}")
        self.assertIn("open", rep[0].utterance.lower())


class TestVLMRung(unittest.TestCase):
    def setUp(self):
        self.prev = {k: os.environ.get(k) for k in
                     ("WINBROW_CLOUD_VISION", "OPENROUTER_API_KEY",
                      "WINBROW_VLM_BUDGET_PER_DAY", "WINBROW_OBSERVE_DIR")}
        os.environ["WINBROW_CLOUD_VISION"] = "0"
        os.environ["OPENROUTER_API_KEY"] = ""
        os.environ["WINBROW_OBSERVE_DIR"] = tempfile.mkdtemp(prefix="m5_vlm_")

    def tearDown(self):
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_disabled_by_default(self):
        from winbrow.computer.vlm import vlm_enabled
        self.assertFalse(vlm_enabled())

    def test_enabled_with_env_and_key(self):
        from winbrow.computer.vlm import vlm_enabled
        os.environ["WINBROW_CLOUD_VISION"] = "1"
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        self.assertTrue(vlm_enabled())

    def test_budget_cap(self):
        from winbrow.computer.vlm import vlm_enabled, budget_used_today, vlm_ground
        os.environ["WINBROW_CLOUD_VISION"] = "1"
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        os.environ["WINBROW_VLM_BUDGET_PER_DAY"] = "2"
        # spend budget twice
        data = {"date": time.strftime("%Y-%m-%d"), "count": 2}
        from winbrow.computer import vlm as vlm_mod
        vlm_mod._budget_path().write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(vlm_enabled())
        r = vlm_ground("save button")
        self.assertFalse(r.get("found"))
        self.assertIn("disabled", r.get("reason", ""))

    def test_ground_translates_to_screen_space(self):
        from winbrow.computer import vlm as vlm_mod

        os.environ["WINBROW_CLOUD_VISION"] = "1"
        os.environ["OPENROUTER_API_KEY"] = "sk-test"

        class FakeView:
            image_path = str(Path(tempfile.mkdtemp()) / "shot.png")
            rect = (100, 50, 800, 600)
        Path(FakeView.image_path).write_bytes(b"fakepng")

        class FakeScreen:
            def capture(self):
                return FakeView()

            def capture_window(self, win):
                self.captured_hwnd = win.hwnd
                return FakeView()

        class FakeWin:
            hwnd = 1
            rect = (150, 120, 950, 720)

        fake_driver = type("D", (), {"list_windows": lambda self: [FakeWin()]})()

        with unittest.mock.patch.object(vlm_mod, "vlm_enabled", return_value=True), \
             unittest.mock.patch("winbrow.computer.tools._get_screen",
                                 return_value=FakeScreen()), \
             unittest.mock.patch("winbrow.computer.tools._get_driver",
                                 return_value=fake_driver), \
             unittest.mock.patch.object(vlm_mod, "_openrouter_chat_vision",
                                        return_value={"found": True, "x": 90, "y": 60,
                                                      "label": "Save"}), \
             unittest.mock.patch.object(vlm_mod, "_spend_one"):
            r = vlm_mod.vlm_ground("save", hwnd=1)
        self.assertTrue(r["found"])
        self.assertEqual(r["x"], 150 + 90)   # origin 150 + img x 90
        self.assertEqual(r["y"], 120 + 60)   # origin 120 + img y 60
        self.assertEqual(r["source"], "vlm")

    def test_ladder_reaches_rung4_and_reports(self):
        """Ladder: UIA miss + OCR miss → VLM hit (enabled + mocked)."""
        from winbrow.computer.ladder import GroundingLadder, GroundingHit
        from winbrow.computer.uia import UIATree, UIAElement
        import winbrow.computer.ladder as ladder_mod

        class FakeUIA:
            def observe(self, hwnd):
                return []

        class FakeDriver:
            user32 = None
            def list_windows(self):
                return []
            def GetForegroundWindow(self):  # type: ignore
                return 0

        d = FakeDriver()
        lad = GroundingLadder(driver=d, uia=FakeUIA(), screen=None, ocr_enabled=True)
        d.user32 = type("U", (), {"GetForegroundWindow": lambda s, *a: 0})()

        fake = GroundingHit(ok=True, source="vlm", x=10, y=20)
        os.environ["WINBROW_CLOUD_VISION"] = "1"
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        with unittest.mock.patch("winbrow.computer.vlm.vlm_enabled", return_value=True), \
             unittest.mock.patch("winbrow.computer.vlm.vlm_ground",
                                 return_value={"found": True, "x": 10, "y": 20,
                                               "matched": "Save", "source": "vlm"}):
            hit = lad.resolve_target("save", hwnd=1)   # nonzero so the early guard passes
        os.environ["WINBROW_CLOUD_VISION"] = "0"
        self.assertTrue(hit.ok)
        self.assertEqual(hit.source, "vlm")


class TestSuggestionsEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("WINBROW_SKIP_MODEL_DL", "1")
        os.environ["WINBROW_TTS"] = "0"
        os.environ["WINBROW_OBSERVE_DIR"] = tempfile.mkdtemp(prefix="m5_srv_")
        import server
        cls.server = server

    def _inject_marker(self) -> None:
        """Startup's refresh() clears top-of-class injection; inject inside
        the TestClient context instead (after startup ran)."""
        cls = self.__class__
        obs = cls.server.get_observer()
        obs.current = [Suggestion(
            id="test:open-x", kind="repeat_open", title="Open x.png again?",
            utterance="open x.png", why="opened 2 times recently", score=3.0,
            target="x.png")]

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(self.server.app)

    def test_suggestions_endpoint_shape(self):
        with self._client() as client:
            self._inject_marker()
            r = client.get("/api/suggestions")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertIn("suggestions", data)
            ids = [s["id"] for s in data["suggestions"]]
            self.assertIn("test:open-x", ids)

    def test_dismiss_endpoint(self):
        server_cls = self.server
        with self._client() as client:
            self._inject_marker()
            r = client.post("/api/suggestions/dismiss", json={"id": "test:open-x"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json().get("status"), "dismissed")
            # removed from current view
            ids = [s["id"] for s in server_cls.get_observer().current]
            self.assertNotIn("test:open-x", ids)
            # recorded in the dismiss ledger
            self.assertTrue(server_cls.get_observer().dismissals.dismissed("test:open-x"))
            # idempotent
            r2 = client.post("/api/suggestions/dismiss", json={"id": "test:open-x"})
            self.assertEqual(r2.status_code, 200)

    def test_refresh_rate_limit(self):
        """?refresh=1 with last refresh <30s ago must not rebuild."""
        server_cls = self.server
        with self._client() as client:
            self._inject_marker()
            server_cls._last_refresh_ts = time.time()   # fresh → skip rebuild
            r = client.get("/api/suggestions?refresh=1")
            self.assertEqual(r.status_code, 200)
            self.assertIn("test:open-x", [s["id"] for s in r.json()["suggestions"]])


# ── M5 UX: brain-free step-by-step "editor recipe" ──────────────────────
# Sequential voice session on one agent/one session: open notepad →
# "write hello world" (types into focused editor) → "save it as home.txt"
# → "save it in the downloads folder". Each step routes from HEURISTICS
# alone (heuristic layer), nothing online.


class TestEditorSequence(unittest.TestCase):
    def _runner(self, context_name="notepad"):
        from test_scenarios import ScenarioRunner
        return ScenarioRunner(context_name=context_name)

    def test_notepad_step_by_step_recipe(self):
        """The 4-command notepad recipe routes, in order, brain-free."""
        r = self._runner()
        outs = [r.execute(u) for u in (
            "open notepad", "write hello world program",
            "save it as home.txt", "save it in the downloads folder")]
        # 1) launch/focus
        routes = [(o["route"].get("kind"), o["route"].get("tool_name"))
                  for o in outs]
        self.assertEqual(routes[0][0], "tool")
        self.assertIn(routes[0][1],
                      ("app_focus_or_launch", "open_common_app", "notepad_open"))
        # 2) typing gated into the focused editable app
        self.assertEqual(routes[1], ("tool", "computer_type"))
        self.assertEqual(outs[1]["route"]["args"].get("text"),
                         "hello world program")
        # 3) save-as with explicit filename
        self.assertEqual(routes[2], ("tool", "notepad_save_as"))
        args = outs[2]["route"]["args"]
        self.assertEqual(args.get("filename"), "home.txt")
        self.assertEqual(args.get("folder"), "")
        # 4) folder-only save (no explicit name; op falls back to title)
        self.assertEqual(routes[3], ("tool", "notepad_save_as"))
        args = outs[3]["route"]["args"]
        self.assertEqual(args.get("filename"), "")
        self.assertIn("download", (args.get("folder") or "").lower())

    def test_typing_never_fires_when_not_editable_focused(self):
        """Browser focused → 'write ...' must not type into the browser."""
        r = self._runner(context_name="home")
        out = r.execute("write hello world program")
        route = out["route"]
        self.assertNotEqual(route.get("tool_name"), "computer_type")
        self.assertNotEqual(route.get("kind"), "tool")

    def test_save_phrase_gated_without_extension_or_focus(self):
        """'save the document' w/o editor focus and w/o extension → no route."""
        r = self._runner(context_name="home")
        out = r.execute("save the document")
        self.assertNotEqual(out["route"].get("tool_name"), "notepad_save_as")

    def test_save_variants(self):
        r = self._runner()
        variants = [
            ("save it as home.txt", "home.txt", ""),
            ("save it as home.txt in downloads", "home.txt", "downloads"),
            ("save this as notes.md", "notes.md", ""),
            ("save it in my downloads folder", "", "downloads"),
            ("save it in documents folder", "", "documents"),
            ("save it as report.txt on desktop", "report.txt", "desktop"),
        ]
        for utterance, fname, folder in variants:
            with self.subTest(utterance=utterance):
                out = r.execute(utterance)
                route = out["route"]
                self.assertEqual(route.get("kind"), "tool", utterance)
                self.assertEqual(route.get("tool_name"), "notepad_save_as",
                                 utterance)
                self.assertEqual(route["args"].get("filename"), fname,
                                 utterance)
                self.assertEqual(route["args"].get("folder"), folder,
                                 utterance)

    def test_editable_focused_variants(self):
        from winbrow.router import _editable_focused
        from winbrow.windows import WindowsContext
        self.assertTrue(_editable_focused(WindowsContext(
            active_app="notepad", active_title="Untitled - Notepad")))
        self.assertTrue(_editable_focused(WindowsContext(
            active_app="Code.exe", active_title="main.py - Visual Studio Code")))
        self.assertTrue(_editable_focused(WindowsContext(
            active_app="Notepad", active_title="")))
        self.assertFalse(_editable_focused(WindowsContext(
            active_app="Brave", active_title="OpenRouter Dashboard - Brave")))
        self.assertFalse(_editable_focused(
            WindowsContext(active_app="explorer", active_title="Downloads")))


class TestTitleFilename(unittest.TestCase):
    def _fn(self, title):
        from winbrow.adapters.notepad import _title_filename
        return _title_filename(title)

    def test_saved_titles(self):
        self.assertEqual(self._fn("home.txt - Notepad"), "home.txt")
        self.assertEqual(self._fn("notes.md - Notepad"), "notes.md")
        self.assertEqual(self._fn("*home.txt - Notepad"), "home.txt")
        self.assertEqual(self._fn("home.txt"), "home.txt")

    def test_unsaved_titles(self):
        self.assertEqual(self._fn("Untitled - Notepad"), "")
        self.assertEqual(self._fn("Untitled"), "")
        self.assertEqual(self._fn("*Untitled - Notepad"), "")
        self.assertEqual(self._fn(""), "")

    def test_path_hosts_stripped(self):
        self.assertEqual(self._fn("C:\\Users\\x\\home.txt - Notepad"),
                         "home.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
