"""ZB-2C1: Offline, serialized Zento dispatch simulation.

No network access, sockets, RS485 frames, or physical actuation.
Not wired into the production listener or API.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from zento_command_admission import (
    AuthorizationEvidence,
    CommandRequest,
    GatewaySnapshot,
    admit_command,
)
from zento_command_ledger import CommandLedger, timestamp


@dataclass(frozen=True)
class DispatchResult:
    command_id: str
    outcome: str
    reason: str
    simulated: bool = True
    transmitted: bool = False


class SimulationDispatcher:
    """Serialize simulated dispatch per gateway within this instance.

    A lock is not a cross-process execution fence. Production dispatch
    will require atomic session ownership and database coordination.
    """

    def __init__(
        self,
        ledger: CommandLedger,
        snapshot_provider: Callable[[str], GatewaySnapshot],
    ):
        self.ledger = ledger
        self.snapshot_provider = snapshot_provider
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _gateway_lock(self, gateway_serial: str):
        with self._guard:
            return self._locks.setdefault(
                gateway_serial, threading.Lock()
            )

    def simulate(
        self,
        request: CommandRequest,
        authorization: AuthorizationEvidence,
        *,
        now: datetime,
    ) -> DispatchResult:
        """Simulate one already-reserved command.

        This method deliberately cannot transmit anything.
        """
        with self._gateway_lock(request.gateway_serial):
            return self._simulate_locked(request, authorization, now)

    def _simulate_locked(self, request, authorization, now):
        moment = timestamp(now)

        record = self.ledger.get(request.command_id)
        if record is None:
            return DispatchResult(
                request.command_id, "rejected", "not_reserved"
            )

        if record["state"] != "reserved":
            return DispatchResult(
                request.command_id,
                "rejected",
                f"not_reserved_state:{record['state']}",
            )

        # Bind the request and verified authorization subject to
        # the immutable persisted reservation.
        import json

        try:
            fingerprint, stored_expiry = json.loads(
                record["fingerprint"]
            )
            valid_fingerprint = (
                type(fingerprint) is list
                and len(fingerprint) == 5
                and fingerprint == [
                    authorization.subject_id,
                    request.gateway_serial,
                    request.rs485_address,
                    request.command,
                    request.generation,
                ]
                and stored_expiry == record["expires_at"]
            )
        except (ValueError, TypeError):
            valid_fingerprint = False

        if (
            record["gateway_serial"] != request.gateway_serial
            or record["rs485_address"] != request.rs485_address
            or record["command"] != request.command
            or record["generation"] != request.generation
            or record["expires_at"] != timestamp(request.expires_at)
        ):
            return DispatchResult(
                request.command_id, "rejected",
                "reservation_mismatch"
            )

        if (
            record["idempotency_key"] != request.idempotency_key
            or not valid_fingerprint
        ):
            return DispatchResult(
                request.command_id, "rejected",
                "reservation_identity_mismatch"
            )

        try:
            snapshot = self.snapshot_provider(
                request.gateway_serial
            )
        except Exception:
            return DispatchResult(
                request.command_id, "rejected",
                "snapshot_unavailable"
            )

        if not isinstance(snapshot, GatewaySnapshot):
            return DispatchResult(
                request.command_id, "rejected",
                "invalid_snapshot"
            )

        decision = admit_command(
            request, authorization, snapshot, now=now
        )
        if not decision.admitted:
            return DispatchResult(
                request.command_id, "rejected",
                f"admission:{decision.code.value}"
            )

        # Recheck session evidence immediately before the durable
        # transition. This remains a simulation-only check and is
        # not atomic with live socket ownership.
        try:
            latest = self.snapshot_provider(
                request.gateway_serial
            )
        except Exception:
            return DispatchResult(
                request.command_id, "rejected",
                "snapshot_unavailable"
            )

        if not isinstance(latest, GatewaySnapshot):
            return DispatchResult(
                request.command_id, "rejected",
                "invalid_snapshot"
            )

        second = admit_command(
            request, authorization, latest, now=now
        )
        if not second.admitted:
            return DispatchResult(
                request.command_id, "rejected",
                f"recheck:{second.code.value}"
            )

        if not self.ledger.transition(
            request.command_id,
            "reserved",
            "dispatch_started",
            now,
            generation=latest.generation,
            reason="zb2c1_simulation_only",
        ):
            return DispatchResult(
                request.command_id, "rejected",
                "atomic_transition_denied"
            )

        # No frame is constructed, queued, or sent.
        # Preserve a conservative terminal state.
        if not self.ledger.transition(
            request.command_id,
            "dispatch_started",
            "outcome_unknown",
            now,
            reason="simulation_no_physical_execution",
        ):
            return DispatchResult(
                request.command_id,
                "outcome_unknown",
                "terminal_transition_unconfirmed",
            )

        return DispatchResult(
            request.command_id,
            "outcome_unknown",
            "simulation_completed_without_transmission",
        )
