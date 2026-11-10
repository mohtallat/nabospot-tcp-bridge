import os
import tempfile
import unittest
from pathlib import Path

from zento_durable_ipc import DurableIPCHandler
from zento_local_ipc import LocalIPCServer, ipc_request
from zento_recovery_listener import SessionRegistry


class ReadOnlyBoundaryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        self.database = root / "missing.sqlite3"
        self.socket_path = root / "status.sock"

        class LedgerReference:
            pass

        ledger = LedgerReference()
        ledger.path = str(self.database)

        handler = DurableIPCHandler(
            registry=SessionRegistry(),
            expected_identity="gateway",
            ledger=ledger,
        )

        self.server = LocalIPCServer(
            self.socket_path,
            allowed_uids={os.getuid()},
            handler=handler,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def request(self, message):
        return ipc_request(self.socket_path, message)

    def test_missing_database_not_created(self):
        response = self.request({
            "type": "ledger_status",
            "identity": "gateway",
            "command_id": "missing",
        })

        self.assertFalse(response["accepted"])
        self.assertEqual(
            response["code"], "ledger_unavailable"
        )
        self.assertFalse(self.database.exists())

    def test_status_does_not_require_database(self):
        response = self.request({
            "type": "session_status",
            "identity": "gateway",
        })

        self.assertTrue(response["accepted"])
        self.assertFalse(response["online"])
        self.assertFalse(response["transmitted"])

    def test_submission_remains_disabled(self):
        response = self.request({
            "type": "submit_command",
            "identity": "gateway",
            "command": "open",
        })

        self.assertFalse(response["accepted"])
        self.assertFalse(response["transmitted"])
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
