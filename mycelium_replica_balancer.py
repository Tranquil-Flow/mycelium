# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request balancing across independent whole-model routes (V2 PV8), pure.

Each route is a complete copy of the model on its own hosts with its own
A6/A7 recovery. The balancer decides which route a NEW request goes to and
keeps the request bound to that route until it is released. It never moves an
admitted request: a route that stops being ready only stops receiving new
requests, and its bound requests follow that route's own recovery.

Policy: join-shortest-queue by outstanding tokens normalized by route weight
(``(outstanding_tokens + cost) / weight``). Ties go to the route with fewer
outstanding requests, then the least recently selected route (so equal routes
alternate and none starves), then the route id. Nothing here reads a clock,
a socket or a thread other than its own lock.

Nothing in the live product calls this yet; see docs/design/REPLICAS.md.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Any, Sequence

NO_HEALTHY_REPLICA = "no_healthy_replica"
REPLICA_CAPACITY_EXHAUSTED = "replica_capacity_exhausted"
REQUEST_TOO_LARGE = "request_too_large"

DEFAULT_RETRY_AFTER_S = 0.25
_MAX_ID = 256


class RouteHealth(str, Enum):
    READY = "ready"
    RECOVERING = "recovering"
    DOWN = "down"


class ReplicaRejected(RuntimeError):
    """No route can take the request now; ``code`` is closed, never free text."""

    def __init__(self, code: str, retry_after_s: float) -> None:
        self.code = code
        self.retry_after_s = retry_after_s
        super().__init__(code)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _identifier(value: object, error: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_ID:
        raise ValueError(error)
    return value


def _weight(value: Any) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("invalid_route_weight")
    return float(value)


def _limits(requests: Any, tokens: Any) -> None:
    if not _is_int(requests) or requests <= 0:
        raise ValueError("invalid_route_limit")
    if tokens is not None and (not _is_int(tokens) or tokens <= 0):
        raise ValueError("invalid_route_limit")


def _cost(value: Any) -> int:
    if not _is_int(value) or value <= 0:
        raise ValueError("invalid_request_cost")
    return value


@dataclass(frozen=True)
class RouteSpec:
    """Static facts about one route: its share weight and admission limits."""

    route_id: str
    weight: float = 1.0
    max_outstanding_requests: int = 8
    max_outstanding_tokens: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.route_id, "invalid_route_id")
        object.__setattr__(self, "weight", _weight(self.weight))
        _limits(self.max_outstanding_requests, self.max_outstanding_tokens)


@dataclass(frozen=True)
class RouteLoad:
    """One route's state at the moment of a selection."""

    route_id: str
    health: RouteHealth
    weight: float
    outstanding_requests: int
    outstanding_tokens: int
    max_outstanding_requests: int
    max_outstanding_tokens: int | None = None
    last_selected_sequence: int = 0

    def __post_init__(self) -> None:
        _identifier(self.route_id, "invalid_route_id")
        if not isinstance(self.health, RouteHealth):
            raise ValueError("invalid_route_health")
        object.__setattr__(self, "weight", _weight(self.weight))
        _limits(self.max_outstanding_requests, self.max_outstanding_tokens)
        if (not _is_int(self.outstanding_requests) or self.outstanding_requests < 0
                or not _is_int(self.outstanding_tokens) or self.outstanding_tokens < 0
                or not _is_int(self.last_selected_sequence) or self.last_selected_sequence < 0):
            raise ValueError("invalid_route_load")


def _has_room(item: RouteLoad, cost: int) -> bool:
    if item.outstanding_requests >= item.max_outstanding_requests:
        return False
    cap = item.max_outstanding_tokens
    return cap is None or item.outstanding_tokens + cost <= cap


def select_route(
    loads: Sequence[RouteLoad],
    cost_tokens: int,
    *,
    retry_after_s: float = DEFAULT_RETRY_AFTER_S,
) -> str:
    """Pick the route for one new request, or raise ``ReplicaRejected``.

    ``cost_tokens`` is the request's token cost (prompt plus the new tokens it
    may produce). Deterministic: the answer does not depend on input order.
    """
    cost = _cost(cost_tokens)
    ready = [item for item in loads if item.health is RouteHealth.READY]
    if not ready:
        raise ReplicaRejected(NO_HEALTHY_REPLICA, retry_after_s)
    open_routes = [item for item in ready if _has_room(item, cost)]
    if not open_routes:
        # A request no ready route could ever hold must not be told to retry.
        if all(item.max_outstanding_tokens is not None and cost > item.max_outstanding_tokens
               for item in ready):
            raise ReplicaRejected(REQUEST_TOO_LARGE, retry_after_s)
        raise ReplicaRejected(REPLICA_CAPACITY_EXHAUSTED, retry_after_s)
    best = min(
        open_routes,
        key=lambda item: (
            Fraction(item.outstanding_tokens + cost) / Fraction(item.weight),
            item.outstanding_requests,
            item.last_selected_sequence,
            item.route_id,
        ),
    )
    return best.route_id


