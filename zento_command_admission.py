"""ZB-2A: Pure, fail-closed Zento command admission policy.

No network I/O, socket access, RS485 transmission, or command execution.

AuthorizationEvidence must be constructed by a trusted integration
after validating the backend caller and its authorization decision.
It is not a credential and must never be populated directly from
untrusted HTTP request fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional


class DecisionCode(str, Enum):
    ADMITTED = "admitted"
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    TARGET_MISMATCH = "target_mismatch"
    EXPIRED = "expired"
    INVALID_EXPIRY = "invalid_expiry"
    OFFLINE = "offline"
    STALE_STATUS = "stale_status"
    UNKNOWN_STATE = "unknown_state"
    UNSAFE_STATE = "unsafe_state"
    SESSION_MISMATCH = "session_mismatch"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


@dataclass(frozen=True)
class AuthorizationEvidence:
    trusted: bool
    subject_id: str
    gateway_serial: str
    rs485_address: int
    permitted_command: str
    valid_until: datetime


@dataclass(frozen=True)
class GatewaySnapshot:
    gateway_serial: str
    rs485_address: int
    connected: bool
    online: bool
    barrier_state: str
    last_seen_at: Optional[datetime]
    generation: int


@dataclass(frozen=True)
class CommandRequest:
    command_id: str
    idempotency_key: str
    gateway_serial: str
    rs485_address: int
    command: str
    generation: int
    expires_at: datetime


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    code: DecisionCode
    command_id: str
    gateway_serial: str
    rs485_address: int
    command: str
    generation: int

    @property
    def execution_enabled(self) -> bool:
        # ZB-2A never authorizes actual transmission.
        return False


def _utc(value: datetime) -> Optional[datetime]:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _decision(
    request: CommandRequest, code: DecisionCode
) -> AdmissionDecision:
    return AdmissionDecision(
        admitted=code == DecisionCode.ADMITTED,
        code=code,
        command_id=request.command_id,
        gateway_serial=request.gateway_serial,
        rs485_address=request.rs485_address,
        command=request.command,
        generation=request.generation,
    )


def admit_command(
    request: CommandRequest,
    authorization: AuthorizationEvidence,
    snapshot: GatewaySnapshot,
    *,
    now: datetime,
    existing_fingerprint: Optional[tuple] = None,
    max_status_age_seconds: int = 20,
    max_command_ttl_seconds: int = 30,
) -> AdmissionDecision:
    """Evaluate a movement request without performing any side effects.

    existing_fingerprint is the previously recorded canonical request
    for the same idempotency key, supplied by a trusted store.

    Durable idempotency reservation and atomic execution are deliberately
    outside ZB-2A and must be implemented before live movement.
    """
    current = _utc(now)
    expiry = _utc(request.expires_at)
    auth_expiry = _utc(authorization.valid_until)
    seen = _utc(snapshot.last_seen_at) if snapshot.last_seen_at else None

    if (
        current is None
        or not request.command_id
        or not request.command_id.strip()
        or not request.idempotency_key
        or not request.idempotency_key.strip()
        or not request.gateway_serial
        or not request.gateway_serial.strip()
        or request.command not in {"open", "close", "stop"}
        or type(request.rs485_address) is not int
        or not 0 <= request.rs485_address <= 255
        or type(request.generation) is not int
        or request.generation < 1
        or max_status_age_seconds <= 0
        or max_command_ttl_seconds <= 0
    ):
        return _decision(request, DecisionCode.INVALID_REQUEST)

    if (
        expiry is None
        or expiry <= current
        or expiry > current + timedelta(seconds=max_command_ttl_seconds)
    ):
        return _decision(request, DecisionCode.INVALID_EXPIRY)

    if (
        not authorization.trusted
        or not authorization.subject_id
        or not authorization.subject_id.strip()
        or auth_expiry is None
        or auth_expiry <= current
    ):
        return _decision(request, DecisionCode.UNAUTHORIZED)

    if (
        authorization.gateway_serial != request.gateway_serial
        or authorization.rs485_address != request.rs485_address
        or authorization.permitted_command != request.command
    ):
        return _decision(request, DecisionCode.UNAUTHORIZED)

    if (
        snapshot.gateway_serial != request.gateway_serial
        or snapshot.rs485_address != request.rs485_address
    ):
        return _decision(request, DecisionCode.TARGET_MISMATCH)

    if snapshot.generation != request.generation:
        return _decision(request, DecisionCode.SESSION_MISMATCH)

    if not snapshot.connected or not snapshot.online:
        return _decision(request, DecisionCode.OFFLINE)

    if (
        seen is None
        or seen > current
        or (current - seen).total_seconds() > max_status_age_seconds
    ):
        return _decision(request, DecisionCode.STALE_STATUS)

    if snapshot.barrier_state not in {
        "open", "closed", "opening", "closing", "stopped"
    }:
        return _decision(request, DecisionCode.UNKNOWN_STATE)

    if snapshot.barrier_state in {"opening", "closing", "stopped"}:
        return _decision(request, DecisionCode.UNSAFE_STATE)

    fingerprint = (
        authorization.subject_id,
        request.gateway_serial,
        request.rs485_address,
        request.command,
        request.generation,
        expiry.isoformat(),
    )

    if (
        existing_fingerprint is not None
        and existing_fingerprint != fingerprint
    ):
        return _decision(request, DecisionCode.IDEMPOTENCY_CONFLICT)

    return _decision(request, DecisionCode.ADMITTED)
