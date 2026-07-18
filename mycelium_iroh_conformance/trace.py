"""Stable bounded traces, replay records, JSON, and deletion-1 reduction.

Enumeration uses tuple order and :mod:`itertools` product order only.  It never
iterates a set or samples randomness, so trace indexes are reproducible across
processes and platforms.  The default prefix creates a running adapter; each
alphabet action is also emitted alone to exercise pre-start rejection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import itertools
import json
from typing import Callable, Iterable, Sequence, TypeVar

from .model import AdapterAction, IrohAdapterModel, ModelState, Transition

TraceAction = AdapterAction

STARTUP_ACTIONS: tuple[TraceAction, ...] = (
    TraceAction("bind_router"),
    TraceAction("start"),
)

# Stable action-class order.  Repetition in a Cartesian tail creates queue
# exhaustion and cancellation/confirmation races without special randomness.
DEFAULT_ACTIONS: tuple[TraceAction, ...] = tuple(
    TraceAction(name)
    for name in (
        "close",
        "restart",
        "fatal_receive",
        "queue_send",
        "send_begin",
        "send_disconnect",
        "send_reconnect_complete",
        "send_reconnect_fail",
        "send_confirmed",
        "send_failed",
        "delay_confirmation",
        "lose_confirmation",
        "deadline",
        "finish_cancelled",
        "receive_disconnect",
        "receive_reconnect_complete",
        "receive_reconnect_fail",
        "rotate_peer",
        "receive_frame",
        "dispatch",
        "dispatch_begin",
        "dispatch_complete",
        "dispatch_fail",
        "ack_begin",
        "ack_success",
        "delayed_ack",
        "lost_ack",
        "receive_exact_replay",
        "receive_collision",
        "receive_stale_sequence",
        "receive_future_sequence",
        "receive_stale_generation",
        "receive_future_generation",
        "receive_malformed_frame",
        "receive_truncated_frame",
    )
)


@dataclass(frozen=True)
class ReferenceTraceRun:
    """Immutable state/disposition record for one reference replay."""

    initial_state: ModelState
    actions: tuple[TraceAction, ...]
    states: tuple[ModelState, ...]
    transitions: tuple[Transition, ...]

    @property
    def final_state(self) -> ModelState:
        return self.states[-1]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.transitions)

    @property
    def accepted(self) -> tuple[bool, ...]:
        return tuple(item.accepted for item in self.transitions)

    @property
    def mutated(self) -> tuple[bool, ...]:
        return tuple(item.mutated for item in self.transitions)


TraceRun = ReferenceTraceRun


def generate_bounded_traces(
    *,
    maximum_tail_depth: int = 3,
    actions: Sequence[TraceAction] = DEFAULT_ACTIONS,
    startup: Sequence[TraceAction] = STARTUP_ACTIONS,
) -> tuple[tuple[TraceAction, ...], ...]:
    """Enumerate bounded running traces and one pre-start probe per action.

    For an alphabet of size ``n`` the result has
    ``sum(n**d for d in 0..maximum_tail_depth) + n`` traces.  The first trace is
    exactly ``startup``.  Duplicate actions are rejected because they would
    make distinct trace indexes encode the same counterexample.
    """

    if maximum_tail_depth < 0:
        raise ValueError("maximum_tail_depth_must_be_non_negative")
    alphabet = tuple(actions)
    prefix = tuple(startup)
    if len(set(alphabet)) != len(alphabet):
        raise ValueError("duplicate_trace_action")
    if len(set(prefix)) != len(prefix):
        raise ValueError("duplicate_startup_action")
    traces: list[tuple[TraceAction, ...]] = []
    for depth in range(maximum_tail_depth + 1):
        for tail in itertools.product(alphabet, repeat=depth):
            traces.append(prefix + tail)
    traces.extend((action,) for action in alphabet)
    return tuple(traces)


def run_reference_trace(
    machine: IrohAdapterModel,
    trace: Iterable[TraceAction],
    *,
    initial_state: ModelState | None = None,
) -> ReferenceTraceRun:
    """Replay actions against the pure model without mutating caller values."""

    actions = tuple(trace)
    state = machine.initial_state() if initial_state is None else initial_state
    initial = state
    states = [state]
    transitions: list[Transition] = []
    for action in actions:
        transition = machine.apply(state, action)
        state = transition.state
        transitions.append(transition)
        states.append(state)
    return ReferenceTraceRun(
        initial_state=initial,
        actions=actions,
        states=tuple(states),
        transitions=tuple(transitions),
    )


T = TypeVar("T")


def minimize_trace(
    trace: Sequence[T],
    reproduces: Callable[[tuple[T, ...]], bool],
) -> tuple[T, ...]:
    """Return a deterministic deletion-1-minimal reproducing subsequence.

    Candidates are tried from index zero upward.  After a successful deletion,
    scanning restarts at zero; therefore both the result and predicate call
    order are stable.  The returned trace reproduces, but deleting any one of
    its remaining actions does not.
    """

    current = tuple(trace)
    if not reproduces(current):
        raise ValueError("trace_does_not_reproduce")
    while True:
        for index in range(len(current)):
            candidate = current[:index] + current[index + 1 :]
            if reproduces(candidate):
                current = candidate
                break
        else:
            return current


def deletion_one_minimize(
    trace: Sequence[T],
    reproduces: Callable[[tuple[T, ...]], bool],
) -> tuple[T, ...]:
    """Explicitly named alias for :func:`minimize_trace`."""

    return minimize_trace(trace, reproduces)


_ACTION_DEFAULTS = {
    field.name: field.default
    for field in fields(AdapterAction)
    if field.name != "name"
}
_ACTION_FIELDS = frozenset({"name", *_ACTION_DEFAULTS})


def _action_object(action: TraceAction) -> dict[str, object]:
    raw = asdict(action)
    return {
        key: value
        for key, value in raw.items()
        if key == "name" or value != _ACTION_DEFAULTS[key]
    }


def trace_to_json(trace: Iterable[TraceAction]) -> str:
    """Encode actions as canonical compact JSON with default fields omitted."""

    return json.dumps(
        [_action_object(action) for action in trace],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def trace_from_json(encoded: str) -> tuple[TraceAction, ...]:
    """Strict inverse of :func:`trace_to_json` for direct reproduction."""

    value = json.loads(encoded)
    if not isinstance(value, list):
        raise ValueError("trace_json_must_be_an_array")
    actions: list[TraceAction] = []
    for item in value:
        if not isinstance(item, dict) or "name" not in item:
            raise ValueError("trace_action_must_be_an_object_with_name")
        if not set(item) <= _ACTION_FIELDS:
            raise ValueError("trace_action_has_unknown_fields")
        actions.append(TraceAction(**item))
    return tuple(actions)


def state_to_json(state: ModelState) -> str:
    """Canonical JSON snapshot of every observable and model context field."""

    return json.dumps(
        asdict(state),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


# Short spellings useful to small external harnesses.
generate_traces = generate_bounded_traces
run_trace = run_reference_trace


__all__ = [
    "DEFAULT_ACTIONS",
    "ReferenceTraceRun",
    "STARTUP_ACTIONS",
    "TraceAction",
    "TraceRun",
    "deletion_one_minimize",
    "generate_bounded_traces",
    "generate_traces",
    "minimize_trace",
    "run_reference_trace",
    "run_trace",
    "state_to_json",
    "trace_from_json",
    "trace_to_json",
]
