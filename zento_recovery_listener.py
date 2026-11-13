"""Read-only, concurrent, mTLS Zento commissioning listener.

One active session per certificate identity. No movement commands.
"""
from __future__ import annotations

import hashlib
import logging
import os
import signal
import socket
import ssl
import threading
import time
from datetime import datetime, timezone

from zento_protocol import ZentoFrameBuffer, ZentoProtocolError, build_frame, decode_status
from zento_local_ipc import LocalIPCServer
from zento_status_ipc import ZentoStatusIPCHandler
from zento_live_commands import LiveCommandChannel, CommandIPCServer

LOG = logging.getLogger("zento.recovery")
HOST = os.getenv("ZENTO_LISTEN_HOST", "0.0.0.0")
PORT = int(os.getenv("ZENTO_LISTEN_PORT", "9667"))
CERT_DIR = os.getenv("ZENTO_CERT_DIR", "/etc/nabospot/zento-tls")
EXPECTED_CN = os.getenv("ZENTO_EXPECTED_CN", "nabospot-trb145-pilot-01")
EXPECTED_SHA256 = os.getenv("ZENTO_CLIENT_CERT_SHA256", "").replace(":", "").lower().strip()
RS485_ADDRESS = int(os.getenv("ZENTO_RS485_ADDRESS", "0"))
STATUS_INTERVAL = float(os.getenv("ZENTO_STATUS_INTERVAL", "10"))
RESPONSE_TIMEOUT = float(os.getenv("ZENTO_RESPONSE_TIMEOUT", "5"))
MAX_MISSED_POLLS = int(os.getenv("ZENTO_MAX_MISSED_POLLS", "3"))
HANDSHAKE_TIMEOUT = float(os.getenv("ZENTO_HANDSHAKE_TIMEOUT", "10"))
KEEPALIVE_IDLE = int(os.getenv("ZENTO_TCP_KEEPIDLE", "30"))
KEEPALIVE_INTERVAL = int(os.getenv("ZENTO_TCP_KEEPINTVL", "15"))
KEEPALIVE_COUNT = int(os.getenv("ZENTO_TCP_KEEPCNT", "3"))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def certificate_cn(cert):
    for rdn in cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


def authorized_identity(cert, cert_der):
    identity = certificate_cn(cert)
    if identity != EXPECTED_CN or not cert_der:
        return None
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    if EXPECTED_SHA256 and fingerprint != EXPECTED_SHA256:
        return None
    return identity


def create_tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(f"{CERT_DIR}/server.crt", f"{CERT_DIR}/server.key")
    context.load_verify_locations(cafile=f"{CERT_DIR}/ca.crt")
    return context


def configure_keepalive(conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", KEEPALIVE_IDLE),
                        ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL),
                        ("TCP_KEEPCNT", KEEPALIVE_COUNT)):
        opt = getattr(socket, name, None)
        if opt is not None:
            conn.setsockopt(socket.IPPROTO_TCP, opt, value)


class SessionRegistry:
    """Atomic generation-based session replacement; old workers cannot evict new ones."""
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}
        self._generation = 0
        self._last_status = {}

    def replace(self, identity, conn):
        with self._lock:
            self._generation += 1
            generation = self._generation
            previous = self._sessions.get(identity)
            self._sessions[identity] = (generation, conn)
            # A new TLS connection is not proof of a fresh controller status.
            self._last_status.pop(identity, None)
        if previous:
            try:
                previous[1].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                previous[1].close()
            except OSError:
                pass
        return generation

    def active(self, identity, generation):
        with self._lock:
            item = self._sessions.get(identity)
            return item is not None and item[0] == generation

    def record_status(self, identity, generation, state):
        with self._lock:
            if self._sessions.get(identity, (None,))[0] != generation:
                return False
            self._last_status[identity] = (time.monotonic(), utc_now(), state)
            return True

    def release(self, identity, generation):
        with self._lock:
            if self._sessions.get(identity, (None,))[0] == generation:
                del self._sessions[identity]
                self._last_status.pop(identity, None)

    def snapshot(self, identity, stale_after=None):
        if stale_after is None:
            stale_after = STATUS_INTERVAL + RESPONSE_TIMEOUT + 5
        with self._lock:
            session = self._sessions.get(identity)
            connected = session is not None
            generation = session[0] if session else None
            status = self._last_status.get(identity)
            fresh = bool(
                connected
                and status
                and 0 <= time.monotonic() - status[0] <= stale_after
            )
            return {
                "connected": connected,
                "online": fresh,
                "generation": generation,
                "barrier_state": status[2] if fresh else "unknown",
                "last_seen_at": status[1] if status else None,
            }


REGISTRY = SessionRegistry()
COMMAND_CHANNEL = None


def confirm_after_command(conn, parser, identity, generation, expected_state):
    """Observe independent controller status; do not treat a socket write as success."""
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and REGISTRY.active(identity, generation):
        conn.sendall(build_frame("status", address=RS485_ADDRESS))
        until = min(deadline, time.monotonic() + RESPONSE_TIMEOUT)
        while time.monotonic() < until and REGISTRY.active(identity, generation):
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("gateway_disconnected")
            for frame in parser.feed(data):
                if frame.address != RS485_ADDRESS or frame.command != 0x0A or len(frame.payload) < 2:
                    continue
                state = decode_status(frame, reversed_direction=True)
                REGISTRY.record_status(identity, generation, state)
                if expected_state is not None and state == expected_state:
                    return True
        time.sleep(0.1)
    return False



