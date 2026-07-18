"""Pure-stdlib bounded traces and deterministic counterexample reduction."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence, TypeVar

from mycelium_conformance.router_model import (
    ModelEvent,
    ModelState,
    RouterModel,
)


@dataclass(frozen=True, order=True)
class TraceAction:
    """Symbolic action resolved relative to current request state."""

    name: str


DEFAULT_ACTIONS = tuple(
    TraceAction(name)
    for name in (
        "duplicate_admit",
        "token_next",
        "token_exact_replay",
        "token_conflicting_replay",
        "token_future_sequence",
        "token_stale_attempt",
        "token_future_attempt",
        "token_non_final",
        "token_off_path",
        "failure_current",
        "failure_stale_sequence",
        "failure_future_attempt",
        "failure_non_owner",
        "failure_off_path",
        "failure_recovery_fails",
        "cancel",
    )
)


@dataclass(frozen=True)
class ReferenceTraceRun:
    """Stable replay record: one disposition and state per symbolic action."""

    initial_state: ModelState
    states: tuple[ModelState, ...]
    codes: tuple[str, ...]
    accepted: tuple[bool, ...]
    resolved_events: tuple[tuple[ModelEvent, ...], ...]

    @property
    def final_state(self) -> ModelState:
        return self.states[-1]


def generate_bounded_traces(
    *,
    maximum_tail_depth: int = 3,
    actions: Sequence[TraceAction] = DEFAULT_ACTIONS,
) -> tuple[tuple[TraceAction, ...], ...]:
    """Enumerate all admitted action products plus one pre-admission probe each.

    Default bound yields 4,385 traces with maximum depth four.  Ordering follows
    ``DEFAULT_ACTIONS`` and ``itertools.product``; no hash iteration participates.
    """

    if maximum_tail_depth < 0:
        raise ValueError("maximum_tail_depth_must_be_non_negative")
    alphabet = tuple(actions)
    if len(set(alphabet)) != len(alphabet):
        raise ValueError("duplicate_trace_action")
    admitted = TraceAction("admit")
    traces: list[tuple[TraceAction, ...]] = []
    for depth in range(maximum_tail_depth + 1):
        for tail in itertools.product(alphabet, repeat=depth):
            traces.append((admitted, *tail))
    traces.extend((action,) for action in alphabet)
    return tuple(traces)


def run_reference_trace(
    machine: RouterModel,
    trace: Iterable[TraceAction],
) -> ReferenceTraceRun:
    """Resolve and run one symbolic trace against the independent automaton."""

    state = machine.initial_state()
    initial = state
    states = [state]
    codes: list[str] = []
    accepted: list[bool] = []
    resolved: list[tuple[ModelEvent, ...]] = []

    for action in trace:
        events = _resolve(action, state)
        action_code = "accepted"
        action_accepted = True
        for event in events:
            transition = machine.apply(state, event)
            state = transition.state
            action_code = transition.code
            action_accepted = transition.accepted
            if not transition.accepted:
                break
        states.append(state)
        codes.append(action_code)
        accepted.append(action_accepted)
        resolved.append(events)

    return ReferenceTraceRun(
        initial_state=initial,
        states=tuple(states),
        codes=tuple(codes),
        accepted=tuple(accepted),
        resolved_events=tuple(resolved),
    )


def _resolve(action: TraceAction, state: ModelState) -> tuple[ModelEvent, ...]:
    name = action.name
    if name == "admit":
        if state.phase != "NEW":
            return (ModelEvent("ADMIT"),)
        return (
            ModelEvent("ADMIT"),
            ModelEvent("BEGIN_PREFILL", path_id="path-0", path_attempt=0),
            ModelEvent("LOCK", path_id="path-0", path_attempt=0),
            ModelEvent("PREFILL_COMPLETE", path_id="path-0", path_attempt=0),
        )
    if name == "duplicate_admit":
        if state.phase == "NEW":
            return (
                ModelEvent("ADMIT"),
                ModelEvent("BEGIN_PREFILL", path_id="path-0", path_attempt=0),
                ModelEvent("LOCK", path_id="path-0", path_attempt=0),
                ModelEvent("PREFILL_COMPLETE", path_id="path-0", path_attempt=0),
            )
        return (ModelEvent("ADMIT"),)
    if name == "cancel":
        return (ModelEvent("CANCEL"),)

    path_attempt = max(state.path_attempt, 0)
    path_id = state.path_id or f"path-{path_attempt}"
    sequence = state.next_sequence

    if name.startswith("token_"):
        peer = "final"
        token_id = 101 + sequence
        if name in {"token_exact_replay", "token_conflicting_replay"}:
            previous = _last_accepted(state, "TOKEN")
            if previous is None:
                event = ModelEvent(
                    "TOKEN",
                    path_id=path_id,
                    path_attempt=path_attempt,
                    sequence=-1,
                    peer=peer,
                    payload=(101,),
                )
            else:
                event = previous
            if name == "token_conflicting_replay":
                original = event.payload[0] if event.payload else 0
                event = replace(event, payload=(original + 1_000,))
            return (event,)
        if name == "token_future_sequence":
            sequence += 1
        elif name == "token_stale_attempt":
            path_attempt -= 1
            path_id = f"path-{path_attempt}"
        elif name == "token_future_attempt":
            path_attempt += 1
            path_id = f"path-{path_attempt}"
        elif name == "token_non_final":
            peer = "non_final"
        elif name == "token_off_path":
            peer = "off_path"
        return (
            ModelEvent(
                "TOKEN",
                path_id=path_id,
                path_attempt=path_attempt,
                sequence=sequence,
                peer=peer,
                payload=(token_id,),
            ),
        )

    if name.startswith("failure_"):
        peer = "path"
        outcome = "success"
        if name == "failure_stale_sequence":
            sequence -= 1
        elif name == "failure_future_attempt":
            path_attempt += 1
            path_id = f"path-{path_attempt}"
        elif name == "failure_non_owner":
            peer = "non_owner"
        elif name == "failure_off_path":
            peer = "off_path"
        elif name == "failure_recovery_fails":
            outcome = "failure"
        return (
            ModelEvent(
                "FAILURE",
                path_id=path_id,
                path_attempt=path_attempt,
                sequence=sequence,
                peer=peer,
                payload=(outcome,),
            ),
        )

    return (ModelEvent(name.upper()),)


def _last_accepted(state: ModelState, kind: str) -> ModelEvent | None:
    return next(
        (event for event in reversed(state.accepted_events) if event.kind == kind),
        None,
    )


T = TypeVar("T")


def minimize_trace(
    trace: Sequence[T],
    disagrees: Callable[[tuple[T, ...]], bool],
) -> tuple[T, ...]:
    """Return deterministic deletion-1-minimal failing subsequence."""

    current = tuple(trace)
    if not disagrees(current):
        raise ValueError("trace_does_not_reproduce")
    changed = True
    while changed:
        changed = False
        for index in range(len(current)):
            candidate = current[:index] + current[index + 1 :]
            if disagrees(candidate):
                current = candidate
                changed = True
                break
    return current


def trace_to_json(trace: Iterable[TraceAction]) -> str:
    """Encode a trace without platform-dependent whitespace or key order."""

    return json.dumps(
        [{"name": action.name} for action in trace],
        sort_keys=True,
        separators=(",", ":"),
    )
