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
from zento_command_dispatcher import SimulationDispatcher
from zento_command_ledger import CommandLedger, Reservation


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = CommandLedger(
            Path(self.tmp.name) / "commands.sqlite3"
        )
        self.now = datetime(
            2026, 11, 10, 14, 0, tzinfo=timezone.utc
        )
        self.request = CommandRequest(
            command_id="cmd-1",
            idempotency_key="idem-1",
            gateway_serial="pilot-01",
            rs485_address=0,
            command="open",
            generation=7,
            expires_at=self.now + timedelta(seconds=15),
        )
        self.auth = AuthorizationEvidence(
            trusted=True,
            subject_id="user-1",
            gateway_serial="pilot-01",
            rs485_address=0,
            permitted_command="open",
            valid_until=self.now + timedelta(seconds=25),
        )
        self.snapshot = GatewaySnapshot(
            gateway_serial="pilot-01",
            rs485_address=0,
            connected=True,
            online=True,
            barrier_state="closed",
            last_seen_at=self.now,
            generation=7,
        )
        self.reserve()
        self.dispatcher = SimulationDispatcher(
            self.ledger, lambda _: self.snapshot
        )

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def reserve(self):
        self.ledger.reserve(
            Reservation(
                command_id=self.request.command_id,
                idempotency_key=self.request.idempotency_key,
                fingerprint=(
                    "user-1", "pilot-01", 0, "open", 7
                ),
                gateway_serial="pilot-01",
                rs485_address=0,
                command="open",
                generation=7,
                expires_at=self.request.expires_at,
            ),
            self.now,
        )

    def run_dispatch(self, request=None, auth=None, now=None):
        return self.dispatcher.simulate(
            request or self.request,
            auth or self.auth,
            now=now or self.now,
        )

    def test_simulation_never_transmits(self):
        result = self.run_dispatch()
        self.assertTrue(result.simulated)
        self.assertFalse(result.transmitted)
        self.assertEqual(result.outcome, "outcome_unknown")
        self.assertEqual(
            self.ledger.get("cmd-1")["state"],
            "outcome_unknown",
        )

    def test_duplicate_dispatch_blocked(self):
        self.run_dispatch()
        result = self.run_dispatch()
        self.assertEqual(result.outcome, "rejected")
        self.assertIn("not_reserved_state", result.reason)

    def test_offline_gateway_rejected(self):
        self.snapshot = replace(
            self.snapshot, online=False
        )
        result = self.run_dispatch()
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(
            self.ledger.get("cmd-1")["state"], "reserved"
        )

    def test_stale_status_rejected(self):
        self.snapshot = replace(
            self.snapshot,
            last_seen_at=self.now - timedelta(seconds=30),
        )
        result = self.run_dispatch()
        self.assertIn("stale_status", result.reason)

    def test_session_generation_mismatch(self):
        self.snapshot = replace(
            self.snapshot, generation=8
        )
        result = self.run_dispatch()
        self.assertIn("session_mismatch", result.reason)

    def test_expired_request_rejected(self):
        result = self.run_dispatch(
            now=self.now + timedelta(seconds=16)
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(
            self.ledger.get("cmd-1")["state"], "reserved"
        )

    def test_untrusted_authorization_rejected(self):
        result = self.run_dispatch(
            auth=replace(self.auth, trusted=False)
        )
        self.assertIn("unauthorized", result.reason)

    def test_request_reservation_mismatch(self):
        result = self.run_dispatch(
            request=replace(self.request, command="close")
        )
        self.assertEqual(
            result.reason, "reservation_mismatch"
        )

    def test_missing_reservation_rejected(self):
        result = self.run_dispatch(
            request=replace(
                self.request, command_id="missing"
            )
        )
        self.assertEqual(result.reason, "not_reserved")

    def test_session_replaced_during_recheck(self):
        calls = [0]

        def provider(_):
            calls[0] += 1
            if calls[0] == 1:
                return self.snapshot
            return replace(self.snapshot, generation=8)

        self.dispatcher = SimulationDispatcher(
            self.ledger, provider
        )
        result = self.run_dispatch()
        self.assertIn("session_mismatch", result.reason)
        self.assertEqual(
            self.ledger.get("cmd-1")["state"], "reserved"
        )

    def test_crash_recovery_prevents_replay(self):
        self.ledger.transition(
            "cmd-1", "reserved", "dispatch_started",
            self.now, generation=7,
        )
        self.ledger.recover(self.now)
        result = self.run_dispatch()
        self.assertEqual(result.outcome, "rejected")
        self.assertEqual(
            self.ledger.get("cmd-1")["state"],
            "outcome_unknown",
        )

    def test_moving_barrier_rejected(self):
        self.snapshot = replace(
            self.snapshot, barrier_state="opening"
        )
        result = self.run_dispatch()
        self.assertIn("unsafe_state", result.reason)


    def test_wrong_idempotency_key_rejected(self):
        changed = replace(
            self.request, idempotency_key="another-key"
        )
        result = self.run_dispatch(request=changed)
        self.assertEqual(
            result.reason, "reservation_identity_mismatch"
        )

    def test_authorization_subject_mismatch_rejected(self):
        changed = replace(
            self.auth, subject_id="different-user"
        )
        result = self.run_dispatch(auth=changed)
        self.assertEqual(
            result.reason, "reservation_identity_mismatch"
        )


if __name__ == "__main__":
    unittest.main()
