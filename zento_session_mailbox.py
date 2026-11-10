"""ZB-2C2A: Offline session-owned command mailbox.

No sockets, RS485 encoding, transport writes, or physical actuation.
The owning session worker alone may claim an intent.

This is a process-local simulation component, not a production
cross-process execution fence.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CommandIntent:
    command_id: str
    identity: str
    generation: int
    expires_at: float


@dataclass(frozen=True)
class MailboxResult:
    accepted: bool
    code: str
    command_id: str | None = None
    transmitted: bool = False


class SessionMailbox:
    def __init__(
        self,
        *,
        stale_after: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if stale_after <= 0:
            raise ValueError("stale_after must be positive")

        self._clock = clock
        self._stale_after = stale_after
        self._lock = threading.Lock()
        self._sessions = {}
        self._pending = {}
        self._claimed = set()

    def replace(self, identity: str, generation: int):
        """Activate a new generation and invalidate pending intents."""
        if not identity or type(generation) is not int or generation < 1:
            raise ValueError("Invalid session identity or generation")

        with self._lock:
            previous = self._sessions.get(identity)
            if previous is not None and generation <= previous[0]:
                return MailboxResult(False, "non_increasing_generation")

            self._sessions[identity] = (generation, None)
            self._pending[identity] = deque()
            return MailboxResult(True, "session_replaced")

    def release(self, identity: str, generation: int):
        """Only the active generation can release its session."""
        with self._lock:
            session = self._sessions.get(identity)
            if session is None or session[0] != generation:
                return MailboxResult(False, "not_session_owner")

            del self._sessions[identity]
            self._pending.pop(identity, None)
            return MailboxResult(True, "session_released")

    def record_status(self, identity: str, generation: int):
        """Record fresh controller evidence from the owning worker."""
        with self._lock:
            session = self._sessions.get(identity)
            if session is None or session[0] != generation:
                return MailboxResult(False, "not_session_owner")

            self._sessions[identity] = (
                generation, self._clock()
            )
            return MailboxResult(True, "status_recorded")

    def _fresh(self, identity, generation):
        session = self._sessions.get(identity)
        if session is None or session[0] != generation:
            return False
        seen = session[1]
        return (
            seen is not None
            and 0 <= self._clock() - seen <= self._stale_after
        )

    def submit(self, intent: CommandIntent):
        """Accept an intent only for an active, fresh generation."""
        if (
            not isinstance(intent, CommandIntent)
            or not intent.command_id
            or not intent.identity
            or type(intent.generation) is not int
            or intent.generation < 1
            or type(intent.expires_at) not in (int, float)
        ):
            return MailboxResult(False, "invalid_intent")

        with self._lock:
            if not self._fresh(intent.identity, intent.generation):
                return MailboxResult(False, "session_not_fresh")

            if not self._clock() < intent.expires_at:
                return MailboxResult(False, "intent_expired")

            if intent.command_id in self._claimed:
                return MailboxResult(False, "already_claimed")

            if any(
                item.command_id == intent.command_id
                for queue in self._pending.values()
                for item in queue
            ):
                return MailboxResult(False, "already_pending")

            self._pending[intent.identity].append(intent)
            return MailboxResult(
                True, "queued", intent.command_id
            )

    def claim(self, identity: str, generation: int):
        """Simulation-only claim by the session owner.

        Claiming never constructs or sends a command frame.
        """
        with self._lock:
            session = self._sessions.get(identity)
            if session is None or session[0] != generation:
                return MailboxResult(False, "not_session_owner")

            if not self._fresh(identity, generation):
                self._pending[identity].clear()
                return MailboxResult(False, "session_not_fresh")

            queue = self._pending[identity]
            while queue:
                intent = queue.popleft()

                if intent.generation != generation:
                    continue
                if not self._clock() < intent.expires_at:
                    continue
                if intent.command_id in self._claimed:
                    continue

                self._claimed.add(intent.command_id)
                return MailboxResult(
                    True, "simulated_claim", intent.command_id
                )

            return MailboxResult(False, "no_valid_intent")

    def pending_count(self, identity: str):
        with self._lock:
            return len(self._pending.get(identity, ()))
