"""Deterministic privacy-reduced lock-order enforcement for A4 runtime scopes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import threading
from typing import Iterator


LOCK_ORDER = (
    "authority",
    "deployment",
    "session",
    "request",
    "path",
    "placement",
    "transport",
    "detector",
)
MAXIMUM_LOCK_ORDER_INCIDENTS = 256
_RANKS = {name: rank for rank, name in enumerate(LOCK_ORDER)}


class LockOrderViolation(RuntimeError):
    """Raised before a caller can enter an inverted runtime scope."""

    def __init__(self, code: str = "lock_order_inversion") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LockOrderIncident:
    sequence: int
    owner_digest: str
    held_scope: str
    held_rank: int
    requested_scope: str
    requested_rank: int
    outcome: str = "rejected_before_physical_command"


class LockOrderDetector:
    """Track scoped acquisitions and reject inversions before blocking work.

    The detector never stores a request, peer, hostname, or thread name. Owner
    identities are reduced to bounded digests before entering the incident ledger.
    It is intentionally independent from the protected locks, so recording an
    incident cannot create a second lock dependency.
    """

    def __init__(self, *, maximum_incidents: int = MAXIMUM_LOCK_ORDER_INCIDENTS) -> None:
        if (
            type(maximum_incidents) is not int
            or not 1 <= maximum_incidents <= MAXIMUM_LOCK_ORDER_INCIDENTS
        ):
            raise ValueError("invalid_maximum_lock_order_incidents")
        self._maximum_incidents = maximum_incidents
        self._local = threading.local()
        self._incident_lock = threading.Lock()
        self._incidents: list[LockOrderIncident] = []
        self._sequence = 0

    @staticmethod
    def _owner_digest(owner_id: str) -> str:
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
            raise ValueError("invalid_lock_owner_id")
        return "sha256:" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()

    def _stack(self) -> list[tuple[str, int, str]]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack

    @contextmanager
    def scope(self, scope: str, *, owner_id: str) -> Iterator[None]:
        if scope not in _RANKS:
            raise ValueError("invalid_lock_scope")
        owner_digest = self._owner_digest(owner_id)
        requested_rank = _RANKS[scope]
        stack = self._stack()
        if stack and requested_rank <= stack[-1][1]:
            held_scope, held_rank, _held_owner = stack[-1]
            with self._incident_lock:
                self._sequence += 1
                self._incidents.append(
                    LockOrderIncident(
                        sequence=self._sequence,
                        owner_digest=owner_digest,
                        held_scope=held_scope,
                        held_rank=held_rank,
                        requested_scope=scope,
                        requested_rank=requested_rank,
                    )
                )
                if len(self._incidents) > self._maximum_incidents:
                    del self._incidents[: -self._maximum_incidents]
            raise LockOrderViolation()
        stack.append((scope, requested_rank, owner_digest))
        try:
            yield
        finally:
            popped = stack.pop()
            if popped != (scope, requested_rank, owner_digest):
                stack.clear()
                raise LockOrderViolation("lock_order_stack_corrupted")

    def incidents(self) -> tuple[LockOrderIncident, ...]:
        with self._incident_lock:
            return tuple(self._incidents)


__all__ = [
    "LOCK_ORDER",
    "LockOrderDetector",
    "LockOrderIncident",
    "LockOrderViolation",
    "MAXIMUM_LOCK_ORDER_INCIDENTS",
]
