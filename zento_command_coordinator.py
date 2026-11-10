"""ZB-2C3: Offline coordination of existing safety components.

No sockets, RS485 frames, HTTP endpoints or physical movement.
Trusted authorization must originate outside this module.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from zento_command_admission import (
    AuthorizationEvidence,
    CommandRequest,
    GatewaySnapshot,
    admit_command,
)
from zento_command_dispatcher import (
    DispatchResult,
    SimulationDispatcher,
)
from zento_command_ledger import (
    CommandLedger,
    Reservation,
)
from zento_session_mailbox import (
    CommandIntent,
    SessionMailbox,
)


class SimulationCoordinator:
    """One-process offline command lifecycle coordinator.

    The mailbox is a simulated session owner. The SQLite ledger
    provides durable idempotency. Neither provides authorization
    or permission to transmit physical movement commands.
    """

    def __init__(
        self,
        ledger: CommandLedger,
        mailbox: SessionMailbox,
        snapshot_provider: Callable[[str], GatewaySnapshot],
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ledger = ledger
        self.mailbox = mailbox
        self.snapshot_provider = snapshot_provider
        self.clock = clock
        self.dispatcher = SimulationDispatcher(
            ledger, snapshot_provider
        )
        self._authorizations = {}
        self._requests = {}

    def submit(
        self,
        request: CommandRequest,
        authorization: AuthorizationEvidence,
        *,
        now: datetime,
    ):
        """Admit, durably reserve, then enqueue simulation intent."""
        if not isinstance(now, datetime) or now.tzinfo is None:
            return "invalid_time"

        try:
            snapshot = self.snapshot_provider(
                request.gateway_serial
            )
            decision = admit_command(
                request, authorization, snapshot, now=now
            )
        except (ValueError, TypeError, AttributeError):
            return "invalid_request"
        except Exception:
            return "snapshot_unavailable"

        if not decision.admitted:
            return f"admission:{decision.code.value}"

        reservation = Reservation(
            command_id=request.command_id,
            idempotency_key=request.idempotency_key,
            fingerprint=(
                authorization.subject_id,
                request.gateway_serial,
                request.rs485_address,
                request.command,
                request.generation,
            ),
            gateway_serial=request.gateway_serial,
            rs485_address=request.rs485_address,
            command=request.command,
            generation=request.generation,
            expires_at=request.expires_at,
        )

        try:
            result, state = self.ledger.reserve(
                reservation, now
            )
        except (ValueError, TypeError):
            return "invalid_reservation"

        if result != "created":
            return f"reservation:{result}:{state}"

        # Convert UTC expiry into a process-local monotonic deadline.
        # A later claim still rechecks UTC expiry through admission.
        remaining = (
            request.expires_at.astimezone(timezone.utc)
            - now.astimezone(timezone.utc)
        ).total_seconds()

        if remaining <= 0:
            return "expired_before_enqueue"

        intent = CommandIntent(
            command_id=request.command_id,
            identity=request.gateway_serial,
            generation=request.generation,
            expires_at=self.clock() + remaining,
        )

        queued = self.mailbox.submit(intent)
        if not queued.accepted:
            return f"mailbox:{queued.code}"

        self._requests[request.command_id] = request
        self._authorizations[request.command_id] = authorization
        return "queued"

    def claim(
        self,
        identity: str,
        generation: int,
        *,
        now: datetime,
    ) -> DispatchResult:
        """Claim one intent and run durable simulation.

        The mailbox is the simulated owner. No socket is accessible.
        """
        result = self.mailbox.claim(identity, generation)

        if not result.accepted:
            return DispatchResult(
                "", "rejected", f"mailbox:{result.code}"
            )

        command_id = result.command_id
        request = self._requests.pop(command_id, None)
        authorization = self._authorizations.pop(
            command_id, None
        )

        if request is None or authorization is None:
            return DispatchResult(
                command_id, "rejected",
                "missing_local_request"
            )

        return self.dispatcher.simulate(
            request, authorization, now=now
        )

    def recover(self, *, now: datetime):
        """Conservative restart reconciliation."""
        self._requests.clear()
        self._authorizations.clear()
        return self.ledger.recover(now)
