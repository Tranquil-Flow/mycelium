# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium_router.analyze — statistical analyzer for hop-level timing traces.

Reads JSONL files produced by `mycelium_router.instrument.HopRecorder` and
produces:

  * `summarize(path) -> dict` — machine-readable summary:
        n_requests
        ttft_ms: min / median / p95 / p99 / max
        per_hop_stage_node: { "stage@node": {n, wall_ms: {stats}, compute_ms: {...},
                                  transport_ms: {...}, queue_ms: {...}, bytes_sent: {...} } }
        per_hop_breakdown_pct: { "stage@node": {compute_pct, transport_pct, queue_pct} }
        theoretical_overlap_floor_ms: best-case TTFT if every per-hop stage
                                       ran on the slowest node with no overlap
        sequential_floor_ms: sum of per-hop wall times (current behaviour)
        bottleneck_ranking: ordered list of (stage@node, save_potential_ms) where
                            a hypothetical 50% reduction would save the most ms
        correlation: {"transport_to_next_queue": float or None} — does transport
                     time on hop N predict queue time on hop N+1?
        drift: sparkline-friendly list of (request_index, ttft_ms) — drift detection

  * `render_markdown(summary) -> str` — markdown report with sections.

The analyzer is read-only and side-effect free. It imports nothing from
mycelium_router at module load time (zero coupling).
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def _read_jsonl(path: str | Path) -> list[dict]:
    out: list[dict] = []
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    with p.open() as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            out.append(json.loads(ln))
    return out


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def _stats(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0, "min": 0.0, "median": 0.0, "mean": 0.0,
                "p95": 0.0, "p99": 0.0, "max": 0.0, "stdev": 0.0}
    return {
        "n": len(xs),
        "min": round(min(xs), 3),
        "median": round(_percentile(xs, 0.50), 3),
        "mean": round(statistics.fmean(xs), 3),
        "p95": round(_percentile(xs, 0.95), 3),
        "p99": round(_percentile(xs, 0.99), 3),
        "max": round(max(xs), 3),
        "stdev": round(statistics.pstdev(xs), 3) if len(xs) > 1 else 0.0,
    }


def _histogram_sparkline(values: list[float], width: int = 24) -> str:
    """Tiny in-line sparkline: bins into `width` cells, '▁▂▃▄▅▆▇█'."""
    if not values:
        return "(no data)"
    bars = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return bars[4] * width
    n = len(values)
    bins = [0] * width
    for v in values:
        idx = min(width - 1, int((v - lo) / (hi - lo) * width))
        bins[idx] += 1
    peak = max(bins)
    if peak == 0:
        return bars[0] * width
    return "".join(bars[min(7, int(bins[i] / peak * 7))] for i in range(width))


