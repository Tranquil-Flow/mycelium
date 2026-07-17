from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from .contracts import DirectedLinkObservation, PlanningPolicy


EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class EdgeCost:
    total_ms: float
    base_one_way_ms: float
    serialization_ms: float
    jitter_guard_ms: float
    loss_penalty_ms: float
    confidence: float
    payload_bytes: int
    bandwidth_Bps: float
    diagnostics: tuple[str, ...] = ()


def geodesic_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def geolocation_floor_ms(distance_km: float, propagation_km_per_ms: float = 200.0) -> float:
    if distance_km < 0 or propagation_km_per_ms <= 0:
        raise ValueError("invalid propagation inputs")
    return distance_km / propagation_km_per_ms


def transfer_time_ms(
    link: DirectedLinkObservation,
    payload_bytes: int,
    policy: PlanningPolicy,
) -> EdgeCost:
    if payload_bytes < 0:
        raise ValueError("payload must be non-negative")
    diagnostics: list[str] = []
    bandwidth = link.bandwidth_Bps
    if bandwidth is None:
        if policy.exclude_missing_bandwidth or policy.conservative_bandwidth_Bps is None:
            raise ValueError(f"link {link.src}->{link.dst} lacks measured bandwidth")
        bandwidth = policy.conservative_bandwidth_Bps
        diagnostics.append("fallback_bandwidth")
    base = max(link.rtt_ms / 2.0, link.geolocation_floor_ms)
    serialization = payload_bytes / bandwidth * 1000.0
    jitter = policy.jitter_guard_sigma * link.jitter_ms
    loss = policy.loss_penalty_ms * link.loss_ratio
    confidence = link.confidence
    if link.inferred:
        confidence *= 0.7
        diagnostics.append("inferred_measurement")
    if link.stale:
        confidence *= 0.7
        diagnostics.append("stale_measurement")
    if link.geolocation_floor_ms > link.rtt_ms / 2.0:
        diagnostics.append("geolocation_floor_active")
    return EdgeCost(
        total_ms=base + serialization + jitter + loss,
        base_one_way_ms=base,
        serialization_ms=serialization,
        jitter_guard_ms=jitter,
        loss_penalty_ms=loss,
        confidence=confidence,
        payload_bytes=payload_bytes,
        bandwidth_Bps=bandwidth,
        diagnostics=tuple(diagnostics),
    )


def phase_edge_costs(
    link: DirectedLinkObservation,
    payloads: Mapping[str, int],
    policy: PlanningPolicy,
) -> dict[str, EdgeCost]:
    return {name: transfer_time_ms(link, payloads[name], policy) for name in sorted(payloads)}
