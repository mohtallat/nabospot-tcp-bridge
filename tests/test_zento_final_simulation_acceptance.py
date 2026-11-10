"""ZB-2D2 offline acceptance.

No controller connections, movement frames, service changes,
or production authorization claims.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zento_command_ledger import CommandLedger, Reservation
from zento_session_mailbox import SessionMailbox, CommandIntent
from zento_durable_ipc import DurableIPCHandler
from zento_local_ipc import LocalIPCServer, ipc_request
from zento_recovery_listener import SessionRegistry


IDENTITY = "gateway"


def now():
    return datetime.now(timezone.utc)


def reservation(command_id, key, *, generation=1, expiry=None):
    return Reservation(
        command_id=command_id,
        idempotency_key=key,
        fingerprint=(
            "trusted-offline-test-subject",
            IDENTITY, 0, "open", generation,
        ),
        gateway_serial=IDENTITY,
        rs485_address=0,
        command="open",
        generation=generation,
        expires_at=expiry or now() + timedelta(minutes=5),
    )


class FinalSimulationAcceptance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "commands.sqlite3"
        self.ledger = CommandLedger(self.db_path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_duplicate_reservation_is_idempotent(self):
        item = reservation("cmd-1", "key-1")
        self.assertEqual(
            self.ledger.reserve(item, now())[0],
            "created",
        )
        self.assertEqual(
            self.ledger.reserve(item, now())[0],
            "duplicate",
        )

    def test_conflicting_idempotency_key_rejected(self):
        first = reservation("cmd-1", "shared-key")
        second = reservation("cmd-2", "shared-key")

        self.assertEqual(
            self.ledger.reserve(first, now())[0],
            "created",
        )
        self.assertEqual(
            self.ledger.reserve(second, now())[0],
            "conflict",
        )
        self.assertIsNone(self.ledger.get("cmd-2"))

    def test_concurrent_duplicate_reservations(self):
        item = reservation("concurrent-1", "concurrent-key")
        results = []
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            try:
                with CommandLedger(self.db_path) as ledger:
                    barrier.wait(timeout=5)
                    result = ledger.reserve(item, now())[0]
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(repr(exc))

        threads = [
            threading.Thread(target=worker)
            for _ in range(8)
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(
            any(thread.is_alive() for thread in threads)
        )
        self.assertEqual(errors, [])
        self.assertEqual(results.count("created"), 1)
        self.assertEqual(results.count("duplicate"), 7)

    def test_session_replacement_invalidates_intent(self):
        mailbox = SessionMailbox()
        mailbox.replace(IDENTITY, 1)
        mailbox.record_status(IDENTITY, 1)

        intent = CommandIntent(
            command_id="old-command",
            identity=IDENTITY,
            generation=1,
            expires_at=10**12,
        )

        self.assertTrue(mailbox.submit(intent).accepted)
        mailbox.replace(IDENTITY, 2)
        mailbox.record_status(IDENTITY, 2)

        self.assertFalse(
            mailbox.claim(IDENTITY, 1).accepted
        )
        self.assertFalse(
            mailbox.claim(IDENTITY, 2).accepted
        )
        self.assertEqual(mailbox.pending_count(IDENTITY), 0)

    def test_expired_intent_cannot_be_claimed(self):
        mailbox = SessionMailbox()
        mailbox.replace(IDENTITY, 1)
        mailbox.record_status(IDENTITY, 1)

        intent = CommandIntent(
            command_id="expired-command",
            identity=IDENTITY,
            generation=1,
            expires_at=0.0,
        )

        self.assertFalse(mailbox.submit(intent).accepted)
        self.assertFalse(
            mailbox.claim(IDENTITY, 1).accepted
        )

    def test_expired_reservation_cannot_dispatch(self):
        item = reservation(
            "expires-1",
            "expires-key",
            expiry=now() + timedelta(seconds=1),
        )

        self.assertEqual(
            self.ledger.reserve(item, now())[0],
            "created",
        )

        future = now() + timedelta(minutes=1)

        self.assertFalse(
            self.ledger.transition(
                item.command_id,
                "reserved",
                "dispatch_started",
                future,
                generation=1,
            )
        )

    def test_recovery_never_replays_dispatch(self):
        item = reservation("recovery-1", "recovery-key")
        moment = now()

        self.ledger.reserve(item, moment)

        self.assertTrue(
            self.ledger.transition(
                item.command_id,
                "reserved",
                "dispatch_started",
                moment,
                generation=1,
            )
        )

        result = self.ledger.recover(now())

        self.assertEqual(result["outcome_unknown"], 1)
        self.assertEqual(
            self.ledger.get(item.command_id)["state"],
            "outcome_unknown",
        )

        self.assertFalse(
            self.ledger.transition(
                item.command_id,
                "reserved",
                "dispatch_started",
                now(),
                generation=1,
            )
        )

    def test_recovery_expires_old_reservations(self):
        item = reservation(
            "old-reservation",
            "old-reservation-key",
            expiry=now() + timedelta(seconds=1),
        )

        self.ledger.reserve(item, now())

        result = self.ledger.recover(
            now() + timedelta(minutes=1)
        )

        self.assertEqual(result["expired"], 1)
        self.assertEqual(
            self.ledger.get(item.command_id)["state"],
            "expired",
        )

    def test_untrusted_ipc_cannot_submit_or_reserve(self):
        socket_path = self.root / "status.sock"

        handler = DurableIPCHandler(
            registry=SessionRegistry(),
            expected_identity=IDENTITY,
            ledger=self.ledger,
        )

        server = LocalIPCServer(
            socket_path,
            allowed_uids={os.getuid()},
            handler=handler,
        )

        server.start()
        try:
            for kind in (
                "submit_command",
                "reserve",
                "dispatch",
                "open",
                "close",
                "stop",
            ):
                response = ipc_request(
                    socket_path,
                    {
                        "type": kind,
                        "identity": IDENTITY,
                        "command_id": "forbidden",
                    },
                )

                self.assertFalse(response["accepted"])
                self.assertFalse(response["transmitted"])

            self.assertIsNone(
                self.ledger.get("forbidden")
            )
        finally:
            server.stop()

    def test_disconnected_session_cannot_claim(self):
        mailbox = SessionMailbox()
        mailbox.replace(IDENTITY, 1)
        mailbox.record_status(IDENTITY, 1)

        intent = CommandIntent(
            command_id="disconnected-command",
            identity=IDENTITY,
            generation=1,
            expires_at=10**12,
        )

        self.assertTrue(mailbox.submit(intent).accepted)
        mailbox.release(IDENTITY, 1)

        self.assertFalse(
            mailbox.claim(IDENTITY, 1).accepted
        )
        self.assertEqual(mailbox.pending_count(IDENTITY), 0)


if __name__ == "__main__":
    unittest.main()
