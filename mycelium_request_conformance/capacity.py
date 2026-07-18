"""Deterministic session-capacity lifecycle reference model."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SessionRecord:
    request_id: str
    terminal: str | None = None
    attached: bool = False
    backend_started: bool = False
    worker_done: bool = False
    cleanup_count: int = 0


@dataclass(frozen=True)
class CapacityState:
    sessions: tuple[SessionRecord, ...] = ()
    runtime_starts: int = 0
    admission_rejections: int = 0


@dataclass(frozen=True)
class CapacityResult:
    state: CapacityState
    code: str


class CapacityModel:
    """Pure model of bounded session admission and reachable cleanup states."""

    _TERMINALS = frozenset({"completed", "cancelled", "failed"})

    def __init__(self, *, max_sessions: int) -> None:
        if max_sessions < 1:
            raise ValueError("invalid_max_sessions")
        self.max_sessions = max_sessions

    @staticmethod
    def initial_state() -> CapacityState:
        return CapacityState()

    @staticmethod
    def replace(state: CapacityState, record: SessionRecord) -> CapacityState:
        """Replace one known record; retained for deterministic state probes."""
        sessions = tuple(
            record if session.request_id == record.request_id else session
            for session in state.sessions
        )
        if sessions == state.sessions and not any(
            session.request_id == record.request_id for session in state.sessions
        ):
            raise KeyError("unknown_request")
        return replace(state, sessions=sessions)

    @staticmethod
    def _record(state: CapacityState, request_id: str) -> SessionRecord | None:
        return next(
            (session for session in state.sessions if session.request_id == request_id),
            None,
        )

    @classmethod
    def _transition(
        cls,
        state: CapacityState,
        request_id: str,
        **changes: object,
    ) -> CapacityState:
        record = cls._record(state, request_id)
        if record is None:
            raise KeyError("unknown_request")
        return cls.replace(state, replace(record, **changes))

    def admit(self, state: CapacityState, request_id: str) -> CapacityResult:
        sessions = state.sessions
        if len(sessions) >= self.max_sessions:
            eviction_index = next(
                (
                    index
                    for index, session in enumerate(sessions)
                    if session.terminal is not None
                    and not session.attached
                    and (
                        session.worker_done
                        or session.terminal in {"completed", "failed"}
                    )
                ),
                None,
            )
            if eviction_index is None:
                return CapacityResult(
                    replace(
                        state,
                        admission_rejections=state.admission_rejections + 1,
                    ),
                    "gateway_capacity_exhausted",
                )
            sessions = sessions[:eviction_index] + sessions[eviction_index + 1 :]
        room_made = replace(state, sessions=sessions)
        if any(session.request_id == request_id for session in sessions):
            return CapacityResult(room_made, "duplicate_request_id")
        admitted = SessionRecord(request_id=request_id)
        return CapacityResult(
            replace(room_made, sessions=sessions + (admitted,)),
            "admitted",
        )

    def start(self, state: CapacityState, request_id: str) -> CapacityResult:
        record = self._record(state, request_id)
        if record is None:
            return CapacityResult(state, "unknown_request")
        if record.backend_started:
            return CapacityResult(state, "already_started")
        if record.terminal is not None or record.worker_done:
            return CapacityResult(state, "already_terminal")
        return CapacityResult(
            replace(
                self._transition(state, request_id, backend_started=True),
                runtime_starts=state.runtime_starts + 1,
            ),
            "started",
        )

    def attach(self, state: CapacityState, request_id: str) -> CapacityResult:
        record = self._record(state, request_id)
        if record is None:
            return CapacityResult(state, "unknown_request")
        if record.attached:
            return CapacityResult(state, "stream_already_attached")
        return CapacityResult(
            self._transition(state, request_id, attached=True),
            "attached",
        )

    def detach(self, state: CapacityState, request_id: str) -> CapacityResult:
        record = self._record(state, request_id)
        if record is None:
            return CapacityResult(state, "unknown_request")
        if not record.attached:
            return CapacityResult(state, "already_detached")
        return CapacityResult(
            self._transition(state, request_id, attached=False),
            "detached",
        )

    def terminate(
        self,
        state: CapacityState,
        request_id: str,
        terminal: str,
    ) -> CapacityResult:
        record = self._record(state, request_id)
        if record is None:
            return CapacityResult(state, "unknown_request")
        if terminal not in self._TERMINALS:
            return CapacityResult(state, "invalid_terminal")
        if record.terminal is not None:
            return CapacityResult(state, "already_terminal")
        return CapacityResult(
            self._transition(state, request_id, terminal=terminal),
            terminal,
        )

    def finish_worker(self, state: CapacityState, request_id: str) -> CapacityResult:
        record = self._record(state, request_id)
        if record is None:
            return CapacityResult(state, "unknown_request")
        if record.worker_done:
            return CapacityResult(state, "worker_already_done")
        if record.terminal is None:
            return CapacityResult(state, "worker_not_terminal")
        return CapacityResult(
            self._transition(
                state,
                request_id,
                worker_done=True,
                cleanup_count=record.cleanup_count + int(record.backend_started),
            ),
            "worker_finished",
        )
