# SPDX-License-Identifier: AGPL-3.0-only
"""A11 Wave D: the native MLX draft runtime (draft KV ownership only).

A tiny random Qwen2 configuration stands in for the 0.5B draft: nothing is
downloaded and no model artifact is read.  The authority checked here is the
draft's own full-context forward, which shares no KV code with the cached path.
"""

from __future__ import annotations

import mlx.core as mx
import pytest

import mycelium_a11_draft_runtime as dr
from mycelium_speculative_decode_execution import DraftRuntimePort

CONFIG = {
    "model_type": "qwen2",
    "hidden_size": 64,
    "intermediate_size": 96,
    "num_hidden_layers": 2,
    "num_attention_heads": 4,
    "num_key_value_heads": 2,
    "rms_norm_eps": 1e-6,
    "rope_theta": 10000.0,
    "vocab_size": 97,
    "tie_word_embeddings": True,
    "use_sliding_window": False,
}
DIGEST = "sha256:" + "d" * 64


def _weights(seed: int = 3) -> dict[str, mx.array]:
    mx.random.seed(seed)
    hidden, inter, layers = CONFIG["hidden_size"], CONFIG["intermediate_size"], CONFIG["num_hidden_layers"]
    head_dim = hidden // CONFIG["num_attention_heads"]
    kv = CONFIG["num_key_value_heads"] * head_dim
    weights = {"model.embed_tokens.weight": mx.random.normal((CONFIG["vocab_size"], hidden)) * 0.5,
               "model.norm.weight": mx.ones((hidden,))}
    for layer in range(layers):
        prefix = f"model.layers.{layer}."
        for name, shape in (
            ("self_attn.q_proj.weight", (hidden, hidden)),
            ("self_attn.k_proj.weight", (kv, hidden)),
            ("self_attn.v_proj.weight", (kv, hidden)),
            ("self_attn.o_proj.weight", (hidden, hidden)),
            ("mlp.gate_proj.weight", (inter, hidden)),
            ("mlp.up_proj.weight", (inter, hidden)),
            ("mlp.down_proj.weight", (hidden, inter)),
        ):
            weights[prefix + name] = mx.random.normal(shape) * 0.2
        for name, size in (("self_attn.q_proj.bias", hidden), ("self_attn.k_proj.bias", kv), ("self_attn.v_proj.bias", kv)):
            weights[prefix + name] = mx.random.normal((size,)) * 0.1
        weights[prefix + "input_layernorm.weight"] = mx.ones((hidden,))
        weights[prefix + "post_attention_layernorm.weight"] = mx.ones((hidden,))
    return weights


@pytest.fixture(scope="module")
def model() -> dr.Qwen2DraftModel:
    return dr.Qwen2DraftModel(CONFIG, _weights(), quantize_bits=None)


def _greedy_full(model: dr.Qwen2DraftModel, context: list[int], steps: int) -> list[int]:
    out: list[int] = []
    tokens = list(context)
    for _ in range(steps):
        logits = model.full_context_logits(tokens)
        token = int(mx.argmax(logits[0, -1, :]).item())
        out.append(token)
        tokens.append(token)
    return out


def _runtime(model: dr.Qwen2DraftModel, **kwargs) -> dr.MLXDraftRuntime:
    return dr.MLXDraftRuntime(model, incarnation=DIGEST, **kwargs)


def test_runtime_satisfies_the_declared_draft_port(model):
    assert isinstance(_runtime(model), DraftRuntimePort)


def test_cached_forward_matches_the_full_context_forward(model):
    context = [5, 9, 13, 2, 40, 7]
    cache = dr.DraftKVCache(CONFIG["num_hidden_layers"])
    first = model.forward(context[:4], cache)
    second = model.forward(context[4:], cache)
    full = model.full_context_logits(context)
    assert cache.length == len(context)
    assert mx.allclose(first[0, -1], full[0, 3], atol=1e-4).item()
    assert mx.allclose(second[0, -1], full[0, -1], atol=1e-4).item()


