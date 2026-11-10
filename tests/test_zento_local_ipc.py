import os
import socket
import tempfile
import unittest
from pathlib import Path

from zento_local_ipc import (
    IPCError,
    LocalIPCServer,
    encode_message,
    ipc_request,
    read_message,
)


class LocalIPCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "zento.sock"
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.stop()
        self.tmp.cleanup()

    def start_server(self, **kwargs):
        self.server = LocalIPCServer(
            self.path,
            allowed_uids=kwargs.pop(
                "allowed_uids", {os.getuid()}
            ),
            **kwargs,
        )
        self.server.start()

    def test_round_trip_is_simulation_only(self):
        self.start_server()
        response = ipc_request(
            self.path, {"type": "health"}
        )
        self.assertFalse(response["accepted"])
        self.assertFalse(response["transmitted"])
        self.assertEqual(
            response["code"],
            "simulation_transport_only",
        )

    def test_socket_permissions(self):
        self.start_server()
        self.assertEqual(
            self.path.stat().st_mode & 0o777, 0o600
        )

    def test_unauthorized_peer(self):
        self.start_server(
            allowed_uids={os.getuid() + 100000}
        )
        response = ipc_request(
            self.path, {"type": "health"}
        )
        self.assertEqual(
            response["code"], "unauthorized_peer"
        )
        self.assertFalse(response["transmitted"])

    def test_handler_cannot_claim_transmission(self):
        self.start_server(
            handler=lambda _: {
                "accepted": True,
                "code": "simulated",
                "transmitted": True,
            }
        )
        response = ipc_request(
            self.path, {"type": "test"}
        )
        self.assertFalse(response["transmitted"])

    def test_oversized_message_rejected(self):
        with self.assertRaises(IPCError):
            encode_message({"data": "x" * 5000})

    def test_invalid_json_rejected(self):
        left, right = socket.socketpair()
        try:
            left.sendall(b"{invalid}\n")
            with self.assertRaises(IPCError):
                read_message(right)
        finally:
            left.close()
            right.close()

    def test_existing_file_not_overwritten(self):
        self.path.write_text("preserve")
        with self.assertRaises(FileExistsError):
            self.start_server()
        self.assertEqual(
            self.path.read_text(), "preserve"
        )

    def test_socket_removed_on_stop(self):
        self.start_server()
        self.assertTrue(self.path.exists())
        self.server.stop()
        self.assertFalse(self.path.exists())

    def test_no_implicit_server_start(self):
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
