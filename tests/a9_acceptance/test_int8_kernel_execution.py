# SPDX-License-Identifier: AGPL-3.0-only
"""Efficient int8 kernel execution — deterministic source-level acceptance tests.

Covers the frozen A9 decisions ``persistent_int8_execution`` (spec §3.4) and the
``accelerated_windows_backend`` claim boundary for the source tranche landed in
``mycelium_platform_neutral/int8_execution.py``.

Claim boundary of this module, asserted below rather than described: the kernel
here is a *portable reference host* (``numpy``) executing deterministic integer
arithmetic in process.  It proves the materialization/bounded-workspace
*invariants* and the negative control that detects the rejected per-call
whole-weight cast.  It does NOT prove hardware acceleration, does not execute the
frozen A9 benchmark, and the physical acceptance case
``int8_decode_materialization_rejection`` stays unearned.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from mycelium_platform_neutral.int8_execution import (
    BOUNDARY_CODES,
    CORRECTNESS_ONLY_HOSTS,
    DEFAULT_WORKSPACE_BYTES,
    KERNEL_HOST_BACKEND,
    ACCEPTANCE_COVERAGE,
    Int8KernelUnavailable,
    Int8LinearKernel,
    PerCallFullWeightCastLinear,
    accelerated_windows_kernel_required,
    backend_is_correctness_only,
    quantized_reference_linear,
)

SEED = 20260926


def _fixture(out_features: int = 64, in_features: int = 32, rows: int = 16):
    rng = np.random.default_rng(SEED)
    weight = rng.integers(-127, 128, size=(out_features, in_features), dtype=np.int16)
    weight = weight.astype(np.int8)
    scales = (rng.random(out_features, dtype=np.float32) * 0.01 + 0.001).astype(np.float32)
    activations = (rng.standard_normal((rows, in_features)) * 3.0).astype(np.float32)
    return weight, scales, activations


def test_packed_state_is_bounded_and_never_grows_with_tokens() -> None:
    weight, scales, activations = _fixture()
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=4096)
    declared = int(weight.nbytes + scales.nbytes)
    assert kernel.packed_state_bytes == declared
    for _ in range(5):
        out = kernel.linear(activations)
        assert out.shape == (activations.shape[0], weight.shape[0])
        assert out.dtype == np.float32
        assert kernel.packed_state_bytes == declared
    assert kernel.packed_state_is_stable() is True


def test_packed_state_is_stored_as_int8_not_expanded() -> None:
    weight, scales, _ = _fixture()
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=4096)
    assert kernel.packed_values_dtype == np.int8
    assert kernel.packed_state_bytes == weight.nbytes + scales.nbytes
    float32_equivalent = int(weight.size * 4 + scales.size * 4)
    assert kernel.packed_state_bytes < float32_equivalent


def test_warm_decode_keeps_full_weight_materialization_at_zero() -> None:
    weight, scales, activations = _fixture()
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=4096)
    kernel.warmup(activations)
    before = kernel.ledger()
    assert before.warmup_complete is True
    assert before.full_weight_materializations == 0
    for _ in range(8):
        kernel.linear(activations)
    after = kernel.ledger()
    assert after.full_weight_materializations == 0
    assert after.decode_calls == before.decode_calls + 8
    assert after.bounded_slab_conversions > before.bounded_slab_conversions
    kernel.assert_warm_decode()


def test_weight_workspace_peak_is_independent_of_token_count() -> None:
    weight, scales, activations = _fixture(rows=64)
    one_token = Int8LinearKernel(weight, scales, workspace_bytes=4096)
    many_tokens = Int8LinearKernel(weight, scales, workspace_bytes=4096)
    one_token.linear(activations[:1])
    many_tokens.linear(activations)
    assert (
        one_token.ledger().peak_weight_workspace_bytes
        == many_tokens.ledger().peak_weight_workspace_bytes
    )
    assert many_tokens.ledger().peak_weight_workspace_bytes <= 4096
    assert (
        many_tokens.ledger().peak_activation_workspace_bytes
        > one_token.ledger().peak_activation_workspace_bytes
    )
    assert many_tokens.ledger().peak_activation_workspace_bytes <= (
        activations.shape[0] * weight.shape[1] * 4
    )


def test_workspace_never_holds_a_float32_copy_of_the_whole_weight() -> None:
    weight, scales, activations = _fixture(out_features=256, in_features=256, rows=4)
    workspace_bytes = 8192
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=workspace_bytes)
    kernel.linear(activations)
    peak = kernel.ledger().peak_weight_workspace_bytes
    whole_weight_float32 = weight.size * 4
    assert peak < whole_weight_float32
    assert peak <= workspace_bytes


def test_a_matrix_that_fits_the_budget_is_reported_as_a_full_materialization() -> None:
    rng = np.random.default_rng(SEED)
    weight = rng.integers(-127, 128, size=(8, 4), dtype=np.int16).astype(np.int8)
    scales = np.ones(8, dtype=np.float32)
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=1 << 20)
    kernel.linear(np.ones((2, 4), dtype=np.float32))
    ledger = kernel.ledger()
    assert ledger.full_weight_materializations == 1
    assert ledger.bounded_slab_conversions == 0
    with pytest.raises(Int8KernelUnavailable) as raised:
        kernel.assert_warm_decode()
    assert raised.value.code == "warm_decode_not_proven"


def test_every_boundary_code_is_registered_and_non_empty() -> None:
    assert isinstance(BOUNDARY_CODES, frozenset)
    assert "windows_x86_64_provider_not_physically_qualified" in BOUNDARY_CODES
    for code in BOUNDARY_CODES:
        assert isinstance(code, str) and code
        assert code == code.strip()


def test_integer_accumulation_is_exact_against_the_quantized_reference() -> None:
    weight, scales, activations = _fixture()
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=2048)
    observed = kernel.linear(activations)
    expected = quantized_reference_linear(activations, weight, scales)
    assert np.array_equal(observed, expected)


def test_kernel_tracks_the_float32_reference_within_a_declared_bound() -> None:
    weight, scales, activations = _fixture()
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=2048)
    observed = kernel.linear(activations)
    reference = activations @ (weight.astype(np.float32) * scales[:, None]).T
    scale = float(np.abs(reference).max()) or 1.0
    relative_error = float(np.abs(observed - reference).max()) / scale
    assert relative_error < 0.05, relative_error


def test_per_call_whole_weight_cast_is_detected_and_refused() -> None:
    weight, scales, activations = _fixture()
    rejected = PerCallFullWeightCastLinear(weight, scales)
    rejected.linear(activations)
    ledger = rejected.ledger()
    assert ledger.full_weight_materializations >= 1
    with pytest.raises(Int8KernelUnavailable) as raised:
        rejected.assert_warm_decode()
    assert raised.value.code == "warm_decode_not_proven"


def test_unsupported_weight_representations_are_refused() -> None:
    weight, scales, _ = _fixture()
    with pytest.raises(Int8KernelUnavailable) as raised:
        Int8LinearKernel(weight.astype(np.float32), scales)
    assert raised.value.code == "weight_representation_unsupported"
    with pytest.raises(Int8KernelUnavailable) as raised:
        Int8LinearKernel(weight, np.zeros(weight.shape[0], dtype=np.float32))
    assert raised.value.code == "weight_scale_invalid"
    with pytest.raises(Int8KernelUnavailable) as raised:
        Int8LinearKernel(weight, scales, workspace_bytes=16)
    assert raised.value.code == "workspace_budget_invalid"


def test_activation_width_must_match_the_packed_weight() -> None:
    weight, scales, _ = _fixture()
    kernel = Int8LinearKernel(weight, scales, workspace_bytes=2048)
    with pytest.raises(Int8KernelUnavailable) as raised:
        kernel.linear(np.zeros((3, weight.shape[1] + 1), dtype=np.float32))
    assert raised.value.code == "activation_width_mismatch"


def test_reference_host_is_correctness_only_and_never_accelerated() -> None:
    assert KERNEL_HOST_BACKEND == "numpy"
    assert KERNEL_HOST_BACKEND in CORRECTNESS_ONLY_HOSTS
    assert backend_is_correctness_only(KERNEL_HOST_BACKEND) is True
    assert backend_is_correctness_only("future_native_provider") is False


def test_windows_accelerated_kernel_remains_blocked() -> None:
    with pytest.raises(Int8KernelUnavailable) as raised:
        accelerated_windows_kernel_required()
    assert raised.value.code == "windows_x86_64_provider_not_physically_qualified"
    assert "windows_x86_64_provider_not_physically_qualified" in BOUNDARY_CODES
    assert DEFAULT_WORKSPACE_BYTES > 0


def test_acceptance_case_int8_decode_materialization_rejection_is_not_earned() -> None:
    inventory = json.loads(
        (Path(__file__).parent / "inventory.v1.json").read_text(encoding="utf-8")
    )
    assert inventory["state"] == "design_only"
    cases = {case["case_id"]: case for case in inventory["acceptance_cases"]}
    case = cases["int8_decode_materialization_rejection"]
    assert case["gate_kind"] == "physical_negative"
    covered = ACCEPTANCE_COVERAGE["int8_decode_materialization_rejection"]
    assert covered.startswith("not_earned:")
    assert "physical" in covered