def test_proposal_is_the_draft_greedy_continuation_of_the_committed_context(model):
    runtime = _runtime(model)
    context = [3, 1, 4, 1, 5]
    runtime.sync(context)
    proposal = runtime.propose(prefix_position=len(context), prefix_digest=DIGEST, width=4, deadline_ms=1e6)
    assert not proposal.lost and not proposal.unavailable
    assert list(proposal.tokens) == _greedy_full(model, context, 4)
    snapshot = runtime.snapshot()
    assert snapshot["tentative_tokens"] == 4 and snapshot["wrote_target_state"] is False


@pytest.mark.parametrize("accepted", [0, 1, 2, 3])
def test_commit_with_target_correction_keeps_only_the_accepted_draft_kv(model, accepted):
    runtime = _runtime(model)
    context = [8, 6, 7, 5, 3]
    runtime.sync(context)
    proposal = list(runtime.propose(prefix_position=5, prefix_digest=DIGEST, width=4, deadline_ms=1e6).tokens)
    correction = (proposal[accepted] + 1) % CONFIG["vocab_size"]
    assert runtime.commit_verified_prefix(tokens=proposal[:accepted], correction_token=correction)
    committed = context + proposal[:accepted] + [correction]
    assert runtime.committed_context() == tuple(committed)
    # KV holds every committed position except the pending last token.
    assert runtime.snapshot()["kv_positions"] == len(committed) - 1
    following = runtime.propose(prefix_position=len(committed), prefix_digest=DIGEST, width=3, deadline_ms=1e6)
    assert list(following.tokens) == _greedy_full(model, committed, 3)


def test_all_match_commit_keeps_the_whole_tentative_kv(model):
    runtime = _runtime(model)
    context = [2, 7, 1, 8]
    runtime.sync(context)
    proposal = list(runtime.propose(prefix_position=4, prefix_digest=DIGEST, width=3, deadline_ms=1e6).tokens)
    assert runtime.commit_verified_prefix(tokens=proposal, correction_token=None)
    committed = context + proposal
    assert runtime.snapshot()["kv_positions"] == len(committed) - 1
    following = runtime.propose(prefix_position=len(committed), prefix_digest=DIGEST, width=2, deadline_ms=1e6)
    assert list(following.tokens) == _greedy_full(model, committed, 2)


def test_commit_that_is_not_the_draft_own_prefix_is_not_reproducible(model):
    runtime = _runtime(model)
    runtime.sync([4, 4, 4])
    proposal = list(runtime.propose(prefix_position=3, prefix_digest=DIGEST, width=3, deadline_ms=1e6).tokens)
    forged = [(proposal[0] + 1) % CONFIG["vocab_size"]]
    assert runtime.commit_verified_prefix(tokens=forged, correction_token=None) is False
    # The draft state is unchanged and stays usable from its committed context.
    assert runtime.committed_context() == (4, 4, 4)
    assert runtime.snapshot()["tentative_tokens"] == 0


def test_rollback_discards_the_tentative_suffix(model):
    runtime = _runtime(model)
    runtime.sync([9, 8, 7])
    runtime.propose(prefix_position=3, prefix_digest=DIGEST, width=4, deadline_ms=1e6)
    runtime.rollback_suffix(4)
    snapshot = runtime.snapshot()
    assert snapshot["tentative_tokens"] == 0 and snapshot["kv_positions"] == 2
    assert snapshot["rolled_back_positions"] == 4


def test_sync_extends_a_prefix_and_resets_on_divergence(model):
    runtime = _runtime(model)
    runtime.sync([1, 2, 3])
    runtime.sync([1, 2, 3, 4, 5])
    assert runtime.committed_context() == (1, 2, 3, 4, 5)
    assert runtime.snapshot()["kv_positions"] == 4
    assert runtime.snapshot()["resets"] == 0
    runtime.sync([1, 9, 3])
    assert runtime.committed_context() == (1, 9, 3)
    assert runtime.snapshot()["resets"] == 1
    proposal = runtime.propose(prefix_position=3, prefix_digest=DIGEST, width=2, deadline_ms=1e6)
    assert list(proposal.tokens) == _greedy_full(model, [1, 9, 3], 2)


