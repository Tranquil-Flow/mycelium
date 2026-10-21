# SPDX-License-Identifier: AGPL-3.0-only
"""Owner decision D-1, relaxed 2026-10-13: Qwen3 demo models fetch only a slice."""
from mycelium_a13_macos import serving


def test_relaxed_rule_is_stated_and_scoped():
    rule = serving.D1_MODEL_BYTES_RULE
    assert "ONLY its assigned layer slice" in rule and "never the whole" in rule
    assert "pre-staged" in rule


def test_slice_fetch_gate_by_architecture_and_source_size():
    permitted = serving.slice_fetch_permitted
    assert permitted(16_380_000_000, architecture="qwen3")       # Qwen3-8B bf16 source
    assert permitted(29_536_000_000, architecture="qwen3")       # Qwen3-14B bf16 source
    assert not permitted(65_500_000_000, architecture="qwen3")   # Qwen3-32B
    assert not permitted(15_231_271_888, architecture="qwen2")   # pinned Qwen2.5 7B: pre-staged
    for bad in (0, -1, True, 1.5e9, None):
        assert not permitted(bad, architecture="qwen3")


def test_small_models_use_the_whole_snapshot_path():
    # A small model goes whole to each Mac (the path that served on two Macs in
    # September); slicing it saves little. Slices are byte-exact for it too
    # (test_slice_real_builder; runtime dtype/quantization apply at load).
    assert serving.SLICE_FETCH_MIN_SOURCE_BYTES == 4_000_000_000
    assert not serving.slice_fetch_permitted(1_503_300_328, architecture="qwen3")   # Qwen3-0.6B
    assert not serving.slice_fetch_permitted(4_000_000_000, architecture="qwen3")
    assert serving.slice_fetch_permitted(4_000_000_001, architecture="qwen3")
    assert "whole snapshot" in serving.D1_MODEL_BYTES_RULE


def test_7b_whole_model_proposal_is_unchanged():
    assert serving.RESOURCE_PROPOSAL.network_download_bytes == 11_535_382
    assert serving.RESOURCE_PROPOSAL.execution_download_bytes == 0
