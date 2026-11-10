import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zento_command_ledger import CommandLedger, Reservation


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "commands.sqlite3"
        self.now = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
        self.item = Reservation(
            command_id="cmd-1",
            idempotency_key="idem-1",
            fingerprint=("user-1", "pilot-01", 0, "open", 7),
            gateway_serial="pilot-01",
            rs485_address=0,
            command="open",
            generation=7,
            expires_at=self.now + timedelta(seconds=20),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_reservation_is_persistent(self):
        with CommandLedger(self.path) as ledger:
            self.assertEqual(
                ledger.reserve(self.item, self.now),
                ("created", "reserved")
            )
        with CommandLedger(self.path) as ledger:
            self.assertEqual(
                ledger.get("cmd-1")["state"], "reserved"
            )

    def test_duplicate_does_not_create_second_command(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            self.assertEqual(
                ledger.reserve(self.item, self.now),
                ("duplicate", "reserved")
            )

    def test_inconsistent_fingerprint_rejected(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            changed = replace(self.item, command="close")
            with self.assertRaisesRegex(
                ValueError, "Reservation fingerprint mismatch"
            ):
                ledger.reserve(changed, self.now)

    def test_conflicting_idempotency_rejected(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)

            changed = replace(
                self.item,
                command_id="cmd-2",
                command="close",
                fingerprint=("user-1", "pilot-01", 0, "close", 7),
            )

            self.assertEqual(
                ledger.reserve(changed, self.now)[0],
                "conflict"
            )

    def test_command_id_collision_rejected(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            changed = replace(
                self.item, idempotency_key="different-key"
            )
            self.assertEqual(
                ledger.reserve(changed, self.now)[0],
                "conflict"
            )

    def test_expired_reservation_rejected(self):
        with CommandLedger(self.path) as ledger:
            with self.assertRaises(ValueError):
                ledger.reserve(
                    self.item, self.now + timedelta(seconds=21)
                )

    def test_generation_fencing(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            self.assertFalse(ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=8
            ))
            self.assertEqual(
                ledger.get("cmd-1")["state"], "reserved"
            )

    def test_expired_command_cannot_start_dispatch(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            self.assertFalse(ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now + timedelta(seconds=21),
                generation=7
            ))

    def test_dispatch_start_marker(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            self.assertTrue(ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=7
            ))
            self.assertEqual(
                ledger.get("cmd-1")["state"], "dispatch_started"
            )

    def test_no_duplicate_dispatch(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            self.assertTrue(ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=7
            ))
            self.assertFalse(ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=7
            ))

    def test_recovery_marks_uncertain_outcome(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=7
            )
        with CommandLedger(self.path) as ledger:
            result = ledger.recover(
                self.now + timedelta(seconds=1)
            )
            self.assertEqual(result["outcome_unknown"], 1)
            self.assertEqual(
                ledger.get("cmd-1")["state"], "outcome_unknown"
            )

    def test_recovery_expires_old_reservations(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
        with CommandLedger(self.path) as ledger:
            result = ledger.recover(
                self.now + timedelta(seconds=21)
            )
            self.assertEqual(result["expired"], 1)
            self.assertEqual(
                ledger.get("cmd-1")["state"], "expired"
            )

    def test_invalid_transition_rejected(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            with self.assertRaises(ValueError):
                ledger.transition(
                    "cmd-1", "reserved", "completed", self.now
                )

    def test_terminal_command_cannot_be_replayed(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=7
            )
            ledger.recover(self.now)
            self.assertFalse(ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=7
            ))
            self.assertEqual(
                ledger.reserve(self.item, self.now),
                ("duplicate", "outcome_unknown")
            )

    def test_concurrent_connections_share_uniqueness(self):
        with CommandLedger(self.path) as first:
            with CommandLedger(self.path) as second:
                self.assertEqual(
                    first.reserve(self.item, self.now)[0],
                    "created"
                )
                self.assertEqual(
                    second.reserve(self.item, self.now)[0],
                    "duplicate"
                )


    def test_empty_authorization_subject_rejected(self):
        with CommandLedger(self.path) as ledger:
            changed = replace(
                self.item,
                fingerprint=("", "pilot-01", 0, "open", 7),
            )
            with self.assertRaisesRegex(
                ValueError, "Invalid authorization subject"
            ):
                ledger.reserve(changed, self.now)

    def test_expiry_change_conflicts_with_existing_key(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            changed = replace(
                self.item,
                expires_at=self.now + timedelta(seconds=25),
            )
            self.assertEqual(
                ledger.reserve(changed, self.now)[0],
                "conflict"
            )

    def test_recovery_is_idempotent(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.now, generation=7
            )
            first = ledger.recover(self.now)
            second = ledger.recover(self.now)
            self.assertEqual(first["outcome_unknown"], 1)
            self.assertEqual(second["outcome_unknown"], 0)

    def test_no_dispatch_after_expiry_boundary(self):
        with CommandLedger(self.path) as ledger:
            ledger.reserve(self.item, self.now)
            self.assertFalse(ledger.transition(
                "cmd-1", "reserved", "dispatch_started",
                self.item.expires_at, generation=7
            ))


if __name__ == "__main__":
    unittest.main()
