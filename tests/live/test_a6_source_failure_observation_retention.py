# SPDX-License-Identifier: AGPL-3.0-only
"""A6 source-failure observation survives the gateway's release of the request.

When recovery is refused synchronously (no disjoint successor, withheld
candidates, ...) the gateway releases the request as soon as the backend run
returns, which pops the router's pending state before a controlled-fault
observer can poll. The observed source-failure identity must stay readable for
that request, exactly and boundedly, without ever matching a foreign binding.
"""
from __future__ import annotations

from dataclasses import replace

import mycelium_live.router_port as router_port_module
from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute,
    _graph,
    _runtime,
    _submit_and_drain,
)


def _source_only_graph():
    full = _graph()
    return replace(
        full,
        stages=(
            replace(full.stages[0], placements=(full.stages[0].placements[0],)),
            replace(full.stages[1], placements=(full.stages[1].placements[0],)),
        ),
        edges=(full.edges[0],),
        loopback_edges=(full.loopback_edges[0],),
    )


def _capture_identity_at_release(router):
    captured: dict = {}
    original = router.release_request

    def release(request_id):
        with router._lock:
            pending = router._pending.get(request_id)
            if pending is not None and pending.source_failure_identity is not None:
                captured[request_id] = dict(pending.source_failure_identity)
        original(request_id)

    router.release_request = release
    return captured


def test_source_failure_stays_observed_after_synchronous_refusal_and_release(tmp_path) -> None:
    route = FailingThenRecoveringRoute()
    _coordinator, router, service, qualification = _runtime(
        tmp_path, route, base_graph=_source_only_graph()
    )
    captured = _capture_identity_at_release(router)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed", "cancelled"}] == [
            "failed"
        ]
        assert router.recovery_failure_reason(request_id) == "recovery_successor_unavailable"
        # The gateway released the request: the pending state is gone.
        assert request_id not in router._pending
        identity = captured[request_id]
        assert identity["request_id"] == request_id
        binding = {**identity, "absolute_deadline_ms": 1, "source_node_id": "node-0"}
        assert router.a6_source_failure_observed(binding) is True
        # A binding differing in any identity field never matches.
        for field in identity:
            if field == "request_id":
                continue
            forged = dict(binding)
            forged[field] = "forged"
            assert router.a6_source_failure_observed(forged) is False, field
        assert router.a6_source_failure_observed({"request_id": "unknown"}) is False
        assert router.a6_source_failure_observed({"request_id": 7}) is False
    finally:
        service.close()


def test_retained_source_failure_identities_are_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(router_port_module, "_RETAINED_SOURCE_FAILURES_MAX", 2)
    route = FailingThenRecoveringRoute()
    _coordinator, router, service, _qualification = _runtime(
        tmp_path, route, base_graph=_source_only_graph()
    )
    try:
        with router._lock:
            for index in range(4):
                router._retain_source_failure_identity(
                    f"request-{index}", {"request_id": f"request-{index}", "command_id": index}
                )
            router._retain_source_failure_identity("ignored", None)
            assert list(router._retained_source_failures) == ["request-2", "request-3"]
        assert router.a6_source_failure_observed(
            {"request_id": "request-3", "command_id": 3}) is True
        assert router.a6_source_failure_observed(
            {"request_id": "request-0", "command_id": 0}) is False
    finally:
        service.close()


def test_pending_request_identity_remains_authoritative(tmp_path) -> None:
    """While pending, a stale retained entry can never answer for the request."""

    route = FailingThenRecoveringRoute()
    _coordinator, router, service, _qualification = _runtime(
        tmp_path, route, base_graph=_source_only_graph()
    )
    try:
        with router._lock:
            router._retain_source_failure_identity(
                "request-x", {"request_id": "request-x", "command_id": 1})
            router._pending["request-x"] = type("P", (), {"source_failure_identity": None})()
        assert router.a6_source_failure_observed(
            {"request_id": "request-x", "command_id": 1}) is False
        with router._lock:
            router._pending.pop("request-x")
        assert router.a6_source_failure_observed(
            {"request_id": "request-x", "command_id": 1}) is True
    finally:
        service.close()


def test_binding_from_the_executing_generation_matches_the_failure_generation(tmp_path) -> None:
    """The failure joins the next cancellation generation; the armed binding trails it."""

    route = FailingThenRecoveringRoute()
    _coordinator, router, service, _qualification = _runtime(
        tmp_path, route, base_graph=_source_only_graph()
    )
    identity = {"request_id": "request-g", "command_id": "c", "cancellation_generation": 1}
    try:
        with router._lock:
            router._retain_source_failure_identity("request-g", identity)
        for generation, expected in ((0, True), (1, True), (2, False), (-1, False), ("0", False)):
            binding = {**identity, "cancellation_generation": generation}
            assert router.a6_source_failure_observed(binding) is expected, generation
        assert router.a6_source_failure_observed(
            {"request_id": "request-g", "command_id": "c"}) is False
        assert router.a6_source_failure_observed(
            {**identity, "cancellation_generation": 0, "command_id": "other"}) is False
    finally:
        service.close()

