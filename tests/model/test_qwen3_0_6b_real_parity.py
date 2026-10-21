# SPDX-License-Identifier: AGPL-3.0-only
"""REAL Qwen/Qwen3-0.6B: a two-stage MLX route equals a single-process reference.

The route is the production path end to end: ``prepare_physical_deployment``
(manifest compile, safetensors stage sharding, stage packs, provisioning),
``load_assignment_stage`` and two ``MLXRuntimePort`` stages with stage-local KV
(one PREFILL hop chain, then one DECODE hop chain per token).  The reference is
the repository's independent NumPy ``NumpyQwen3Runtime`` running the complete
context in one process from the original bf16 checkpoint (no MLX involved; the
safetensors file is parsed with NumPy).  Both sides pick tokens with the shared
``quantized_greedy_token_id`` rule.  Skipped when the pinned snapshot is absent.
"""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import numpy as np
import pytest

from mycelium_live.codec import Qwen3PromptCodec, prompt_codec_from_deployment
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    build_execution_graph,
    prepare_physical_deployment,
)
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.mlx_runtime import MLXRuntimePort
from numpy_runtime import NumpyQwen3Runtime
from runtime_contracts import normalize_qwen3_model_config
from runtime_loader import load_assignment_stage

from tests.model.test_qwen3_dense_mlx_path import _phase

MODEL_ID = "Qwen/Qwen3-0.6B"
COMMIT = "c1899de289a04d12100db370d81485cdf75e47ca"
SNAPSHOT = Path(
    "~/.cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots/" + COMMIT
).expanduser()
PROMPT = "Name three primary colours, separated by commas."
GENERATED_TOKENS = 20

requires_snapshot = pytest.mark.skipif(
    not (SNAPSHOT / "model.safetensors").exists(),
    reason="pinned Qwen/Qwen3-0.6B snapshot absent",
)


def _numpy_bf16_checkpoint(path: Path) -> dict[str, np.ndarray]:
    raw = path.read_bytes()
    (length,) = struct.unpack("<Q", raw[:8])
    header = json.loads(raw[8 : 8 + length])
    base = 8 + length
    tensors: dict[str, np.ndarray] = {}
    for name, record in header.items():
        if name == "__metadata__":
            continue
        assert record["dtype"] == "BF16", (name, record["dtype"])
        start, end = record["data_offsets"]
        bits = np.frombuffer(raw, dtype="<u2", count=(end - start) // 2, offset=base + start)
        tensors[name] = (
            (bits.astype(np.uint32) << 16).view(np.float32).reshape(record["shape"])
        )
    return tensors


def _reference_tokens(
    prompt_ids: tuple[int, ...], quantization: str, steps: int
) -> list[int]:
    config = json.loads((SNAPSHOT / "config.json").read_text(encoding="utf-8"))
    runtime = {
        "backend": "numpy",
        "dtype": "float32",
        "quantization": quantization,
        "architecture": "qwen3",
        "model_config": normalize_qwen3_model_config(
            config, expected_layers=config["num_hidden_layers"]
        ),
    }
    tensors = _numpy_bf16_checkpoint(SNAPSHOT / "model.safetensors")
    # The checkpoint stores a redundant lm_head.weight although
    # tie_word_embeddings is true; HF ties it to the embedding (as the Mycelium
    # manifest does).  Prove the copy is bit-identical, then drop it.
    assert config["tie_word_embeddings"] is True
    assert np.array_equal(
        tensors.pop("lm_head.weight"), tensors["model.embed_tokens.weight"]
    )
    reference = NumpyQwen3Runtime(runtime=runtime, tensors=tensors)
    context = list(prompt_ids)
    tokens: list[int] = []
    for _ in range(steps):
        logits = reference.forward_token_ids(np.array([context], dtype=np.int64))
        tokens.append(quantized_greedy_token_id(logits[0, -1].tolist()))
        context.append(tokens[-1])
    return tokens


@requires_snapshot
@pytest.mark.parametrize("quantization", ("none", "int8-weight-only"))
def test_real_qwen3_0_6b_two_stage_mlx_route_matches_single_process_reference(
    tmp_path: Path, quantization: str
) -> None:
    codec = prompt_codec_from_deployment(SNAPSHOT)
    assert isinstance(codec, Qwen3PromptCodec)
    prompt_ids = codec.encode(PROMPT)

    started = time.monotonic()
    deployment = prepare_physical_deployment(
        tmp_path / "qwen3-0.6b",
        node_ids=("node-000", "node-001"),
        model_source=LocalModelSource(
            root=SNAPSHOT,
            model_id=MODEL_ID,
            requested_revision="main",
            resolved_commit=COMMIT,
        ),
        runtime_dtype="float32",
        runtime_quantization=quantization,
    )
    assert deployment.manifest["runtime_model"]["architecture"] == "qwen3"
    ranges = [
        (a["range"]["start_layer"], a["range"]["end_layer_exclusive"])
        for a in deployment.assignments
    ]
    assert ranges[0][0] == 0 and ranges[-1][1] == 28 and ranges[0][1] == ranges[1][0]
    loaded = [
        load_assignment_stage(assignment, report, load_generation=17)
        for assignment, report in zip(
            deployment.assignments, deployment.artifact_reports, strict=True
        )
    ]
    graph = build_execution_graph(
        deployment.assignments,
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
        for index, assignment in enumerate(deployment.assignments)
    )
    generated = [
        _phase(ports, graph, prompt_ids, phase="PREFILL", token_index=-1, position=0)
    ]
    for token_index in range(1, GENERATED_TOKENS):
        generated.append(
            _phase(
                ports,
                graph,
                (generated[-1],),
                phase="DECODE",
                token_index=token_index,
                position=len(prompt_ids) + token_index - 1,
                terminal=token_index == GENERATED_TOKENS - 1,
            )
        )
    route_seconds = time.monotonic() - started
    del ports, loaded

    started = time.monotonic()
    expected = _reference_tokens(prompt_ids, quantization, GENERATED_TOKENS)
    reference_seconds = time.monotonic() - started

    decoder = codec.new_decoder()
    text = "".join(decoder.push(token) for token in generated) + decoder.finish()
    print(
        json.dumps(
            {
                "model": f"{MODEL_ID}@{COMMIT}",
                "quantization": quantization,
                "layer_ranges": ranges,
                "prompt_token_count": len(prompt_ids),
                "route_tokens": generated,
                "reference_tokens": expected,
                "equal": generated == expected,
                "route_text": text,
                "route_seconds": round(route_seconds, 2),
                "reference_seconds": round(reference_seconds, 2),
            }
        )
    )
    assert generated == expected
    assert "<think>" not in text
