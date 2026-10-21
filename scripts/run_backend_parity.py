#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-backend parity gate v0 (PV29 L1) - command line.

Sub-commands (all offline; nothing is downloaded, no device is contacted).  The HF float32 oracle
archive is made by the standalone scripts/backend_parity_hf_oracle.py (needs torch + transformers).

  corpus     build the checked-in teacher-forced prompt corpus from a local snapshot
  calibrate  measure the legitimate spread, write the provisional policy, run the seeded-defect
             battery and write the calibration report (in-process MLX + NumPy route)
  run        run one in-process route, evaluate gates H/Q0/N (G,P not evaluated), sign the record
  stage      run ONE stage of a route and write its MYAC hop frames / logits (cross-machine replay)
  evaluate   assemble stage outputs from several machines, evaluate and sign the route record
  verify     verify a signed route record against a policy (fail closed)

Run from the repository root.  Exit status: 0 ok, 1 verdict ``fail`` / verification failure, 2 usage.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from mycelium_platform_neutral import backend_parity as bp  # noqa: E402
from mycelium_platform_neutral import backend_parity_route as rt  # noqa: E402

DEFAULT_POLICY = ROOT / "tolerances" / "backend-parity-qwen2.5-0.5b-instruct.v2.json"
DEFAULT_CORPUS = ROOT / "tolerances" / "backend-parity-corpus-qwen2.5-0.5b-instruct.v1.json"
FORCED_TOKENS = 24

CORPUS_TEXTS: list[dict[str, str]] = [
    {
        "case_id": "chat_short",
        "kind": "chat",
        "prompt": "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nWhat is the capital of France?<|im_end|>\n<|im_start|>assistant\n",
        "continuation": "The capital of France is Paris. It is known for the Eiffel Tower, the Louvre museum and its many cafes along the river Seine.",
    },
    {
        "case_id": "code_python",
        "kind": "code",
        "prompt": "def fibonacci(n):\n    ",
        "continuation": "if n < 2:\n        return n\n    return fibonacci(n - 1) + fibonacci(n - 2)\n\nprint(fibonacci(10))\n",
    },
    {
        "case_id": "multilingual",
        "kind": "multilingual",
        "prompt": "Berlin ist die Hauptstadt von Deutschland. \u5317\u4eac\u662f\u4e2d\u56fd\u7684\u9996\u90fd\u3002",
        "continuation": " Paris est la capitale de la France. \u6771\u4eac\u306f\u65e5\u672c\u306e\u9996\u90fd\u3067\u3059\u3002 Madrid es la capital de Espa\u00f1a.",
    },
    {
        "case_id": "repeated_token",
        "kind": "repeat",
        "prompt": "the the the the the the the the",
        "continuation": " the" * 28,
    },
    {
        "case_id": "arithmetic",
        "kind": "numbers",
        "prompt": "1 + 1 = 2\n2 + 2 = 4\n3 + 3 = 6\n4 + 4 =",
        "continuation": " 8\n5 + 5 = 10\n6 + 6 = 12\n7 + 7 = 14\n8 + 8 = 16\n9 + 9 = 18",
    },
    {
        "case_id": "alphabet",
        "kind": "sequence",
        "prompt": "a b c d e f g h",
        "continuation": " i j k l m n o p q r s t u v w x y z a b c d e f g h",
    },
    {
        "case_id": "medium_context",
        "kind": "prose",
        "prompt": (
            "A distributed language model splits its layers across several machines. Each machine holds a contiguous range "
            "of layers and a local key-value cache, receives the hidden state of the previous machine over the network, runs "
            "its layers, and forwards the result to the next machine. The last machine applies the final normalisation and "
            "the output projection and picks the next token. Because every machine may use a different numerical backend, "
            "the same arithmetic can round differently, so a route must be checked against a reference before it is trusted. "
            "The check compares the hidden state at every hop and the final probability distribution, and it must catch "
            "wiring mistakes such as a wrong layer range or a wrong rotary base while accepting ordinary rounding noise."
        ),
        "continuation": " In practice the reference is computed once on a trusted machine, and the thresholds are fixed before any new device is measured.",
    },
    {
        "case_id": "json_records",
        "kind": "structured",
        "prompt": '{"name": "Alice", "age": 30, "city": "Paris", "tags": ["a", "b"]}\n{"name": "Bob", "age": 25, "city":',
        "continuation": ' "Berlin", "tags": ["c", "d"]}\n{"name": "Carol", "age": 41, "city": "Rome"}',
    },
]


