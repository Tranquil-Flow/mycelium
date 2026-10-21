#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A11 speculative-decoding benchmark runner (target-only vs speculative, same route).

Subcommands (every output is an owner-private file under ``--out``, mode 0700/0600):

``local``
    Live-local evidence on ONE host: the exact target is split over two stages that
    run in two in-process routers joined by the real loopback socket mesh (real
    router wire framing, asynchronous delivery), the draft runs natively beside the
    entry, and both modes run on the same route in one session.  Its records say
    ``evidence_class = live_local``: this is never physical, two-host or product
    evidence and cannot satisfy an A11 gate.  ``--quick`` runs a short smoke
    protocol; without it the frozen paired protocol below runs.

``product``
    The R7 physical run: drives the ordinary product gateway of a live two-Mac serve
    and switches modes per request with the explicit speculation preference (no
    restart, no route reselection).  It needs the gateway to accept
    ``speculation_preference`` (A11 product integration); a gateway that does not
    advertise it is refused with ``product_speculation_unavailable`` before any
    measurement.

``decide``
    Recomputes the frozen decision from a windows file: paired reduction, paired
    bootstrap lower bound, TTFT regression, and the closed ``measured_disabled``
    reason when a threshold is missed.  The result is validated by
    ``mycelium_speculative_decode.validate_speculative_decode_benchmark``.

