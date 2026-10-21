# SPDX-License-Identifier: AGPL-3.0-only
"""Memory-proportional N-host layer split: pure, deterministic, fail-closed.

Synthetic profiles and tiny on-disk Qwen2/Qwen3 checkpoints only; no device,
model load or network. A split is a placement proposal, never qualification.
"""
from __future__ import annotations

import pytest

from mycelium_a13_macos import layer_split as ls
from _nhost_fixtures import tiny_dense_snapshot

GiB = 1 << 30
MiB = 1 << 20


def _profile(layers=24, layer_bytes=100 * MiB, embedding=0, output=0, tied=False):
    return ls.LayerProfile(model_type="qwen3", num_layers=layers,
                           layer_bytes=(layer_bytes,) * layers,
                           embedding_bytes=embedding, output_bytes=output,
                           tie_word_embeddings=tied)


def _members(*gib, tps=None):
    return [{"node_id": f"node-{i}", "memory_bytes": g * GiB,
             **({} if tps is None else {"decode_tokens_per_second": tps[i]})}
            for i, g in enumerate(gib)]


def _counts(split):
    return [s["end"] - s["start"] for s in split["stages"]]


def test_split_is_proportional_to_usable_memory():
    # Usable budget = memory/2 - 1 GiB runtime overhead -> 7, 15, 23 GiB weights.
    split = ls.memory_proportional_split(_members(16, 32, 48), _profile())
    assert _counts(split) == [4, 8, 12]
    assert [s["start"] for s in split["stages"]] == [0, 4, 12]
    assert split["stages"][-1]["end"] == 24
    assert split["weighting"] == "memory"


def test_split_accounts_for_first_and_last_stage_statics():
    # Embedding on the first stage and a tied head on the last are not free:
    # equal members must give the edge stages fewer layers than the middle.
    profile = _profile(layers=12, layer_bytes=100 * MiB, embedding=300 * MiB,
                       output=1 * MiB, tied=True)
    split = ls.memory_proportional_split(_members(16, 16, 16), profile)
    counts = _counts(split)
    assert sum(counts) == 12 and counts[1] > counts[0] and counts[1] > counts[2]
    loads = [s["stage_bytes"] for s in split["stages"]]
    assert max(loads) - min(loads) <= 2 * 100 * MiB


def test_split_is_deterministic_and_digest_pinned():
    a = ls.memory_proportional_split(_members(16, 24, 36, 8, 64), _profile(layers=28))
    b = ls.memory_proportional_split(_members(16, 24, 36, 8, 64), _profile(layers=28))
    assert a == b and a["split_digest"].startswith("sha256:")
    c = ls.memory_proportional_split(_members(16, 24, 36, 8, 65), _profile(layers=28))
    assert c["split_digest"] != a["split_digest"]


def test_five_host_split_covers_every_layer_with_minimum_one():
    split = ls.memory_proportional_split(_members(8, 128, 128, 128, 8), _profile(layers=28))
    counts = _counts(split)
    assert len(counts) == 5 and sum(counts) == 28 and min(counts) >= 1
    ends = [s["end"] for s in split["stages"]]
    starts = [s["start"] for s in split["stages"]]
    assert starts[1:] == ends[:-1] and starts[0] == 0


def test_minimum_one_layer_even_for_a_tiny_member():
    # A member that would round to zero layers still receives one.
    split = ls.memory_proportional_split(_members(4, 512), _profile(layers=8, layer_bytes=1 * MiB))
    assert _counts(split) == [1, 7]


def test_decode_microbench_weights_split_only_when_every_member_reports():
    profile = _profile(layers=24, layer_bytes=10 * MiB)
    fast = ls.memory_proportional_split(_members(32, 32, tps=[40.0, 20.0]), profile)
    assert fast["weighting"] == "memory_and_decode_microbench"
    assert _counts(fast)[0] > _counts(fast)[1]
    partial = _members(32, 32, tps=[40.0, 20.0])
    del partial[1]["decode_tokens_per_second"]
    plain = ls.memory_proportional_split(partial, profile)
    assert plain["weighting"] == "memory" and _counts(plain) == [12, 12]


def test_refuses_member_that_cannot_hold_its_share():
    # 2.5 GiB of weights; the 4 GiB Mac has a 1 GiB budget and cannot hold even
    # one 1.25 GiB layer -> refused by name, never silently overcommitted.
    profile = _profile(layers=2, layer_bytes=1280 * MiB)
    with pytest.raises(ls.SplitRefused) as caught:
        ls.memory_proportional_split(_members(64, 4), profile)
    assert caught.value.code == "member_cannot_hold_share"
    assert caught.value.node_id == "node-1"


