# SPDX-License-Identifier: AGPL-3.0-only
"""Honest benchmark harness boundary — numbers without a qualification claim.

The harness in ``mycelium_platform_neutral/efficiency_benchmark.py`` measures the
local reference host so the source tranche has real numbers attached.  These tests
pin that a report can never read as hardware qualification, that the Windows
x86_64 device blocker stays named, and that the frozen A9 thresholds are absent
rather than asserted.
"""

from __future__ import annotations

import copy
import math

import pytest

from mycelium_platform_neutral.efficiency_benchmark import (
    BENCHMARK_KIND,
    BLOCKER_CODE,
    FROZEN_A9_THRESHOLD_KEYS,
    assert_no_qualification_claim,
    run_benchmark,
)
from mycelium_platform_neutral.int8_execution import Int8KernelUnavailable


@pytest.fixture(scope="module")
def report() -> dict:
    return run_benchmark(iterations=2, shapes=((512, 4096),))


def test_report_is_labelled_a_local_microbenchmark(report: dict) -> None:
    assert report["benchmark_kind"] == BENCHMARK_KIND
    assert report["hardware_qualified"] is False
    assert report["qualification_claim"] is False
    assert report["frozen_a9_benchmark"] is False
    assert report["windows_x86_64_qualified"] is False


def test_report_names_the_device_blocker(report: dict) -> None:
    assert report["blocker"] == BLOCKER_CODE
    assert report["blocker"] == "windows_x86_64_provider_not_physically_qualified"
    assert report["kernel_host_backend"] == "numpy"
    assert report["correctness_only_host"] is True


def test_reported_numbers_are_real_measured_values(report: dict) -> None:
    kernel = report["int8_linear"]
    assert kernel["full_weight_materializations"] == 0
    assert kernel["bounded_slab_conversions"] >= 1
    assert kernel["packed_state_bytes"] > 0
    assert kernel["peak_workspace_bytes"] > 0
    assert kernel["peak_workspace_bytes"] < kernel["whole_weight_float32_bytes"]
    assert kernel["decode_token_rate_per_second"] > 0
    assert kernel["reference_token_rate_per_second"] > 0
    assert kernel["decode_speedup_vs_reference"] > 0
    assert math.isfinite(kernel["max_abs_error"])
    assert math.isfinite(kernel["relative_error"])
    assert kernel["integer_accumulation_exact"] is True
    codec = report["activation_codec"]
    assert codec["bytes_before"] == codec["float32_bytes"]
    assert codec["bytes_after"] == codec["float16_bytes"]
    assert codec["byte_reduction_ratio"] == 2.0
    assert math.isfinite(codec["encode_mb_per_second"])
    assert codec["rejections_observed"] == 7


def test_frozen_a9_thresholds_are_absent_not_asserted(report: dict) -> None:
    for key in FROZEN_A9_THRESHOLD_KEYS:
        assert key not in report
        assert key not in report["int8_linear"]
    assert report["thresholds_evaluated"] is False
    assert report["reliability_accounting"] is None


def test_a_tampered_report_is_refused_by_the_claim_guard(report: dict) -> None:
    assert_no_qualification_claim(report)
    tampered = copy.deepcopy(report)
    tampered["hardware_qualified"] = True
    with pytest.raises(Int8KernelUnavailable) as raised:
        assert_no_qualification_claim(tampered)
    assert raised.value.code == "benchmark_must_not_claim_qualification"
    tampered = copy.deepcopy(report)
    tampered["blocker"] = None
    with pytest.raises(Int8KernelUnavailable) as raised:
        assert_no_qualification_claim(tampered)
    assert raised.value.code == "benchmark_must_not_claim_qualification"


def test_report_is_deterministic_for_a_fixed_seed() -> None:
    first = run_benchmark(iterations=1, shapes=((512, 4096),))
    second = run_benchmark(iterations=1, shapes=((512, 4096),))
    assert first["int8_linear"]["max_abs_error"] == second["int8_linear"]["max_abs_error"]
    assert (
        first["activation_codec"]["bytes_after"] == second["activation_codec"]["bytes_after"]
    )


def test_a_matrix_that_fits_one_slab_is_refused_not_reported() -> None:
    with pytest.raises(Int8KernelUnavailable) as raised:
        run_benchmark(iterations=1, shapes=((16, 32),))
    assert raised.value.code == "materializing_shape_rejected"


def test_report_explains_what_the_portable_path_numbers_mean(report: dict) -> None:
    assert isinstance(report["interpretation"], str)
    assert report["interpretation"]
    assert "qualification" in report["interpretation"]
    assert isinstance(report["portable_path_faster_than_blas"], bool)
    assert report["accelerated"] is False
