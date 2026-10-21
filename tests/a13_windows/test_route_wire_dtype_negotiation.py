# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV29 L2: the operator-plan compiler negotiates the hop dtype instead of assuming it."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from mycelium_a13_macos import serving
from mycelium_a13_macos.serving import (
    A9_ROUTE_MODEL,
    PreparedSource,
    ServingPreparationError,
)
from tests.a13_windows.test_route_model_and_node_runtime import _plan

_MIXED = {"node-mac": "mlx", "node-win": "numpy"}


def _prepared(tmp_path: Path, model) -> tuple[PreparedSource, str]:
    digest = "sha256:" + hashlib.sha256(b"m").hexdigest()
    return (
        PreparedSource(
            model=model, source_root=Path(tmp_path), source_bytes=1, manifest={}, manifest_digest=digest
        ),
        digest,
    )


def test_float32_mixed_route_still_validates(tmp_path):
    prepared, digest = _prepared(tmp_path, A9_ROUTE_MODEL)
    plan = _plan(tmp_path, A9_ROUTE_MODEL, digest, _MIXED)
    serving._validate_operator_plan(plan, prepared, _MIXED)


@pytest.mark.parametrize("backends", [_MIXED, {"node-a": "mlx", "node-b": "mlx"}])
def test_a_reduced_precision_hop_dtype_is_refused_at_plan_validation(tmp_path, monkeypatch, backends):
    rogue = replace(A9_ROUTE_MODEL, runtime_dtype="float16")
    monkeypatch.setattr(serving, "REGISTERED_MODELS", (*serving.REGISTERED_MODELS, rogue))
    prepared, digest = _prepared(tmp_path, rogue)
    plan = _plan(tmp_path, rogue, digest, backends)
    with pytest.raises(ServingPreparationError, match="operator_plan_wire_dtype_mismatch"):
        serving._validate_operator_plan(plan, prepared, backends)


def test_a_fenced_backend_cannot_be_named_by_an_operator_plan(tmp_path):
    prepared, digest = _prepared(tmp_path, A9_ROUTE_MODEL)
    plan = _plan(tmp_path, A9_ROUTE_MODEL, digest, _MIXED)
    with pytest.raises(ServingPreparationError, match="representation_mismatch"):
        serving._validate_operator_plan(plan, prepared, {"node-mac": "mlx", "node-win": "llama_stage"})
