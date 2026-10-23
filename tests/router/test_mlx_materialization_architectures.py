# SPDX-License-Identifier: AGPL-3.0-only
"""Run adjacent real tiny-model oracles through the new serving mode."""
from functools import partial

import pytest

from mycelium_router.mlx_runtime import MLXRuntimePort
from tests.model import test_qwen3_dense_mlx_path as dense
from tests.model import test_qwen35_hybrid_mlx_path as hybrid


@pytest.mark.parametrize("quantization", ["none", "int8-weight-only"])
def test_qwen3_serving_preserves_existing_oracle(tmp_path, monkeypatch, quantization):
    monkeypatch.setattr(dense, "MLXRuntimePort", partial(MLXRuntimePort, materialization="per_stage"))
    dense.test_qwen3_two_stage_mlx_stage_local_kv_matches_monolithic_reference(
        tmp_path, "float32", quantization,
    )


@pytest.mark.parametrize("quantization", ["none", "int8-weight-only"])
def test_qwen35_serving_preserves_existing_oracle(tmp_path, monkeypatch, quantization):
    monkeypatch.setattr(hybrid, "MLXRuntimePort", partial(MLXRuntimePort, materialization="per_stage"))
    hybrid.test_qwen35_two_stage_mlx_stage_local_state_matches_monolithic_oracle(tmp_path, quantization)
