import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from zento_local_ipc import LocalIPCServer, ipc_request
from zento_recovery_listener import SessionRegistry
from zento_status_ipc import ZentoStatusIPCHandler


class StatusIPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "status.sock"
        self.registry = SessionRegistry()
        self.handler = ZentoStatusIPCHandler(
            self.registry,
            expected_identity="gateway",
        )
        self.server = LocalIPCServer(
            self.path,
            allowed_uids={os.getuid()},
            handler=self.handler,
        )
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def request(self, **changes):
        message = {
            "type": "session_status",
            "identity": "gateway",
        }
        message.update(changes)
        return ipc_request(self.path, message)

    def test_disconnected(self):
        response = self.request()
        self.assertTrue(response["accepted"])
        self.assertFalse(response["connected"])
        self.assertFalse(response["online"])
        self.assertIsNone(response["generation"])

    def test_connected_without_status(self):
        generation = self.registry.replace("gateway", Mock())
        response = self.request()
        self.assertEqual(response["generation"], generation)
        self.assertTrue(response["connected"])
        self.assertFalse(response["online"])

    def test_fresh_controller_status(self):
        generation = self.registry.replace("gateway", Mock())
        self.registry.record_status(
            "gateway", generation, "closed"
        )
        response = self.request()
        self.assertTrue(response["online"])
        self.assertEqual(response["barrier_state"], "closed")
        self.assertFalse(response["transmitted"])

    def test_replaced_generation(self):
        old = self.registry.replace("gateway", Mock())
        self.registry.record_status(
            "gateway", old, "closed"
        )
        new = self.registry.replace("gateway", Mock())
        response = self.request()
        self.assertEqual(response["generation"], new)
        self.assertFalse(response["online"])

    def test_unknown_identity(self):
        response = self.request(identity="other")
        self.assertFalse(response["accepted"])

    def test_movement_request_rejected(self):
        for command in ("open", "close", "stop"):
            response = ipc_request(
                self.path,
                {
                    "type": "command",
                    "identity": "gateway",
                    "command": command,
                },
            )
            self.assertFalse(response["accepted"])
            self.assertFalse(response["transmitted"])

    def test_extra_fields_rejected(self):
        response = self.request(command="open")
        self.assertFalse(response["accepted"])

    def test_no_physical_execution(self):
        response = self.request()
        self.assertFalse(
            response["physical_execution_enabled"]
        )


class SocketOwnershipTests(unittest.TestCase):
    def test_replacement_socket_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "original.sock"
            replacement = Path(directory) / "replacement.sock"

            server = LocalIPCServer(
                path,
                allowed_uids={os.getuid()},
            )
            server.start()

            other = socket.socket(
                socket.AF_UNIX, socket.SOCK_STREAM
            )

            try:
                other.bind(str(replacement))

                # Simulate external replacement of the path.
                path.unlink()
                replacement.rename(path)

                server.stop()

                self.assertTrue(path.is_socket())
            finally:
                other.close()
                if path.exists():
                    path.unlink()

    def test_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.sock"
            server = LocalIPCServer(
                path,
                allowed_uids={os.getuid()},
            )
            server.start()
            server.stop()
            server.stop()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
