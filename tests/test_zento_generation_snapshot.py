import unittest
from unittest.mock import Mock

from zento_recovery_listener import SessionRegistry


class GenerationSnapshotTests(unittest.TestCase):

    def setUp(self):
        self.registry = SessionRegistry()

    def test_disconnected_snapshot(self):
        result = self.registry.snapshot("gateway")
        self.assertFalse(result["connected"])
        self.assertFalse(result["online"])
        self.assertIsNone(result["generation"])

    def test_new_session_is_not_fresh(self):
        generation = self.registry.replace("gateway", Mock())
        result = self.registry.snapshot("gateway")

        self.assertTrue(result["connected"])
        self.assertFalse(result["online"])
        self.assertEqual(result["generation"], generation)
        self.assertEqual(result["barrier_state"], "unknown")

    def test_status_is_generation_fenced(self):
        old = self.registry.replace("gateway", Mock())
        self.assertTrue(
            self.registry.record_status("gateway", old, "closed")
        )

        new = self.registry.replace("gateway", Mock())

        self.assertFalse(
            self.registry.record_status("gateway", old, "open")
        )

        result = self.registry.snapshot("gateway")
        self.assertEqual(result["generation"], new)
        self.assertFalse(result["online"])
        self.assertEqual(result["barrier_state"], "unknown")

    def test_active_status_is_fresh(self):
        generation = self.registry.replace("gateway", Mock())

        self.assertTrue(
            self.registry.record_status("gateway", generation, "closed")
        )

        result = self.registry.snapshot("gateway")

        self.assertTrue(result["online"])
        self.assertEqual(result["generation"], generation)
        self.assertEqual(result["barrier_state"], "closed")

    def test_release_invalidates_generation(self):
        generation = self.registry.replace("gateway", Mock())
        self.registry.record_status("gateway", generation, "closed")
        self.registry.release("gateway", generation)

        result = self.registry.snapshot("gateway")

        self.assertFalse(result["connected"])
        self.assertFalse(result["online"])
        self.assertIsNone(result["generation"])


if __name__ == "__main__":
    unittest.main()