The frozen protocol (pre-registration ``runs/v2/pm-a11/PREREG-R10.md``): one
unscored warmup per mode, then windows A B B A A B (A = target-only), each window
12 completed requests over two prompt-length and two output-length buckets and at
least 384 target-committed tokens.  Requests run one at a time (interactive,
concurrency 1).  No output file contains a prompt, decoded text, token ids,
hostnames, addresses, private paths or exception strings; output parity is judged
privately per request by digest.
"""

# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mycelium_speculative_decode as sd

PROTOCOL = "mycelium.a11_route_benchmark_run.v1"
WINDOW_ORDER = sd.BENCHMARK_WINDOW_MODE_ORDER
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261007

#: Frozen workload.  Prompt texts are inputs, not outputs; they never enter evidence.
SHORT_PROMPTS = (
    "Explain how a bicycle gear system works to a ten year old.",
    "Write a short Python function that checks whether a string is a palindrome, with a docstring.",
    "What are three practical tips for learning German vocabulary faster?",
)
LONG_PROMPTS = (
    "Summarise the causes of the French Revolution in one paragraph. Mention the fiscal crisis, the role of the "
    "Estates-General, Enlightenment ideas, food prices and the political weakness of the monarchy, and keep the "
    "tone neutral and suitable for a secondary-school history class.",
    "Draft a polite email to a landlord asking them to fix a broken heater. The heater stopped working three days "
    "ago, the flat is cold at night, you have already reported it once by phone, and you would like a technician "
    "to visit this week. Offer two possible time slots and thank them.",
    "Compare TCP and UDP in a short bullet list. Cover connection setup, reliability, ordering, flow control, "
    "header overhead, typical latency and two example applications for each protocol, and finish with one "
    "sentence on when to prefer each.",
)
OUTPUT_BUCKETS = {"short": 24, "long": 40}
QUICK_OUTPUT_BUCKETS = {"short": 16, "long": 24}
SYSTEM = "You are a helpful assistant."


class RunError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequestShape:
    index: int
    prompt_bucket: str
    output_bucket: str
    prompt: str
    max_new_tokens: int

    @property
    def key(self) -> str:
        return f"{self.prompt_bucket}:{self.output_bucket}:{self.index}"


def window_workload(*, quick: bool = False) -> list[RequestShape]:
    """12 requests (4 when quick): every prompt/output bucket combination, fixed order."""

    outputs = QUICK_OUTPUT_BUCKETS if quick else OUTPUT_BUCKETS
    shapes = []
    count = 4 if quick else 12
    for index in range(count):
        prompt_bucket = ("short", "long")[index % 2]
        output_bucket = ("short", "long")[(index // 2) % 2]
        prompts = SHORT_PROMPTS if prompt_bucket == "short" else LONG_PROMPTS
        shapes.append(
            RequestShape(
                index=index,
                prompt_bucket=prompt_bucket,
                output_bucket=output_bucket,
                prompt=prompts[(index // 4) % len(prompts)],
                max_new_tokens=outputs[output_bucket],
            )
        )
    return shapes


def chat_text(prompt: str) -> str:
    return f"<|im_start|>system\n{SYSTEM}<|im_end|>\n<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"


@dataclass
class RequestResult:
    shape: RequestShape
    mode: str
    outcome: str
    submitted_at: float
    first_token_at: float | None
    finished_at: float
    tokens: int
    output_digest: str | None
    speculation: dict[str, Any] | None = None

    def record(self) -> dict[str, Any]:
        """Privacy-reduced request record (no prompt, text or token ids)."""

        ttft = None if self.first_token_at is None else (self.first_token_at - self.submitted_at) * 1000.0
        decode_ms = (
            None
            if self.first_token_at is None or self.tokens < 2
            else (self.finished_at - self.first_token_at) * 1000.0 / (self.tokens - 1)
        )
        return {
            "shape": self.shape.key,
            "mode": self.mode,
            "outcome": self.outcome,
            "tokens": self.tokens,
            "ttft_ms": None if ttft is None else round(ttft, 3),
            "tpot_ms": None if decode_ms is None else round(decode_ms, 3),
            "wall_ms": round((self.finished_at - self.submitted_at) * 1000.0, 3),
            "output_digest": self.output_digest,
            "speculation": self.speculation,
        }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    data = sorted(value for value in values if value is not None)
    if not data:
        return None
    index = min(len(data) - 1, max(0, int(round(fraction * (len(data) - 1)))))
    return data[index]


def window_document(mode: str, results: Sequence[RequestResult]) -> dict[str, Any]:
    completed = [result for result in results if result.outcome == "completed"]
    records = [result.record() for result in results]
    return {
        "mode": mode,
        "completed_requests": len(completed),
        "target_committed_output_tokens": sum(result.tokens for result in completed),
        "prompt_length_buckets": len({result.shape.prompt_bucket for result in completed}),
        "output_length_buckets": len({result.shape.output_bucket for result in completed}),
        "wall_clock_ms": round(sum((r.finished_at - r.submitted_at) * 1000.0 for r in results), 3),
        "ttft_p95_ms": _percentile([rec["ttft_ms"] for rec in records], 0.95),
        "error_timeout_cancellation_fallback_count": sum(1 for result in results if result.outcome != "completed"),
    }


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def paired_lower_bound(windows: Sequence[Mapping[str, Any]], *, seed: int = BOOTSTRAP_SEED) -> float:
    """5th percentile of the paired bootstrap of ``1 - mean(B)/mean(A)`` over the 3 pairs."""

    pairs = []
    for start in range(0, len(windows), 2):
        first, second = windows[start], windows[start + 1]
        a, b = (first, second) if first["mode"] == "target_only" else (second, first)
        pairs.append((float(a["wall_clock_ms"]), float(b["wall_clock_ms"])))
    rng = random.Random(seed)
    estimates = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        estimates.append(1.0 - (sum(b for _, b in sample) / len(sample)) / (sum(a for a, _ in sample) / len(sample)))
    estimates.sort()
    return estimates[int(0.05 * (len(estimates) - 1))]


def decide(windows: Sequence[Mapping[str, Any]], *, plan_digest: str, invalidation_reasons: Sequence[str] = ()) -> dict[str, Any]:
    """The frozen benchmark document for six measured windows (validated, sealed)."""

    reasons = sorted(set(invalidation_reasons))
    if tuple(window["mode"] for window in windows) != WINDOW_ORDER:
        reasons = sorted(set(reasons) | {"missing_samples"})
    document: dict[str, Any] = {
        "protocol": sd.BENCHMARK_PROTOCOL,
        "plan_digest": plan_digest,
        "executed": True,
        "paired_order": list(sd.BENCHMARK_PAIRED_ORDER),
        "warmups": {"target_only": 1, "speculative": 1},
        "windows": [dict(window) for window in windows],
        "confidence": sd.BENCHMARK_CONFIDENCE,
        "valid": not reasons,
        "invalidation_reasons": reasons,
    }
    if reasons:
        document["decision"] = {
            "state": sd.DISABLED_DECISION,
            "reason": "confidence_bound_not_met",
            "observed_reduction_fraction": None,
            "paired_lower_bound_fraction": None,
            "ttft_regression_fraction": None,
            "blocking_conditions": ["benchmark_pair_invalid"],
        }
        return sd.seal(document)
    target = [w for w in windows if w["mode"] == "target_only"]
    speculative = [w for w in windows if w["mode"] == "speculative"]
    reduction = 1.0 - (sum(w["wall_clock_ms"] for w in speculative) / 3) / (sum(w["wall_clock_ms"] for w in target) / 3)
    ttft = (sum(w["ttft_p95_ms"] for w in speculative) / 3) / (sum(w["ttft_p95_ms"] for w in target) / 3) - 1.0
    lower = min(paired_lower_bound(windows), reduction)
    blocking = []
    if reduction < sd.BENCHMARK_IMPROVEMENT_FRACTION:
        blocking.append("material_gain_below_threshold")
    if lower < sd.BENCHMARK_PAIRED_LOWER_BOUND_FRACTION:
        blocking.append("paired_lower_bound_below_threshold")
    if ttft > sd.BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION:
        blocking.append("ttft_regression_above_limit")
    if not blocking:
        state, reason = sd.ENABLED_DECISION, None
    elif "material_gain_below_threshold" in blocking:
        state, reason = sd.DISABLED_DECISION, "material_gain_not_observed"
    else:
        state, reason = sd.DISABLED_DECISION, "confidence_bound_not_met"
    document["decision"] = {
        "state": state,
        "reason": reason,
        "observed_reduction_fraction": reduction,
        "paired_lower_bound_fraction": lower,
        "ttft_regression_fraction": ttft,
        "blocking_conditions": sorted(blocking),
    }
    sealed = sd.seal(document)
    sd.validate_speculative_decode_benchmark(sealed)
    return sealed


# ---------------------------------------------------------------------------
# Live-local harness (one host, two in-process routers over loopback sockets)
# ---------------------------------------------------------------------------


class _NotifyingSink:
    def __init__(self) -> None:
        self.token_ids: list[int] = []
        self.token_indexes: list[int] = []
        self.first_at: float | None = None
        self.changed = threading.Condition()

    def emit(self, token_index: int, token_id: int) -> None:
        with self.changed:
            if self.first_at is None:
                self.first_at = time.monotonic()
            self.token_indexes.append(token_index)
            self.token_ids.append(token_id)
            self.changed.notify_all()


class LocalRoute:
    """The exact target split over two routers on one host plus a native draft."""

    def __init__(self, *, target_snapshot: Path, target_id: str, target_commit: str, draft_snapshot: Path,
                 work: Path, quantization: str, draft_bits: int | None, width: int) -> None:
        import mlx.core as mx  # noqa: F401 - fail early on hosts without MLX

        from mycelium_a11_draft_runtime import MLXDraftRuntime, Qwen2DraftModel
        from mycelium_qualification.physical_deployment import (
            LocalModelSource,
            build_execution_graph,
            build_physical_device_states,
            prepare_assignment_artifacts,
        )
        from mycelium_router.contracts import RouterConfig
        from mycelium_router.fakes import FakeCapacityPort, FakeDeviceStateProvider, FakeTopologyProvider, SequenceIdSource
        from mycelium_router.mlx_runtime import MLXRuntimePort
        from mycelium_router.router import Router
        from mycelium_router.transports.loopback_socket import LoopbackSocketMesh
        from runtime_loader import canonical_json, load_assignment_stage

        started = time.monotonic()
        _private_directory(work)
        prepared = prepare_assignment_artifacts(
            work / "target",
            node_ids=("node-a", "node-b"),
            model_source=LocalModelSource(root=target_snapshot, model_id=target_id, resolved_commit=target_commit),
            runtime_dtype="float32",
            runtime_quantization=quantization,
        )
        loaded = [load_assignment_stage(a, r, load_generation=1) for a, r in zip(prepared.assignments, prepared.reports)]
        self.graph = build_execution_graph(prepared.assignments, [json.loads(canonical_json(item.proof)) for item in loaded])
        self.manifest_digest = self.graph.manifest_digest
        self.ports = tuple(
            MLXRuntimePort(
                assignment["node_id"],
                self.graph,
                {self.graph.stages[index].placements[0].placement_id: loaded[index]},
                decode_mode="stage_local_kv",
            )
            for index, assignment in enumerate(prepared.assignments)
        )
        self.load_seconds = time.monotonic() - started
        draft_started = time.monotonic()
        self.draft_model = Qwen2DraftModel.from_snapshot(draft_snapshot, quantize_bits=draft_bits)
        self.draft_load_seconds = time.monotonic() - draft_started
        self._draft_runtime = MLXDraftRuntime
        self.width = width
        self.mesh = LoopbackSocketMesh(connect_timeout_seconds=30.0)
        states = build_physical_device_states(self.graph)
        capacity = FakeCapacityPort()
        self.routers = {}
        for assignment, port in zip(prepared.assignments, self.ports):
            node_id = assignment["node_id"]
            router = Router(
                node_id=node_id,
                topology=FakeTopologyProvider(self.graph),
                device_states=FakeDeviceStateProvider(states),
                capacity=capacity,
                runtime=port,
                transport=self.mesh.transport_for(node_id),
                clock=_WallClock(),
                id_source=SequenceIdSource(),
                config=RouterConfig(prefill_chunk_size_tokens=0),
            )
            self.mesh.register_router(node_id, router)
            self.routers[node_id] = router
        self.entry = self.routers["node-a"]
        self.controller = None
        self.mesh.start()
        self._sequence = 0

    def enable_speculation(self, *, warm_p95_ms: float, warm_p50_ms: float) -> None:
        from mycelium_a11_route_speculation import RouteSpeculationController, RouteSpeculationPolicy

        identity = self.draft_model.identity_digest()
        self.controller = RouteSpeculationController(
            policy=RouteSpeculationPolicy(
                width=self.width,
                warm_target_tpot_p95_ms=warm_p95_ms,
                warm_target_tpot_p50_ms=warm_p50_ms,
                draft_identity_digest=identity,
                target_binding_digest=self.manifest_digest,
            ),
            draft_factory=lambda request_id: self._draft_runtime(self.draft_model, incarnation=identity),
            runtime_incarnation=identity,
        )
        self.entry.entry.a11_controller = self.controller

    def run(self, prompt_ids: Sequence[int], shape: RequestShape, mode: str, *, timeout: float = 600.0) -> RequestResult:
        from mycelium_router.contracts import RequestContext

        self._sequence += 1
        request_id = f"a11-{mode}-{self._sequence}"
        if mode == "speculative":
            if self.controller is None:
                raise RunError("speculation_not_enabled")
            self.controller.set_preference(request_id, "speculative_requested")
        sink = _NotifyingSink()
        submitted = time.monotonic()
        request = RequestContext(
            request_id=request_id,
            prompt_token_ids=tuple(prompt_ids),
            max_new_tokens=shape.max_new_tokens,
            expected_new_tokens=shape.max_new_tokens,
            qos_class="interactive",
            admitted_at=time.monotonic(),
            target_ttft_ms=120_000.0,
            target_tpot_ms=10_000.0,
            target_tokens_per_second=0.1,
            sampling_seed=0,
            generation_config_digest="sha256:" + hashlib.sha256(b"deterministic_greedy").hexdigest(),
        )
        self.entry.start_distributed_prefill(request, sink)
        deadline = submitted + timeout
        step: Callable[[str], bool] = (
            self.entry.speculate_one_distributed if mode == "speculative" else self.entry.decode_one_distributed
        )
        while self.entry.request_status(request_id) in {"PREFILL", "LOCKED"}:
            if time.monotonic() > deadline:
                raise RunError("prefill_timeout")
            time.sleep(0.001)
        while self.entry.request_status(request_id) == "DECODING":
            with sink.changed:
                count = len(sink.token_ids)
            step(request_id)
            with sink.changed:
                while len(sink.token_ids) == count and self.entry.request_status(request_id) == "DECODING":
                    if time.monotonic() > deadline:
                        raise RunError("decode_timeout")
                    sink.changed.wait(0.05)
        finished = time.monotonic()
        status = self.entry.request_status(request_id)
        digest = "sha256:" + hashlib.sha256(json.dumps(sink.token_ids).encode()).hexdigest()
        speculation = None
        if mode == "speculative" and self.controller is not None and self.controller.completed:
            speculation = {key: value for key, value in self.controller.completed[-1].items() if key != "admission"}
        self._last_tokens = list(sink.token_ids)
        return RequestResult(
            shape=shape,
            mode=mode,
            outcome="completed" if status == "COMPLETED" else status.lower(),
            submitted_at=submitted,
            first_token_at=sink.first_at,
            finished_at=finished,
            tokens=len(sink.token_ids),
            output_digest=digest if status == "COMPLETED" else None,
            speculation=speculation,
        )

    def residue(self) -> list[int]:
        return [port.kv_snapshot()["active_state_count"] for port in self.ports]

    def close(self) -> None:
        self.mesh.close()


class _WallClock:
    def now(self) -> float:
        return time.monotonic()


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_private_json(path: Path, document: Any) -> None:
    _private_directory(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def run_local(args: argparse.Namespace) -> dict[str, Any]:
    from tokenizers import Tokenizer

    out = _private_directory(Path(args.out))
    tokenizer = Tokenizer.from_file(str(Path(args.target_snapshot) / "tokenizer.json"))
    shapes = window_workload(quick=args.quick)
    prompt_ids = {shape.key: tuple(tokenizer.encode(chat_text(shape.prompt)).ids) for shape in shapes}
    route = LocalRoute(
        target_snapshot=Path(args.target_snapshot),
        target_id=args.target_id,
        target_commit=args.target_commit,
        draft_snapshot=Path(args.draft_snapshot),
        work=Path(args.work),
        quantization=args.quantization,
        draft_bits=None if args.draft_bits == 0 else args.draft_bits,
        width=args.width,
    )
    manifest = {
        "protocol": PROTOCOL,
        "evidence_class": "live_local",
        "target_manifest_digest": route.manifest_digest,
        "draft_identity_digest": route.draft_model.identity_digest(),
        "width": args.width,
        "quantization": args.quantization,
        "draft_bits": args.draft_bits,
        "workload": [{"shape": s.key, "max_new_tokens": s.max_new_tokens} for s in shapes],
        "window_order": list(WINDOW_ORDER),
        "quick": bool(args.quick),
    }
    plan_digest = "sha256:" + hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    try:
        reference: dict[str, str] = {}
        parity_failures = 0
        # Unscored target-only warmup; its decode TPOT is the measured warm baseline.
        warm = [route.run(prompt_ids[s.key], s, "target_only") for s in shapes]
        for result in warm:
            reference[result.shape.key] = result.output_digest or ""
        tpots = [r.record()["tpot_ms"] for r in warm if r.record()["tpot_ms"] is not None]
        warm_p95, warm_p50 = _percentile(tpots, 0.95), statistics.median(tpots)
        route.enable_speculation(warm_p95_ms=warm_p95, warm_p50_ms=warm_p50)
        warm_speculative = [route.run(prompt_ids[s.key], s, "speculative") for s in shapes]
        parity_failures += sum(1 for r in warm_speculative if r.output_digest != reference[r.shape.key])
        order = ("target_only", "speculative") if args.quick else WINDOW_ORDER
        windows, requests = [], []
        for mode in order:
            results = [route.run(prompt_ids[s.key], s, mode) for s in shapes]
            parity_failures += sum(1 for r in results if r.output_digest != reference[r.shape.key])
            windows.append(window_document(mode, results))
            requests.extend(result.record() for result in results)
            print(json.dumps({"window": mode, "wall_ms": windows[-1]["wall_clock_ms"],
                              "tokens": windows[-1]["target_committed_output_tokens"]}), flush=True)
        residue = route.residue()
    finally:
        route.close()
    summary = _speculation_totals(requests)
    document = {
        "manifest": manifest,
        "plan_digest": plan_digest,
        "load_seconds": round(route.load_seconds, 1),
        "draft_load_seconds": round(route.draft_load_seconds, 1),
        "warm_target_tpot_ms": {"p50": round(warm_p50, 3), "p95": round(warm_p95, 3)},
        "warmup": {
            "target_only": [r.record() for r in warm],
            "speculative": [r.record() for r in warm_speculative],
        },
        "windows": windows,
        "requests": requests,
        "parity_failures": parity_failures,
        "residue_active_kv_states": residue,
        "speculation_totals": summary,
        "claim": "live_local: one host, in-process routers over loopback sockets; not physical, not product, no gate",
    }
    if not args.quick:
        document["benchmark"] = decide(windows, plan_digest=plan_digest,
                                       invalidation_reasons=() if parity_failures == 0 else ("configuration_mismatch",))
    write_private_json(out / "a11-live-local.json", document)
    return document


def _speculation_totals(requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals: dict[str, float] = {}
    for record in requests:
        speculation = record.get("speculation") or {}
        for key in ("rounds", "decode_rounds", "deadline_fallbacks", "proposed_count", "accepted_count",
                    "rejected_count", "target_corrected_count", "committed_count", "draft_ms_total",
                    "speculative_round_ms_total"):
            value = speculation.get(key)
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    if totals.get("proposed_count"):
        totals["acceptance_rate"] = round(totals["accepted_count"] / totals["proposed_count"], 4)
    if totals.get("rounds"):
        totals["committed_per_verify_round"] = round(
            (totals["committed_count"]) / totals["rounds"], 3
        )
    return totals


# ---------------------------------------------------------------------------
# Product mode (R7: two Macs, ordinary gateway)
# ---------------------------------------------------------------------------


def run_product(args: argparse.Namespace) -> dict[str, Any]:
    from scripts.run_a5_benchmark_gate import ProductSession, RotatingProductSessions

    out = _private_directory(Path(args.out))
    session = ProductSession(args.base_url)
    bootstrap = session.json("GET", "/api/v1/bootstrap")
    features = bootstrap.get("features") if isinstance(bootstrap, Mapping) else None
    if not isinstance(features, Mapping) or features.get("speculation_preference") is not True:
        raise RunError("product_speculation_unavailable")
    sessions = RotatingProductSessions(session, args.base_url)
    shapes = window_workload(quick=args.quick)

    def run(shape: RequestShape, mode: str) -> RequestResult:
        child = sessions.fork_for_submission()
        submitted = time.monotonic()
        body = {
            "protocol": "mycelium.request_gateway.v2",
            "prompt": shape.prompt,
            "max_new_tokens": shape.max_new_tokens,
            "qualification": child._qualification["binding"],  # noqa: SLF001 - same session binding
            "workload_profile_id": "interactive_chat_v1",
            "qos_class": "interactive",
            "speculation_preference": "speculative_requested" if mode == "speculative" else "target_only",
        }
        accepted = child.json("POST", "/api/v1/inference", body=body, mutate=True)
        first, tokens, terminal = None, 0, None
        digest = hashlib.sha256()
        try:
            with child.request("GET", accepted["event_path"]) as response:
                kind = None
                for raw in response:
                    line = raw.decode("utf-8", "strict").rstrip("\r\n")
                    if line.startswith("event: "):
                        kind = line[7:]
                        if kind in {"completed", "failed", "cancelled"}:
                            terminal = kind
                            break
                    elif line.startswith("data: ") and kind == "token":
                        tokens += 1
                        first = first or time.monotonic()
                        digest.update(line[6:].encode())
        finally:
            if terminal is None:
                try:
                    child.cancel(accepted)
                except Exception:
                    pass  # Preserve the stream failure; cancellation is best effort.
        outcome = terminal or "failed"
        return RequestResult(shape, mode, outcome, submitted, first, time.monotonic(), tokens,
                             "sha256:" + digest.hexdigest() if outcome == "completed" else None)

    reference = {r.shape.key: r.output_digest for r in (run(s, "target_only") for s in shapes)}
    for shape in shapes:
        run(shape, "speculative")
    windows, requests, parity_failures = [], [], 0
    for mode in WINDOW_ORDER:
        results = [run(shape, mode) for shape in shapes]
        parity_failures += sum(1 for r in results if r.output_digest != reference[r.shape.key])
        windows.append(window_document(mode, results))
        requests.extend(r.record() for r in results)
    plan_digest = args.plan_digest
    document = {
        "protocol": PROTOCOL,
        "evidence_class": "product_route",
        "windows": windows,
        "requests": requests,
        "parity_failures": parity_failures,
        "benchmark": decide(windows, plan_digest=plan_digest,
                            invalidation_reasons=() if parity_failures == 0 else ("configuration_mismatch",)),
    }
    write_private_json(out / "a11-product-benchmark.json", document)
    return document


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local")
    local.add_argument("--target-snapshot", required=True)
    local.add_argument("--target-id", default="Qwen/Qwen2.5-7B-Instruct")
    local.add_argument("--target-commit", required=True)
    local.add_argument("--draft-snapshot", required=True)
    local.add_argument("--work", required=True, help="empty private directory for the provisioned target copy")
    local.add_argument("--out", required=True)
    local.add_argument("--quantization", default="int8-weight-only")
    local.add_argument("--draft-bits", type=int, default=8, help="0 = unquantized draft")
    local.add_argument("--width", type=int, default=4)
    local.add_argument("--quick", action="store_true")
    product = commands.add_parser("product")
    product.add_argument("--base-url", required=True)
    product.add_argument("--plan-digest", required=True)
    product.add_argument("--out", required=True)
    product.add_argument("--quick", action="store_true")
    decide_parser = commands.add_parser("decide")
    decide_parser.add_argument("--windows", required=True)
    decide_parser.add_argument("--plan-digest", required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "local":
        document = run_local(args)
        print(json.dumps({key: document[key] for key in ("warm_target_tpot_ms", "parity_failures",
                                                         "residue_active_kv_states", "speculation_totals")}))
        return 0 if document["parity_failures"] == 0 else 1
    if args.command == "product":
        document = run_product(args)
        print(json.dumps(document["benchmark"]["decision"]))
        return 0
    windows = json.loads(Path(args.windows).read_text())
    print(json.dumps(decide(windows, plan_digest=args.plan_digest)["decision"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
