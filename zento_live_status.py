"""Read-only Zento controller status via authenticated local IPC.

This module never sends RS485 commands or executes barrier movement.
"""

import os

from zento_local_ipc import ipc_request

IPC_PATH = "/run/nabospot-zento/status.sock"

GATEWAY_SERIAL = "6003791910"
RS485_ADDRESS = 0
TLS_IDENTITY = "nabospot-trb145-pilot-01"


def read_live_status(gateway_serial, rs485_address):
    serial = str(gateway_serial).strip()

    try:
        address = int(rs485_address)
    except (TypeError, ValueError):
        address = -1

    result = {
        "connected": False,
        "online": False,
        "barrier_state": "unknown",
        "last_seen_at": None,
        "status_source": "unavailable",
        "live_status_available": False,
        "physical_execution_enabled": False,
    }

    if serial != GATEWAY_SERIAL or address != RS485_ADDRESS:
        result["status_source"] = "unmapped_gateway"
        return result

    try:
        response = ipc_request(
            IPC_PATH,
            {
                "type": "session_status",
                "identity": TLS_IDENTITY,
            },
            timeout=2.0,
        )
    except (OSError, TimeoutError, ValueError, RuntimeError):
        return result

    if not isinstance(response, dict):
        return result

    if response.get("accepted") is not True:
        return result

    if response.get("source") != "listener_session_registry":
        return result

    if response.get("identity") != TLS_IDENTITY:
        return result

    if response.get("transmitted") is not False:
        return result

    if response.get("physical_execution_enabled") is not False:
        return result

    connected = response.get("connected") is True
    online = connected and response.get("online") is True

    state = response.get("barrier_state")

    if state not in {"open", "closed", "stopped", "unknown"}:
        state = "unknown"

    if not online:
        state = "unknown"

    result.update({
        "connected": connected,
        "online": online,
        "barrier_state": state,
        "last_seen_at": (
            response.get("last_seen_at") if online else None
        ),
        "status_source": "listener_session_registry",
        "live_status_available": online,
    })

    return result
