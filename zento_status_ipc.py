"""Read-only Zento session status over authenticated local IPC.

No command reservation, controller socket access, or movement.
"""

from __future__ import annotations


class ZentoStatusIPCHandler:
    def __init__(self, registry, *, expected_identity):
        if not expected_identity:
            raise ValueError("expected_identity_required")

        self.registry = registry
        self.expected_identity = expected_identity

    def __call__(self, message):
        denied = {
            "accepted": False,
            "code": "unsupported_request",
            "transmitted": False,
        }

        if not isinstance(message, dict):
            return denied

        # Strictly allow one read-only request shape.
        if set(message) != {"type", "identity"}:
            return denied

        if message.get("type") != "session_status":
            return denied

        if message.get("identity") != self.expected_identity:
            return {
                "accepted": False,
                "code": "unknown_identity",
                "transmitted": False,
            }

        snapshot = self.registry.snapshot(self.expected_identity)

        return {
            "accepted": True,
            "code": "session_status",
            "source": "listener_session_registry",
            "identity": self.expected_identity,
            "connected": bool(snapshot["connected"]),
            "online": bool(snapshot["online"]),
            "generation": snapshot["generation"],
            "barrier_state": snapshot["barrier_state"],
            "last_seen_at": snapshot["last_seen_at"],
            "physical_execution_enabled": False,
            "transmitted": False,
        }
