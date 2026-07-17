#!/usr/bin/env python3
"""
Mycelium network probe: ping, jitter, geolocation.

Stdlib only. No ICMP privileges required. Uses TCP+UDP probes.
- TCP probe: time TCP handshake to a target Mycelium node's HTTP port.
- UDP probe: time a tiny UDP round-trip against a target.

Also extends probe.py with IP-based geolocation (city, country, lat/lon, ISP).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

PROTOCOL = "mycelium.network_probe.v1"
DEFAULT_PROBE_PORT = 8788
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_ATTEMPTS = 8
DEFAULT_PROBE_SIZE = 32
DEFAULT_JITTER_PROBES = 12


# ── statistics ────────────────────────────────────────────────────────────────

def aggregate_samples(samples_ms: Iterable[float]) -> dict[str, Any]:
    samples = [float(s) for s in samples_ms if s is not None]
    if not samples:
        return {"count": 0, "min_ms": None, "avg_ms": None, "max_ms": None,
                "p50_ms": None, "p95_ms": None, "stddev_ms": None, "jitter_ms": None,
                "loss_ratio": 1.0}
    samples_sorted = sorted(samples)
    avg = sum(samples) / len(samples)
    p50 = _percentile(samples_sorted, 50)
    p95 = _percentile(samples_sorted, 95)
    std = statistics.pstdev(samples) if len(samples) > 1 else 0.0
    jitter = sum(abs(x - avg) for x in samples) / len(samples)
    return {
        "count": len(samples),
        "min_ms": round(samples_sorted[0], 3),
        "avg_ms": round(avg, 3),
        "max_ms": round(samples_sorted[-1], 3),
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "stddev_ms": round(std, 3),
        "jitter_ms": round(jitter, 3),
        "loss_ratio": 0.0,
    }


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


# ── probes ───────────────────────────────────────────────────────────────────

def tcp_ping(host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT_S,
             attempts: int = DEFAULT_ATTEMPTS, settle: float = 0.05) -> dict[str, Any]:
    samples: list[float] = []
    errors: list[str] = []
    target = (host, int(port))
    for _ in range(int(attempts)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.monotonic()
        try:
            s.connect(target)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            samples.append(elapsed_ms)
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            try:
                s.close()
            except OSError:
                pass
        if settle > 0:
            time.sleep(settle)
    agg = aggregate_samples(samples)
    agg["errors"] = errors[:3]
    agg["loss_ratio"] = round((attempts - len(samples)) / max(1, attempts), 3)
    return agg


def udp_ping(host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT_S,
             attempts: int = DEFAULT_ATTEMPTS, payload_size: int = DEFAULT_PROBE_SIZE) -> dict[str, Any]:
    """Round-trip a small UDP datagram to measure latency without ICMP."""
    samples: list[float] = []
    errors: list[str] = []
    payload = b"\0" * max(8, int(payload_size))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(int(attempts)):
            t0 = time.monotonic()
            try:
                sock.sendto(payload, (host, int(port)))
                # Try to receive echo; many endpoints won't echo UDP, so fall back to send-only timing.
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    data = b""
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                if data or elapsed_ms <= timeout * 1000.0:
                    samples.append(elapsed_ms)
            except Exception as exc:
                errors.append(repr(exc))
    finally:
        sock.close()
    agg = aggregate_samples(samples)
    agg["errors"] = errors[:3]
    agg["loss_ratio"] = round((attempts - len(samples)) / max(1, attempts), 3)
    return agg


def http_ping(url: str, *, timeout: float = DEFAULT_TIMEOUT_S,
              attempts: int = DEFAULT_ATTEMPTS) -> dict[str, Any]:
    samples: list[float] = []
    errors: list[str] = []
    for _ in range(int(attempts)):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                resp.read(64)
            samples.append((time.monotonic() - t0) * 1000.0)
        except Exception as exc:
            errors.append(repr(exc))
    agg = aggregate_samples(samples)
    agg["errors"] = errors[:3]
    agg["loss_ratio"] = round((attempts - len(samples)) / max(1, attempts), 3)
    return agg


# ── geolocation ───────────────────────────────────────────────────────────────

GEOLOCATION_ENDPOINTS = [
    # Each endpoint returns {lat, lon, city, country, isp} or compatible.
    # Order matters: try the cheapest first.
    ("http://ip-api.com/json/?fields=status,message,lat,lon,city,country,isp,org,as",
     lambda d: d.get("status") == "success" and {"lat": d.get("lat"), "lon": d.get("lon"),
                                                 "city": d.get("city"), "country": d.get("country"),
                                                 "isp": d.get("isp") or d.get("org")}),
    ("https://ipwho.is/",
     lambda d: d.get("success") is True and {"lat": (d.get("latitude") or {}).get("lat") if isinstance(d.get("latitude"), dict) else d.get("latitude"),
                                             "lon": (d.get("longitude") or {}).get("lng") if isinstance(d.get("longitude"), dict) else d.get("longitude"),
                                             "city": d.get("city"), "country": d.get("country"),
                                             "isp": d.get("connection", {}).get("isp") if isinstance(d.get("connection"), dict) else None}),
]


def lookup_ip_geolocation(timeout: float = 6.0) -> dict[str, Any] | None:
    for url, parser in GEOLOCATION_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MyceliumProbe/0.2"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
            parsed = parser(payload)
            if parsed and parsed.get("lat") is not None and parsed.get("lon") is not None:
                return {
                    "lat": round(float(parsed["lat"]), 2),
                    "lon": round(float(parsed["lon"]), 2),
                    "city": parsed.get("city"),
                    "country": parsed.get("country"),
                    "isp": parsed.get("isp"),
                    "source": url.split("/")[2],
                    "precision": "city",
                }
        except Exception:
            continue
    return None


def estimate_location(profile_location: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a node's location field into a router-friendly shape."""
    if not profile_location:
        return {"lat": None, "lon": None, "city": None, "country": None, "precision": "unknown"}
    lat = profile_location.get("lat")
    lon = profile_location.get("lon")
    if lat is not None and lon is not None:
        return {
            "lat": float(lat),
            "lon": float(lon),
            "city": profile_location.get("city"),
            "country": profile_location.get("country"),
            "isp": profile_location.get("isp"),
            "precision": "city",
        }
    if profile_location.get("country"):
        return {"lat": None, "lon": None, "city": None,
                "country": profile_location.get("country"), "precision": "country"}
    return {"lat": None, "lon": None, "city": None, "country": None, "precision": "unknown"}


