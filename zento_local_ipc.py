"""ZB-2D2: Offline Unix-domain IPC transport.

No command execution, TLS socket access, RS485 encoding,
or physical barrier movement.

The transport authenticates a local OS peer, not a NaboSpot
user or booking. Application authorization is separate.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from pathlib import Path

MAX_MESSAGE = 4096


class IPCError(Exception):
    pass


def encode_message(message):
    if not isinstance(message, dict):
        raise IPCError("invalid_message")

    raw = json.dumps(
        message, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    if len(raw) > MAX_MESSAGE:
        raise IPCError("message_too_large")

    return raw + b"\n"


def read_message(conn):
    data = bytearray()

    while True:
        chunk = conn.recv(1)

        if not chunk:
            raise IPCError("connection_closed")

        if chunk == b"\n":
            break

        data.extend(chunk)

        if len(data) > MAX_MESSAGE:
            raise IPCError("message_too_large")

    try:
        message = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise IPCError("invalid_json")

    if not isinstance(message, dict):
        raise IPCError("invalid_message")

    return message


def peer_uid(conn):
    """Linux Unix-socket peer credentials."""
    if not hasattr(socket, "SO_PEERCRED"):
        raise IPCError("peer_credentials_unavailable")

    raw = conn.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )

    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def simulation_handler(message):
    """Default handler deliberately cannot execute commands."""
    return {
        "accepted": False,
        "code": "simulation_transport_only",
        "transmitted": False,
    }


class LocalIPCServer:
    """Testable, single-process Unix IPC server.

    No production listener is started by importing this module.
    """

    def __init__(
        self,
        path,
        *,
        allowed_uids,
        handler=simulation_handler,
    ):
        self.path = Path(path)
        self.allowed_uids = frozenset(allowed_uids)
        self.handler = handler
        self._server = None
        self._thread = None
        self._stop = threading.Event()

        if not self.allowed_uids:
            raise ValueError("allowed_uids must not be empty")

    def start(self):
        if self._server is not None:
            raise RuntimeError("already_started")

        if not self.path.parent.is_dir():
            raise FileNotFoundError(self.path.parent)

        # Never unlink a pre-existing socket or arbitrary file.
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError(self.path)

        server = socket.socket(
            socket.AF_UNIX, socket.SOCK_STREAM
        )

        try:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(8)
            server.settimeout(0.2)
        except BaseException:
            server.close()
            if self.path.is_socket():
                self.path.unlink()
            raise

        self._server = server
        self._thread = threading.Thread(
            target=self._serve, daemon=True
        )
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with conn:
                conn.settimeout(2.0)

                try:
                    if peer_uid(conn) not in self.allowed_uids:
                        response = {
                            "accepted": False,
                            "code": "unauthorized_peer",
                            "transmitted": False,
                        }
                    else:
                        request = read_message(conn)
                        response = self.handler(request)

                    # Transport cannot report physical transmission.
                    if not isinstance(response, dict):
                        raise IPCError("invalid_handler_response")

                    response = dict(response)
                    response["transmitted"] = False

                except (
                    IPCError, OSError, ValueError,
                    TypeError, UnicodeError,
                ) as exc:
                    response = {
                        "accepted": False,
                        "code": (
                            str(exc)
                            if isinstance(exc, IPCError)
                            else "transport_error"
                        ),
                        "transmitted": False,
                    }
                except Exception:
                    response = {
                        "accepted": False,
                        "code": "handler_error",
                        "transmitted": False,
                    }

                try:
                    conn.sendall(encode_message(response))
                except (OSError, IPCError):
                    pass

    def stop(self):
        self._stop.set()

        if self._server is not None:
            self._server.close()

        if self._thread is not None:
            self._thread.join(timeout=3)

        if self.path.is_socket():
            self.path.unlink()

        self._server = None


def ipc_request(path, message, *, timeout=2.0):
    """Local client; does not contact the TRB145."""
    with socket.socket(
        socket.AF_UNIX, socket.SOCK_STREAM
    ) as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        conn.sendall(encode_message(message))
        return read_message(conn)
