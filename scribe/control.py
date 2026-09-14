"""Two-way control state: pending approvals and the reply queue.

Both features are opt-in and default off, because both spend something real. A
held ``PermissionRequest`` hook means the terminal shows nothing until we let
go — the approval dialog does not appear while a hook is still running — and an
injected reply is a genuine message in your conversation.

Kept apart from the daemon's transport code so the state machine can be tested
without sockets.
"""

from __future__ import annotations

import threading
import time
import uuid as uuidlib
from dataclasses import dataclass, field


@dataclass
class PendingCall:
    """A tool call waiting on a decision from the browser."""

    call_id: str
    session_id: str
    tool_name: str
    tool_input: dict
    permission_reason: str = ""
    created: float = field(default_factory=time.time)
    deadline: float = 0.0
    explanation: str = ""
    explanation_tier: int = 0
    token: str = field(default_factory=lambda: uuidlib.uuid4().hex[:12])

    # Set by whoever decides; the waiting hook thread blocks on `event`.
    event: threading.Event = field(default_factory=threading.Event)
    behavior: str = ""  # allow | deny | pass
    updated_input: dict | None = None
    decided_by: str = ""

    def seconds_left(self) -> float:
        return max(0.0, self.deadline - time.time()) if self.deadline else 0.0

    def as_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "token": self.token,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "permission_reason": self.permission_reason,
            "explanation": self.explanation,
            "explanation_tier": self.explanation_tier,
            "created": self.created,
            "seconds_left": round(self.seconds_left(), 1),
            "decided": bool(self.behavior),
            "behavior": self.behavior,
        }


class ControlState:
    """Per-session arming, pending approvals, and queued replies."""

    def __init__(self):
        self._lock = threading.RLock()
        self._pending: dict[str, PendingCall] = {}
        self._armed: dict[str, bool] = {}
        self._queue: dict[str, list[str]] = {}
        self._chain: dict[str, int] = {}
        self._listeners: list = []

    # -- arming ---------------------------------------------------------

    def arm(self, session_id: str, on: bool = True) -> bool:
        with self._lock:
            self._armed[session_id] = bool(on)
            if not on:
                # Releasing control must not strand a hook that is already
                # waiting: hand every pending call straight back to the terminal.
                for call in list(self._pending.values()):
                    if call.session_id == session_id and not call.behavior:
                        self._resolve(call, "pass", None, "disarm")
            return bool(on)

    def is_armed(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._armed.get(session_id))

    # -- approvals ------------------------------------------------------

    def open_call(self, call: PendingCall, wait_s: float) -> PendingCall:
        call.deadline = time.time() + wait_s
        with self._lock:
            # Decided calls are kept a while so a late explanation can still
            # find its card, but not forever.
            cutoff = time.time() - 600
            for stale in [k for k, c in self._pending.items() if c.behavior and c.created < cutoff]:
                self._pending.pop(stale, None)
            self._pending[call.call_id] = call
        return call

    def wait_for(self, call: PendingCall, wait_s: float, still_watching=None) -> tuple[str, dict | None]:
        """Block until a decision, a timeout, or the browser going away.

        Polled in short slices rather than one long wait so the hook is released
        the moment the last viewer disconnects. A reader who closes the tab
        should get their terminal back immediately, not `wait_s` later.
        """
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if call.event.wait(timeout=0.25):
                break
            if still_watching is not None and not still_watching():
                self.resolve(call.call_id, "pass", None, "no-client")
                break
        else:
            self.resolve(call.call_id, "pass", None, "timeout")

        with self._lock:
            self._pending.pop(call.call_id, None)
        return call.behavior or "pass", call.updated_input

    def resolve(self, call_id: str, behavior: str, updated_input=None, by: str = "web") -> bool:
        with self._lock:
            call = self._pending.get(call_id)
            if call is None or call.behavior:
                return False
            self._resolve(call, behavior, updated_input, by)
            return True

    def _resolve(self, call: PendingCall, behavior: str, updated_input, by: str) -> None:
        call.behavior = behavior
        call.updated_input = updated_input
        call.decided_by = by
        call.event.set()

    def pending_for(self, session_id: str) -> list[PendingCall]:
        with self._lock:
            return [
                c
                for c in self._pending.values()
                if c.session_id == session_id and not c.behavior
            ]

    def get_pending(self, call_id: str) -> PendingCall | None:
        with self._lock:
            return self._pending.get(call_id)

    def annotate(self, call_id: str, explanation: str, tier: int = 1) -> PendingCall | None:
        """Attach an explanation that arrived after the card was already shown."""
        with self._lock:
            call = self._pending.get(call_id)
            if call is None:
                return None
            call.explanation = explanation
            call.explanation_tier = tier
            return call

    # -- reply queue ----------------------------------------------------

    def enqueue(self, session_id: str, text: str) -> int:
        text = (text or "").strip()
        if not text:
            return 0
        with self._lock:
            self._queue.setdefault(session_id, []).append(text)
            return len(self._queue[session_id])

    def queued(self, session_id: str) -> list[str]:
        with self._lock:
            return list(self._queue.get(session_id, []))

    def drop_queued(self, session_id: str, index: int) -> bool:
        with self._lock:
            items = self._queue.get(session_id) or []
            if 0 <= index < len(items):
                items.pop(index)
                return True
            return False

    def take_queued(self, session_id: str, max_chain: int, stop_hook_active: bool) -> str:
        """Pop everything queued, or return "" if we must not inject now.

        Two guards. ``stop_hook_active`` is Claude Code telling us this Stop was
        itself caused by a hook block — continuing there is how you build an
        infinite loop. ``max_chain`` bounds how many times in a row we may
        extend one turn even with fresh messages each time; the count resets
        when a real prompt arrives from the terminal.
        """
        if stop_hook_active:
            return ""
        with self._lock:
            items = self._queue.get(session_id) or []
            if not items:
                return ""
            if self._chain.get(session_id, 0) >= max_chain:
                return ""
            self._queue[session_id] = []
            self._chain[session_id] = self._chain.get(session_id, 0) + 1
            return "\n\n".join(items)

    def reset_chain(self, session_id: str) -> None:
        with self._lock:
            self._chain[session_id] = 0

    def chain_count(self, session_id: str) -> int:
        with self._lock:
            return self._chain.get(session_id, 0)

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._armed.pop(session_id, None)
            self._queue.pop(session_id, None)
            self._chain.pop(session_id, None)
            for call in list(self._pending.values()):
                if call.session_id == session_id and not call.behavior:
                    self._resolve(call, "pass", None, "session-end")


WEB_MESSAGE_PREFIX = (
    "[Message from the user, sent from the scribe web console rather than the "
    "terminal. Treat it exactly as you would a typed message.]"
)


def format_injection(text: str) -> str:
    return f"{WEB_MESSAGE_PREFIX}\n\n{text}"
