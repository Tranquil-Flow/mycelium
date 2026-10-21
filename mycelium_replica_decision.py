# SPDX-License-Identifier: AGPL-3.0-or-later
"""When does the swarm run more than one copy of the model? (V2 PV8), pure.

Two gates, both required for k >= 2 copies (otherwise there is one copy, which
is what the product does today):

1. Memory. k disjoint host groups must each be able to hold a complete copy
   with headroom. A group of hosts fits when the sum over its hosts of
   ``max(0, usable_i - fixed_overhead)`` is at least
   ``(load_peak_factor * resident + kv) * (1 + headroom)``, with
   ``usable_i = memory_i * usable_memory_fraction`` and the group size inside
   ``[min_hosts_per_route, max_hosts_per_route]``. With a memory-proportional
   layer split this equals the per-member check. It is a pre-filter: the real
   compile, resource check and qualification stay authoritative.
2. Planner score. The best k-way layout's robust throughput gain over today's
   single route (all hosts) must exceed the planner's minimum gain, using the
   planner's own formula (``mycelium_layer_planner.replication``):
   ``robust = max(0, gain * (1 - max(uncertainty, 1 - confidence)))`` against
   ``max(eps, single * minimum_gain_fraction)``. Capacity per host group comes
   from an injected estimator, so a missing estimate means no replicas.

The search is exact over set partitions of at most ``MAX_EXACT_HOSTS`` hosts;
hosts may sit out of every copy. Ties prefer fewer copies, then fewer unused
hosts, then the lexicographically smallest layout. Nothing here reads a
clock, a file or the network. Nothing in the live product calls it yet; see
docs/design/REPLICAS.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Iterable, Iterator, Sequence

MAX_EXACT_HOSTS = 10
NUMERIC_EPSILON = 1e-12
_MAX_ID = 256

REASON_ACCEPTED = "accepted_positive_robust_gain"
REASON_BELOW_THRESHOLD = "gain_below_threshold"
REASON_ONE_COPY_MEMORY = "single_copy_only_memory"
REASON_NO_FIT = "model_does_not_fit"
REASON_NO_ESTIMATE = "no_capacity_estimate"


class ReplicaDecisionError(ValueError):
    """A closed ``code`` for malformed input (never free text)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class HostMemory:
    node_id: str
    memory_bytes: int

    def __post_init__(self) -> None:
        if (not isinstance(self.node_id, str) or not self.node_id
                or len(self.node_id) > _MAX_ID
                or not _is_int(self.memory_bytes) or self.memory_bytes <= 0):
            raise ReplicaDecisionError("invalid_host")


@dataclass(frozen=True)
class ModelFootprint:
    """One complete copy: resident weight bytes, KV bytes and the load-peak factor."""

    resident_bytes: int
    kv_bytes: int = 0
    load_peak_factor: int = 2

    def __post_init__(self) -> None:
        if (not _is_int(self.resident_bytes) or self.resident_bytes <= 0
                or not _is_int(self.kv_bytes) or self.kv_bytes < 0
                or not _is_int(self.load_peak_factor) or self.load_peak_factor < 1):
            raise ReplicaDecisionError("invalid_footprint")


@dataclass(frozen=True)
class ReplicaPolicy:
    """Defaults mirror the live resource model and the planner's policy."""

    usable_memory_fraction: Fraction = Fraction(3, 4)    # MEMBER_PEAK_MEMORY_FRACTION
    fixed_overhead_bytes: int = 1 << 30                  # GROUP_FIXED_BYTES
    headroom_fraction: Fraction = Fraction(15, 100)      # GUARD_HEADROOM
    min_hosts_per_route: int = 2                         # layer_split.MIN_HOSTS (1 after PV31)
    max_hosts_per_route: int = 8                         # layer_split.MAX_HOSTS
    max_copies: int = 8
    minimum_gain_fraction: float = 0.05                  # PlanningPolicy
    uncertainty_fraction: float = 0.1                    # PlanningPolicy

    def __post_init__(self) -> None:
        ok = (
            isinstance(self.usable_memory_fraction, Fraction)
            and 0 < self.usable_memory_fraction <= 1
            and _is_int(self.fixed_overhead_bytes) and self.fixed_overhead_bytes >= 0
            and isinstance(self.headroom_fraction, Fraction) and self.headroom_fraction >= 0
            and _is_int(self.min_hosts_per_route) and self.min_hosts_per_route >= 1
            and _is_int(self.max_hosts_per_route)
            and self.max_hosts_per_route >= self.min_hosts_per_route
            and _is_int(self.max_copies) and self.max_copies >= 1
            and _is_number(self.minimum_gain_fraction)
            and math.isfinite(self.minimum_gain_fraction)
            and 0 <= self.minimum_gain_fraction <= 1
            and _is_number(self.uncertainty_fraction)
            and math.isfinite(self.uncertainty_fraction)
            and 0 <= self.uncertainty_fraction < 1
        )
        if not ok:
            raise ReplicaDecisionError("invalid_policy")