def test_refuses_when_whole_model_does_not_fit_the_swarm():
    with pytest.raises(ls.SplitRefused) as caught:
        ls.memory_proportional_split(_members(8, 8), _profile(layers=24, layer_bytes=1 * GiB))
    assert caught.value.code == "member_cannot_hold_share"


@pytest.mark.parametrize("members,code", [
    ([{"node_id": "a", "memory_bytes": GiB * 16}], "host_count_invalid"),
    ([{"node_id": f"n{i}", "memory_bytes": GiB * 16} for i in range(9)], "host_count_invalid"),
    ([{"node_id": "a", "memory_bytes": GiB * 16}, {"node_id": "a", "memory_bytes": GiB * 16}],
     "member_duplicate"),
    ([{"node_id": "a", "memory_bytes": GiB * 16}, {"node_id": "b", "memory_bytes": 0}],
     "member_capability_invalid"),
    ([{"node_id": "a", "memory_bytes": GiB * 16}, {"node_id": "b", "memory_bytes": True}],
     "member_capability_invalid"),
    ([{"node_id": "a", "memory_bytes": GiB * 16},
      {"node_id": "b", "memory_bytes": GiB * 16, "decode_tokens_per_second": float("nan")}],
     "member_capability_invalid"),
])
def test_refuses_invalid_member_sets(members, code):
    with pytest.raises(ls.SplitRefused) as caught:
        ls.memory_proportional_split(members, _profile())
    assert caught.value.code == code


def test_refuses_more_hosts_than_layers():
    with pytest.raises(ls.SplitRefused) as caught:
        ls.memory_proportional_split(_members(16, 16, 16), _profile(layers=2, layer_bytes=MiB))
    assert caught.value.code == "model_too_small_for_members"


@pytest.mark.parametrize("model_type", ["qwen2", "qwen3"])
@pytest.mark.parametrize("split_files", [1, 3])
def test_profile_reads_layer_bytes_from_headers_only(tmp_path, model_type, split_files):
    snapshot = tiny_dense_snapshot(tmp_path, model_type=model_type, num_layers=5,
                                   split_files=split_files, tie_word_embeddings=True)
    profile = ls.model_layer_profile(snapshot)
    assert profile.model_type == model_type and profile.num_layers == 5
    # 9 tensors/layer: 2 norms (8) + 3 mlp (8x16) + 4 attn (8x8), BF16.
    per_layer = (2 * 8 + 3 * 128 + 4 * 64) * 2
    assert profile.layer_bytes == (per_layer,) * 5
    assert profile.embedding_bytes == 32 * 8 * 2
    assert profile.output_bytes == 8 * 2 and profile.tie_word_embeddings is True


def test_profile_refuses_unknown_architecture(tmp_path):
    snapshot = tiny_dense_snapshot(tmp_path, model_type="llama")
    with pytest.raises(ls.SplitRefused) as caught:
        ls.model_layer_profile(snapshot)
    assert caught.value.code == "model_architecture_unsupported"


def test_qwen3_14b_int8_demo_split_48gb_mac_and_16gb_air():
    # Qwen3-14B header shape: 40 layers, untied 151936x5120 embedding + lm_head,
    # ~29.5 GB bf16 source; int8-weight-only runtime (~14.8 GB weights).
    embedding = 151_936 * 5_120 * 2
    profile = ls.LayerProfile(model_type="qwen3", num_layers=40,
                              layer_bytes=(660_600_000,) * 40, embedding_bytes=embedding,
                              output_bytes=embedding + 10_240, tie_word_embeddings=False)
    members = [{"node_id": "mac-48", "memory_bytes": 48 * GiB},
               {"node_id": "air-16", "memory_bytes": 16 * GiB}]
    split = ls.memory_proportional_split(members, profile, ls.policy_for_runtime("int8-weight-only"))
    big, air = split["stages"]
    assert 10e9 <= big["stage_bytes"] / 2 <= 12.5e9      # ~11 GB int8 on the 48 GB Mac
    assert 3e9 <= air["stage_bytes"] / 2 <= 4.5e9        # ~4 GB int8 on the 16 GB Air
    assert big["cost_bytes"] <= big["budget_bytes"] and air["cost_bytes"] <= air["budget_bytes"]
    assert ls.policy_for_runtime("none") == ls.DEFAULT_POLICY
