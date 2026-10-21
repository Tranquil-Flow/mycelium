# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25 local device check: RSS before/after an idle unload and reload time, on this Mac.

Builds a deterministic GPT-2-shaped two-stage model that is large enough for resident memory to
show (default ~0.5 GB float32, generated locally, nothing downloaded), loads stage 0 into the real
``MLXRuntimePort`` and drives the real ``NodeIdleLink``/``WeightResidency`` over it:

* one cycle through the real idle policy (``--idle-minutes 1`` waits ~65 s of wall time),
* ``--cycles`` manual unload/reload cycles.

Every cycle records resident set size (``ps``) and MLX active memory before the unload, after it
and after the reload, the reload time, and checks the greedy token before == after.  Prints one JSON
document.  Pure local: no network, no sudo, no powermetrics.
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mlx.core as mx  # noqa: E402

import two_process_runtime_qualification as tp  # noqa: E402
from layer_assignment import compile_layer_assignments  # noqa: E402
from model_manifest import compile_model_manifest  # noqa: E402
from mycelium_idle import node_link, policy  # noqa: E402
from mycelium_router.mlx_runtime import MLXRuntimePort  # noqa: E402
from mycelium_router.payloads import encode_token_ids  # noqa: E402
from runtime_loader import load_assignment_stage  # noqa: E402
from weight_provisioning import provision_assignment  # noqa: E402


def _layer(layer: int, d: int, inner: int) -> dict[str, mx.array]:
    prefix = f"transformer.h.{layer}."
    o = 100 * (layer + 1)
    v = tp._values
    return {
        prefix + "ln_1.weight": mx.ones((d,)), prefix + "ln_1.bias": mx.zeros((d,)),
        prefix + "attn.c_attn.weight": v((d, 3 * d), offset=o + 1, scale=2e-9),
        prefix + "attn.c_attn.bias": v((3 * d,), offset=o + 2, scale=1e-6),
        prefix + "attn.c_proj.weight": v((d, d), offset=o + 3, scale=2e-9),
        prefix + "attn.c_proj.bias": v((d,), offset=o + 4, scale=1e-6),
        prefix + "ln_2.weight": mx.ones((d,)), prefix + "ln_2.bias": mx.zeros((d,)),
        prefix + "mlp.c_fc.weight": v((d, inner), offset=o + 5, scale=2e-9),
        prefix + "mlp.c_fc.bias": v((inner,), offset=o + 6, scale=1e-6),
        prefix + "mlp.c_proj.weight": v((inner, d), offset=o + 7, scale=2e-9),
        prefix + "mlp.c_proj.bias": v((d,), offset=o + 8, scale=1e-6),
    }


def build_model(work: Path, d: int, vocab: int, heads: int):
    inner = 4 * d
    first = {"transformer.wte.weight": tp._values((vocab, d), offset=1, scale=1e-9),
             "transformer.wpe.weight": tp._values((16, d), offset=2, scale=5e-7), **_layer(0, d, inner)}
    second = {**_layer(1, d, inner), "transformer.ln_f.weight": mx.ones((d,)),
              "transformer.ln_f.bias": mx.zeros((d,)),
              "lm_head.weight": tp._values((vocab, d), offset=3, scale=7e-10)}
    for name, tensors in zip(tp.SHARD_NAMES, (first, second)):
        tp._save_shard(work / name, tensors)
    config = tp._model_config(n_positions=16)
    config.update({"n_embd": d, "n_head": heads, "n_inner": inner, "vocab_size": vocab})
    weight_map = {t: n for n, ts in zip(tp.SHARD_NAMES, (first, second)) for t in sorted(ts)}
    index = {"metadata": {"total_size": sum((work / n).stat().st_size for n in tp.SHARD_NAMES)},
             "weight_map": weight_map}
    tp._replace_file(work / "config.json", tp._canonical_json(config) + "\n")
    tp._replace_file(work / "model.safetensors.index.json", tp._canonical_json(index) + "\n")
    meta = {n: {"size_bytes": (work / n).stat().st_size, "sha256": tp.sha256_file(work / n)}
            for n in tp.SHARD_NAMES}
    return compile_model_manifest(
        model_id=tp.MODEL_ID, requested_revision="offline-generated", resolved_commit=tp.RESOLVED_COMMIT,
        config=config, checkpoint_index=index, file_metadata=meta)


