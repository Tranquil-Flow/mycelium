# SPDX-License-Identifier: AGPL-3.0-only
"""Shared A11 Wave D fixtures: a tiny two-stage GPT-2 whose greedy decode varies.

The runtime suites' generated GPT-2 (``two_process_runtime_qualification``) decodes
the same token forever, which would let a broken KV rollback pass unnoticed.  This
builder keeps that model's file layout, manifest compiler and provisioning path but
draws seeded random weights at a scale that makes greedy continuations vary.
Nothing is downloaded.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import mlx.core as mx

from layer_assignment import compile_layer_assignments
from model_manifest import compile_model_manifest
from mycelium_router.mlx_runtime import MLXRuntimePort
from runtime_loader import load_assignment_stage
from test_router_mlx_runtime import _build_graph
from two_process_runtime_qualification import (
    DEPLOYMENT_EPOCH,
    DEPLOYMENT_ID,
    LOAD_GENERATION,
    MODEL_ID,
    RESOLVED_COMMIT,
    SHARD_NAMES,
    _LocalOnlyFetcher,
    _control_plane_binding,
    _route_for_manifest,
)
from weight_provisioning import provision_assignment, sha256_file

VOCAB = 16
EMBED = 8
INNER = 16


def _normal(shape, scale, key):
    return mx.random.normal(shape, key=key) * scale


def _layer(layer: int, key) -> dict[str, mx.array]:
    keys = mx.random.split(key, 8)
    prefix = f"transformer.h.{layer}."
    return {
        prefix + "ln_1.weight": mx.ones((EMBED,)),
        prefix + "ln_1.bias": mx.zeros((EMBED,)),
        prefix + "attn.c_attn.weight": _normal((EMBED, 3 * EMBED), 0.6, keys[0]),
        prefix + "attn.c_attn.bias": _normal((3 * EMBED,), 0.1, keys[1]),
        prefix + "attn.c_proj.weight": _normal((EMBED, EMBED), 0.6, keys[2]),
        prefix + "attn.c_proj.bias": _normal((EMBED,), 0.1, keys[3]),
        prefix + "ln_2.weight": mx.ones((EMBED,)),
        prefix + "ln_2.bias": mx.zeros((EMBED,)),
        prefix + "mlp.c_fc.weight": _normal((EMBED, INNER), 0.6, keys[4]),
        prefix + "mlp.c_fc.bias": _normal((INNER,), 0.1, keys[5]),
        prefix + "mlp.c_proj.weight": _normal((INNER, EMBED), 0.6, keys[6]),
        prefix + "mlp.c_proj.bias": _normal((EMBED,), 0.1, keys[7]),
    }


def build_varied_gpt2(root: Path, *, n_positions: int = 48, seed: int = 11) -> dict:
    keys = mx.random.split(mx.random.key(seed), 5)
    first = {
        "transformer.wte.weight": _normal((VOCAB, EMBED), 1.0, keys[0]),
        "transformer.wpe.weight": _normal((n_positions, EMBED), 0.3, keys[1]),
        **_layer(0, keys[2]),
    }
    second = {
        **_layer(1, keys[3]),
        "transformer.ln_f.weight": mx.ones((EMBED,)),
        "transformer.ln_f.bias": mx.zeros((EMBED,)),
        "lm_head.weight": _normal((VOCAB, EMBED), 1.0, keys[4]),
    }
    for name, tensors in zip(SHARD_NAMES, (first, second)):
        mx.save_safetensors(str(root / name), tensors)
    config = {
        "model_type": "gpt2",
        "architectures": ["GPT2LMHeadModel"],
        "n_layer": 2,
        "n_embd": EMBED,
        "n_head": 2,
        "n_inner": INNER,
        "vocab_size": VOCAB,
        "n_positions": n_positions,
        "layer_norm_epsilon": 1e-5,
        "activation_function": "gelu_new",
        "scale_attn_weights": True,
        "scale_attn_by_inverse_layer_idx": False,
        "reorder_and_upcast_attn": False,
        "add_cross_attention": False,
        "tie_word_embeddings": False,
    }
    index = {
        "metadata": {"total_size": sum((root / name).stat().st_size for name in SHARD_NAMES)},
        "weight_map": {
            tensor: name for name, tensors in zip(SHARD_NAMES, (first, second)) for tensor in sorted(tensors)
        },
    }
    (root / "config.json").write_text(json.dumps(config, sort_keys=True) + "\n")
    (root / "model.safetensors.index.json").write_text(json.dumps(index, sort_keys=True) + "\n")
    return compile_model_manifest(
        model_id=MODEL_ID,
        requested_revision="offline-generated",
        resolved_commit=RESOLVED_COMMIT,
        config=config,
        checkpoint_index=index,
        file_metadata={
            name: {"size_bytes": (root / name).stat().st_size, "sha256": sha256_file(root / name)}
            for name in SHARD_NAMES
        },
    )


def varied_two_stage_case(*, n_positions: int = 48, seed: int = 11):
    """(temporary, case) with ``graph``, ``loaded`` and ``assignments`` of a two-stage route."""

    temporary = tempfile.TemporaryDirectory(prefix="mycelium-a11-route-")
    root = Path(temporary.name)
    manifest = build_varied_gpt2(root, n_positions=n_positions, seed=seed)
    route = _route_for_manifest(manifest)
    assignments = compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=DEPLOYMENT_EPOCH,
        cache_roots={node: str(root) for node in route["node_order"]},
        runtime_by_node={
            node: {"backend": "mlx", "dtype": "float32", "quantization": "none"}
            for node in route["node_order"]
        },
        control_plane_binding=_control_plane_binding(),
    )
    fetcher = _LocalOnlyFetcher(root)
    reports = [provision_assignment(a, fetch_file=fetcher, local_files_only=True) for a in assignments]
    loaded = tuple(
        load_assignment_stage(a, r, load_generation=LOAD_GENERATION) for a, r in zip(assignments, reports)
    )
    graph = _build_graph(assignments, loaded)
    return temporary, SimpleNamespace(graph=graph, loaded=loaded, assignments=assignments, vocab=VOCAB)


def fresh_ports(case, **kwargs):
    return tuple(
        MLXRuntimePort(
            assignment["node_id"],
            case.graph,
            {case.graph.stages[index].placements[0].placement_id: case.loaded[index]},
            **kwargs,
        )
        for index, assignment in enumerate(case.assignments)
    )