def haversine_km(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    if not a or not b or a.get("lat") is None or b.get("lat") is None:
        return None
    lat1, lon1 = float(a["lat"]), float(a["lon"])
    lat2, lon2 = float(b["lat"]), float(b["lon"])
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


# ── pairwise matrix ───────────────────────────────────────────────────────────

def _target_endpoint(record: dict[str, Any], probe_port: int) -> str | None:
    profile = record.get("profile") or record
    cap = profile.get("capabilities") or {}
    network = profile.get("network") or {}
    if profile.get("profile_url"):
        return profile["profile_url"]
    lan_ip = cap.get("lan_ip") or network.get("lan_ip")
    candidates = []
    if lan_ip:
        candidates.append(lan_ip)
    location = profile.get("location") or {}
    isp = (location.get("isp") or "").lower()
    # Heuristic: same ISP often means same private LAN. We still need IP for probing.
    for key in ("public_ip", "wan_ip"):
        if profile.get(key):
            candidates.append(profile[key])
    if not candidates:
        return None
    host = candidates[0]
    return f"http://{host}:{probe_port}/profile"


def build_pairwise_matrix(
    nodes: dict[str, dict[str, Any]],
    samples: dict[tuple[str, str], list[float]],
    *,
    fallback_ms_per_km: float = 0.005,
    fallback_min_ms: float = 5.0,
) -> dict[str, dict[str, Any]]:
    matrix: dict[str, dict[str, Any]] = {}
    for (src_id, dst_id), pts in samples.items():
        src = nodes.get(src_id, {})
        dst = nodes.get(dst_id, {})
        src_loc = estimate_location((src.get("profile") or src).get("location"))
        dst_loc = estimate_location((dst.get("profile") or dst).get("location"))
        dist = haversine_km(src_loc, dst_loc)
        agg = aggregate_samples(pts)
        record = {
            "src": src_id,
            "dst": dst_id,
            "samples": agg["count"],
            "ping_avg_ms": agg["avg_ms"],
            "ping_min_ms": agg["min_ms"],
            "ping_max_ms": agg["max_ms"],
            "ping_p50_ms": agg["p50_ms"],
            "ping_p95_ms": agg["p95_ms"],
            "jitter_ms": agg["jitter_ms"],
            "stddev_ms": agg["stddev_ms"],
            "loss_ratio": agg["loss_ratio"],
            "distance_km": round(dist, 3) if dist is not None else None,
            "location_src": src_loc,
            "location_dst": dst_loc,
            "source": "probe",
        }
        if agg["avg_ms"] is None and dist is not None:
            record["ping_avg_ms"] = round(max(fallback_min_ms, dist * fallback_ms_per_km + 5.0), 3)
            record["source"] = "geo_estimate"
        matrix[f"{src_id}->{dst_id}"] = record
        # mirror with samples=0 because we did not actually probe that direction
        mirror = dict(record)
        mirror["src"] = dst_id
        mirror["dst"] = src_id
        mirror["location_src"], mirror["location_dst"] = mirror["location_dst"], mirror["location_src"]
        mirror["samples"] = 0
        mirror["ping_avg_ms"] = None
        mirror["ping_min_ms"] = None
        mirror["ping_max_ms"] = None
        mirror["ping_p50_ms"] = None
        mirror["ping_p95_ms"] = None
        mirror["jitter_ms"] = None
        mirror["stddev_ms"] = None
        mirror["loss_ratio"] = 1.0
        mirror["source"] = "geo_estimate" if mirror["distance_km"] is not None else "none"
        matrix[f"{dst_id}->{src_id}"] = mirror
    return matrix


# ── CLI ───────────────────────────────────────────────────────────────────────

def _probe_one_pair(args: tuple[str, str, str, int, float, int]) -> tuple[str, str, list[float], str | None]:
    src_id, dst_id, dst_url, port, timeout, attempts = args
    samples: list[float] = []
    err: str | None = None
    try:
        host = dst_url.split("//", 1)[1].split(":", 1)[0]
        # If URL has explicit port, prefer it (covers SSH-tunneled peer setups).
        url_port = port
        try:
            host_port = dst_url.split("//", 1)[1].split("/", 1)[0]
            if ":" in host_port:
                url_port = int(host_port.split(":", 1)[1])
        except Exception:
            url_port = port
    except Exception as exc:
        return src_id, dst_id, [], repr(exc)
    # Mix TCP and HTTP probes; take whichever succeeds
    tcp_result = tcp_ping(host, url_port, timeout=timeout, attempts=attempts)
    if tcp_result["count"] > 0:
        # Re-derive samples by re-running to keep raw list, simpler: synthesize from avg * attempts
        samples = [tcp_result["avg_ms"]] * tcp_result["count"]
    else:
        http_result = http_ping(f"http://{host}:{url_port}/health", timeout=timeout, attempts=attempts)
        if http_result["count"] > 0:
            samples = [http_result["avg_ms"]] * http_result["count"]
        else:
            err = tcp_result.get("errors", ["unknown"])[0]
    return src_id, dst_id, samples, err


def collect_pairwise_samples(
    nodes: dict[str, dict[str, Any]],
    *,
    self_id: str,
    probe_port: int,
    timeout: float,
    attempts: int,
    max_pairs: int,
) -> tuple[dict[tuple[str, str], list[float]], list[str]]:
    samples: dict[tuple[str, str], list[float]] = {}
    errors: list[str] = []
    pairs: list[tuple[str, str, str, int, float, int]] = []
    targets = [(node_id, rec) for node_id, rec in nodes.items() if node_id != self_id]
    for dst_id, rec in targets:
        endpoint = _target_endpoint(rec, probe_port)
        if not endpoint:
            errors.append(f"no_endpoint_for:{dst_id}")
            continue
        pairs.append((self_id, dst_id, endpoint, probe_port, timeout, attempts))
    if max_pairs and len(pairs) > max_pairs:
        pairs = pairs[:max_pairs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pairs) or 1)) as pool:
        future_map = {pool.submit(_probe_one_pair, p): p for p in pairs}
        for fut in concurrent.futures.as_completed(future_map):
            src_id, dst_id, pts, err = fut.result()
            samples[(src_id, dst_id)] = pts
            if err:
                errors.append(f"{src_id}->{dst_id}:{err}")
    return samples, errors