def rss_bytes() -> int:
    import subprocess
    import os
    out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, check=True)
    return int(out.stdout.decode().strip()) * 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dim", type=int, default=2048)
    parser.add_argument("--vocab", type=int, default=8192)
    parser.add_argument("--heads", type=int, default=16)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--idle-minutes", type=int, default=0, help="also wait for one real policy-driven unload")
    args = parser.parse_args()

    from test_router_mlx_runtime import _build_graph, _work_item  # shared graph/work-item builders

    scratch = Path(tempfile.mkdtemp(prefix="pv25-idle-device-"))
    work = scratch / "model"
    work.mkdir(mode=0o700)
    manifest = build_model(work, args.dim, args.vocab, args.heads)
    route = tp._route_for_manifest(manifest)
    assignments = compile_layer_assignments(
        route_plan=route, manifest=manifest, deployment_id=tp.DEPLOYMENT_ID, deployment_epoch=tp.DEPLOYMENT_EPOCH,
        cache_roots={n: str(work) for n in route["node_order"]},
        runtime_by_node={n: {"backend": "mlx", "dtype": "float32", "quantization": "none"} for n in route["node_order"]},
        control_plane_binding=tp._control_plane_binding())
    fetcher = tp._LocalOnlyFetcher(work)

    def load(index: int):
        report = provision_assignment(assignments[index], fetch_file=fetcher, local_files_only=True)
        return load_assignment_stage(assignments[index], report, load_generation=tp.LOAD_GENERATION)

    loaded = [load(0), load(1)]
    graph = _build_graph(assignments, loaded)
    placement_id = graph.stages[0].placements[0].placement_id
    del loaded[1]  # this process models only the stage-0 member: stage 1's tensors must not be resident
    gc.collect()
    mx.clear_cache()
    port = MLXRuntimePort(assignments[0]["node_id"], graph, {placement_id: loaded[0]})
    capacity_root = scratch / "capacity"
    capacity_root.mkdir(mode=0o700)
    policy.write_policy(capacity_root, 1)
    adapter = node_link.RuntimePortAdapter(port, lambda: load(0))
    link = node_link.NodeIdleLink(capacity_root=capacity_root, port=adapter,
                                  on_reload_failed=lambda code: print("RELOAD FAILED", code, file=sys.stderr))

    def prefill(path: str):
        item = _work_item(types_case(graph, assignments), 0, encode_token_ids((1, 2, 3)), token_span=3,
                          request_id=path, path_id=path, terminal=True)
        result = port.execute(item)
        assert result.success, result.failure_reason
        return bytes(result.payload)

    class types_case:  # minimal object the shared work-item builder reads
        def __init__(self, graph, assignments):
            self.graph, self.assignments = graph, assignments

    def snapshot():
        gc.collect()
        time.sleep(1.0)  # let the OS settle resident pages before sampling
        return {"rss_bytes": rss_bytes(), "mlx_active_bytes": mx.get_active_memory(),
                "mlx_cache_bytes": mx.get_cache_memory()}

    record = {"model_bytes_on_disk": sum((work / n).stat().st_size for n in tp.SHARD_NAMES), "cycles": []}
    baseline = prefill("before")
    record["loaded"] = snapshot()
    if args.idle_minutes:
        policy.write_policy(capacity_root, args.idle_minutes)
        started = time.monotonic()
        while link.tick() != "idle":
            time.sleep(1.0)
            if time.monotonic() - started > args.idle_minutes * 60 + 120:
                raise SystemExit("policy-driven unload never happened")
        record["policy_driven_unload_after_s"] = round(time.monotonic() - started, 1)
        record["after_policy_unload"] = snapshot()
        reload_started = time.monotonic()
        link.work_gate(timeout_s=60.0)
        record["policy_driven_reload_s"] = round(time.monotonic() - reload_started, 3)
        record["after_policy_reload"] = snapshot()
        assert prefill("policy") == baseline
    policy.write_policy(capacity_root, "never")
    for cycle in range(args.cycles):
        before = snapshot()
        assert link.command("idle_unload", {})["state"] == "idle"
        mx.clear_cache()
        after_unload = snapshot()
        started = time.monotonic()
        link.work_gate(timeout_s=60.0)
        reload_s = time.monotonic() - started
        after_reload = snapshot()
        ok = prefill(f"cycle-{cycle}") == baseline
        record["cycles"].append({"before_unload": before, "after_unload": after_unload,
                                 "after_reload": after_reload, "reload_s": round(reload_s, 3),
                                 "output_identical": ok})
    record["status"] = link.residency.snapshot()
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if all(c["output_identical"] for c in record["cycles"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
