import unittest
from unittest.mock import patch

from zento_live_status import read_live_status


class LiveStatusTests(unittest.TestCase):

    @patch("zento_live_status.ipc_request")
    def test_fresh_live_status(self, request):
        request.return_value = {
            "accepted": True,
            "source": "listener_session_registry",
            "identity": "nabospot-trb145-pilot-01",
            "connected": True,
            "online": True,
            "barrier_state": "open",
            "last_seen_at": "2026-11-12T14:22:44+00:00",
            "transmitted": False,
            "physical_execution_enabled": False,
        }

        result = read_live_status("6003791910", 0)

        self.assertTrue(result["connected"])
        self.assertTrue(result["online"])
        self.assertTrue(result["live_status_available"])
        self.assertEqual(result["barrier_state"], "open")

    @patch("zento_live_status.ipc_request")
    def test_disconnected_is_unknown(self, request):
        request.return_value = {
            "accepted": True,
            "source": "listener_session_registry",
            "identity": "nabospot-trb145-pilot-01",
            "connected": False,
            "online": False,
            "barrier_state": "open",
            "transmitted": False,
            "physical_execution_enabled": False,
        }

        result = read_live_status("6003791910", 0)

        self.assertFalse(result["online"])
        self.assertEqual(result["barrier_state"], "unknown")

    @patch("zento_live_status.ipc_request")
    def test_unmapped_gateway_never_queries_ipc(self, request):
        result = read_live_status("other-gateway", 0)

        self.assertEqual(result["barrier_state"], "unknown")
        self.assertFalse(result["connected"])
        request.assert_not_called()

    @patch("zento_live_status.ipc_request")
    def test_wrong_address_never_queries_ipc(self, request):
        result = read_live_status("6003791910", 1)

        self.assertEqual(result["barrier_state"], "unknown")
        request.assert_not_called()

    @patch("zento_live_status.ipc_request")
    def test_ipc_unavailable_fails_closed(self, request):
        request.side_effect = OSError("socket unavailable")

        result = read_live_status("6003791910", 0)

        self.assertEqual(result["barrier_state"], "unknown")
        self.assertFalse(result["live_status_available"])

    @patch("zento_live_status.ipc_request")
    def test_untrusted_response_rejected(self, request):
        request.return_value = {
            "accepted": True,
            "source": "listener_session_registry",
            "identity": "nabospot-trb145-pilot-01",
            "connected": True,
            "online": True,
            "barrier_state": "open",
            "transmitted": True,
            "physical_execution_enabled": False,
        }

        result = read_live_status("6003791910", 0)

        self.assertEqual(result["barrier_state"], "unknown")


if __name__ == "__main__":
    unittest.main()
