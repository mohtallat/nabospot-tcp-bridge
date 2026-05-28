import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from omni_protocol import build_server_command


@dataclass
class PendingOperation:
    operation: str
    user_id: int
    timestamp: int
    key: Optional[int] = None


@dataclass
class LockConnection:
    imei: str
    conn: socket.socket
    addr: tuple
    last_seen_at: datetime
    pending: Optional[PendingOperation] = None
    telemetry: dict = field(default_factory=dict)


class OmniConnectionManager:
    def __init__(self):
        self._connections: Dict[str, LockConnection] = {}
        self._lock = threading.Lock()

    def register(self, imei: str, conn: socket.socket, addr: tuple):
        """
        Register or refresh a device TCP connection.

        Important:
        - Do NOT erase pending operation when the same socket sends another packet.
        - Preserve pending operation if the device reconnects quickly.
        """
        with self._lock:
            existing = self._connections.get(imei)

            # Same socket: refresh metadata only, keep pending + telemetry.
            if existing and existing.conn is conn:
                existing.addr = addr
                existing.last_seen_at = datetime.now(timezone.utc)
                return

            # New socket: preserve pending + telemetry where possible.
            old_pending = existing.pending if existing else None
            old_telemetry = existing.telemetry if existing else {}

            if existing and existing.conn is not conn:
                try:
                    existing.conn.close()
                except Exception:
                    pass

            self._connections[imei] = LockConnection(
                imei=imei,
                conn=conn,
                addr=addr,
                last_seen_at=datetime.now(timezone.utc),
                pending=old_pending,
                telemetry=old_telemetry,
            )

    def unregister(self, imei: str):
        with self._lock:
            self._connections.pop(imei, None)

    def get(self, imei: str) -> Optional[LockConnection]:
        with self._lock:
            return self._connections.get(imei)

    def mark_seen(self, imei: str):
        with self._lock:
            if imei in self._connections:
                self._connections[imei].last_seen_at = datetime.now(timezone.utc)

    def update_telemetry(self, imei: str, telemetry: dict):
        with self._lock:
            if imei in self._connections:
                self._connections[imei].telemetry.update(telemetry)
                self._connections[imei].last_seen_at = datetime.now(timezone.utc)

    def send_raw(self, imei: str, payload: bytes) -> bool:
        c = self.get(imei)
        if not c:
            print(f"[SEND ERROR] {imei} not connected")
            return False

        try:
            print(f"[TX] {payload}")
            c.conn.sendall(payload)
            return True
        except Exception as e:
            print(f"[SEND ERROR] {imei}: {e}")
            self.unregister(imei)
            return False

    def send_command(self, imei: str, command: str, *args) -> bool:
        return self.send_raw(imei, build_server_command(imei, command, *args))

    def request_unlock(self, imei: str, user_id: int = 1) -> bool:
        ts = int(time.time())

        with self._lock:
            c = self._connections.get(imei)
            if not c:
                print(f"[UNLOCK ERROR] {imei} not connected")
                return False

            c.pending = PendingOperation(
                operation="unlock",
                user_id=user_id,
                timestamp=ts,
            )

        return self.send_command(imei, "R0", 0, 300, user_id, ts)

    def request_lock(self, imei: str, user_id: int = 1) -> bool:
        ts = int(time.time())

        with self._lock:
            c = self._connections.get(imei)
            if not c:
                print(f"[LOCK ERROR] {imei} not connected")
                return False

            c.pending = PendingOperation(
                operation="lock",
                user_id=user_id,
                timestamp=ts,
            )

        return self.send_command(imei, "R0", 1, 300, user_id, ts)

    def handle_operation_key(
        self,
        imei: str,
        operation_code: str,
        key: str,
        user_id: str,
        ts: str,
    ):
        """
        Handle R0 response:
        Lock returns temporary operation key.
        Then bridge immediately sends L0 or L1.
        """
        c = self.get(imei)

        if not c:
            print(f"[R0 ERROR] {imei} not connected")
            return

        if not c.pending:
            print(f"[R0] No pending operation for {imei}")
            return

        if str(c.pending.user_id) != str(user_id) or str(c.pending.timestamp) != str(ts):
            print(
                f"[R0] Pending operation mismatch for {imei}. "
                f"pending_user={c.pending.user_id}, received_user={user_id}, "
                f"pending_ts={c.pending.timestamp}, received_ts={ts}"
            )
            return

        c.pending.key = int(key)

        if c.pending.operation == "unlock":
            # L0 requires: key, user_id, timestamp
            self.send_command(imei, "L0", key, user_id, ts)

        elif c.pending.operation == "lock":
            # L1 in the protocol requires only: key
            self.send_command(imei, "L1", key)

    def complete_operation(self, imei: str):
        with self._lock:
            c = self._connections.get(imei)
            if c:
                c.pending = None

    def list_connections(self):
        with self._lock:
            return list(self._connections.values())


manager = OmniConnectionManager()
