"""Bridge authentication and read-only Zento integration metadata.

No network communication or physical command execution.
"""

import hmac


def bridge_key_valid(provided, configured):
    """Fail closed if the configured credential is missing or unsafe."""
    if not isinstance(configured, str):
        return False

    if not configured or configured == "change-me":
        return False

    if not isinstance(provided, str) or not provided:
        return False

    return hmac.compare_digest(provided, configured)


def integration_status(*, mock_mode):
    """Report integration capability without claiming live telemetry."""
    return {
        "provider": "zento",
        "bridge_mode": "mock" if mock_mode else "unimplemented",
        "live_status_available": False,
        "physical_execution_enabled": False,
        "command_execution": "simulation_only" if mock_mode else "disabled",
        "source": "bridge_configuration",
    }
