"""Deterministic trace enumeration, replay, encoding, and minimization."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Iterable, Sequence

from .model import Action, Authority, GatewayModel, ModelState, Phase, StepResult


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

_BACKEND_SYMBOLS = frozenset(
    {
        "token_next",
        "token_exact_replay",
        "token_conflicting_replay",
        "token_future",
        "complete",
    }
)
_TERMINAL_PHASES = frozenset({Phase.COMPLETED, Phase.CANCELLED, Phase.FAILED})


def _symbolic_action(name: str) -> Action:
    return Action(name)


def _apply_symbolic(
    model: GatewayModel,
    state: ModelState,
    symbolic: Action,
) -> tuple[Action, ModelState] | None:
    if symbolic.kind in {"token_exact_replay", "token_conflicting_replay"} and not state.token_digests:
        return None
    if symbolic.kind == "disconnect" and not state.attached:
        return None
    if state.phase in _TERMINAL_PHASES and symbolic.kind in _BACKEND_SYMBOLS:
        return None
    concrete = materialize_action(symbolic, state)
    return concrete, model.apply(concrete, state=state).state


def generate_bounded_traces(
    current: Authority,
    maximum_tail_depth: int = 2,
) -> tuple[tuple[Action, ...], ...]:
    """Enumerate unique reachable bounded traces over the declared alphabet."""
    if maximum_tail_depth < 0:
        raise ValueError("invalid_trace_depth")
    admit = Action.admit(current, payload="fixture-prompt")
    start = Action.start()
    base = (admit, start)
    model = GatewayModel(current=current)
    state = model.apply(admit).state
    state = model.apply(start, state=state).state
    traces: list[tuple[Action, ...]] = [base]
    frontier: list[tuple[tuple[Action, ...], ModelState]] = [(base, state)]
    seen_concrete: set[tuple[Action, ...]] = {base}
    tails = tuple(_symbolic_action(name) for name in TAIL_ACTIONS)

    for _depth in range(1, maximum_tail_depth + 1):
        next_frontier: list[tuple[tuple[Action, ...], ModelState]] = []
        for prefix, prefix_state in frontier:
            for symbolic in tails:
                applied = _apply_symbolic(model, prefix_state, symbolic)
                if applied is None:
                    continue
                concrete, _next_state = applied
                # Symbolic prefixes are unique only when their concrete action
                # sequence is unique. Re-materialize from the initial state.
                concrete_trace: list[Action] = []
                replay_state = model.initial_state
                published = False
                for item in (*prefix, symbolic):
                    action = materialize_action(item, replay_state)
                    concrete_trace.append(action)
                    replay_state = model.apply(action, state=replay_state).state
                    # The sequential production harness waits for terminal
                    # publication after every terminal transition; model it
                    # as an explicit publish step.
                    if replay_state.publication_pending_kind is not None:
                        publish = Action("publish")
                        concrete_trace.append(publish)
                        replay_state = model.apply(
                            publish, state=replay_state
                        ).state
                        published = True
                key = tuple(concrete_trace)
                if key in seen_concrete:
                    continue
                seen_concrete.add(key)
                trace = (*prefix, symbolic)
                if published:
                    trace = (*trace, _symbolic_action("publish"))
                traces.append(trace)
                next_frontier.append((trace, replay_state))
        frontier = next_frontier

    # Public no-request negatives. Backend callbacks and completion have no
    # public request-scoped entry point before admission and are tested directly.
    for symbolic in tails:
        if symbolic.kind not in {
            "cancel",
            "reconnect",
            "revoke",
            "epoch_change",
            "path_change",
            "evidence_change",
        }:
            continue
        concrete = materialize_action(symbolic, model.initial_state)
        key = (concrete,)
        if key not in seen_concrete:
            seen_concrete.add(key)
            traces.append((symbolic,))
    return tuple(traces)


def generate_race_traces(current: Authority) -> tuple[tuple[Action, ...], ...]:
    """Enumerate unique reachable serial linearizations of race actions."""
    admit = Action.admit(current, payload="fixture-prompt")
    start = Action.start()
    reconnect = Action.reconnect(-1)
    actions = tuple(_symbolic_action(name) for name in RACE_ACTIONS)
    model = GatewayModel(current=current)
    traces: list[tuple[Action, ...]] = []
    seen_concrete: set[tuple[Action, ...]] = set()

    for ordering in itertools.permutations(actions):
        for settle_publication in (False, True):
            trace: list[Action] = [admit, start, reconnect]
            state = model.initial_state
            concrete_trace: list[Action] = []
            published = False
            for item in trace:
                concrete = materialize_action(item, state)
                concrete_trace.append(concrete)
                state = model.apply(concrete, state=state).state
            for symbolic in ordering:
                applied = _apply_symbolic(model, state, symbolic)
                if applied is None:
                    continue
                concrete, state = applied
                trace.append(symbolic)
                concrete_trace.append(concrete)
                if (
                    settle_publication
                    and not published
                    and state.publication_pending_kind is not None
                ):
                    publish = _symbolic_action("publish")
                    trace.append(publish)
                    concrete_trace.append(Action("publish"))
                    state = model.apply(Action("publish"), state=state).state
                    published = True
            key = tuple(concrete_trace)
            if key in seen_concrete:
                continue
            seen_concrete.add(key)
            traces.append(tuple(trace))
    return tuple(traces)


def materialize_action(action: Action, state: ModelState) -> Action:
    if action.kind == "token_next":
        return Action.token(state.expected_token_index, f"token-{state.expected_token_index}")
    if action.kind == "token_exact_replay":
        if not state.token_digests:
            raise ValueError("inapplicable_exact_replay")
        index = state.expected_token_index - 1
        return Action.token(index, f"token-{index}")
    if action.kind == "token_conflicting_replay":
        if not state.token_digests:
            raise ValueError("inapplicable_conflicting_replay")
        index = state.expected_token_index - 1
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
        result = model.apply(materialize_action(action, result.state), state=result.state)
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


def _safe_value_document(value: object) -> dict[str, str]:
    if value is None:
        value_type = "none"
        encoded = b"null"
    elif isinstance(value, bool):
        value_type = "bool"
        encoded = b"true" if value else b"false"
    elif isinstance(value, int):
        value_type = "int"
        encoded = str(value).encode("ascii")
    elif isinstance(value, str):
        value_type = "str"
        encoded = value.encode("utf-8", errors="surrogatepass")
    else:
        value_type = "unsupported"
        encoded = type(value).__qualname__.encode("utf-8", errors="backslashreplace")
    return {
        "value_type": value_type,
        "value_digest": hashlib.sha256(encoded).hexdigest(),
    }


def _safe_action_document(action: Action) -> dict[str, object]:
    document: dict[str, object] = {"kind": action.kind}
    if action.max_new_tokens is not None:
        document["max_new_tokens"] = action.max_new_tokens
    if action.token_index is not None:
        document["token_index"] = action.token_index
    if action.field is not None:
        document["field"] = action.field
    if action.value is not None:
        document.update(_safe_value_document(action.value))
    if action.cursor is not None:
        document["cursor"] = action.cursor
    if action.authority is not None:
        authority = {
            "deployment": action.authority.deployment,
            "epoch": action.authority.epoch,
            "path": action.authority.path,
            "evidence": action.authority.evidence,
            "qualification": action.authority.qualification,
            "ready": action.authority.ready,
        }
        encoded = json.dumps(authority, sort_keys=True, separators=(",", ":"))
        document["authority_digest"] = hashlib.sha256(encoded.encode()).hexdigest()
    if action.payload is not None:
        document["payload_digest"] = hashlib.sha256(
            action.payload.encode("utf-8", errors="surrogatepass")
        ).hexdigest()
    if action.text is not None:
        document["text_digest"] = hashlib.sha256(
            action.text.encode("utf-8", errors="surrogatepass")
        ).hexdigest()
    return document


def trace_to_json(trace: Iterable[Action]) -> str:
    return json.dumps(
        [_safe_action_document(action) for action in trace],
        sort_keys=True,
        separators=(",", ":"),
    )
