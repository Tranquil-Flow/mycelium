# SPDX-License-Identifier: AGPL-3.0-only
"""A11 Wave D: the multi-position VERIFY phase on the stage-local KV runtime.

One VERIFY hop feeds ``(pending, c_0 .. c_{k-1})`` through every target stage in one
pass and the final stage returns the target's greedy decision for all ``k+1``
positions.  Rollback is piggybacked: the next operation names the committed
watermark and every stage truncates its tentative KV to it, inside a window bound
to the previous VERIFY.  The authority checked here is the incumbent one-token
DECODE path on a separate path of the same loaded stages.
"""

from __future__ import annotations

import math
import random

import mlx.core as mx
import numpy as np
import pytest

from mycelium_router.contracts import HopWorkItem, RuntimeBatchKey
from mycelium_router.decoding import (
    GreedySelectionError,
    quantized_greedy_token_id,
    quantized_greedy_token_ids,
)
from mycelium_router.payloads import decode_token_ids, encode_token_ids
from a11_route_fixtures import fresh_ports, varied_two_stage_case

PROMPT = (6, 5, 4, 3, 2)
LEASE = 1_000_000_000_000.0


# ---------------------------------------------------------------------------
# The vectorised greedy readout is exactly the scalar contract
# ---------------------------------------------------------------------------


def test_vectorised_greedy_readout_equals_the_scalar_rule_row_by_row():
    rng = random.Random(7)
    quantum = 1e-5
    rows = []
    for _ in range(40):
        rows.append([rng.uniform(-30.0, 30.0) for _ in range(257)])
    # Exact ties, half-quantum boundaries and values that round together.
    tie = [0.0] * 257
    tie[11] = tie[200] = 5.0
    rows.append(tie)
    half = [0.0] * 257
    half[3] = 2.5 * quantum
    half[4] = 1.5 * quantum  # rint(2.5) == rint(1.5) == 2: lowest index wins
    rows.append(half)
    close = [0.0] * 257
    close[90] = 7.000004
    close[10] = 7.0000041  # same bucket, lower index wins
    rows.append(close)
    array = np.array(rows, dtype=np.float32)
    expected = tuple(quantized_greedy_token_id(row.tolist()) for row in array)
    assert quantized_greedy_token_ids(array) == expected
    assert quantized_greedy_token_ids(mx.array(array)) == expected


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_vectorised_greedy_readout_refuses_non_finite_logits(bad):
    row = np.zeros((2, 8), dtype=np.float32)
    row[1, 3] = bad
    with pytest.raises(GreedySelectionError, match="invalid_logit"):
        quantized_greedy_token_ids(row)


def test_vectorised_greedy_readout_refuses_empty_and_wrong_rank():
    with pytest.raises(GreedySelectionError):
        quantized_greedy_token_ids(np.zeros((0, 4), dtype=np.float32))
    with pytest.raises(GreedySelectionError):
        quantized_greedy_token_ids(np.zeros((4,), dtype=np.float32))


# ---------------------------------------------------------------------------
# Runtime fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def case():
    temporary, value = varied_two_stage_case()
    yield value
    temporary.cleanup()


def _ports(case):
    return fresh_ports(case)


def _key(case, stage_index, phase, span, *, role=None, width=None):
    placement = case.graph.stages[stage_index].placements[0]
    verify = phase == "VERIFY"
    return RuntimeBatchKey(
        deployment_id=case.graph.deployment_id,
        deployment_epoch=case.graph.deployment_epoch,
        model_commit=case.graph.resolved_commit,
        manifest_digest=case.graph.manifest_digest,
        placement_id=placement.placement_id,
        assignment_id=placement.assignment_id,
        stage_signature=placement.stage_signature,
        load_proof_digest=placement.load_proof_digest,
        runtime_backend=placement.runtime_backend,
        phase=phase,
        hidden_size=case.graph.hidden_size,
        activation_bytes=case.graph.activation_bytes,
        token_span=span,
        speculative_role=role if role is not None else ("TARGET_VERIFY" if verify else "NONE"),
        speculative_width=width if width is not None else (span - 1 if verify else 0),
    )


