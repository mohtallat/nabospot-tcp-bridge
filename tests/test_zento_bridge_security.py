import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from zento_bridge_security import (
    bridge_key_valid,
    integration_status,
)


class BridgeSecurityTests(unittest.TestCase):
    def test_valid_key(self):
        self.assertTrue(bridge_key_valid("secret-123", "secret-123"))

    def test_wrong_key(self):
        self.assertFalse(bridge_key_valid("wrong", "secret-123"))

    def test_missing_provided_key(self):
        self.assertFalse(bridge_key_valid(None, "secret-123"))

    def test_missing_configured_key(self):
        self.assertFalse(bridge_key_valid("secret-123", ""))

    def test_unsafe_default_rejected(self):
        self.assertFalse(bridge_key_valid("change-me", "change-me"))

    def test_mock_metadata(self):
        result = integration_status(mock_mode=True)
        self.assertEqual(result["bridge_mode"], "mock")
        self.assertFalse(result["physical_execution_enabled"])
        self.assertFalse(result["live_status_available"])

    def test_nonmock_metadata(self):
        result = integration_status(mock_mode=False)
        self.assertEqual(result["bridge_mode"], "unimplemented")
        self.assertEqual(result["command_execution"], "disabled")

    def test_endpoint_requires_auth(self):
        with patch.dict("os.environ", {"BRIDGE_API_KEY": "test-secret"}):
            import importlib
            import app
            importlib.reload(app)
            client = TestClient(app.http_app)
            response = client.get("/barriers/integration/status")
            self.assertEqual(response.status_code, 401)

    def test_endpoint_returns_safe_metadata(self):
        with patch.dict("os.environ", {"BRIDGE_API_KEY": "test-secret"}):
            import importlib
            import app
            importlib.reload(app)
            client = TestClient(app.http_app)
            response = client.get(
                "/barriers/integration/status",
                headers={"X-Bridge-Key": "test-secret"},
            )
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertFalse(result["physical_execution_enabled"])
            self.assertFalse(result["live_status_available"])


if __name__ == "__main__":
    unittest.main()
