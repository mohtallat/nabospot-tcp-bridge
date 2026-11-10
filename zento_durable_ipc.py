"""Read-only durable command inspection over local IPC.

This module cannot reserve, dispatch, or transmit commands.
The command ledger remains the source of durable state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from zento_status_ipc import ZentoStatusIPCHandler


class DurableIPCHandler:
    def __init__(
        self,
        *,
        registry,
        expected_identity,
        ledger,
    ):
        self.status_handler = ZentoStatusIPCHandler(
            registry,
            expected_identity=expected_identity,
        )
        self.expected_identity = expected_identity
        self.ledger_path = ledger.path

    @staticmethod
    def reject(code="unsupported_request"):
        return {
            "accepted": False,
            "code": code,
            "transmitted": False,
        }

    def __call__(self, message):
        if not isinstance(message, dict):
            return self.reject()

        kind = message.get("type")

        if kind == "session_status":
            return self.status_handler(message)

        if kind != "ledger_status":
            return self.reject()

        if set(message) != {
            "type", "identity", "command_id"
        }:
            return self.reject()

        if message["identity"] != self.expected_identity:
            return self.reject("unknown_identity")

        command_id = message["command_id"]

        if (
            type(command_id) is not str
            or not command_id
            or len(command_id) > 128
        ):
            return self.reject("invalid_command_id")

        try:
            # Open an existing database in read-only mode.
            # Never initialize schema or share a connection
            # across IPC worker threads.
            database_path = Path(self.ledger_path).resolve(
                strict=True
            )
            uri = database_path.as_uri() + "?mode=ro"

            with sqlite3.connect(
                uri,
                uri=True,
                timeout=2,
            ) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.row_factory = sqlite3.Row

                record = connection.execute(
                    """
                    SELECT command_id, gateway_serial,
                           state, generation, expires_at
                    FROM commands
                    WHERE command_id=?
                    """,
                    (command_id,),
                ).fetchone()

                row = dict(record) if record else None

        except (
            sqlite3.Error,
            OSError,
            ValueError,
        ):
            return self.reject("ledger_unavailable")

        if row is None:
            return self.reject("command_not_found")

        if row["gateway_serial"] != self.expected_identity:
            return self.reject("command_not_found")

        # Deliberately exclude fingerprint, authorization
        # subject, idempotency key, and other internal data.
        return {
            "accepted": True,
            "code": "ledger_status",
            "command_id": row["command_id"],
            "state": row["state"],
            "generation": row["generation"],
            "expires_at": row["expires_at"],
            "transmitted": False,
            "physical_execution_enabled": False,
        }
