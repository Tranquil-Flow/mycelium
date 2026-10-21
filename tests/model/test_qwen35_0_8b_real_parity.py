# SPDX-License-Identifier: AGPL-3.0-only
"""REAL Qwen/Qwen3.5-0.8B: a two-stage Mycelium MLX route equals single-process mlx-lm.

The route is the production path end to end: ``prepare_assignment_artifacts``
(manifest compile with the vision encoder and MTP head excluded, assignment
compile, provisioning, stage packs and their verification),
``load_assignment_stage`` and two ``MLXRuntimePort`` stages with stage-local
hybrid state (KV for the gated-attention layers, conv + recurrent state for the
Gated DeltaNet layers): one PREFILL hop chain, then one single-token DECODE hop
chain per generated token.  The split is deliberately mid-period ([0, 10) +
[10, 24)) so a stage boundary falls between two linear-attention layers.

The reference is mlx-lm's own ``qwen3_5`` implementation (independent code:
fused gated-delta Metal kernel, its own caches and RoPE), run single-process in
a scratch venv on the same bf16 checkpoint cast to float32, with the prompt
rendered by the shipped chat template (``enable_thinking=False``).  Its tokens
are pinned in ``fixtures/qwen35_0_8b_mlx_lm_reference.json`` together with the
mlx-lm version, the checkpoint revision and its weight digest; the ladder venv
does not carry mlx-lm.  A second, in-repo reference (the full-context NumPy
oracle reading the bf16 file directly) must agree with mlx-lm, and is then the
reference for the int8-weight-only route (mlx-lm cannot reproduce Mycelium's
row-wise int8).  Skipped when the pinned snapshot is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import time
from pathlib import Path

import numpy as np
import pytest

from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    build_execution_graph,
    prepare_assignment_artifacts,
)
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.mlx_runtime import MLXRuntimePort
from runtime_loader import load_assignment_stage

from tests.model.test_qwen35_hybrid_mlx_path import _phase

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "qwen35_0_8b_mlx_lm_reference.json").read_text()
)
MODEL_ID = FIXTURE["model_id"]
COMMIT = FIXTURE["resolved_commit"]
SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--Qwen--Qwen3.5-0.8B/snapshots/" + COMMIT
).expanduser()
WEIGHTS = "model.safetensors-00001-of-00001.safetensors"
SOURCE_FILES = (
    "config.json",
    "model.safetensors.index.json",
    WEIGHTS,
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
)

requires_snapshot = pytest.mark.skipif(
    not (SNAPSHOT / WEIGHTS).exists(),
    reason="pinned Qwen/Qwen3.5-0.8B snapshot absent",
)


def _plain_source(tmp_path: Path) -> Path:
    """Materialize the snapshot as regular files.

    Newer huggingface_hub caches store blobs in a shared store behind a second
    symlink, which the local-source reader refuses by design (it accepts only the
    classic snapshot -> ../../blobs/<sha> link).  APFS clones keep this cheap.
    """

    root = tmp_path / "source"
    root.mkdir()
    for name in SOURCE_FILES:
        shutil.copyfile((SNAPSHOT / name).resolve(strict=True), root / name)
    return root


def _route_tokens(tmp_path: Path, quantization: str, steps: int) -> tuple[list[int], dict]:
    started = time.monotonic()
    # Split mid-period, [0, 10) + [10, 24): the stage boundary falls between two
    # Gated DeltaNet layers, not at a full-attention layer.
    prepared = prepare_assignment_artifacts(
        tmp_path / "deployment",
        node_ids=("node-000", "node-001"),
        model_source=LocalModelSource(
            root=_plain_source(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
        ),
        runtime_dtype="float32",
        runtime_quantization=quantization,
        layer_ranges=(range(0, 10), range(10, 24)),
    )
    manifest = prepared.manifest
    assert manifest["runtime_model"]["architecture"] == "qwen3_5"
    assert manifest["runtime_model"]["model_config"]["tie_word_embeddings"] is True
    assert len(manifest["excluded_tensor_keys"]) == 153 + 15  # vision encoder + MTP head
    assert prepared.assignments[1]["component_aliases"] == {"lm_head": "input_embedding"}
    loaded = [
        load_assignment_stage(assignment, report, load_generation=1)
        for assignment, report in zip(prepared.assignments, prepared.reports, strict=True)
    ]
    assert not any(
        key.startswith(("model.visual.", "mtp.")) for stage in loaded for key in stage.tensors
    )
    graph = build_execution_graph(
        prepared.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    ports = tuple(
        MLXRuntimePort(
            assignment["node_id"],
            graph,
            {graph.stages[index].placements[0].placement_id: loaded[index]},
        )
        for index, assignment in enumerate(prepared.assignments)
    )
    prompt = list(FIXTURE["prompt_ids"])
    load_seconds = time.monotonic() - started
    generated = [_phase(ports, graph, prompt, phase="PREFILL", token_index=-1, position=0)]
    for token_index in range(1, steps):
        generated.append(
            _phase(
                ports,
                graph,
                [generated[-1]],
                phase="DECODE",
                token_index=token_index,
                position=len(prompt) + token_index - 1,
                terminal=token_index == steps - 1,
            )
        )
    timing = {
        "load_seconds": round(load_seconds, 2),
        "decode_seconds": round(time.monotonic() - started - load_seconds, 2),
        "stage_int8_execution": [port.int8_execution for port in ports],
    }
    return generated, timing


def _numpy_checkpoint(path: Path) -> dict[str, np.ndarray]:
    raw = path.read_bytes()
    (length,) = struct.unpack("<Q", raw[:8])
    header = json.loads(raw[8 : 8 + length])
    base = 8 + length
    tensors: dict[str, np.ndarray] = {}
    for name, record in header.items():
        if name == "__metadata__" or name.startswith(("model.visual.", "mtp.")):
            continue
        start, end = record["data_offsets"]
        if record["dtype"] == "BF16":
            bits = np.frombuffer(raw, dtype="<u2", count=(end - start) // 2, offset=base + start)
            value = (bits.astype(np.uint32) << 16).view(np.float32)
        else:
            assert record["dtype"] == "F32", (name, record["dtype"])
            value = np.frombuffer(raw, dtype="<f4", count=(end - start) // 4, offset=base + start)
        tensors[name] = value.reshape(record["shape"])
    return tensors


def _oracle_tokens(quantization: str, steps: int) -> list[int]:
    from numpy_runtime import NumpyQwen35Runtime
    from runtime_contracts import normalize_qwen35_model_config

    config = json.loads((SNAPSHOT / "config.json").read_text(encoding="utf-8"))
    reference = NumpyQwen35Runtime(
        model_config=normalize_qwen35_model_config(
            config, expected_layers=config["text_config"]["num_hidden_layers"]
        ),
        tensors=_numpy_checkpoint(SNAPSHOT / WEIGHTS),
        quantization=quantization,
    )
    context = list(FIXTURE["prompt_ids"])
    tokens: list[int] = []
    for _ in range(steps):
        logits = reference.forward_token_ids(np.array([context], dtype=np.int64))
        tokens.append(quantized_greedy_token_id(logits[0, -1].tolist()))
        context.append(tokens[-1])
    return tokens


def _record(name: str, document: dict) -> None:
    evidence = os.environ.get("MYCELIUM_QWEN35_PARITY_DIR")
    if evidence:
        Path(evidence, f"{name}.json").write_text(json.dumps(document, indent=1))
    print(json.dumps(document))


@requires_snapshot
def test_qwen35_0_8b_two_stage_mlx_route_matches_mlx_lm_reference(tmp_path: Path) -> None:
    steps = len(FIXTURE["generated"])
    assert steps >= 16
    generated, timing = _route_tokens(tmp_path, "none", steps)
    _record(
        "real-parity-fp32-vs-mlx-lm",
        {
            "model": f"{MODEL_ID}@{COMMIT}",
            "split": [[0, 10], [10, 24]],
            "runtime": "mlx float32, quantization none",
            "reference": FIXTURE["reference"],
            "prompt_tokens": len(FIXTURE["prompt_ids"]),
            "route_tokens": generated,
            "reference_tokens": FIXTURE["generated"],
            "equal": generated == FIXTURE["generated"],
            **timing,
        },
    )

    assert generated == FIXTURE["generated"]


@requires_snapshot
def test_qwen35_0_8b_numpy_oracle_agrees_with_mlx_lm() -> None:
    """The repo's NumPy oracle, on real weights, is itself a faithful reference."""

    tokens = _oracle_tokens("none", 16)
    _record(
        "real-oracle-fp32-vs-mlx-lm",
        {"oracle_tokens": tokens, "reference_tokens": FIXTURE["generated"][:16]},
    )

    assert tokens == FIXTURE["generated"][:16]


@requires_snapshot
def test_qwen35_0_8b_two_stage_int8_route_matches_numpy_int8_oracle(tmp_path: Path) -> None:
    generated, timing = _route_tokens(tmp_path, "int8-weight-only", 16)
    expected = _oracle_tokens("int8-weight-only", 16)
    _record(
        "real-parity-int8-vs-numpy-oracle",
        {
            "model": f"{MODEL_ID}@{COMMIT}",
            "split": [[0, 10], [10, 24]],
            "runtime": "mlx float32 activations, int8-weight-only",
            "reference": "repo NumpyQwen35Runtime full context, same row-wise int8 weights",
            "route_tokens": generated,
            "reference_tokens": expected,
            "equal": generated == expected,
            **timing,
        },
    )

    assert generated == expected
