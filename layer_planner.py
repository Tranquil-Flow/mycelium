#!/usr/bin/env python3
"""
Mycelium layer planner / allocator v1.

Inputs:
- Mycelium `/nodes` JSON from broadcast layer
- model shape/spec

Output:
- contiguous layer route plan for initial static allocation

Claim boundary:
- uses join-time Tier-1 node profiles and heuristic throughput estimates
- does not benchmark real layer execution yet
- does not measure pairwise jitter/latency yet; location/network estimates are provisional
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REAL_LAYER_BACKENDS = {
    "mlx",
    "torch_mps",
    "cuda",
    "torch_cuda",
    "llama_cpp",
    "mlc_llm",
    "onnxruntime",
}

BACKEND_EFFICIENCY = {
    "cuda": 1.0,
    "torch_cuda": 1.0,
    "mlx": 0.85,
    "torch_mps": 0.75,
    "llama_cpp": 0.55,
    "mlc_llm": 0.55,
    "onnxruntime": 0.45,
    "torch_cpu": 0.18,
}

DEVICE_EFFICIENCY = {
    "server": 1.0,
    "desktop": 0.9,
    "laptop": 0.75,
    "phone": 0.30,
}

FALLBACK_RAM_BW_GBPS = {
    "Darwin": 100.0,
    "Linux": 40.0,
    "Android": 25.0,
    "Windows": 40.0,
}


@dataclass
class ModelSpec:
    model_id: str
    num_layers: int
    hidden_size: int
    weight_bytes: int = 2
    activation_bytes: int = 2
    context_length: int = 2048
    batch_size: int = 1
    compression_ratio: float = 1.0
    min_memory_reserve_gb: float = 2.0
    layer_weight_gb: float | None = None
    kv_cache_gb_per_layer: float | None = None
    required_backends: list[str] | None = None

    def estimated_layer_weight_gb(self) -> float:
        if self.layer_weight_gb is not None:
            return float(self.layer_weight_gb)
        # Transformer decoder rough memory-bound layer footprint:
        # attention projections + MLP weights ≈ 12 * hidden^2 params.
        params = 12 * (int(self.hidden_size) ** 2)
        return max(0.01, params * int(self.weight_bytes) / (1024**3))

    def estimated_kv_cache_gb_per_layer(self) -> float:
        if self.kv_cache_gb_per_layer is not None:
            return float(self.kv_cache_gb_per_layer)
        # K+V cache: 2 * batch * context * hidden * bytes.
        bytes_total = 2 * int(self.batch_size) * int(self.context_length) * int(self.hidden_size) * int(self.activation_bytes)
        return bytes_total / (1024**3)

    def per_layer_memory_gb(self) -> float:
        return self.estimated_layer_weight_gb() + self.estimated_kv_cache_gb_per_layer()

    def boundary_bytes_per_token(self) -> float:
        return int(self.hidden_size) * int(self.activation_bytes) * float(self.compression_ratio)


@dataclass
class CandidateNode:
    node_id: str
    profile: dict[str, Any]
    capabilities: dict[str, Any]
    max_layers: int
    layer_time_ms: float
    memory_bandwidth_gbps: float
    upload_mbps: float | None
    location: dict[str, Any] | None
    reasons: list[str]
    pairwise_ping_ms: float | None = None
    pairwise_jitter_ms: float | None = None


def _get_cap(record: dict[str, Any]) -> dict[str, Any]:
    profile = record.get("profile") or record
    return profile.get("capabilities") or {}


def _get_profile(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("profile") or record


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _backend_match(backends: list[str], required: list[str] | None) -> bool:
    if not required:
        return bool(set(backends) & REAL_LAYER_BACKENDS)
    return bool(set(backends) & set(required))


def _best_backend_efficiency(backends: list[str]) -> float:
    vals = [BACKEND_EFFICIENCY.get(b, 0.0) for b in backends]
    return max(vals) if vals else 0.0


def _memory_bandwidth(cap: dict[str, Any]) -> float:
    vals = [
        _num(cap.get("vram_bandwidth_gbps")),
        _num(cap.get("ram_bandwidth_gbps")),
    ]
    vals = [v for v in vals if v and v > 0]
    if vals:
        return max(vals)
    return FALLBACK_RAM_BW_GBPS.get(str(cap.get("platform")), 30.0)


def candidate_from_record(
    node_id: str,
    record: dict[str, Any],
    spec: ModelSpec,
    *,
    require_ac: bool = True,
    allow_phone_layers: bool = True,
    pairwise_ping_ms: float | None = None,
    pairwise_jitter_ms: float | None = None,
) -> CandidateNode:
    profile = _get_profile(record)
    cap = _get_cap(record)
    reasons: list[str] = []
    device_class = cap.get("device_class") or profile.get("device_class")
    backends = list(cap.get("backends") or profile.get("backends") or [])

    if require_ac and cap.get("on_ac_power") is not True:
        reasons.append("not_on_ac_power")
    if device_class == "phone" and not allow_phone_layers:
        reasons.append("phone_layers_disabled")
    if not _backend_match(backends, spec.required_backends):
        reasons.append("no_required_backend")

    available_gb = _num(cap.get("ram_available_gb"), 0.0) or 0.0
    usable_gb = max(0.0, available_gb - float(spec.min_memory_reserve_gb))
    per_layer_gb = spec.per_layer_memory_gb()
    max_layers = int(usable_gb // per_layer_gb) if per_layer_gb > 0 else spec.num_layers
    if max_layers <= 0:
        reasons.append("insufficient_memory")

    bw = _memory_bandwidth(cap)
    backend_eff = _best_backend_efficiency(backends)
    device_eff = DEVICE_EFFICIENCY.get(str(device_class), 0.6)
    effective_bw = max(1.0, bw * max(backend_eff, 0.1) * device_eff)
    # One layer pass roughly streams layer weights; efficiency folds backend + device class.
    layer_time_ms = spec.estimated_layer_weight_gb() / effective_bw * 1000.0
    # Keep nonzero so DP behaves with tiny toy models.
    layer_time_ms = max(layer_time_ms, 0.01)

    upload = _num(cap.get("upload_mbps"), None)
    node_id = str(node_id or profile.get("node_id") or profile.get("hostname"))
    return CandidateNode(
        node_id=node_id,
        profile=profile,
        capabilities=cap,
        max_layers=max_layers,
        layer_time_ms=layer_time_ms,
        memory_bandwidth_gbps=bw,
        upload_mbps=upload,
        location=profile.get("location"),
        reasons=reasons,
        pairwise_ping_ms=pairwise_ping_ms,
        pairwise_jitter_ms=pairwise_jitter_ms,
    )


def haversine_km(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    if not a or not b:
        return None
    lat1, lon1 = _num(a.get("lat")), _num(a.get("lon"))
    lat2, lon2 = _num(b.get("lat")), _num(b.get("lon"))
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def estimate_link_ms(src: CandidateNode, dst: CandidateNode, spec: ModelSpec) -> float:
    """Crude initial transfer penalty per token for one layer-boundary transfer.

    Prefer measured pairwise ping/jitter when available; fall back to upload-based
    transfer estimate plus distance-based latency floor.
    """
    upload = src.upload_mbps if src.upload_mbps and src.upload_mbps > 0 else 25.0
    transfer_ms = (spec.boundary_bytes_per_token() * 8.0) / (upload * 1_000_000.0) * 1000.0
    dist = haversine_km(src.location, dst.location)
    if dist is None:
        latency_ms = 20.0
    else:
        # Fiber one-way speed floor + internet/routing overhead.
        latency_ms = max(2.0, dist / 200.0 + 5.0)
    # Use measured ping from src->dst if provided; jitter adds to transfer budget.
    measured_ping = dst.pairwise_ping_ms if dst.pairwise_ping_ms is not None else None
    measured_jitter = dst.pairwise_jitter_ms if dst.pairwise_jitter_ms is not None else 0.0
    if measured_ping is not None:
        return measured_ping + measured_jitter + transfer_ms
    return latency_ms + transfer_ms


def _best_counts_for_order(order: tuple[CandidateNode, ...], layers: int) -> tuple[float, list[int]] | None:
    k = len(order)
    inf = 10**18
    dp = [[inf] * (layers + 1) for _ in range(k + 1)]
    prev: list[list[int | None]] = [[None] * (layers + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for i, node in enumerate(order, start=1):
        for total in range(1, layers + 1):
            max_x = min(node.max_layers, total)
            for x in range(1, max_x + 1):
                if dp[i - 1][total - x] >= inf:
                    continue
                cost = max(dp[i - 1][total - x], x * node.layer_time_ms)
                if cost < dp[i][total]:
                    dp[i][total] = cost
                    prev[i][total] = x
    if dp[k][layers] >= inf:
        return None
    counts = []
    total = layers
    for i in range(k, 0, -1):
        x = prev[i][total]
        if x is None:
            return None
        counts.append(int(x))
        total -= int(x)
    counts.reverse()
    return dp[k][layers], counts


def _route_from_counts(order: tuple[CandidateNode, ...], counts: list[int]) -> list[dict[str, Any]]:
    route = []
    start = 0
    for node, count in zip(order, counts):
        end = start + count - 1
        route.append({
            "node_id": node.node_id,
            "layers": [start, end],
            "layer_count": count,
            "estimated_compute_ms": round(count * node.layer_time_ms, 4),
            "backend": node.capabilities.get("primary_gpu_backend"),
            "memory_bandwidth_gbps": node.memory_bandwidth_gbps,
            "ram_available_gb": node.capabilities.get("ram_available_gb"),
        })
        start = end + 1
    return route


def _select_search_nodes(candidates: list[CandidateNode], max_nodes: int) -> list[CandidateNode]:
    # Avoid factorial blow-up. Keep nodes with highest memory-bw adjusted per-layer speed.
    ranked = sorted(candidates, key=lambda n: (n.max_layers, 1.0 / n.layer_time_ms), reverse=True)
    return ranked[: max(1, max_nodes)]


def _lookup_link(matrix: dict[str, dict[str, Any]] | None, src: str | None, dst: str) -> dict[str, Any] | None:
    if not matrix or not src:
        return None
    return matrix.get(f"{src}->{dst}")


def plan_layer_allocation(
    nodes_doc: dict[str, Any],
    spec: ModelSpec,
    *,
    max_nodes: int = 6,
    require_ac: bool = True,
    allow_phone_layers: bool = True,
    self_node_id: str | None = None,
    link_matrix: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records = nodes_doc.get("nodes", nodes_doc)
    if not isinstance(records, dict):
        raise ValueError("nodes_doc must contain a nodes object")

    candidates: list[CandidateNode] = []
    ineligible: dict[str, list[str]] = {}
    diagnostics: dict[str, Any] = {}
    for node_id, record in records.items():
        link = _lookup_link(link_matrix, self_node_id, str(node_id)) if link_matrix else None
        cand = candidate_from_record(
            str(node_id),
            record,
            spec,
            require_ac=require_ac,
            allow_phone_layers=allow_phone_layers,
            pairwise_ping_ms=(link or {}).get("ping_avg_ms"),
            pairwise_jitter_ms=(link or {}).get("jitter_ms"),
        )
        diagnostics[cand.node_id] = {
            "max_layers": cand.max_layers,
            "layer_time_ms": round(cand.layer_time_ms, 4),
            "memory_bandwidth_gbps": cand.memory_bandwidth_gbps,
            "upload_mbps": cand.upload_mbps,
            "pairwise_ping_ms": cand.pairwise_ping_ms,
            "pairwise_jitter_ms": cand.pairwise_jitter_ms,
            "reasons": cand.reasons,
        }
        if cand.reasons:
            ineligible[cand.node_id] = cand.reasons
        else:
            candidates.append(cand)

    if not candidates:
        return {
            "ok": False,
            "error": "no_eligible_nodes",
            "model": _model_report(spec),
            "route": [],
            "ineligible": ineligible,
            "diagnostics": diagnostics,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    total_capacity = sum(c.max_layers for c in candidates)
    if total_capacity < spec.num_layers:
        return {
            "ok": False,
            "error": "insufficient_total_layer_capacity",
            "model": _model_report(spec),
            "route": [],
            "ineligible": ineligible,
            "diagnostics": diagnostics,
            "total_capacity_layers": total_capacity,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    search_nodes = _select_search_nodes(candidates, max_nodes=max_nodes)
    # V1 allocation policy: use the widest feasible eligible set up to max_nodes,
    # then optimize order/counts within that fixed width. This makes the planner an
    # allocator, not merely a single-node latency selector. Later network-aware
    # pruning can lower target width when measured links are truly bad.
    best: dict[str, Any] | None = None
    max_k = min(len(search_nodes), max_nodes, spec.num_layers)
    for k in range(max_k, 0, -1):
        width_best: dict[str, Any] | None = None
        for order in itertools.permutations(search_nodes, k):
            if sum(n.max_layers for n in order) < spec.num_layers:
                continue
            allocation = _best_counts_for_order(order, spec.num_layers)
            if allocation is None:
                continue
            compute_ms, counts = allocation
            transfer_ms = sum(estimate_link_ms(a, b, spec) for a, b in zip(order, order[1:]))
            objective_ms = compute_ms + transfer_ms
            candidate_plan = {
                "objective_ms": objective_ms,
                "estimated_decode_ms_per_token": objective_ms,
                "estimated_pipeline_compute_ms": compute_ms,
                "estimated_transfer_ms_per_token": transfer_ms,
                "route": _route_from_counts(order, counts),
                "node_order": [n.node_id for n in order],
            }
            if width_best is None or objective_ms < width_best["objective_ms"]:
                width_best = candidate_plan
        if width_best is not None:
            best = width_best
            break

    if best is None:
        return {
            "ok": False,
            "error": "no_valid_contiguous_allocation",
            "model": _model_report(spec),
            "route": [],
            "ineligible": ineligible,
            "diagnostics": diagnostics,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    return {
        "ok": True,
        "protocol": "mycelium.route_plan.v1",
        "model": _model_report(spec),
        "route": best["route"],
        "node_order": best["node_order"],
        "estimated_decode_ms_per_token": round(best["estimated_decode_ms_per_token"], 4),
        "estimated_pipeline_compute_ms": round(best["estimated_pipeline_compute_ms"], 4),
        "estimated_transfer_ms_per_token": round(best["estimated_transfer_ms_per_token"], 4),
        "ineligible": ineligible,
        "diagnostics": diagnostics,
        "claim_boundary": "heuristic_initial_layer_allocation_from_tier1_profiles; not benchmarked real inference",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _model_report(spec: ModelSpec) -> dict[str, Any]:
    return {
        "model_id": spec.model_id,
        "num_layers": spec.num_layers,
        "hidden_size": spec.hidden_size,
        "weight_bytes": spec.weight_bytes,
        "activation_bytes": spec.activation_bytes,
        "context_length": spec.context_length,
        "batch_size": spec.batch_size,
        "compression_ratio": spec.compression_ratio,
        "required_backends": spec.required_backends,
        "estimated_layer_weight_gb": round(spec.estimated_layer_weight_gb(), 6),
        "estimated_kv_cache_gb_per_layer": round(spec.estimated_kv_cache_gb_per_layer(), 6),
        "per_layer_memory_gb": round(spec.per_layer_memory_gb(), 6),
        "boundary_bytes_per_token": round(spec.boundary_bytes_per_token(), 2),
    }


def load_nodes_from_url(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def load_nodes(args: argparse.Namespace) -> dict[str, Any]:
    if args.nodes_url:
        return load_nodes_from_url(args.nodes_url)
    if args.nodes_file:
        return json.loads(Path(args.nodes_file).read_text())
    raise SystemExit("must pass --nodes-file or --nodes-url")


def build_spec(args: argparse.Namespace) -> ModelSpec:
    required = args.required_backend or ["mlx", "torch_mps", "cuda", "torch_cuda", "llama_cpp", "mlc_llm", "onnxruntime"]
    return ModelSpec(
        model_id=args.model_id,
        num_layers=args.num_layers,
        hidden_size=args.hidden_size,
        weight_bytes=args.weight_bytes,
        activation_bytes=args.activation_bytes,
        context_length=args.context_length,
        batch_size=args.batch_size,
        compression_ratio=args.compression_ratio,
        min_memory_reserve_gb=args.reserve_gb,
        layer_weight_gb=args.layer_weight_gb,
        kv_cache_gb_per_layer=args.kv_cache_gb_per_layer,
        required_backends=required,
    )


def _load_link_matrix(args: argparse.Namespace) -> tuple[str | None, dict[str, dict[str, Any]] | None]:
    if args.link_matrix_file:
        data = json.loads(Path(args.link_matrix_file).read_text())
        return data.get("self"), data.get("pairwise")
    if args.link_matrix_url:
        with urllib.request.urlopen(args.link_matrix_url, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("self"), data.get("pairwise")
    return None, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mycelium heuristic layer planner")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--nodes-file", help="path to /nodes JSON")
    src.add_argument("--nodes-url", help="URL to /nodes JSON")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--num-layers", type=int, required=True)
    parser.add_argument("--hidden-size", type=int, required=True)
    parser.add_argument("--weight-bytes", type=int, default=2)
    parser.add_argument("--activation-bytes", type=int, default=2)
    parser.add_argument("--context-length", type=int, default=2048)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--compression-ratio", type=float, default=1.0)
    parser.add_argument("--reserve-gb", type=float, default=2.0)
    parser.add_argument("--layer-weight-gb", type=float)
    parser.add_argument("--kv-cache-gb-per-layer", type=float)
    parser.add_argument("--required-backend", action="append", default=[])
    parser.add_argument("--max-nodes", type=int, default=6)
    parser.add_argument("--allow-battery", action="store_true")
    phone_policy = parser.add_mutually_exclusive_group()
    phone_policy.add_argument(
        "--disable-phone-layers",
        action="store_true",
        help="exclude phones from layer allocation (phones are eligible by default)",
    )
    phone_policy.add_argument(
        "--allow-phone-layers",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--link-matrix-file", help="network_probe.json to consume for measured ping/jitter")
    parser.add_argument("--link-matrix-url", help="URL to network_probe.json")
    parser.add_argument("--self-node-id", help="node_id used as src for measured link matrix")
    parser.add_argument("--out", help="write route plan JSON to file")
    args = parser.parse_args(argv)

    nodes_doc = load_nodes(args)
    spec = build_spec(args)
    self_id, link_matrix = _load_link_matrix(args)
    plan = plan_layer_allocation(
        nodes_doc,
        spec,
        max_nodes=args.max_nodes,
        require_ac=not args.allow_battery,
        allow_phone_layers=not args.disable_phone_layers,
        self_node_id=args.self_node_id or self_id,
        link_matrix=link_matrix,
    )
    text = json.dumps(plan, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if plan.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