@dataclass(frozen=True)
class RouteEstimate:
    """The planner's (or calibration's) view of one host group serving one copy."""

    capacity_rps: float
    calibration_confidence: float = 1.0

    def __post_init__(self) -> None:
        if (not _is_number(self.capacity_rps) or not math.isfinite(self.capacity_rps)
                or self.capacity_rps < 0
                or not _is_number(self.calibration_confidence)
                or not math.isfinite(self.calibration_confidence)
                or not 0 <= self.calibration_confidence <= 1):
            raise ReplicaDecisionError("invalid_estimate")


CapacityEstimator = Callable[[tuple[str, ...]], "RouteEstimate | None"]


@dataclass(frozen=True)
class ReplicaDecision:
    copies: int
    groups: tuple[tuple[str, ...], ...]
    reason: str
    single_route_admitted_rps: float = 0.0
    layout_admitted_rps: float = 0.0
    robust_gain_rps: float = 0.0
    minimum_required_gain_rps: float = 0.0
    uncertainty_fraction: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "copies": self.copies,
            "groups": [list(group) for group in self.groups],
            "reason": self.reason,
            "single_route_admitted_rps": self.single_route_admitted_rps,
            "layout_admitted_rps": self.layout_admitted_rps,
            "robust_gain_rps": self.robust_gain_rps,
            "minimum_required_gain_rps": self.minimum_required_gain_rps,
            "uncertainty_fraction": self.uncertainty_fraction,
        }


# --- memory ------------------------------------------------------------------

def _need(footprint: ModelFootprint, policy: ReplicaPolicy) -> Fraction:
    peak = footprint.load_peak_factor * footprint.resident_bytes + footprint.kv_bytes
    return Fraction(peak) * (1 + policy.headroom_fraction)


def _supply(host: HostMemory, policy: ReplicaPolicy) -> Fraction:
    usable = Fraction(host.memory_bytes) * policy.usable_memory_fraction
    return max(Fraction(0), usable - policy.fixed_overhead_bytes)


def group_fits(
    group: Sequence[HostMemory],
    footprint: ModelFootprint,
    policy: ReplicaPolicy,
) -> bool:
    """Can this host group hold one complete copy with headroom?"""
    if not policy.min_hosts_per_route <= len(group) <= policy.max_hosts_per_route:
        return False
    return sum((_supply(host, policy) for host in group), Fraction(0)) >= _need(footprint, policy)


def _checked_hosts(hosts: Iterable[HostMemory], policy: ReplicaPolicy) -> list[HostMemory]:
    items = list(hosts)
    if not all(isinstance(host, HostMemory) for host in items):
        raise ReplicaDecisionError("invalid_host")
    if len({host.node_id for host in items}) != len(items):
        raise ReplicaDecisionError("duplicate_host")
    if len(items) > min(policy.max_hosts_per_route, MAX_EXACT_HOSTS):
        raise ReplicaDecisionError("hosts_out_of_bounds")
    return sorted(items, key=lambda host: host.node_id)


def _layouts(
    hosts: Sequence[HostMemory],
    footprint: ModelFootprint,
    policy: ReplicaPolicy,
) -> Iterator[tuple[tuple[tuple[str, ...], ...], int]]:
    """Every layout of disjoint, individually fitting host groups.

    Yields ``(groups, unused_host_count)``; groups are sorted tuples of node
    ids in lexicographic order. Hosts may sit out.
    """
    need = _need(footprint, policy)
    supplies = [_supply(host, policy) for host in hosts]
    scale = 1
    for value in (*supplies, need):
        scale = scale * value.denominator // math.gcd(scale, value.denominator)
    weights = [int(value * scale) for value in supplies]
    need_scaled = int(need * scale)
    ids = [host.node_id for host in hosts]
    count = len(ids)
    blocks: list[list[int]] = []

    def walk(index: int, unused: int) -> Iterator[tuple[tuple[tuple[str, ...], ...], int]]:
        if index == count:
            if blocks and all(
                len(block) >= policy.min_hosts_per_route
                and sum(weights[i] for i in block) >= need_scaled
                for block in blocks
            ):
                yield tuple(tuple(ids[i] for i in block) for block in blocks), unused
            return
        for block in blocks:
            if len(block) < policy.max_hosts_per_route:
                block.append(index)
                yield from walk(index + 1, unused)
                block.pop()
        if len(blocks) < policy.max_copies:
            blocks.append([index])
            yield from walk(index + 1, unused)
            blocks.pop()
        yield from walk(index + 1, unused + 1)

    return walk(0, 0)