def summarize(path: str | Path) -> dict[str, Any]:
    traces = _read_jsonl(path)
    if not traces:
        return {"n_requests": 0, "error": "no traces in file"}

    # Per-hop aggregation keyed by (stage_id, node_id).
    by_hop: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    ttfts: list[float] = []
    total_walls: list[float] = []  # request wall = finished_at - received_at
    drift: list[tuple[int, float]] = []
    per_hop_walls_by_request: dict[str, float] = defaultdict(float)

    for i, t in enumerate(traces):
        ttfts.append(float(t.get("ttft_ms", 0.0)))
        received = float(t.get("received_at", 0.0))
        finished = float(t.get("finished_at", 0.0))
        if finished > 0 and received > 0:
            total_walls.append((finished - received) * 1000.0)
        drift.append((i, float(t.get("ttft_ms", 0.0))))
        for hop in t.get("hops", []):
            key = f"{hop['stage_id']}@{hop['node_id']}"
            by_hop[key]["wall_ms"].append(float(hop.get("wall_ms", 0.0)))
            by_hop[key]["compute_ms"].append(float(hop.get("compute_ms", 0.0)))
            by_hop[key]["transport_ms"].append(
                float(hop.get("transport_ms", 0.0)))
            by_hop[key]["queue_ms"].append(float(hop.get("queue_ms", 0.0)))
            by_hop[key]["bytes_sent"].append(float(hop.get("bytes_sent", 0)))
            per_hop_walls_by_request[key] += float(hop.get("wall_ms", 0.0))

    per_hop_summary: dict[str, dict] = {}
    breakdown_pct: dict[str, dict] = {}
    for key, metrics in sorted(by_hop.items()):
        per_hop_summary[key] = {f"{m}": _stats(v) for m, v in metrics.items()}
        # Compute the percentage breakdown on medians (more robust than means).
        med_compute = per_hop_summary[key]["compute_ms"]["median"]
        med_transport = per_hop_summary[key]["transport_ms"]["median"]
        med_queue = per_hop_summary[key]["queue_ms"]["median"]
        total = med_compute + med_transport + med_queue
        if total <= 0:
            breakdown_pct[key] = {"compute": 0.0, "transport": 0.0, "queue": 0.0}
        else:
            breakdown_pct[key] = {
                "compute": round(100.0 * med_compute / total, 1),
                "transport": round(100.0 * med_transport / total, 1),
                "queue": round(100.0 * med_queue / total, 1),
            }

    # Two honest bounds on the floor, depending on pipeline topology:
    #   parallel_lower: all stages run concurrently (best case). One
    #     bottleneck stage dominates, plus one transport hop. This is the
    #     absolute floor.
    #   parallel_upper: stages run in parallel but each takes its own worst
    #     compute + transport. Sum over stages.
    # We cannot know the topology from client-side measurements, so we report
    # both. When sequential_floor_ms falls inside this band, parallelism is
    # partial; when it's at or below parallel_lower, the pipeline is fully
    # overlapped.
    stage_keys = sorted(by_hop.keys())
    parallel_lower = 0.0
    parallel_upper = 0.0
    for key in stage_keys:
        metrics = by_hop[key]
        max_compute = max(metrics["compute_ms"]) if metrics["compute_ms"] else 0.0
        max_transport = max(metrics["transport_ms"]) if metrics["transport_ms"] else 0.0
        parallel_upper += max_compute + max_transport
        parallel_lower = max(parallel_lower, max_compute + max_transport)
    theoretical_overlap_floor_ms = round(parallel_lower, 3)
    theoretical_parallel_upper_ms = round(parallel_upper, 3)

    # The "sequential floor" is the mean end-to-end wall time per request.
    # We prefer total_wall (finished_at - received_at in ms) over ttft_ms
    # because ttft_ms only covers the time-to-first-token; the full request
    # includes decode. If total_wall isn't available, fall back to ttft_ms.
    if total_walls:
        sequential_floor_ms = round(sum(total_walls) / len(total_walls), 3)
    else:
        sequential_floor_ms = round(sum(ttfts) / len(ttfts), 3) if ttfts else 0.0

    # Bottleneck ranking: assume a 50% reduction of each hop's wall_ms; rank by
    # absolute savings if every request saw that reduction. Use medians.
    bottlenecks: list[tuple[str, float]] = []
    for key, stats in per_hop_summary.items():
        median = stats["wall_ms"]["median"]
        n = stats["wall_ms"]["n"]
        save = median * 0.5 * n
        bottlenecks.append((key, round(save, 3)))
    bottlenecks.sort(key=lambda x: -x[1])

    # Correlation: hop N transport_ms vs hop N+1 queue_ms across requests.
    # Align by request_id and hop-order.
    request_to_hops: dict[str, list[dict]] = defaultdict(list)
    for t in traces:
        request_to_hops[t["request_id"]] = t.get("hops", [])
    pairs: list[tuple[float, float]] = []
    for hops in request_to_hops.values():
        for i in range(len(hops) - 1):
            pairs.append((
                float(hops[i].get("transport_ms", 0.0)),
                float(hops[i + 1].get("queue_ms", 0.0)),
            ))
    # Causal chain: when the instrument records causal edges (hop_id +
    # causal_predecessor), verify every hop's predecessor matches the previous
    # hop in its request. Tolerates traces that predate the edge fields.
    chain_ok = True
    chain_checked = 0
    for hops in request_to_hops.values():
        previous_id = None
        for hop in hops:
            if "hop_id" in hop and "causal_predecessor" in hop:
                chain_checked += 1
                if previous_id is not None and hop.get("causal_predecessor") != previous_id:
                    chain_ok = False
                previous_id = hop.get("hop_id")
    causal_chain_intact = chain_ok and chain_checked > 0


    corr = None
    if len(pairs) >= 2:
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        try:
            corr = round(statistics.correlation(xs, ys), 4)
        except statistics.StatisticsError:
            corr = None

    return {
        "n_requests": len(traces),
        "ttft_ms": _stats(ttfts),
        "total_wall_ms": _stats(total_walls) if total_walls else None,
        "per_hop_stage_node": per_hop_summary,
        "per_hop_breakdown_pct": breakdown_pct,
        "theoretical_overlap_floor_ms": theoretical_overlap_floor_ms,
        "theoretical_parallel_upper_ms": theoretical_parallel_upper_ms,
        "sequential_floor_ms": sequential_floor_ms,
        "bottleneck_ranking": bottlenecks,
        "correlation": {"transport_to_next_queue": corr,
                        "n_pairs": len(pairs)},
        "causal_chain_intact": causal_chain_intact,
        "causal_edges_checked": chain_checked,
        "drift": drift,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    if summary.get("n_requests", 0) == 0:
        return "# Pipeline Analysis\n\n_no traces in file_\n"
    lines: list[str] = []
    lines.append("# Pipeline Analysis")
    lines.append("")
    lines.append(f"**Requests analyzed:** {summary['n_requests']}")
    lines.append("")

    # TTFT
    ttft = summary["ttft_ms"]
    lines.append("## End-to-end TTFT")
    lines.append("")
    lines.append(f"- median: **{ttft['median']:.0f} ms**")
    lines.append(f"- p95: {ttft['p95']:.0f} ms · p99: {ttft['p99']:.0f} ms")
    lines.append(f"- min: {ttft['min']:.0f} · max: {ttft['max']:.0f} · mean: {ttft['mean']:.0f}")
    spark = _histogram_sparkline([v for _, v in summary["drift"]])
    lines.append(f"- distribution: `{spark}`")
    lines.append("")

    # Per-hop table
    lines.append("## Per-hop wall-time (ms, by stage@node)")
    lines.append("")
    lines.append("| stage@node | n | min | median | p95 | p99 | max | compute% | transport% | queue% |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    bp = summary["per_hop_breakdown_pct"]
    for key, stats in sorted(summary["per_hop_stage_node"].items()):
        w = stats["wall_ms"]
        b = bp.get(key, {})
        lines.append(
            f"| `{key}` | {w['n']} | {w['min']:.1f} | {w['median']:.1f} | "
            f"{w['p95']:.1f} | {w['p99']:.1f} | {w['max']:.1f} | "
            f"{b.get('compute', 0):.0f}% | {b.get('transport', 0):.0f}% | "
            f"{b.get('queue', 0):.0f}% |")
    lines.append("")

    # Bottleneck ranking
    lines.append("## Bottleneck ranking (savings if 50% reduction)")
    lines.append("")
    for key, save in summary["bottleneck_ranking"][:8]:
        lines.append(f"- `{key}`: -{save:.0f} ms total across all requests")
    lines.append("")

    # Sequential vs theoretical — two honest bounds.
    seq = summary["sequential_floor_ms"]
    floor_low = summary["theoretical_overlap_floor_ms"]
    floor_high = summary.get("theoretical_parallel_upper_ms", floor_low)
    lines.append("## Sequential vs theoretical")
    lines.append("")
    total_wall = summary.get("total_wall_ms") or {}
    if total_wall:
        lines.append(f"- Current total wall (mean): **{seq:.0f} ms** (min/median/p95/p99/max: {total_wall.get('min', 0):.0f}/{seq:.0f}/{total_wall.get('p95', 0):.0f}/{total_wall.get('p99', 0):.0f}/{total_wall.get('max', 0):.0f})")
        lines.append(f"- Current TTFT (mean, first-token only): **{ttft['mean']:.0f} ms**")
    else:
        lines.append(f"- Current TTFT (mean): **{seq:.0f} ms**")
    lines.append(f"- Theoretical floor (fully parallel): **{floor_low:.0f} ms**")
    lines.append(f"- Theoretical floor (parallel, summed worst): **{floor_high:.0f} ms**")
    if seq > floor_high:
        lines.append(f"- Headroom: **{seq - floor_high:.0f} ms** between current and the parallel-summed bound.")
    elif seq > floor_low:
        lines.append(f"- Partial overlap: current TTFT sits between fully-parallel ({floor_low:.0f} ms) and summed-worst ({floor_high:.0f} ms).")
    else:
        lines.append(f"- Already at or below the fully-parallel floor ({floor_low:.0f} ms).")
    lines.append("")

    # Correlation
    corr = summary["correlation"]["transport_to_next_queue"]
    n_pairs = summary["correlation"]["n_pairs"]
    if corr is None:
        lines.append("## Transport → next-queue correlation")
        lines.append("")
        lines.append(f"_insufficient pairs (n={n_pairs}) for correlation_")
    else:
        lines.append("## Transport → next-queue correlation")
        lines.append("")
        lines.append(
            f"- r = **{corr:+.3f}** across {n_pairs} consecutive-hop pairs. "
            + ("Network backpressure suspected." if abs(corr) > 0.3
               else "No strong backpressure signal."))
    lines.append("")

    # Causal chain
    lines.append("## Causal chain")
    lines.append("")
    if summary.get("causal_edges_checked", 0) > 0:
        status = "intact" if summary.get("causal_chain_intact") else "BROKEN"
        lines.append(
            f"- {summary['causal_edges_checked']} causal edges verified: **{status}** "
            "(predecessor links form one unbroken request path)")
    else:
        lines.append("- no causal edges recorded in these traces")
    lines.append("")

    # Recommendation
    lines.append("## Recommendation")
    lines.append("")
    if summary["bottleneck_ranking"]:
        top = summary["bottleneck_ranking"][0]
        lines.append(
            f"Focus next on **{top[0]}** — it accounts for the largest absolute "
            f"savings potential ({top[1]:.0f} ms across all requests).")
    if seq > floor_high:
        lines.append(
            f"Current total wall ({seq:.0f} ms) sits **{(seq / max(1, floor_high)):.1f}x** above "
            "the summed-worst parallel bound — significant serialisation "
            "headroom exists in the request path.")
    elif seq > floor_low:
        lines.append(
            f"Current total wall ({seq:.0f} ms) sits between fully-parallel ({floor_low:.0f} ms) "
            f"and summed-worst ({floor_high:.0f} ms) — partial overlap, "
            "investigate cross-stage serialisation points.")
    if corr is not None and abs(corr) > 0.3:
        lines.append(
            "Transport→queue correlation is non-trivial; transport is a leading "
            "indicator of queueing backpressure. Measure link health alongside hop "
            "timings in future runs.")
    lines.append("")

    return "\n".join(lines) + "\n"


__all__ = ["summarize", "render_markdown"]