def _item(case, stage_index, payload, *, phase, span, token_index, position, path_id, terminal=False, key=None):
    return HopWorkItem(
        request_id=f"request:{path_id}",
        path_id=path_id,
        path_attempt=0,
        phase=phase,
        token_index=token_index,
        hop_index=stage_index,
        placement_id=case.graph.stages[stage_index].placements[0].placement_id,
        qos_class="interactive",
        deficit_ratio=0.0,
        enqueued_at=0.0,
        idempotency_key=f"{path_id}:{phase}:{token_index}:{stage_index}",
        payload=payload,
        batch_key=key or _key(case, stage_index, phase, span),
        position=position,
        terminal=terminal,
        lease_expires_at=LEASE,
    )


class Route:
    """Drive both stages of one path exactly as the relay does."""

    def __init__(self, case, ports, path_id):
        self.case, self.ports, self.path_id = case, ports, path_id

    def _run(self, tokens, *, phase, token_index, position, key0=None, key1=None):
        span = len(tokens)
        first = self.ports[0].execute(
            _item(self.case, 0, encode_token_ids(tokens), phase=phase, span=span, token_index=token_index,
                  position=position, path_id=self.path_id, key=key0)
        )
        if not first.success:
            return first
        return self.ports[1].execute(
            _item(self.case, 1, first.payload, phase=phase, span=span, token_index=token_index,
                  position=position, path_id=self.path_id, key=key1)
        )

    def prefill(self, prompt):
        result = self._run(prompt, phase="PREFILL", token_index=-1, position=0)
        assert result.success, result.failure_reason
        return result.token_id

    def decode(self, token, token_index):
        position = len(PROMPT) + token_index - 1
        return self._run((token,), phase="DECODE", token_index=token_index, position=position)

    def verify(self, tokens, token_index, **keys):
        position = len(PROMPT) + token_index - 1
        return self._run(tuple(tokens), phase="VERIFY", token_index=token_index, position=position, **keys)


def _reference(case, steps=10):
    route = Route(case, _ports(case), "reference")
    tokens = [route.prefill(PROMPT)]
    for index in range(1, steps):
        result = route.decode(tokens[-1], index)
        assert result.success, result.failure_reason
        tokens.append(result.token_id)
    return tokens


def _decisions(result):
    assert result.success, result.failure_reason
    assert result.token_id is None
    return list(decode_token_ids(result.payload))


def _perturb(token, vocab=16):
    return (token + 1) % vocab


# ---------------------------------------------------------------------------
# VERIFY semantics
# ---------------------------------------------------------------------------


def test_verify_decisions_equal_the_sequential_target_decode(case):
    reference = _reference(case)
    route = Route(case, _ports(case), "verify-all")
    assert route.prefill(PROMPT) == reference[0]
    decisions = _decisions(route.verify([reference[0], *reference[1:5]], 1))
    assert decisions == reference[1:6]


@pytest.mark.parametrize("mismatch", [0, 1, 2, 3])
def test_mismatch_commit_then_continue_matches_target_only_decode(case, mismatch):
    reference = _reference(case)
    route = Route(case, _ports(case), f"verify-mismatch-{mismatch}")
    route.prefill(PROMPT)
    candidates = list(reference[1:5])
    candidates[mismatch] = _perturb(candidates[mismatch])
    if candidates[mismatch] == reference[1 + mismatch]:
        candidates[mismatch] = _perturb(candidates[mismatch])
    decisions = _decisions(route.verify([reference[0], *candidates], 1))
    assert decisions[: mismatch + 1] == reference[1 : mismatch + 2]
    committed = reference[: mismatch + 2]  # accepted prefix plus the target correction
    # The next operation names the committed watermark; the stage truncates to it.
    following = route.decode(committed[-1], len(committed))
    assert following.success, following.failure_reason
    assert following.token_id == reference[len(committed)]


def test_all_match_commit_keeps_kv_through_the_last_candidate(case):
    reference = _reference(case)
    route = Route(case, _ports(case), "verify-full")
    route.prefill(PROMPT)
    route.verify(reference[0:5], 1)
    # Four candidates committed: generated count 5, the last candidate is pending.
    following = route.verify(reference[4:8], 5)
    assert _decisions(following) == reference[5:9]


def test_all_match_plus_the_target_bonus_keeps_the_whole_span_and_continues_exactly(case):
    reference = _reference(case)
    route = Route(case, _ports(case), "verify-bonus")
    route.prefill(PROMPT)
    decisions = _decisions(route.verify(reference[0:5], 1))
    # Every candidate matched: commit them and the target's own next decision.
    assert decisions[4] == reference[5]
    committed = reference[:6]
    following = route.decode(committed[-1], len(committed))
    assert following.success, following.failure_reason
    assert following.token_id == reference[6]
    # The window was consumed: a rewind into the old span is refused afterwards.
    stale = route.decode(reference[3], 4)
    assert not stale.success and stale.failure_reason == "kv_position_mismatch"


