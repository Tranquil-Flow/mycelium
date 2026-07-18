"""Deterministic trace enumeration, replay, encoding, and minimization."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable, Sequence

from .model import Action, Authority, GatewayModel, ModelState, StepResult


TAIL_ACTIONS = (
    "token_next",
    "token_exact_replay",
    "token_conflicting_replay",
    "token_future",
    "cancel",
    "complete",
    "disconnect",
    "reconnect",
    "revoke",
    "epoch_change",
    "path_change",
    "evidence_change",
)

RACE_ACTIONS = (
    "token_next",
    "cancel",
    "revoke",
    "disconnect",
    "complete",
)


def _symbolic_action(name: str) -> Action:
    return Action(name)


def generate_bounded_traces(
    current: Authority,
    maximum_tail_depth: int = 2,
) -> tuple[tuple[Action, ...], ...]:
    if maximum_tail_depth < 0:
        raise ValueError("invalid_trace_depth")
    admit = Action.admit(current, payload="fixture-prompt")
    tails = tuple(_symbolic_action(name) for name in TAIL_ACTIONS)
    traces: list[tuple[Action, ...]] = []
    for depth in range(maximum_tail_depth + 1):
        traces.extend((admit, *tail) for tail in itertools.product(tails, repeat=depth))
    traces.extend((action,) for action in tails)
    return tuple(traces)


def generate_race_traces(current: Authority) -> tuple[tuple[Action, ...], ...]:
    admit = Action.admit(current, payload="fixture-prompt")
    actions = tuple(_symbolic_action(name) for name in RACE_ACTIONS)
    return tuple((admit, *ordering) for ordering in itertools.permutations(actions))


def _materialize(action: Action, state: ModelState) -> Action:
    if action.kind == "token_next":
        return Action.token(state.expected_token_index, f"token-{state.expected_token_index}")
    if action.kind == "token_exact_replay":
        if not state.token_payloads:
            return Action.token(0, "token-0")
        index = len(state.token_payloads) - 1
        return Action.token(index, state.token_payloads[index])
    if action.kind == "token_conflicting_replay":
        if not state.token_payloads:
            return Action.token(0, "token-0")
        index = len(state.token_payloads) - 1
        return Action.token(index, f"conflict-{index}")
    if action.kind == "token_future":
        return Action.token(state.expected_token_index + 1, "future-token")
    if action.kind == "cancel":
        return Action.cancel()
    if action.kind == "complete":
        return Action.complete()
    if action.kind == "disconnect":
        return Action.disconnect()
    if action.kind == "reconnect":
        return Action.reconnect(state.acknowledged_through)
    if action.kind == "revoke":
        return Action.change_authority("ready", False)
    if action.kind == "epoch_change":
        return Action.change_authority("epoch", state.current.epoch + 1)
    if action.kind == "path_change":
        return Action.change_authority("path", f"{state.current.path}-changed")
    if action.kind == "evidence_change":
        return Action.change_authority(
            "evidence", f"{state.current.evidence}-changed"
        )
    return action


def run_trace(model: GatewayModel, trace: Sequence[Action]) -> StepResult:
    state = model.initial_state
    result = StepResult(state, "initial")
    for action in trace:
        result = model.apply(_materialize(action, result.state), state=result.state)
    return result


def minimize_trace(
    trace: Sequence[Action],
    failure: Callable[[tuple[Action, ...]], bool],
) -> tuple[Action, ...]:
    candidate = tuple(trace)
    if not failure(candidate):
        raise ValueError("trace_does_not_fail")
    changed = True
    while changed:
        changed = False
        for index in range(len(candidate)):
            reduced = candidate[:index] + candidate[index + 1 :]
            if failure(reduced):
                candidate = reduced
                changed = True
                break
    return candidate


def _safe_action_document(action: Action) -> dict[str, object]:
    document: dict[str, object] = {"kind": action.kind}
    if action.token_index is not None:
        document["token_index"] = action.token_index
    if action.field is not None:
        document["field"] = action.field
    if action.cursor is not None:
        document["cursor"] = action.cursor
    if action.authority is not None:
        document["authority"] = {
            "deployment": action.authority.deployment,
            "epoch": action.authority.epoch,
            "path": action.authority.path,
            "evidence": action.authority.evidence,
            "qualification": action.authority.qualification,
            "ready": action.authority.ready,
        }
    return document


def trace_to_json(trace: Iterable[Action]) -> str:
    return json.dumps(
        [_safe_action_document(action) for action in trace],
        sort_keys=True,
        separators=(",", ":"),
    )
