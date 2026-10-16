"""
M0 smoke test — server boots, endpoints respond, brain offline fallback works.
Run: python -m pytest test_server_m0.py -q
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))


class TestServerM0(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("WINBROW_SKIP_MODEL_DL", "1")
        import server
        cls.server = server

    def test_app_boots(self):
        from fastapi.testclient import TestClient
        with TestClient(self.server.app) as client:
            r = client.get("/api/system")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["version"], "3.0.0")
            self.assertIn("brain", data)
            self.assertIn("session_summary", data)

    def test_session_endpoint(self):
        from fastapi.testclient import TestClient
        with TestClient(self.server.app) as client:
            r = client.get("/api/session")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertIn("turns", data)
            self.assertIn("entities", data)
            # reset works
            r2 = client.post("/api/session/reset", json={"session_id": None})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.json()["status"], "reset")

    def test_memories_endpoint(self):
        from fastapi.testclient import TestClient
        with TestClient(self.server.app) as client:
            r = client.get("/api/memories?q=notepad&limit=5")
            self.assertEqual(r.status_code, 200)
            self.assertIn("results", r.json())
            r2 = client.get("/api/memories")
            self.assertEqual(r2.status_code, 200)

    def test_fact_endpoint(self):
        from fastapi.testclient import TestClient
        with TestClient(self.server.app) as client:
            r = client.post("/api/memories/fact", json={"fact": "Test fact from smoke test"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "saved")

    def test_execute_chat_offline(self):
        """Without OPENROUTER_API_KEY the server must still answer commands."""
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
            with TestClient(self.server.app) as client:
                r = client.post("/api/execute", json={"command": "what time is it"})
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertIn("execution", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
