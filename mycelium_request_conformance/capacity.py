"""Deterministic session-capacity reference model."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SessionRecord:
    request_id: str
    terminal: str | None = None
    attached: bool = False
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
    def __init__(self, *, max_sessions: int) -> None:
        if max_sessions < 1:
            raise ValueError("invalid_max_sessions")
        self.max_sessions = max_sessions

    @staticmethod
    def initial_state() -> CapacityState:
        return CapacityState()

    @staticmethod
    def replace(state: CapacityState, record: SessionRecord) -> CapacityState:
        sessions = tuple(
            record if session.request_id == record.request_id else session
            for session in state.sessions
        )
        if sessions == state.sessions and not any(
            session.request_id == record.request_id for session in state.sessions
        ):
            raise KeyError("unknown_request")
        return replace(state, sessions=sessions)

    def admit(self, state: CapacityState, request_id: str) -> CapacityResult:
        if any(session.request_id == request_id for session in state.sessions):
            return CapacityResult(state, "duplicate_request_id")
        sessions = state.sessions
        if len(sessions) >= self.max_sessions:
            eviction_index = next(
                (
                    index
                    for index, session in enumerate(sessions)
                    if session.terminal is not None
                    and not session.attached
                    and session.cleanup_count == 1
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
        admitted = SessionRecord(request_id=request_id)
        return CapacityResult(
            replace(
                state,
                sessions=sessions + (admitted,),
                runtime_starts=state.runtime_starts + 1,
            ),
            "admitted",
        )
