#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline, single-host stage-kernel benchmark; no transport/control-path claims.

Compares both explicit product materialization modes, with the same authenticated
loader, weights, prompt, KV, arithmetic and token selector. No downloads.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
# Fixed prompt token IDs, identical for every arm/window (no tokenizer download).
PROMPT = (151644, 8948, 198, 2610, 525, 264, 10950, 17847, 13, 151645,
          198, 151644, 872, 198, 9707, 151645, 198, 151644, 77091, 198)


def check_idle():
    for name in ("DEVICE.busy", "CERT.busy"):
        if (Path.home() / "mycelium-demo-run" / name).exists():
            raise RuntimeError(f"benchmark_blocked:{name}")


def snapshot_path(explicit=None):
    candidates = [Path(explicit).expanduser()] if explicit else [
        Path.home() / ".cache/huggingface/hub" / "models--Qwen--Qwen2.5-0.5B-Instruct" / "snapshots" / REVISION,
        Path.home() / "Library/Application Support/Mycelium A13 macOS Demo" /
        "model-source-7ae557604adf/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots" / REVISION,
    ]
    for path in candidates:
        if path.name == REVISION and all((path / name).is_file() for name in ("config.json", "model.safetensors")):
            return path
    raise RuntimeError("pinned_model_not_cached:no_download_permitted")


def stats(samples):
    ordered = sorted(samples)
    return {"count": len(samples), "min_ms": min(samples), "median_ms": statistics.median(samples),
            "p90_ms": ordered[math.ceil(len(ordered) * .9) - 1], "max_ms": max(samples)}


def run(args):
    check_idle()
    snapshot = snapshot_path(args.snapshot)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import mlx.core as mx
    import numpy as np
    from mycelium_qualification.physical_deployment import (
        LocalModelSource, prepare_assignment_artifacts, build_execution_graph,
    )
    from runtime_loader import load_assignment_stage
    from mycelium_router.mlx_runtime import MLXRuntimePort, quantized_greedy_token_ids

    host = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    document = {
        "protocol": "mycelium.stage_decode_microbenchmark.v1", "executed_by": "local_cli",
        "claim_ceiling": "Single-host stage kernel only: no network, admission, serialization, or control path. Shared-host diagnostic, not a causal confidence interval.",
        "host": host, "load_start": os.getloadavg(), "revision": REVISION,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "runtime_sha256": hashlib.sha256((ROOT / "mycelium_router/mlx_runtime.py").read_bytes()).hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "mlx_version": mx.__version__, "device": str(mx.default_device()),
        "prompt_ids": PROMPT, "windows": args.windows, "decode_tokens_per_window": args.tokens,
        "warmup_decode_tokens": 8, "selection": "product quantized_greedy_token_ids (excluded from kernel timer)",
        "timed": "_execute_stage_with_kv, including explicit materialization and finite check; excludes prefill/loading/token selection/logit hashing",
        "reference_mlx_lm": {"status": "NOT_IMPLEMENTED", "reason": "mlx_lm is not installed in the test venv"},
        "cells": [], "parity": [],
    }
    if importlib.util.find_spec("mlx_lm") is not None:
        raise RuntimeError("mlx_lm_present:reference_arm_required_before_campaign")
    scratch = Path.home() / ".hermes/cache/scratch"
    for geometry, ranges in (("full", (range(24),)), ("split", (range(23), range(23, 24)))):
        check_idle()
        with tempfile.TemporaryDirectory(prefix="fasteval-bench-", dir=scratch) as tmp:
            prepared = prepare_assignment_artifacts(
                Path(tmp) / "model", node_ids=tuple(f"bench-{i}" for i in range(len(ranges))),
                model_source=LocalModelSource(root=snapshot, model_id=MODEL, resolved_commit=REVISION),
                runtime_dtype="float32", runtime_quantization="int8-weight-only", layer_ranges=ranges,
            )
            loaded = [load_assignment_stage(a, r, load_generation=1)
                      for a, r in zip(prepared.assignments, prepared.reports)]
            graph = build_execution_graph(prepared.assignments, [x.proof for x in loaded])
            arms = {}
            for mode in ("per_layer", "per_stage"):
                ports = [MLXRuntimePort(a["node_id"], graph, {s.placements[0].placement_id: l}, materialization=mode)
                         for a, s, l in zip(prepared.assignments, graph.stages, loaded)]
                arms[mode] = ports
            records = {mode: [] for mode in arms}

            def window(mode, count):
                check_idle()
                ports = arms[mode]
                past = [{} for _ in ports]
                step = mx.array([PROMPT], dtype=mx.uint32)
                position = 0
                tokens, digests, times = [], [], [[] for _ in ports]
                for index in range(count + 1):  # prefill then exactly count decode steps
                    check_idle()
                    hidden = None
                    for j, (port, stage, item, assignment) in enumerate(zip(ports, graph.stages, loaded, prepared.assignments)):
                        started = time.perf_counter()
                        hidden, past[j] = port._execute_stage_with_kv(
                            stage=stage, loaded=item, runtime=assignment["runtime"],
                            token_ids=step if j == 0 else None, hidden_states=hidden,
                            position=position, past_layers=past[j], path_id="bench-path",
                        )
                        elapsed = (time.perf_counter() - started) * 1000
                        if index:
                            times[j].append(elapsed)
                    token = quantized_greedy_token_ids(hidden[0, -1:, :])[0]
                    tokens.append(token)
                    digests.append(hashlib.sha256(np.asarray(hidden).tobytes()).hexdigest())
                    position += int(step.shape[1])
                    step = mx.array([[token]], dtype=mx.uint32)
                return {"tokens": tokens, "logit_sha256": digests, "stage_ms": times}

            for mode in arms:
                window(mode, 8)
            for repetition in range(args.windows):
                for mode in (("per_layer", "per_stage") if repetition % 2 == 0 else ("per_stage", "per_layer")):
                    load = os.getloadavg()
                    row = window(mode, args.tokens)
                    row.update(window=repetition, load=load)
                    records[mode].append(row)
                    print(f"{geometry} {mode} window={repetition} load={load[0]:.2f} complete", flush=True)
                a = next(x for x in records["per_layer"] if x["window"] == repetition)
                b = next(x for x in records["per_stage"] if x["window"] == repetition)
                document["parity"].append({"geometry": geometry, "window": repetition,
                    "tokens_bit_identical": a["tokens"] == b["tokens"],
                    "logits_bit_identical": a["logit_sha256"] == b["logit_sha256"]})
            for mode, rows in records.items():
                for j, span in enumerate(ranges):
                    samples = [v for r in rows for v in r["stage_ms"][j]]
                    document["cells"].append({"geometry": geometry, "layers": [span.start, span.stop],
                        "mode": mode, "implementation": "product",
                        "label": f"measured (M4 Pro, load {statistics.median(r['load'][0] for r in rows):.2f})",
                        **stats(samples), "samples_ms": samples, "windows": rows if j == 0 else None,
                        "work_unit_evidence": arms[mode][j].kv_snapshot()})
            del arms, ports, loaded, prepared, window
            gc.collect()
            mx.clear_cache()
        args.output.write_text(json.dumps(document, indent=2) + "\n")
    document["load_end"] = os.getloadavg()
    document["all_parity_passed"] = all(r["tokens_bit_identical"] and r["logits_bit_identical"] for r in document["parity"])
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    return 0 if document["all_parity_passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--tokens", type=int, default=64)
    args = parser.parse_args()
    if args.windows < 1 or args.tokens < 1:
        parser.error("positive windows and tokens required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
