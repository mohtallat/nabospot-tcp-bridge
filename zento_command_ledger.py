"""ZB-2B durable command ledger. No network or RS485 execution."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


TERMINAL = frozenset({
    "rejected", "expired", "completed", "outcome_unknown"
})


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware datetime required")
    return value.astimezone(timezone.utc)


def timestamp(value):
    return utc(value).isoformat()


@dataclass(frozen=True)
class Reservation:
    command_id: str
    idempotency_key: str
    fingerprint: tuple
    gateway_serial: str
    rs485_address: int
    command: str
    generation: int
    expires_at: datetime


class CommandLedger:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).parent
            if not parent.is_dir():
                raise FileNotFoundError(
                    f"Ledger directory does not exist: {parent}"
                )
        self.db = sqlite3.connect(
            self.path, timeout=10, isolation_level=None
        )
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                gateway_serial TEXT NOT NULL,
                rs485_address INTEGER NOT NULL,
                command TEXT NOT NULL,
                generation INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT
            )
        """)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, command_id):
        self.db.row_factory = sqlite3.Row
        row = self.db.execute(
            "SELECT * FROM commands WHERE command_id=?",
            (command_id,)
        ).fetchone()
        return dict(row) if row else None

    def reserve(self, item: Reservation, now: datetime):
        """Atomically reserve an already-admitted request.

        Caller must supply a trusted ZB-2A admission result.
        This method does not independently establish authorization.
        """
        moment = timestamp(now)
        expiry = timestamp(item.expires_at)

        if (
            not item.command_id or not item.command_id.strip()
            or not item.idempotency_key
            or not item.idempotency_key.strip()
            or not item.gateway_serial
            or not item.gateway_serial.strip()
            or item.command not in ("open", "close", "stop")
            or type(item.rs485_address) is not int
            or not 0 <= item.rs485_address <= 255
            or type(item.generation) is not int
            or item.generation < 1
            or not isinstance(item.fingerprint, tuple)
            or not item.fingerprint
            or expiry <= moment
        ):
            raise ValueError("Invalid reservation")

        # Verify caller-supplied fingerprint matches immutable fields.
        # Never accept a changed command as an idempotent duplicate.
        expected = (
            item.fingerprint[0],
            item.gateway_serial,
            item.rs485_address,
            item.command,
            item.generation,
        )

        if item.fingerprint != expected:
            raise ValueError("Reservation fingerprint mismatch")

        # Reject non-string or empty authorization subjects.
        if (
            type(item.fingerprint[0]) is not str
            or not item.fingerprint[0].strip()
        ):
            raise ValueError("Invalid authorization subject")

        canonical = json.dumps(
            (
                item.fingerprint,
                expiry,
            ),
            separators=(",", ":"),
            ensure_ascii=True,
        )

        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                """SELECT command_id, fingerprint, state
                   FROM commands WHERE idempotency_key=?""",
                (item.idempotency_key,)
            ).fetchone()

            if row:
                if (
                    row[0] == item.command_id
                    and row[1] == canonical
                ):
                    result = ("duplicate", row[2])
                else:
                    result = ("conflict", row[2])
            else:
                collision = self.db.execute(
                    "SELECT 1 FROM commands WHERE command_id=?",
                    (item.command_id,)
                ).fetchone()
                if collision:
                    result = ("conflict", None)
                else:
                    self.db.execute("""
                        INSERT INTO commands VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL
                        )
                    """, (
                        item.command_id,
                        item.idempotency_key,
                        canonical,
                        item.gateway_serial,
                        item.rs485_address,
                        item.command,
                        item.generation,
                        expiry,
                        "reserved",
                        moment,
                        moment,
                    ))
                    result = ("created", "reserved")
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def transition(
        self, command_id, expected, target, now,
        *, generation=None, reason=None
    ):
        """Atomic state transition with expiry and generation checks.

        dispatch_started is a write-ahead marker only. Actual execution
        is not implemented in ZB-2B.
        """
        allowed = {
            ("reserved", "dispatch_started"),
            ("reserved", "expired"),
            ("reserved", "rejected"),
            ("dispatch_started", "completed"),
            ("dispatch_started", "outcome_unknown"),
        }
        if (expected, target) not in allowed:
            raise ValueError("Unsupported transition")

        moment = timestamp(now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                """SELECT state, expires_at, generation
                   FROM commands WHERE command_id=?""",
                (command_id,)
            ).fetchone()

            if not row or row[0] != expected:
                result = False
            elif target == "dispatch_started" and (
                generation is None
                or type(generation) is not int
                or row[2] != generation
                or row[1] <= moment
            ):
                result = False
            else:
                self.db.execute("""
                    UPDATE commands
                    SET state=?, updated_at=?, reason=?
                    WHERE command_id=?
                """, (target, moment, reason, command_id))
                result = True

            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def recover(self, now):
        """Conservatively reconcile records after a process restart.

        Never requeue a dispatch-started movement.
        """
        moment = timestamp(now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            uncertain = self.db.execute("""
                UPDATE commands
                SET state='outcome_unknown',
                    updated_at=?,
                    reason='recovery_after_dispatch_started'
                WHERE state='dispatch_started'
            """, (moment,)).rowcount

            expired = self.db.execute("""
                UPDATE commands
                SET state='expired',
                    updated_at=?,
                    reason='expired_during_recovery'
                WHERE state='reserved' AND expires_at<=?
            """, (moment, moment)).rowcount

            self.db.execute("COMMIT")
            return {
                "outcome_unknown": uncertain,
                "expired": expired,
            }
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