class ReplicaBalancer:
    """Per-route admission and request binding over a fixed set of routes."""

    def __init__(self, routes: Sequence[RouteSpec], *,
                 retry_after_s: float = DEFAULT_RETRY_AFTER_S) -> None:
        routes = tuple(routes)
        if not routes:
            raise ValueError("routes_required")
        if len({item.route_id for item in routes}) != len(routes):
            raise ValueError("duplicate_route_id")
        if (isinstance(retry_after_s, bool) or not isinstance(retry_after_s, (int, float))
                or not math.isfinite(retry_after_s) or retry_after_s <= 0):
            raise ValueError("invalid_retry_after")
        self._specs: dict[str, RouteSpec] = {item.route_id: item for item in routes}
        self._retry_after_s = float(retry_after_s)
        self._lock = threading.RLock()
        self._health: dict[str, RouteHealth] = {rid: RouteHealth.READY for rid in self._specs}
        self._requests: dict[str, int] = {rid: 0 for rid in self._specs}
        self._tokens: dict[str, int] = {rid: 0 for rid in self._specs}
        self._last: dict[str, int] = {rid: 0 for rid in self._specs}
        self._bound: dict[str, tuple[str, int]] = {}
        self._sequence = 0

    def set_health(self, route_id: str, health: RouteHealth) -> None:
        """Record a route's health (fed from that route's own liveness)."""
        if not isinstance(health, RouteHealth):
            raise ValueError("invalid_route_health")
        with self._lock:
            if route_id not in self._specs:
                raise ValueError("unknown_route")
            self._health[route_id] = health

    def admit(self, request_id: str, cost_tokens: int) -> str:
        """Choose and bind a route for a new request; raises ``ReplicaRejected``."""
        _identifier(request_id, "invalid_request_id")
        cost = _cost(cost_tokens)
        with self._lock:
            if request_id in self._bound:
                raise ValueError("duplicate_request_id")
            route_id = select_route(self._loads(), cost, retry_after_s=self._retry_after_s)
            self._sequence += 1
            self._last[route_id] = self._sequence
            self._requests[route_id] += 1
            self._tokens[route_id] += cost
            self._bound[request_id] = (route_id, cost)
            return route_id

    def release(self, request_id: str) -> bool:
        """Free the request's slot. False when it was not bound (idempotent)."""
        with self._lock:
            binding = self._bound.pop(request_id, None) if isinstance(request_id, str) else None
            if binding is None:
                return False
            route_id, cost = binding
            self._requests[route_id] -= 1
            self._tokens[route_id] -= cost
            return True

    def route_of(self, request_id: str) -> str | None:
        with self._lock:
            binding = self._bound.get(request_id)
            return None if binding is None else binding[0]

    def requests_on(self, route_id: str) -> tuple[str, ...]:
        """Request ids bound to a route, sorted. They are never migrated."""
        with self._lock:
            return tuple(sorted(rid for rid, (route, _) in self._bound.items()
                                if route == route_id))

    def status(self) -> dict[str, Any]:
        """Plain-data status; carries counts and route ids, never request ids."""
        with self._lock:
            routes = [
                {
                    "route_id": rid,
                    "health": self._health[rid].value,
                    "weight": spec.weight,
                    "outstanding_requests": self._requests[rid],
                    "outstanding_tokens": self._tokens[rid],
                    "max_outstanding_requests": spec.max_outstanding_requests,
                    "max_outstanding_tokens": spec.max_outstanding_tokens,
                }
                for rid, spec in sorted(self._specs.items())
            ]
            serving = sum(self._health[rid] is RouteHealth.READY for rid in self._specs)
            configured = len(self._specs)
            any_down = any(self._health[rid] is RouteHealth.DOWN for rid in self._specs)
            return {
                "copies_configured": configured,
                "copies_serving": serving,
                "summary": _summary(serving, configured, any_down),
                "routes": routes,
            }

    def _loads(self) -> list[RouteLoad]:
        return [
            RouteLoad(
                route_id=rid,
                health=self._health[rid],
                weight=spec.weight,
                outstanding_requests=self._requests[rid],
                outstanding_tokens=self._tokens[rid],
                max_outstanding_requests=spec.max_outstanding_requests,
                max_outstanding_tokens=spec.max_outstanding_tokens,
                last_selected_sequence=self._last[rid],
            )
            for rid, spec in self._specs.items()
        ]


def _summary(serving: int, configured: int, any_down: bool) -> str:
    if serving == 0:
        return "no copy of the model is serving"
    if serving == configured:
        if configured == 1:
            return "1 copy of the model is serving"
        return f"{configured} copies of the model are serving; balancing requests"
    verb = "is" if serving == 1 else "are"
    rest = "unavailable" if any_down else "recovering"
    return f"{serving} of {configured} copies of the model {verb} serving; the rest are {rest}"


__all__ = [
    "DEFAULT_RETRY_AFTER_S",
    "NO_HEALTHY_REPLICA",
    "REPLICA_CAPACITY_EXHAUSTED",
    "REQUEST_TOO_LARGE",
    "ReplicaBalancer",
    "ReplicaRejected",
    "RouteHealth",
    "RouteLoad",
    "RouteSpec",
    "select_route",
]
