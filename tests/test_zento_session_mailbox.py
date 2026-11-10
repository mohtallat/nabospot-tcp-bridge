import unittest
from concurrent.futures import ThreadPoolExecutor

from zento_session_mailbox import (
    CommandIntent,
    SessionMailbox,
)


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.box = SessionMailbox(
            stale_after=20,
            clock=lambda: self.now,
        )
        self.box.replace("gateway-1", 7)
        self.box.record_status("gateway-1", 7)

    def intent(self, command_id="cmd-1", generation=7):
        return CommandIntent(
            command_id=command_id,
            identity="gateway-1",
            generation=generation,
            expires_at=115.0,
        )

    def test_claim_is_simulation_only(self):
        self.assertTrue(self.box.submit(self.intent()).accepted)
        result = self.box.claim("gateway-1", 7)
        self.assertTrue(result.accepted)
        self.assertEqual(result.code, "simulated_claim")
        self.assertFalse(result.transmitted)

    def test_no_status_rejects(self):
        self.box.replace("gateway-1", 8)
        result = self.box.submit(self.intent(generation=8))
        self.assertEqual(result.code, "session_not_fresh")

    def test_stale_status_rejects(self):
        self.now = 121.0
        result = self.box.submit(self.intent())
        self.assertEqual(result.code, "session_not_fresh")

    def test_replacement_invalidates_pending(self):
        self.box.submit(self.intent())
        self.box.replace("gateway-1", 8)
        self.assertEqual(self.box.pending_count("gateway-1"), 0)
        self.assertFalse(self.box.claim("gateway-1", 7).accepted)

    def test_old_release_cannot_evict_new(self):
        self.box.replace("gateway-1", 8)
        self.assertFalse(self.box.release("gateway-1", 7).accepted)
        self.assertTrue(
            self.box.record_status("gateway-1", 8).accepted
        )

    def test_release_invalidates_pending(self):
        self.box.submit(self.intent())
        self.box.release("gateway-1", 7)
        self.assertEqual(self.box.pending_count("gateway-1"), 0)

    def test_expired_intent_rejected(self):
        self.now = 115.0
        self.assertEqual(
            self.box.submit(self.intent()).code,
            "intent_expired",
        )

    def test_expired_while_queued_not_claimed(self):
        self.box.submit(self.intent())
        self.now = 115.0
        self.assertEqual(
            self.box.claim("gateway-1", 7).code,
            "no_valid_intent",
        )

    def test_duplicate_pending_rejected(self):
        self.box.submit(self.intent())
        self.assertEqual(
            self.box.submit(self.intent()).code,
            "already_pending",
        )

    def test_duplicate_claim_rejected(self):
        self.box.submit(self.intent())
        self.box.claim("gateway-1", 7)
        self.assertEqual(
            self.box.submit(self.intent()).code,
            "already_claimed",
        )

    def test_non_increasing_generation_rejected(self):
        self.assertFalse(
            self.box.replace("gateway-1", 7).accepted
        )

    def test_old_status_rejected(self):
        self.box.replace("gateway-1", 8)
        self.assertFalse(
            self.box.record_status("gateway-1", 7).accepted
        )

    def test_wrong_owner_cannot_claim(self):
        self.box.submit(self.intent())
        self.assertEqual(
            self.box.claim("gateway-1", 8).code,
            "not_session_owner",
        )

    def test_concurrent_claim_only_once(self):
        self.box.submit(self.intent())
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.box.claim("gateway-1", 7),
                    range(20),
                )
            )
        self.assertEqual(
            sum(r.accepted for r in results), 1
        )

    def test_claim_after_status_stale_invalidates_queue(self):
        self.box.submit(self.intent())
        self.now = 121.0
        self.assertEqual(
            self.box.claim("gateway-1", 7).code,
            "session_not_fresh",
        )
        self.assertEqual(self.box.pending_count("gateway-1"), 0)


if __name__ == "__main__":
    unittest.main()