def poll_session(conn, identity, generation):
    parser = ZentoFrameBuffer()
    missed = 0
    conn.settimeout(min(1.0, RESPONSE_TIMEOUT))
    while REGISTRY.active(identity, generation):
        request = build_frame("status", address=RS485_ADDRESS)
        conn.sendall(request)
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        received = False
        while time.monotonic() < deadline and REGISTRY.active(identity, generation):
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                LOG.warning("gateway disconnected identity=%s", identity)
                return
            try:
                frames = parser.feed(data)
            except ZentoProtocolError as exc:
                LOG.warning("protocol error identity=%s error=%s", identity, exc)
                return
            for frame in frames:
                if frame.address != RS485_ADDRESS or frame.command != 0x0A or len(frame.payload) < 2:
                    continue
                try:
                    state = decode_status(frame, reversed_direction=True)
                except (ValueError, ZentoProtocolError) as exc:
                    LOG.warning("invalid status identity=%s error=%s", identity, exc)
                    continue
                if REGISTRY.record_status(identity, generation, state):
                    LOG.info("[ZENTO STATUS] identity=%s state=%s time=%s", identity, state, utc_now())
                received = True
            if received:
                break
        if not REGISTRY.active(identity, generation):
            return
        if received:
            missed = 0
        else:
            missed += 1
            LOG.warning("status timeout identity=%s missed=%d", identity, missed)
            if missed >= MAX_MISSED_POLLS:
                LOG.error("closing unresponsive gateway identity=%s", identity)
                return
        # Only this worker may read/write its mTLS socket.
        wake = time.monotonic() + STATUS_INTERVAL
        while time.monotonic() < wake:
            if not REGISTRY.active(identity, generation):
                return
            if COMMAND_CHANNEL is not None:
                COMMAND_CHANNEL.process_one(conn, identity, generation, parser,
                    lambda c, p, expected: confirm_after_command(c, p, identity, generation, expected))
            time.sleep(min(0.2, max(0, wake - time.monotonic())))


def client_worker(raw_conn, addr, context):
    identity = None
    generation = None
    try:
        configure_keepalive(raw_conn)
        raw_conn.settimeout(HANDSHAKE_TIMEOUT)
        with context.wrap_socket(raw_conn, server_side=True) as conn:
            identity = authorized_identity(conn.getpeercert(), conn.getpeercert(binary_form=True))
            if not identity:
                LOG.warning("rejected certificate identity peer=%s", addr)
                return
            generation = REGISTRY.replace(identity, conn)
            LOG.info("authenticated identity=%s peer=%s generation=%d", identity, addr, generation)
            poll_session(conn, identity, generation)
    except (ssl.SSLError, OSError, ConnectionError) as exc:
        LOG.warning("connection ended peer=%s error=%s", addr, exc)
    finally:
        if identity is not None and generation is not None:
            REGISTRY.release(identity, generation)
        try:
            raw_conn.close()
        except OSError:
            pass


def main():
    if STATUS_INTERVAL <= 0 or RESPONSE_TIMEOUT <= 0 or MAX_MISSED_POLLS < 1:
        raise ValueError("Invalid polling configuration")
    context = create_tls_context()
    global COMMAND_CHANNEL
    commands_enabled = os.getenv("ZENTO_PHYSICAL_COMMANDS_ENABLED", "false").lower() == "true"
    command_ipc = None
    if commands_enabled:
        COMMAND_CHANNEL = LiveCommandChannel(
            REGISTRY, serial="6003791910", identity=EXPECTED_CN,
            address=RS485_ADDRESS,
            ledger_path="/var/lib/nabospot-zento/commands.sqlite3", enabled=True)
        command_ipc = CommandIPCServer(
            "/run/nabospot-zento/commands.sock", COMMAND_CHANNEL, os.getuid())
        command_ipc.start()
        LOG.warning("PHYSICAL ZENTO COMMAND CHANNEL ENABLED")

    ipc = LocalIPCServer(
        "/run/nabospot-zento/status.sock",
        allowed_uids={os.getuid()},
        handler=ZentoStatusIPCHandler(
            REGISTRY,
            expected_identity=EXPECTED_CN,
        ),
    )
    ipc.start()
    LOG.info("read-only status IPC started")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((HOST, PORT))
            server.listen(16)
            server.settimeout(1)
            LOG.info("listening host=%s port=%s read_only=true", HOST, PORT)
            while not stop.is_set():
                try:
                    raw_conn, addr = server.accept()
                except socket.timeout:
                    continue
                # This commissioning build limits concurrent handshake workers.
                if threading.active_count() >= 32:
                    LOG.warning("rejecting excess connection peer=%s", addr)
                    raw_conn.close()
                    continue
                threading.Thread(target=client_worker, args=(raw_conn, addr, context), daemon=True).start()
    finally:
        ipc.stop()
        if command_ipc is not None:
            command_ipc.stop()
        LOG.info("listener stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