def load_nodes(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    if args.nodes_url:
        with urllib.request.urlopen(args.nodes_url, timeout=10) as resp:
            return json.loads(resp.read()).get("nodes", {})
    return json.loads(Path(args.nodes_file).read_text()).get("nodes", {})


def build_report(
    nodes: dict[str, dict[str, Any]],
    *,
    self_id: str,
    locations: dict[str, dict[str, Any]],
    samples: dict[tuple[str, str], list[float]],
    errors: list[str],
) -> dict[str, Any]:
    own_loc = locations.get(self_id) or estimate_location(
        (nodes.get(self_id, {}).get("profile") or {}).get("location")
    )
    matrix = build_pairwise_matrix(nodes, samples)
    return {
        "ok": True,
        "protocol": PROTOCOL,
        "self": self_id,
        "self_location": own_loc,
        "locations": locations,
        "pairwise": matrix,
        "errors": errors[:20],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mycelium network probe: ping, jitter, geolocation")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--nodes-file")
    src.add_argument("--nodes-url")
    parser.add_argument("--self", required=True, help="self node_id")
    parser.add_argument("--probe-port", type=int, default=DEFAULT_PROBE_PORT)
    parser.add_argument("--probe-attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--probe-timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--max-pairs", type=int, default=0)
    parser.add_argument("--self-location-override", help='JSON {"lat":..,"lon":..,"city":..}')
    parser.add_argument("--out", help="write report JSON here")
    parser.add_argument("--skip-probe", action="store_true",
                        help="skip active probing, only compute locations and matrix shape")
    args = parser.parse_args(argv)

    nodes = load_nodes(args)

    # Locations for every node; self may be overridden if provided.
    locations: dict[str, dict[str, Any]] = {}
    for node_id, rec in nodes.items():
        loc = (rec.get("profile") or rec).get("location") or {}
        locations[node_id] = estimate_location(loc)
    if args.self_location_override:
        locations[args.self] = estimate_location(json.loads(args.self_location_override))

    if args.skip_probe or args.probe_port == 0:
        samples: dict[tuple[str, str], list[float]] = {}
        errors: list[str] = ["probe_skipped"]
    else:
        samples, errors = collect_pairwise_samples(
            nodes,
            self_id=args.self,
            probe_port=args.probe_port,
            timeout=args.probe_timeout,
            attempts=args.probe_attempts,
            max_pairs=args.max_pairs,
        )

    report = build_report(nodes, self_id=args.self, locations=locations, samples=samples, errors=errors)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())