# SPDX-License-Identifier: AGPL-3.0-only
"""The A13 macOS serving seam admits a registered Qwen3 dense contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from mycelium_a13_macos import serving
from mycelium_a13_macos.serving import (
    CANDIDATE_MODEL,
    REGISTERED_MODELS,
    ServingPreparationError,
    prepare_local_source,
    selected_run_model,
)


REAL_SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots/"
    "c1899de289a04d12100db370d81485cdf75e47ca"
).expanduser()


def _snapshot(tmp_path: Path, model, model_type: str) -> Path:
    snapshot = (
        tmp_path
        / ("models--" + model.model_id.replace("/", "--"))
        / "snapshots"
        / model.resolved_commit
    )
    snapshot.mkdir(parents=True)
    architecture = "Qwen3ForCausalLM" if model_type == "qwen3" else "Qwen2ForCausalLM"
    (snapshot / "config.json").write_text(
        json.dumps(
            {
                "architectures": [architecture],
                "model_type": model_type,
                "torch_dtype": "bfloat16",
            }
        ),
        encoding="utf-8",
    )
    (snapshot / "model.safetensors").write_bytes(b"safe-test-header")
    return snapshot


def _compiler(model, architecture: str):
    def compiler(_source):
        return {
            "model_id": model.model_id,
            "resolved_commit": model.resolved_commit,
            "runtime_model": {"architecture": architecture, "model_config": {}},
            "manifest_digest": {"algorithm": "sha256", "value": "b" * 64},
        }

    return compiler


def test_qwen3_0_6b_contract_is_registered_and_pinned() -> None:
    model = serving.QWEN3_0_6B_MODEL

    assert model in REGISTERED_MODELS
    assert model.model_id == "Qwen/Qwen3-0.6B"
    assert model.resolved_commit == "c1899de289a04d12100db370d81485cdf75e47ca"
    assert model.architecture == "qwen3"
    assert model.runtime_backend == "mlx"
    assert model.runtime_quantization == "int8-weight-only"
    assert CANDIDATE_MODEL.architecture == "qwen2"
    assert selected_run_model({"MYCELIUM_A13_RUN_MODEL": "qwen3-0.6b"}) == model
    # The default selection is unchanged.
    assert selected_run_model({}) == CANDIDATE_MODEL


def test_qwen3_source_prepares_through_the_compiler_seam(tmp_path: Path) -> None:
    model = serving.QWEN3_0_6B_MODEL
    snapshot = _snapshot(tmp_path, model, "qwen3")

    prepared = prepare_local_source(
        snapshot, model=model, compiler=_compiler(model, "qwen3")
    )

    assert prepared.model == model
    assert prepared.manifest["runtime_model"]["architecture"] == "qwen3"


def test_architecture_must_match_the_selected_contract(tmp_path: Path) -> None:
    model = serving.QWEN3_0_6B_MODEL
    with pytest.raises(ServingPreparationError, match="source_architecture_mismatch"):
        prepare_local_source(
            _snapshot(tmp_path / "a", model, "qwen2"),
            model=model,
            compiler=_compiler(model, "qwen3"),
        )
    with pytest.raises(ServingPreparationError, match="source_architecture_mismatch"):
        prepare_local_source(
            _snapshot(tmp_path / "b", CANDIDATE_MODEL, "qwen3"),
            compiler=_compiler(CANDIDATE_MODEL, "qwen3"),
        )
    with pytest.raises(
        ServingPreparationError, match="native_manifest_identity_mismatch"
    ):
        prepare_local_source(
            _snapshot(tmp_path / "c", model, "qwen3"),
            model=model,
            compiler=_compiler(model, "qwen2"),
        )


@pytest.mark.skipif(
    not (REAL_SNAPSHOT / "model.safetensors").exists(),
    reason="pinned Qwen/Qwen3-0.6B snapshot absent",
)
def test_real_qwen3_0_6b_snapshot_prepares_with_the_production_compiler(
    tmp_path: Path,
) -> None:
    # The app reads a materialized snapshot (regular files, as its own
    # acquisition writes them), never HF-cache symlinks: hard-link the blobs.
    model = serving.QWEN3_0_6B_MODEL
    snapshot = (
        tmp_path
        / ("models--" + model.model_id.replace("/", "--"))
        / "snapshots"
        / model.resolved_commit
    )
    snapshot.mkdir(parents=True)
    for entry in REAL_SNAPSHOT.iterdir():
        source = entry.resolve(strict=True)
        try:
            os.link(source, snapshot / entry.name)
        except OSError:
            shutil.copyfile(source, snapshot / entry.name)

    prepared = prepare_local_source(snapshot, model=model)

    assert prepared.manifest["runtime_model"]["architecture"] == "qwen3"
    assert prepared.manifest["num_layers"] == 28
    assert prepared.source_bytes == 1_503_300_328
