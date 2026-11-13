"""Opt-in live Zento command channel. Only the mTLS session worker sends frames.

The bridge API key authenticates the backend; this module does not authorize users.
Commands require caller-supplied stable IDs for retry-safe deduplication.
"""
from __future__ import annotations

import os
import queue
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from zento_command_ledger import CommandLedger, Reservation
from zento_local_ipc import encode_message, read_message, peer_uid
from zento_protocol import build_frame


@dataclass
class Pending:
    command_id: str
    command: str
    generation: int
    result: dict | None = None
    finished: threading.Event = field(default_factory=threading.Event)


class LiveCommandChannel:
    def __init__(self, registry, *, serial, identity, address, ledger_path, enabled=False):
        self.registry = registry
        self.serial = serial
        self.identity = identity
        self.address = address
        self.enabled = enabled
        self.ledger_path = ledger_path
        self.queue = queue.Queue()
        self.inflight = False
        self.lock = threading.Lock()
        with CommandLedger(self.ledger_path) as ledger:
            ledger.recover(datetime.now(timezone.utc))

    def _record(self, command_id):
        with CommandLedger(self.ledger_path) as ledger:
            return ledger.get(command_id)

    def submit(self, request):
        denied = lambda code: {"accepted": False, "code": code, "transmitted": False}
        if not self.enabled:
            return denied("physical_execution_disabled")
        if not isinstance(request, dict) or set(request) != {"type", "serial", "address", "command", "command_id"}:
            return denied("invalid_request")
        if request["type"] != "movement" or request["serial"] != self.serial or type(request["address"]) is not int or request["address"] != self.address:
            return denied("target_mismatch")
        command = request["command"]
        command_id = request["command_id"]
        if command not in ("open", "close", "stop") or not isinstance(command_id, str) or not (8 <= len(command_id) <= 128) or not all(c.isalnum() or c in "-_." for c in command_id):
            return denied("invalid_command")
        with self.lock:
            existing = self._record(command_id)
            if existing:
                if existing["command"] != command or existing["gateway_serial"] != self.serial or existing["rs485_address"] != self.address:
                    return denied("idempotency_conflict")
                return {"accepted": False, "code": "duplicate", "state": existing["state"], "transmitted": False}
            if self.inflight:
                return denied("command_in_progress")
            snap = self.registry.snapshot(self.identity)
            if command == snap["barrier_state"]:
                return denied("already_in_target_state")
            if not snap["online"] or not snap["connected"] or type(snap["generation"]) is not int:
                return denied("gateway_offline")
            if snap["barrier_state"] not in ("open", "closed"):
                return denied("unsafe_or_unknown_state")
            # A fresh state is necessary but not sufficient for claiming movement succeeded.
            now = datetime.now(timezone.utc)
            item = Reservation(command_id=command_id, idempotency_key=command_id,
                fingerprint=("authenticated_backend", self.serial, self.address, command, snap["generation"]),
                gateway_serial=self.serial, rs485_address=self.address, command=command,
                generation=snap["generation"], expires_at=now + timedelta(seconds=15))
            with CommandLedger(self.ledger_path) as ledger:
                result, _ = ledger.reserve(item, now)
            if result != "created":
                return denied("reservation_failed")
            pending = Pending(command_id, command, snap["generation"])
            self.inflight = True
            self.queue.put(pending)
        if not pending.finished.wait(timeout=12):
            # The owning worker may still execute; never imply safe retry.
            return {"accepted": True, "code": "outcome_unknown", "transmitted": False, "command_id": command_id}
        return pending.result

    def process_one(self, conn, identity, generation, parser, receive_status):
        """Called exclusively by the owner of the mTLS socket.

        receive_status(conn, parser, expected_state) must consume frames itself.
        """
        try:
            pending = self.queue.get_nowait()
        except queue.Empty:
            return False
        now = datetime.now(timezone.utc)
        try:
            snap = self.registry.snapshot(identity)
            if (identity != self.identity or generation != pending.generation or
                not snap["online"] or snap["generation"] != generation or
                snap["barrier_state"] not in ("open", "closed")):
                with CommandLedger(self.ledger_path) as ledger:
                    ledger.transition(pending.command_id, "reserved", "rejected", now, reason="session_or_status_changed")
                pending.result = {"accepted": False, "code": "session_or_status_changed", "transmitted": False}
                return True
            with CommandLedger(self.ledger_path) as ledger:
                started = ledger.transition(pending.command_id, "reserved", "dispatch_started", now,
                    generation=generation, reason="live_dispatch_started")
            if not started:
                pending.result = {"accepted": False, "code": "dispatch_not_reserved", "transmitted": False}
                return True
            # From here on, any failure is outcome_unknown, never an automatic retry.
            transmitted = False
            try:
                conn.sendall(build_frame(pending.command, address=self.address, reversed_direction=True))
                transmitted = True
                expected = "open" if pending.command == "open" else "closed" if pending.command == "close" else None
                confirmed = receive_status(conn, parser, expected) if expected else False
                with CommandLedger(self.ledger_path) as ledger:
                    ledger.transition(pending.command_id, "dispatch_started",
                        "completed" if confirmed else "outcome_unknown", datetime.now(timezone.utc),
                        reason="status_confirmed" if confirmed else "status_not_confirmed")
                pending.result = {"accepted": True, "code": "status_confirmed" if confirmed else "outcome_unknown",
                    "transmitted": transmitted, "command_id": pending.command_id,
                    "physical_state_confirmed": confirmed}
            except (OSError, TimeoutError, ValueError):
                with CommandLedger(self.ledger_path) as ledger:
                    ledger.transition(pending.command_id, "dispatch_started", "outcome_unknown", datetime.now(timezone.utc), reason="transport_uncertain")
                pending.result = {"accepted": True, "code": "outcome_unknown", "transmitted": transmitted,
                    "command_id": pending.command_id, "physical_state_confirmed": False}
        finally:
            if pending.result is None:
                pending.result = {"accepted": False, "code": "internal_error", "transmitted": False}
            with self.lock:
                self.inflight = False
            pending.finished.set()
        return True


class CommandIPCServer:
    def __init__(self, path, channel, allowed_uid):
        self.path = path
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = False
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(14)
                try:
                    if peer_uid(self.request) != allowed_uid:
                        response = {"accepted": False, "code": "unauthorized_peer", "transmitted": False}
                    else:
                        response = channel.submit(read_message(self.request))
                except Exception:
                    response = {"accepted": False, "code": "command_channel_error", "transmitted": False}
                try:
                    self.request.sendall(encode_message(response))
                except OSError:
                    pass
        self.server = Server(path, Handler)
        os.chmod(path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if os.path.exists(self.path):
            os.unlink(self.path)
