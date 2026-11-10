import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zento_command_admission import (
    AuthorizationEvidence,
    CommandRequest,
    GatewaySnapshot,
)
from zento_command_coordinator import SimulationCoordinator
from zento_command_ledger import CommandLedger
from zento_session_mailbox import SessionMailbox


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = CommandLedger(
            Path(self.tmp.name) / "ledger.sqlite3"
        )
        self.now = datetime(
            2026, 11, 10, 15, 0, tzinfo=timezone.utc
        )
        self.tick = 100.0
        self.mailbox = SessionMailbox(
            clock=lambda: self.tick
        )
        self.mailbox.replace("gateway-1", 7)
        self.mailbox.record_status("gateway-1", 7)

        self.snapshot = GatewaySnapshot(
            gateway_serial="gateway-1",
            rs485_address=0,
            connected=True,
            online=True,
            barrier_state="closed",
            last_seen_at=self.now,
            generation=7,
        )
        self.request = CommandRequest(
            command_id="cmd-1",
            idempotency_key="idem-1",
            gateway_serial="gateway-1",
            rs485_address=0,
            command="open",
            generation=7,
            expires_at=self.now + timedelta(seconds=15),
        )
        self.auth = AuthorizationEvidence(
            trusted=True,
            subject_id="user-1",
            gateway_serial="gateway-1",
            rs485_address=0,
            permitted_command="open",
            valid_until=self.now + timedelta(seconds=20),
        )
        self.coordinator = SimulationCoordinator(
            self.ledger,
            self.mailbox,
            lambda _: self.snapshot,
            clock=lambda: self.tick,
        )

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def submit(self, request=None, auth=None):
        return self.coordinator.submit(
            request or self.request,
            auth or self.auth,
            now=self.now,
        )

    def claim(self, now=None, generation=7):
        return self.coordinator.claim(
            "gateway-1", generation,
            now=now or self.now,
        )

    def test_complete_simulation(self):
        self.assertEqual(self.submit(), "queued")
        result = self.claim()
        self.assertEqual(result.outcome, "outcome_unknown")
        self.assertFalse(result.transmitted)
        self.assertEqual(
            self.ledger.get("cmd-1")["state"],
            "outcome_unknown",
        )

    def test_duplicate_reservation(self):
        self.assertEqual(self.submit(), "queued")
        self.assertEqual(
            self.submit(), "reservation:duplicate:reserved"
        )

    def test_idempotency_conflict(self):
        self.submit()
        changed = replace(
            self.request, command_id="cmd-2"
        )
        self.assertTrue(
            self.submit(request=changed).startswith(
                "reservation:conflict:"
            )
        )

    def test_untrusted_rejected_before_reservation(self):
        result = self.submit(
            auth=replace(self.auth, trusted=False)
        )
        self.assertEqual(
            result, "admission:unauthorized"
        )
        self.assertIsNone(self.ledger.get("cmd-1"))

    def test_session_replacement_blocks_claim(self):
        self.submit()
        self.mailbox.replace("gateway-1", 8)
        result = self.claim()
        self.assertEqual(result.outcome, "rejected")
        self.assertFalse(result.transmitted)

    def test_stale_status_blocks_submit(self):
        self.snapshot = replace(
            self.snapshot,
            last_seen_at=self.now - timedelta(seconds=30),
        )
        self.assertEqual(
            self.submit(), "admission:stale_status"
        )

    def test_expiry_between_submit_and_claim(self):
        self.submit()
        self.tick = 116.0
        result = self.claim(
            now=self.now + timedelta(seconds=16)
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertFalse(result.transmitted)

    def test_generation_change_after_claim_preparation(self):
        self.submit()
        self.snapshot = replace(
            self.snapshot, generation=8
        )
        result = self.claim()
        self.assertEqual(result.outcome, "rejected")
        self.assertIn("session_mismatch", result.reason)

    def test_recovery_does_not_replay(self):
        self.submit()
        self.ledger.transition(
            "cmd-1", "reserved", "dispatch_started",
            self.now, generation=7,
        )
        recovered = self.coordinator.recover(
            now=self.now
        )
        self.assertEqual(
            recovered["outcome_unknown"], 1
        )
        result = self.claim()
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(
            self.ledger.get("cmd-1")["state"],
            "outcome_unknown",
        )

    def test_no_pending_intent(self):
        result = self.claim()
        self.assertEqual(result.outcome, "rejected")
        self.assertFalse(result.transmitted)

    def test_offline_gateway(self):
        self.snapshot = replace(
            self.snapshot, connected=False, online=False
        )
        self.assertEqual(
            self.submit(), "admission:offline"
        )

    def test_moving_barrier(self):
        self.snapshot = replace(
            self.snapshot, barrier_state="opening"
        )
        self.assertEqual(
            self.submit(), "admission:unsafe_state"
        )


if __name__ == "__main__":
    unittest.main()