class _StepClock:
    def __init__(self, step_ms: float) -> None:
        self.now = 0.0
        self.step = step_ms / 1000.0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def test_deadline_truncates_the_proposal_and_a_zero_width_proposal_is_unavailable(model):
    runtime = _runtime(model, clock=_StepClock(10.0))
    runtime.sync([6, 5, 4])
    partial = runtime.propose(prefix_position=3, prefix_digest=DIGEST, width=8, deadline_ms=35.0)
    assert 1 <= len(partial.tokens) < 8
    assert partial.draft_compute_ms <= 35.0 + 1e-9
    runtime.rollback_suffix(len(partial.tokens))
    starved = _runtime(model, clock=_StepClock(50.0))
    starved.sync([6, 5, 4])
    assert starved.propose(prefix_position=3, prefix_digest=DIGEST, width=4, deadline_ms=20.0).unavailable


def test_propose_requires_a_synced_context_and_a_valid_width(model):
    runtime = _runtime(model)
    with pytest.raises(dr.DraftRuntimeError, match="draft_context_not_synced"):
        runtime.propose(prefix_position=0, prefix_digest=DIGEST, width=2, deadline_ms=1e6)
    runtime.sync([1, 2])
    with pytest.raises(dr.DraftRuntimeError, match="draft_prefix_position_mismatch"):
        runtime.propose(prefix_position=5, prefix_digest=DIGEST, width=2, deadline_ms=1e6)
    with pytest.raises(dr.DraftRuntimeError, match="invalid_draft_width"):
        runtime.propose(prefix_position=2, prefix_digest=DIGEST, width=0, deadline_ms=1e6)


def test_lost_runtime_reports_loss_without_touching_state(model):
    runtime = _runtime(model)
    runtime.sync([1, 2, 3])
    runtime.mark_lost()
    proposal = runtime.propose(prefix_position=3, prefix_digest=DIGEST, width=2, deadline_ms=1e6)
    assert proposal.lost and not proposal.tokens
    assert runtime.snapshot()["kv_positions"] == 2
    runtime.close()
    assert runtime.snapshot()["kv_positions"] == 0
    assert runtime.snapshot()["lost"] is True


def test_closed_draft_releases_its_kv_only(model):
    runtime = dr.MLXDraftRuntime(model, incarnation="sha256:" + "1" * 64)
    runtime.sync((1, 2, 3))
    assert runtime.snapshot()["kv_positions"] > 0
    runtime.close()
    assert runtime.snapshot()["kv_positions"] == 0
    assert runtime.snapshot()["tentative_tokens"] == 0
    assert runtime.snapshot()["lost"] is False


def test_quantized_model_keeps_cache_parity_with_its_own_full_forward():
    quantized = dr.Qwen2DraftModel(CONFIG, _weights(), quantize_bits=8, group_size=32)
    runtime = dr.MLXDraftRuntime(quantized, incarnation=DIGEST)
    context = [11, 22, 33, 44]
    runtime.sync(context)
    proposal = runtime.propose(prefix_position=4, prefix_digest=DIGEST, width=3, deadline_ms=1e6)
    assert list(proposal.tokens) == _greedy_full(quantized, context, 3)


def test_identity_binds_config_weights_and_representation():
    a = dr.Qwen2DraftModel(CONFIG, _weights(1), quantize_bits=None).identity_digest()
    b = dr.Qwen2DraftModel(CONFIG, _weights(1), quantize_bits=None).identity_digest()
    c = dr.Qwen2DraftModel(CONFIG, _weights(2), quantize_bits=None).identity_digest()
    d = dr.Qwen2DraftModel(CONFIG, _weights(1), quantize_bits=8, group_size=32).identity_digest()
    assert a == b and len({a, c, d}) == 3 and a.startswith("sha256:")


def test_unsupported_architecture_and_enabled_sliding_window_are_refused():
    with pytest.raises(dr.DraftRuntimeError, match="unsupported_draft_architecture"):
        dr.Qwen2DraftModel({**CONFIG, "model_type": "gpt2"}, _weights(), quantize_bits=None)
    with pytest.raises(dr.DraftRuntimeError, match="unsupported_draft_position_semantics"):
        dr.Qwen2DraftModel({**CONFIG, "use_sliding_window": True}, _weights(), quantize_bits=None)