def max_memory_copies(
    hosts: Iterable[HostMemory],
    footprint: ModelFootprint,
    policy: ReplicaPolicy = ReplicaPolicy(),
) -> int:
    """The most disjoint complete copies the hosts' memory allows (0 if none)."""
    ordered = _checked_hosts(hosts, policy)
    return max((len(groups) for groups, _unused in _layouts(ordered, footprint, policy)),
               default=0)


# --- the decision ------------------------------------------------------------

def choose_replica_layout(
    hosts: Iterable[HostMemory],
    footprint: ModelFootprint,
    capacity_of: CapacityEstimator,
    policy: ReplicaPolicy = ReplicaPolicy(),
    *,
    offered_demand_rps: float | None = None,
) -> ReplicaDecision:
    """Decide how many complete copies to run and on which hosts.

    ``capacity_of(group)`` returns the estimate for one route over that host
    group (or ``None`` when there is none). ``offered_demand_rps`` caps what
    any layout can be admitted at; ``None`` means demand is not the limit.
    """
    ordered = _checked_hosts(hosts, policy)
    if offered_demand_rps is not None and (
        not _is_number(offered_demand_rps)
        or not math.isfinite(offered_demand_rps) or offered_demand_rps < 0
    ):
        raise ReplicaDecisionError("invalid_demand")
    everyone = tuple(host.node_id for host in ordered)
    if not ordered or not group_fits(ordered, footprint, policy):
        return ReplicaDecision(copies=0, groups=(), reason=REASON_NO_FIT)
    one_route = (everyone,)

    layouts = [(groups, unused) for groups, unused in _layouts(ordered, footprint, policy)
               if len(groups) >= 2]
    if not layouts:
        return ReplicaDecision(copies=1, groups=one_route, reason=REASON_ONE_COPY_MEMORY)

    cache: dict[tuple[str, ...], RouteEstimate | None] = {}

    def estimate(group: tuple[str, ...]) -> RouteEstimate | None:
        if group not in cache:
            cache[group] = capacity_of(group)
        return cache[group]

    def admitted(total: float) -> float:
        return total if offered_demand_rps is None else min(total, float(offered_demand_rps))

    base = estimate(everyone)
    if base is None:
        return ReplicaDecision(copies=1, groups=one_route, reason=REASON_NO_ESTIMATE)
    single = admitted(base.capacity_rps)
    minimum = single * policy.minimum_gain_fraction

    best: tuple[Any, ...] | None = None
    for groups, unused in layouts:
        estimates = [estimate(group) for group in groups]
        if any(item is None for item in estimates):
            continue
        total = admitted(math.fsum(item.capacity_rps for item in estimates))  # type: ignore[union-attr]
        confidence = min(item.calibration_confidence for item in estimates)  # type: ignore[union-attr]
        uncertainty = max(policy.uncertainty_fraction, 1.0 - confidence)
        robust = max(0.0, (total - single) * (1.0 - uncertainty))
        key = (-robust, len(groups), unused, groups)
        if best is None or key < best[0]:
            best = (key, groups, total, robust, uncertainty)
    if best is None:
        return ReplicaDecision(copies=1, groups=one_route, reason=REASON_NO_ESTIMATE,
                               single_route_admitted_rps=single)
    _key, groups, total, robust, uncertainty = best
    accepted = robust > max(NUMERIC_EPSILON, minimum)
    return ReplicaDecision(
        copies=len(groups) if accepted else 1,
        groups=groups if accepted else one_route,
        reason=REASON_ACCEPTED if accepted else REASON_BELOW_THRESHOLD,
        single_route_admitted_rps=single,
        layout_admitted_rps=total,
        robust_gain_rps=robust,
        minimum_required_gain_rps=minimum,
        uncertainty_fraction=uncertainty,
    )


__all__ = [
    "CapacityEstimator",
    "HostMemory",
    "MAX_EXACT_HOSTS",
    "ModelFootprint",
    "REASON_ACCEPTED",
    "REASON_BELOW_THRESHOLD",
    "REASON_NO_ESTIMATE",
    "REASON_NO_FIT",
    "REASON_ONE_COPY_MEMORY",
    "ReplicaDecision",
    "ReplicaDecisionError",
    "ReplicaPolicy",
    "RouteEstimate",
    "choose_replica_layout",
    "group_fits",
    "max_memory_copies",
]
