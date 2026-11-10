import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zento_command_ledger import CommandLedger, Reservation
from zento_durable_ipc import DurableIPCHandler
from zento_local_ipc import LocalIPCServer, ipc_request
from zento_recovery_listener import SessionRegistry


class DurableIPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.socket_path = root / "zento.sock"
        self.ledger_path = root / "commands.sqlite3"

        self.ledger = CommandLedger(self.ledger_path)
        self.registry = SessionRegistry()

        self.handler = DurableIPCHandler(
            registry=self.registry,
            expected_identity="gateway",
            ledger=self.ledger,
        )

        self.server = LocalIPCServer(
            self.socket_path,
            allowed_uids={os.getuid()},
            handler=self.handler,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.ledger.close()
        self.tmp.cleanup()

    def request(self, kind, **fields):
        return ipc_request(
            self.socket_path,
            {"type": kind, **fields},
        )

    def reserve_fixture(self):
        now = datetime.now(timezone.utc)
        reservation = Reservation(
            command_id="test-command-1",
            idempotency_key="test-idempotency-1",
            fingerprint=(
                "trusted-test-subject",
                "gateway", 0, "open", 1,
            ),
            gateway_serial="gateway",
            rs485_address=0,
            command="open",
            generation=1,
            expires_at=now + timedelta(minutes=5),
        )
        result = self.ledger.reserve(reservation, now)
        self.assertEqual(result[0], "created")

    def test_missing_command(self):
        response = self.request(
            "ledger_status",
            identity="gateway",
            command_id="missing",
        )
        self.assertEqual(
            response["code"], "command_not_found"
        )

    def test_existing_reservation(self):
        self.reserve_fixture()
        response = self.request(
            "ledger_status",
            identity="gateway",
            command_id="test-command-1",
        )
        self.assertTrue(response["accepted"])
        self.assertEqual(response["state"], "reserved")
        self.assertFalse(response["transmitted"])

    def test_persistence_across_connections(self):
        self.reserve_fixture()

        for _ in range(3):
            response = self.request(
                "ledger_status",
                identity="gateway",
                command_id="test-command-1",
            )
            self.assertEqual(response["state"], "reserved")

    def test_wrong_identity(self):
        self.reserve_fixture()
        response = self.request(
            "ledger_status",
            identity="other",
            command_id="test-command-1",
        )
        self.assertFalse(response["accepted"])

    def test_command_submission_rejected(self):
        for command in ("open", "close", "stop"):
            response = self.request(
                "submit_command",
                identity="gateway",
                command=command,
            )
            self.assertFalse(response["accepted"])
            self.assertFalse(response["transmitted"])

    def test_client_cannot_create_reservation(self):
        response = self.request(
            "reserve",
            identity="gateway",
            command_id="new-command",
        )
        self.assertFalse(response["accepted"])
        self.assertIsNone(self.ledger.get("new-command"))

    def test_status_remains_available(self):
        response = self.request(
            "session_status",
            identity="gateway",
        )
        self.assertTrue(response["accepted"])
        self.assertFalse(response["online"])

    def test_no_sensitive_fingerprint_exposure(self):
        self.reserve_fixture()
        response = self.request(
            "ledger_status",
            identity="gateway",
            command_id="test-command-1",
        )
        self.assertNotIn("fingerprint", response)
        self.assertNotIn("idempotency_key", response)
        self.assertFalse(
            response["physical_execution_enabled"]
        )


if __name__ == "__main__":
    unittest.main()
