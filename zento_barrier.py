"""Zento boom barrier support for the NaboSpot TCP bridge.

Step 5H intentionally starts in mock mode. The backend can call the VPS
bridge end-to-end, while the real RS485/Teltonika implementation can be added
later without changing the backend URLs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class ZentoBarrierState:
    gateway_serial: str
    rs485_address: int
    barrier_state: str = "unknown"
    fault_state: Optional[str] = None
    voltage: Optional[float] = None
    current: Optional[float] = None
    last_command: Optional[str] = None
    last_command_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


class ZentoBarrierProvider:
    """Executes Zento barrier commands.

    Current behavior:
    - mock_mode=True: log and return success, no RS485 traffic.
    - mock_mode=False: raises NotImplementedError until RS485 is implemented.
    """

    def __init__(self, mock_mode: Optional[bool] = None):
        if mock_mode is None:
            raw = os.getenv("ZENTO_BARRIER_MOCK_MODE", "true").strip().lower()
            mock_mode = raw not in ("0", "false", "no", "off")
        self.mock_mode = mock_mode
        self._state: dict[tuple[str, int], ZentoBarrierState] = {}

    def _key(self, gateway_serial: str, rs485_address: int) -> tuple[str, int]:
        return (str(gateway_serial).strip(), int(rs485_address))

    def get_state(self, gateway_serial: str, rs485_address: int) -> ZentoBarrierState:
        key = self._key(gateway_serial, rs485_address)
        if key not in self._state:
            self._state[key] = ZentoBarrierState(
                gateway_serial=key[0],
                rs485_address=key[1],
            )
        return self._state[key]

    def execute(self, gateway_serial: str, rs485_address: int, command: str) -> dict[str, Any]:
        command = command.strip().lower()
        if command not in {"open", "close", "stop", "status"}:
            raise ValueError(f"Unsupported Zento command: {command}")

        state = self.get_state(gateway_serial, rs485_address)
        now = datetime.now(timezone.utc)

        if not self.mock_mode:
            raise NotImplementedError("Real Zento RS485 execution is not implemented yet")

        print(
            f"[ZENTO MOCK] command={command} "
            f"gateway_serial={state.gateway_serial} rs485_address={state.rs485_address}"
        )

        state.last_command = command
        state.last_command_at = now
        state.last_seen_at = now

        if command == "open":
            state.barrier_state = "open"
        elif command == "close":
            state.barrier_state = "closed"
        elif command == "stop":
            state.barrier_state = "stopped"
        elif command == "status":
            if state.barrier_state == "unknown":
                state.barrier_state = "closed"

        state.fault_state = None
        state.raw_payload = {
            "mock": True,
            "command": command,
            "gateway_serial": state.gateway_serial,
            "rs485_address": state.rs485_address,
        }

        return {
            "success": True,
            "mock": True,
            "provider": "zento",
            "gateway_serial": state.gateway_serial,
            "rs485_address": state.rs485_address,
            "command": command,
            "status": "sent",
            "barrier_state": state.barrier_state,
            "fault_state": state.fault_state,
            "voltage": state.voltage,
            "current": state.current,
            "last_seen_at": state.last_seen_at.isoformat(),
            "raw_payload": state.raw_payload,
        }


zento_provider = ZentoBarrierProvider()