def test_rollback_to_the_unchanged_watermark_reproduces_the_decode(case):
    reference = _reference(case)
    route = Route(case, _ports(case), "verify-rollback")
    route.prefill(PROMPT)
    route.verify([reference[0], *(_perturb(t) for t in reference[1:4])], 1)
    result = route.decode(reference[0], 1)
    assert result.success, result.failure_reason
    assert result.token_id == reference[1]


def test_positions_outside_the_verify_window_fail_closed_and_change_nothing(case):
    reference = _reference(case)
    ports = _ports(case)
    route = Route(case, ports, "verify-window")
    route.prefill(PROMPT)
    route.verify(reference[0:5], 1)
    beyond = route.decode(reference[6], 7)  # past the span plus the target's bonus decision
    assert not beyond.success and beyond.failure_reason == "kv_position_mismatch"
    below = route._run((reference[0],), phase="DECODE", token_index=0, position=len(PROMPT) - 2)
    assert not below.success
    # The rejected operations changed nothing: the in-window operation still works.
    ok = route.decode(reference[2], 3)
    assert ok.success and ok.token_id == reference[3]


def test_position_must_stay_bound_to_the_committed_count(case):
    reference = _reference(case)
    route = Route(case, _ports(case), "verify-offset")
    route.prefill(PROMPT)
    route.verify(reference[0:5], 1)
    forged = route._run((reference[1],), phase="DECODE", token_index=3, position=len(PROMPT))
    assert not forged.success and forged.failure_reason == "kv_speculative_offset_mismatch"


def test_window_is_consumed_by_the_next_operation(case):
    reference = _reference(case)
    route = Route(case, _ports(case), "verify-consumed")
    route.prefill(PROMPT)
    route.verify(reference[0:5], 1)
    assert route.decode(reference[2], 3).success
    # A second rewind into the old window is no longer allowed.
    stale = route.decode(reference[1], 2)
    assert not stale.success and stale.failure_reason == "kv_position_mismatch"


def test_verify_batch_key_must_declare_the_target_verify_role_and_width(case):
    reference = _reference(case)
    route = Route(case, _ports(case), "verify-key")
    route.prefill(PROMPT)
    plain = _key(case, 0, "VERIFY", 5, role="NONE", width=0)
    result = route.verify(reference[0:5], 1, key0=plain)
    assert not result.success and result.failure_reason == "batch_key_speculative_role_mismatch"
    narrow = _key(case, 0, "VERIFY", 5, width=3)
    result = route.verify(reference[0:5], 1, key0=narrow)
    assert not result.success and result.failure_reason == "batch_key_speculative_width_mismatch"
    single = route.verify(reference[0:1], 1)
    assert not single.success and single.failure_reason == "verify_requires_candidates"
    # Nothing above touched the KV: a valid VERIFY still runs from the watermark.
    assert _decisions(route.verify(reference[0:5], 1)) == reference[1:6]


def test_duplicate_verify_is_idempotent_and_conflicting_payload_fails_closed(case):
    reference = _reference(case)
    ports = _ports(case)
    route = Route(case, ports, "verify-replay")
    route.prefill(PROMPT)
    item = _item(case, 0, encode_token_ids(tuple(reference[0:5])), phase="VERIFY", span=5, token_index=1,
                 position=len(PROMPT), path_id="verify-replay")
    first = ports[0].execute(item)
    again = ports[0].execute(item)
    assert first.success and again == first
    conflict = ports[0].execute(
        _item(case, 0, encode_token_ids((reference[0], *(_perturb(t) for t in reference[1:5]))), phase="VERIFY", span=5, token_index=1,
              position=len(PROMPT), path_id="verify-replay")
    )
    assert not conflict.success and conflict.failure_reason == "kv_sequence_replay_conflict"


def test_verify_is_never_terminal_and_the_terminal_decode_releases_kv(case):
    reference = _reference(case)
    ports = _ports(case)
    route = Route(case, ports, "verify-release")
    route.prefill(PROMPT)
    route.verify(reference[0:5], 1)
    assert ports[0].kv_snapshot()["active_state_count"] == 1
    for port in ports:
        port.cancel("verify-release")
    assert ports[0].kv_snapshot()["active_state_count"] == 0
    assert ports[1].kv_snapshot()["active_state_count"] == 0
