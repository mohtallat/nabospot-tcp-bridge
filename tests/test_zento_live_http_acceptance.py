import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class LiveHTTPAcceptanceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import app

        cls.app_module = app
        cls.client = TestClient(app.http_app)

    def headers(self):
        return {
            "X-Bridge-Key": "test-bridge-key"
        }

    def live(self, online=True):
        return {
            "connected": online,
            "online": online,
            "barrier_state": "open" if online else "unknown",
            "last_seen_at": (
                "2026-11-12T14:22:44+00:00"
                if online else None
            ),
            "status_source": "listener_session_registry",
            "live_status_available": online,
            "physical_execution_enabled": False,
        }

    def test_missing_key_rejected(self):
        response = self.client.get(
            "/barriers/6003791910/0/status"
        )
        self.assertIn(response.status_code, (401, 403))

    def test_invalid_key_rejected(self):
        with patch.object(
            self.app_module,
            "BRIDGE_API_KEY",
            "test-bridge-key",
        ):
            response = self.client.get(
                "/barriers/6003791910/0/status",
                headers={"X-Bridge-Key": "invalid"},
            )
        self.assertIn(response.status_code, (401, 403))

    def test_live_status(self):
        with (
            patch.object(
                self.app_module,
                "BRIDGE_API_KEY",
                "test-bridge-key",
            ),
            patch.object(
                self.app_module,
                "read_live_status",
                return_value=self.live(True),
            ),
        ):
            response = self.client.get(
                "/barriers/6003791910/0/status",
                headers=self.headers(),
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["connected"])
        self.assertTrue(result["online"])
        self.assertEqual(result["barrier_state"], "open")
        self.assertTrue(result["live_status_available"])
        self.assertFalse(result["physical_execution_enabled"])

    def test_offline_status_never_uses_mock(self):
        with (
            patch.object(
                self.app_module,
                "BRIDGE_API_KEY",
                "test-bridge-key",
            ),
            patch.object(
                self.app_module,
                "read_live_status",
                return_value=self.live(False),
            ),
        ):
            response = self.client.get(
                "/barriers/6003791910/0/status",
                headers=self.headers(),
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(result["connected"])
        self.assertFalse(result["online"])
        self.assertEqual(result["barrier_state"], "unknown")
        self.assertFalse(result["live_status_available"])

    def test_integration_metadata_preserves_mock_mode(self):
        with (
            patch.object(
                self.app_module,
                "BRIDGE_API_KEY",
                "test-bridge-key",
            ),
            patch.object(
                self.app_module,
                "read_live_status",
                return_value=self.live(True),
            ),
        ):
            response = self.client.get(
                "/barriers/integration/status",
                headers=self.headers(),
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["live_status_available"])
        self.assertFalse(result["physical_execution_enabled"])
        self.assertEqual(
            result["command_execution"],
            "simulation_only",
        )


if __name__ == "__main__":
    unittest.main()