class CliError(Exception):
    pass


# --------------------------------------------------------------------------- helpers


def _utc_date() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ctx(args: argparse.Namespace) -> tuple[rt.Context, str]:
    config, tensors, source_digest = rt.load_snapshot(Path(args.snapshot))
    corpus = bp.load_corpus(Path(args.corpus))
    return rt.Context(config=config, base=tensors, corpus=corpus), source_digest


def _plan(args: argparse.Namespace, ctx: rt.Context) -> rt.RoutePlan:
    backends = args.backends.split(",")
    reps = args.reps.split(",") if args.reps else ["none"] * len(backends)
    if len(reps) != len(backends):
        raise CliError("--reps must list one representation per backend")
    n_layer = ctx.config["n_layer"]
    cuts = [int(x) for x in args.splits.split(",")] if args.splits else ([n_layer // 2] if len(backends) == 2 else [])
    if len(cuts) != len(backends) - 1:
        raise CliError("--splits must list len(backends)-1 cut layers")
    edges = [0, *cuts, n_layer]
    if any(a >= b for a, b in zip(edges, edges[1:])):
        raise CliError("--splits must be strictly increasing and inside the model")
    stages = [rt.StagePlan(backends[i], edges[i], edges[i + 1], reps[i]) for i in range(len(backends))]
    return rt.RoutePlan(stages=stages)


def _add_model_args(p: argparse.ArgumentParser, *, oracle: bool = True) -> None:
    p.add_argument("--snapshot", required=True, help="local HF snapshot dir (config.json + model.safetensors); never downloaded")
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    if oracle:
        p.add_argument("--oracle", required=True, help="oracle npz from the 'oracle' sub-command")
        p.add_argument("--oracle-info", help="oracle info json (default: <oracle>.json)")


def _add_route_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backends", required=True, help="comma list, one per stage, e.g. mlx,numpy")
    p.add_argument("--reps", help="comma list of representations, e.g. none,int8-weight-only (default none)")
    p.add_argument("--splits", help="comma list of cut layers (default: n_layer/2 for two stages)")


def _oracle(args: argparse.Namespace, ctx: rt.Context) -> tuple[bp.RouteCapture, dict[str, Any]]:
    info_path = Path(args.oracle_info) if args.oracle_info else Path(str(args.oracle) + ".json")
    info = _read_json(info_path)
    capture = rt.load_oracle(Path(args.oracle), ctx.corpus, expected_digest=info["archive_digest"])
    return capture, {k: info[k] for k in ("implementation", "runtime_digest", "archive_digest", "dtype")}


def _host_context(label: str) -> dict[str, Any]:
    import importlib.metadata as md

    info: dict[str, Any] = {
        "calibrated_on": label,
        "date": _utc_date(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "numpy": md.version("numpy"),
    }
    try:
        info["mlx"] = md.version("mlx")
    except md.PackageNotFoundError:
        info["mlx"] = None
    return info


def _make_record(ctx, plan, args, source_digest, policy, corpus, oracle_info, gates, signer, identities=None):
    route = rt.route_document(
        ctx, plan, model_id=corpus["model_id"], revision=corpus["revision"], source_digest=source_digest, identities=identities
    )
    record = bp.build_route_record(route=route, corpus=corpus, policy=policy, oracle=oracle_info, gates=gates, created_at=time.time())
    return bp.sign_route_record(signer, record)


def _signer(label: str):
    from mycelium_qualification.signing import generate_ed25519_signer

    return generate_ed25519_signer(endpoint_id=label)


def _print_gates(gates: dict[str, dict[str, Any]]) -> None:
    for name, gate in gates.items():
        if gate["status"] == "not_evaluated":
            print(f"  gate {name}: not_evaluated ({gate['reason'][:90]}...)")
            continue
        worst = [c for c in gate["checks"] if not c["pass"]]
        print(f"  gate {name}: {gate['status']}" + (" FAILED " + ", ".join(f"{c['metric']}={c['value']}" for c in worst) if worst else ""))
        if name != "N":
            m = gate["metrics"]
            print(
                "     "
                + " ".join(f"{k}={m[k]:.6g}" for k in ("top1_agreement", "topk_overlap_mean", "logits_cosine_min", "kl_mean", "logits_max_abs_delta_top", "hop_cosine_min", "hop_rel_l2_max") if k in m)
            )


# --------------------------------------------------------------------------- corpus


def cmd_corpus(args: argparse.Namespace) -> int:
    from tokenizers import Tokenizer

    snapshot = Path(args.snapshot)
    config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_file(str(snapshot / "tokenizer.json"))
    revision = snapshot.resolve().name
    cases = []
    for spec in CORPUS_TEXTS:
        text = spec["prompt"] + spec["continuation"]
        encoding = tokenizer.encode(text, add_special_tokens=False)
        boundary = len(spec["prompt"])
        prefill = sum(1 for (_, end) in encoding.offsets if end <= boundary)
        if prefill == 0 or prefill + FORCED_TOKENS > len(encoding.ids):
            raise CliError(f"case {spec['case_id']}: prefill {prefill} + {FORCED_TOKENS} forced > {len(encoding.ids)} tokens")
        ids = list(encoding.ids[: prefill + FORCED_TOKENS])
        cases.append({"case_id": spec["case_id"], "kind": spec["kind"], "text": text, "token_ids": ids, "prefill_len": prefill})
    corpus = bp.validate_corpus(
        {"protocol": bp.CORPUS_PROTOCOL, "model_id": "Qwen/Qwen2.5-0.5B-Instruct", "revision": revision, "cases": cases}
    )
    if any(max(c["token_ids"]) >= config["vocab_size"] for c in corpus["cases"]):
        raise CliError("token id outside the model vocabulary")
    _write_json(Path(args.out), corpus)
    print(f"corpus: {len(cases)} cases, prefill {[c['prefill_len'] for c in corpus['cases']]}, "
          f"scored positions {sum(len(bp.scored_positions(c)) for c in corpus['cases'])}, digest {bp.corpus_digest(corpus)}")
    return 0


# --------------------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    ctx, source_digest = _ctx(args)
    policy = bp.load_policy(Path(args.policy))
    oracle, oracle_info = _oracle(args, ctx)
    plan = _plan(args, ctx)
    evaluator = rt.Evaluator(ctx, policy, oracle)
    gates, _ = evaluator.evaluate(plan)
    print(f"route {[(s.backend, s.layer_start, s.layer_end, s.representation) for s in plan.stages]}")
    _print_gates(gates)
    signer = _signer("backend-parity-run")
    envelope = _make_record(ctx, plan, args, source_digest, policy, ctx.corpus, oracle_info, gates, signer)
    verdict = envelope["record"]["verdict"]
    if args.record_out:
        _write_json(Path(args.record_out), envelope)
    print(f"verdict {verdict}; signer key digest {signer.verification_key_digest}; {time.perf_counter() - started:.1f}s; load {os.getloadavg()}")
    if args.battery:
        rows = rt.run_scenarios(ctx, evaluator, lambda: _plan(args, ctx), rt.default_scenarios())
        summary = bp.summarize_battery(rows)
        _write_json(Path(args.battery), summary)
        print(f"battery: defects {summary['defects_detected']}/{summary['defects_total']} fail, controls {summary['controls_passed']}/{summary['controls_total']} pass, problems {summary['problems']}")
        if summary["problems"]:
            return 1
    return 1 if verdict == "fail" else 0


# --------------------------------------------------------------------------- stage / evaluate (cross-machine replay)


def cmd_stage(args: argparse.Namespace) -> int:
    ctx, _ = _ctx(args)
    plan = _plan(args, ctx)
    index = args.index
    frames_in = None
    if index > 0:
        frames_in, _ = rt.load_frames(Path(args.frames_in), ctx.corpus)
    started = time.perf_counter()
    timings: list[dict[str, Any]] = []
    frames_out, logits = rt.run_stage_frames(ctx, plan, index, frames_in, timings=timings)
    seconds = time.perf_counter() - started
    steps = sum(len(rt._case_steps(c)) for c in ctx.corpus["cases"])
    meta = {
        "stage": index,
        "backends": args.backends,
        "reps": args.reps or "none",
        "splits": args.splits,
        "corpus_digest": bp.corpus_digest(ctx.corpus),
        "seconds": round(seconds, 3),
        "steps": steps,
        "ms_per_step": round(1000 * seconds / steps, 3),
        "speed": rt.summarize_timings(timings),
        "identity": rt.stage_identity(plan.stages[index].backend),
        "load": list(os.getloadavg()),
        "host": platform.node(),
    }
    if frames_out is not None:
        digest = rt.save_frames(Path(args.out), frames_out, meta)
        print(f"stage {index} frames -> {args.out} {digest}\n  speed {meta['speed']}")
    else:
        arrays = {f"logits__{k}": v for k, v in (logits or {}).items()}
        with Path(args.out).open("wb") as handle:
            np.savez(handle, meta=np.frombuffer(json.dumps(meta, sort_keys=True).encode(), dtype=np.uint8), **arrays)
        print(f"stage {index} logits -> {args.out} {bp.file_digest(Path(args.out))}\n  speed {meta['speed']}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    ctx, source_digest = _ctx(args)
    policy = bp.load_policy(Path(args.policy))
    oracle, oracle_info = _oracle(args, ctx)
    plan = _plan(args, ctx)
    frame_files = [Path(p) for p in args.frames.split(",")]
    boundaries = plan.boundaries()
    if len(frame_files) != len(boundaries):
        raise CliError("--frames needs one frames file per non-final stage")
    frames_by_boundary = {}
    identities: list[dict[str, Any]] = [{}] * len(plan.stages)
    for index, (boundary, path) in enumerate(zip(boundaries, frame_files)):
        frames, meta = rt.load_frames(path, ctx.corpus)
        if meta.get("corpus_digest") != bp.corpus_digest(ctx.corpus):
            raise CliError(f"{path}: frames were produced for another corpus")
        if meta.get("stage") != index or "identity" not in meta:
            raise CliError(f"{path}: not the frames of stage {index}")
        identities[index] = meta["identity"]
        frames_by_boundary[boundary] = frames
    with np.load(Path(args.logits), allow_pickle=False) as archive:
        logits_meta = json.loads(bytes(archive["meta"].tobytes()).decode())
        if logits_meta.get("stage") != len(plan.stages) - 1 or "identity" not in logits_meta:
            raise CliError(f"{args.logits}: not the logits of the final stage")
        identities[-1] = logits_meta["identity"]
        logits = {c["case_id"]: np.array(archive[f"logits__{c['case_id']}"], dtype=np.float32) for c in ctx.corpus["cases"]}
    capture = rt.capture_from_frames(ctx, plan, frames_by_boundary, logits)
    evaluator = rt.Evaluator(ctx, policy, oracle)
    gates = bp.evaluate_route(capture, evaluator.monolith(plan), oracle, policy, plan.declared_representations())
    _print_gates(gates)
    signer = _signer("backend-parity-evaluate")
    envelope = _make_record(ctx, plan, args, source_digest, policy, ctx.corpus, oracle_info, gates, signer, identities)
    if args.record_out:
        _write_json(Path(args.record_out), envelope)
    print(f"verdict {envelope['record']['verdict']}; signer key digest {signer.verification_key_digest}")
    return 1 if envelope["record"]["verdict"] == "fail" else 0


def cmd_verify(args: argparse.Namespace) -> int:
    policy = bp.load_policy(Path(args.policy))
    corpus = bp.load_corpus(Path(args.corpus)) if args.corpus else None
    try:
        record = bp.verify_route_record(
            _read_json(Path(args.record)),
            policy=policy,
            now=time.time(),
            expected_key_digest=args.key_digest,
            corpus=corpus,
            unanchored_ok=args.unanchored,
        )
    except bp.BackendParityError as exc:
        print(f"REJECTED: {exc}")
        return 1
    print(f"verified: verdict {record['verdict']} class {record['quantization_class']} route {record['route_digest']} unevaluated {record['unevaluated_gates']}")
    return 0


# --------------------------------------------------------------------------- calibrate


def _route_summary(plan: rt.RoutePlan) -> str:
    return " -> ".join(f"{s.backend}[{s.layer_start},{s.layer_end}):{s.representation}" for s in plan.stages)


def cmd_calibrate(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    load_before = os.getloadavg()
    ctx, source_digest = _ctx(args)
    prior = bp.validate_policy(bp.default_policy_v2())
    oracle, oracle_info = _oracle(args, ctx)
    n_layer = ctx.config["n_layer"]

    def base_plan() -> rt.RoutePlan:
        return rt.two_stage_plan(n_layer, "mlx", "numpy")

    scenarios = rt.default_scenarios()
    controls = [s for s in scenarios if s.kind == "control"]
    # 1. measure the legitimate spread against the (loose) priors
    rows0 = rt.run_scenarios(ctx, rt.Evaluator(ctx, prior, oracle), base_plan, controls)
    # the two primary routes of the desk done-when are controls too, but report them explicitly
    primary = [
        ("mlx_then_numpy", rt.two_stage_plan(n_layer, "mlx", "numpy")),
        ("numpy_then_mlx", rt.two_stage_plan(n_layer, "numpy", "mlx")),
        ("mlx_then_numpy_int8", rt.two_stage_plan(n_layer, "mlx", "numpy", rep0="int8-weight-only", rep1="int8-weight-only")),
    ]
    prim_rows = []
    ev0 = rt.Evaluator(ctx, prior, oracle)
    for name, plan in primary:
        gates, _ = ev0.evaluate(plan)
        h = dict(gates["H"]["metrics"])
        h["activation_abs_max"] = gates["N"]["metrics"]["activation_abs_max"]
        q_gate = next(g for n, g in gates.items() if n in ("Q0", "Q1"))
        prim_rows.append({"name": "primary_" + name, "kind": "control", "metrics": {"H": h, "N": gates["N"]["metrics"], q_gate["gate"]: q_gate["metrics"]}})
        ctx.purge_transient()
    h_samples, q_samples = rt.legit_samples([*rows0, *prim_rows])
    host = _host_context(args.label)
    context = {
        **host,
        "note": f"calibrated on {args.label} {host['date']}, provisional until the SB2 run",
        "load_average_before": [round(x, 2) for x in load_before],
        "corpus_digest": bp.corpus_digest(ctx.corpus),
        "positions": sum(len(bp.scored_positions(c)) for c in ctx.corpus["cases"]),
        "cases": len(ctx.corpus["cases"]),
        "oracle": oracle_info,
        "source_artifact_digest": source_digest,
        "route": "in-process MLX(metal) + NumPy(cpu) kernels, float32 MYAC wire; kernel-level, not router-port level",
        "split_layer": n_layer // 2,
    }
    policy = bp.calibrate_policy(
        prior,
        h_samples=h_samples,
        q_samples=q_samples,
        quantization_class="Q0",
        context=context,
        policy_id="qwen2.5-0.5b-instruct-f32wire-mac-node-20261021",
    )
    # model identity comes from the corpus, not the prior default
    policy["model"] = {"model_id": ctx.corpus["model_id"], "revision": ctx.corpus["revision"]}
    policy["policy_digest"] = bp.policy_digest(policy)
    policy = bp.validate_policy(policy)
    bp.write_policy(Path(args.policy_out), policy)
    print(f"policy written {args.policy_out} {policy['policy_digest']}")

    # 2. battery with the calibrated policy
    evaluator = rt.Evaluator(ctx, policy, oracle)
    rows = rt.run_scenarios(ctx, evaluator, base_plan, scenarios)
    summary = bp.summarize_battery(rows)

    # 3. signed records for the primary routes, verified
    signer = _signer("backend-parity-calibration")
    records = []
    for name, plan in primary[:2]:
        gates, _ = evaluator.evaluate(plan)
        envelope = _make_record(ctx, plan, args, source_digest, policy, ctx.corpus, oracle_info, gates, signer)
        verified = bp.verify_route_record(
            envelope, policy=policy, now=time.time(), expected_key_digest=signer.verification_key_digest, corpus=ctx.corpus, expected_route=envelope["record"]["route"]
        )
        records.append({"name": name, "route": _route_summary(plan), "verdict": verified["verdict"], "gates": {n: g["status"] for n, g in gates.items()}, "gate_metrics": {n: g["metrics"] for n, g in gates.items() if g["status"] != "not_evaluated"}, "envelope": envelope})
        ctx.purge_transient()
    report = {
        "protocol": "mycelium.backend_parity_calibration_report.v1",
        "label": f"measured on {args.label}",
        "date": host["date"],
        "load_average_before": [round(x, 2) for x in load_before],
        "load_average_after": [round(x, 2) for x in os.getloadavg()],
        "seconds": round(time.perf_counter() - t0, 1),
        "host": {k: host[k] for k in ("python", "machine", "system", "numpy", "mlx")},
        "claim_boundary": bp.CLAIM_BOUNDARY,
        "policy_digest": policy["policy_digest"],
        "corpus_digest": bp.corpus_digest(ctx.corpus),
        "oracle": oracle_info,
        "legit_spread_h": h_samples,
        "legit_spread_q0": q_samples,
        "per_metric": policy["calibration"]["per_metric"],
        "battery": summary,
        "primary_routes": records,
        "signer_key_digest": signer.verification_key_digest,
        "notes": [
            "controls are the calibration set; their passing is by construction with margin, not independent validation",
            "G (generation consistency) and P (join-time probe) are not evaluated in v0; verdicts are therefore partial_pass at best",
            "positions: 8 cases x 24 teacher-forced positions; the plan's >=512-position and >1024-position cases are not in the desk corpus",
        ],
    }
    _write_json(Path(args.report_out), report)
    print(f"battery: defects {summary['defects_detected']}/{summary['defects_total']} fail; controls {summary['controls_passed']}/{summary['controls_total']} pass; observations {summary['observations_detected']}/{summary['observations_total']} detected; problems {summary['problems']}")
    for record in records:
        print(f"primary {record['name']}: {record['verdict']} {record['gates']}")
    print(f"report -> {args.report_out}; {time.perf_counter() - t0:.0f}s; load {load_before} -> {os.getloadavg()}")
    return 0 if not summary["problems"] and all(r["verdict"] == "partial_pass" for r in records) else 1


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("corpus", help="build the teacher-forced corpus from a local snapshot")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out", default=str(DEFAULT_CORPUS))
    p.set_defaults(fn=cmd_corpus)

    p = sub.add_parser("calibrate", help="measure spread, write provisional policy, run the battery")
    _add_model_args(p)
    p.add_argument("--label", default="mac-node")
    p.add_argument("--policy-out", default=str(DEFAULT_POLICY))
    p.add_argument("--report-out", required=True)
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("run", help="run + judge one in-process route")
    _add_model_args(p)
    _add_route_args(p)
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--record-out")
    p.add_argument("--battery", help="also run the seeded-defect battery with this route as base; write summary json here")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("stage", help="run one stage; write MYAC frames (or logits for the final stage)")
    _add_model_args(p, oracle=False)
    _add_route_args(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--frames-in")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_stage)

    p = sub.add_parser("evaluate", help="assemble stage outputs, judge and sign the route record")
    _add_model_args(p)
    _add_route_args(p)
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--frames", required=True, help="comma list: frames file of stage 0, stage 1, ... (all but the last)")
    p.add_argument("--logits", required=True, help="logits npz written by 'stage' for the final stage")
    p.add_argument("--record-out")
    p.set_defaults(fn=cmd_evaluate)

    p = sub.add_parser("verify", help="verify a signed route record")
    p.add_argument("--record", required=True)
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    p.add_argument("--key-digest")
    p.add_argument("--unanchored", action="store_true", help="accept an unanchored signing key (integrity only)")
    p.set_defaults(fn=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (CliError, bp.BackendParityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
