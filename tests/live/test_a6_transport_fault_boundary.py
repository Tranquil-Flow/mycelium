# SPDX-License-Identifier: AGPL-3.0-only
"""Simulated local session only; never physical/browser A6 qualification."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

import mycelium_live.route as route_module
from mycelium_live.liveness import LivenessSubject, SubjectKind

DIGEST = "sha256:" + "a" * 64
PATH_DIGEST = "sha256:" + "b" * 64


class LocalSession:
    returncode = None

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send(self, *, command_id, command, payload):
        self.calls.append((command_id, command, payload))
        if self.error is not None:
            raise self.error
        return {"sent": True}


def identity(request_id="request-a"):
    return {
        "deployment_id": "deployment-a", "deployment_epoch": 4,
        "qualification_digest": DIGEST, "request_id": request_id,
        "request_attempt": 2, "path_id": "path-a", "path_attempt": 3,
        "path_digest": PATH_DIGEST, "topology_generation": 8,
        "command_id": "owner-command-a", "publisher_generation": 1,
        "cancellation_generation": 0,
    }


def make_route(monkeypatch, *, bound=None, session=None, fault=True):
    import mycelium_request_gateway.contracts as gateway_contracts
    monkeypatch.setattr(route_module, "execution_graph_from_document", lambda _: SimpleNamespace())
    monkeypatch.setattr(gateway_contracts, "qualification_digest", lambda _: DIGEST)
    exact = identity()
    spec = (route_module._TestOnlyTransportFault(
        identity=exact, source_node_id="node-a", command="infer_start",
        deadline_monotonic_ms=int(time.monotonic() * 1000) + 10_000,
    ) if fault else None)
    options: dict[str, Any] = dict(
        controller=SimpleNamespace(peers=tuple(
            SimpleNamespace(node_id=n, process_transport="local") for n in ("node-a", "node-b")
        )), endpoints={},
        run_plan={"run_id": "local-fixture", "deployment_id": "deployment-a", "request": {},
                  "nodes": [{"node_id": "node-a", "configure": {"graph": {}}}]},
    )
    if fault:
        assert spec is not None
        route = route_module.PhysicalLiveRoute(**options, _test_only_transport_fault=spec)
    else:
        route = route_module.PhysicalLiveRoute(**options)
    route._open = True
    route._deployment_qualification = SimpleNamespace(route_ready=True)
    route._sessions = {"node-a": session or LocalSession(), "node-b": LocalSession()}
    route._active_route_requests["request-a"] = {
        **exact, "absolute_deadline_ms": int(time.monotonic() * 1000) + 10_000,
        "entry_node_id": "node-a", "participating_node_ids": frozenset({"node-a"}),
        "control_bound": True if bound is None else bound,
        "terminal": False, "cancellation_requested": False,
        "inflight_commands": {},
    }
    return route, spec


def send(route, *, request_id="request-a", node_id="node-a", command="infer_start",
         command_id="transport-command-a"):
    return route._send_request_command(
        request_id=request_id, node_id=node_id, command=command,
        command_id=command_id, payload={"request_id": request_id,
                                          "control": identity(request_id)},
    )


def finish(route):
    route._cleanup_reconciliation_stop.set()
    route._cleanup_reconciliation_executor.shutdown(wait=True, cancel_futures=True)


def test_matching_fault_on_command_path_does_not_silently_send(monkeypatch):
    """Behavioral RED against the pre-hook route, without importing a new symbol."""
    route, _ = make_route(monkeypatch, fault=False)
    setattr(route, "_test_only_transport_fault", SimpleNamespace(
        identity=identity(), source_node_id="node-a", command="infer_start",
        deadline_monotonic_ms=int(time.monotonic() * 1000) + 10_000,
        transport_command_id=None,
    ))
    route._test_only_transport_fault_consumed = False
    try:
        with pytest.raises(RuntimeError, match="^transport_not_running$"):
            send(route)
        assert route._sessions["node-a"].calls == []
        assert route._test_only_transport_fault_consumed is True
    finally:
        finish(route)


def test_matching_admitted_source_command_fails_before_local_transport(monkeypatch):
    route, _ = make_route(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="^transport_not_running$"):
            send(route)
        assert route._sessions["node-a"].calls == []
        assert route._active_route_requests["request-a"]["inflight_commands"] == {}
        # One-use: the same command next proceeds normally.
        assert send(route) == {"sent": True}
        assert len(route._sessions["node-a"].calls) == 1
    finally:
        finish(route)


@pytest.mark.parametrize("mutation", [
    "wrong_request", "wrong_node", "wrong_command", "wrong_transport_command_id",
    "wrong_deployment", "wrong_epoch", "wrong_qualification", "wrong_request_attempt",
    "wrong_path", "wrong_path_attempt", "wrong_path_digest", "wrong_topology",
    "wrong_owner_command", "wrong_publisher_generation", "cancelled", "terminal",
    "unbound", "expired", "cleanup", "wrong_assigned_source", "revoked",
])
def test_mismatch_or_retirement_never_consumes_other_request(monkeypatch, mutation):
    route, spec = make_route(monkeypatch)
    try:
        active = route._active_route_requests["request-a"]
        kwargs = {}
        if mutation == "wrong_request":
            route._active_route_requests["request-b"] = {**active, "request_id": "request-b", "inflight_commands": {}}
            kwargs["request_id"] = "request-b"
        elif mutation == "wrong_node":
            kwargs["node_id"] = "node-b"
        elif mutation == "wrong_command":
            kwargs["command"] = "infer_decode"
        elif mutation == "wrong_transport_command_id":
            # A transport command id is optional; when bound it must match.
            route._test_only_transport_fault = replace(spec, transport_command_id="exact-transport-id")
        elif mutation == "cancelled":
            active["cancellation_requested"] = True
            active["cancellation_generation"] = 1
        elif mutation == "terminal":
            active["terminal"] = True
        elif mutation == "unbound":
            active["control_bound"] = False
        elif mutation == "expired":
            route._test_only_transport_fault = replace(spec, deadline_monotonic_ms=1)
        elif mutation == "cleanup":
            route._request_cleanup_receipts["request-a"] = {"sealed": True}
        elif mutation == "wrong_assigned_source":
            active["entry_node_id"] = "node-b"
        elif mutation == "revoked":
            route._deployment_qualification = SimpleNamespace(route_ready=False)
        else:
            field = {
                "wrong_deployment": "deployment_id", "wrong_epoch": "deployment_epoch",
                "wrong_qualification": "qualification_digest", "wrong_request_attempt": "request_attempt",
                "wrong_path": "path_id", "wrong_path_attempt": "path_attempt",
                "wrong_path_digest": "path_digest", "wrong_topology": "topology_generation",
                "wrong_owner_command": "command_id", "wrong_publisher_generation": "publisher_generation",
            }[mutation]
            active[field] = "different" if isinstance(active[field], str) else active[field] + 1
        if mutation == "terminal":
            with pytest.raises(RuntimeError, match="request_control_identity_unavailable"):
                send(route, **kwargs)
        else:
            assert send(route, **kwargs) == {"sent": True}
        assert route._test_only_transport_fault_consumed is False
        assert route.a4_scoped_runtime_incidents() == ()
        if mutation != "terminal":
            assert len(route._sessions[kwargs.get("node_id", "node-a")].calls) == 1
    finally:
        finish(route)


def test_absent_hook_and_real_worker_error_are_not_masked(monkeypatch):
    real_error = RuntimeError("real_worker_error")
    route, _ = make_route(monkeypatch, fault=False, session=LocalSession(real_error))
    try:
        with pytest.raises(RuntimeError, match="real_worker_error"):
            send(route)
        assert len(route._sessions["node-a"].calls) == 1
        assert route.a4_scoped_runtime_incidents() == ()
    finally:
        finish(route)


def test_nonmatching_fault_does_not_mask_real_worker_error(monkeypatch):
    route, _ = make_route(monkeypatch, session=LocalSession(RuntimeError("real_worker_error")))
    route._active_route_requests["request-a"]["entry_node_id"] = "node-b"
    try:
        with pytest.raises(RuntimeError, match="^real_worker_error$"):
            send(route)
        assert len(route._sessions["node-a"].calls) == 1
        assert route._test_only_transport_fault_consumed is False
    finally:
        finish(route)


def test_default_constructor_has_no_fault_and_invalid_capability_is_rejected(monkeypatch):
    monkeypatch.setattr(route_module, "execution_graph_from_document", lambda _: SimpleNamespace())
    options = dict(
        controller=SimpleNamespace(peers=()), endpoints={},
        run_plan={"run_id": "local-fixture", "deployment_id": "deployment-a",
                  "nodes": [{"node_id": "node-a", "configure": {"graph": {}}}]},
    )
    route = route_module.PhysicalLiveRoute(**options)
    try:
        assert route._test_only_transport_fault is None
        session = LocalSession()
        route._sessions = {"node-a": session}
        route._active_route_requests["request-a"] = {
            **identity(), "terminal": False, "inflight_commands": {},
        }
        assert send(route) == {"sent": True}
        assert len(session.calls) == 1
    finally:
        finish(route)
    with pytest.raises(ValueError, match="invalid_test_transport_fault"):
        route_module.PhysicalLiveRoute(**options, _test_only_transport_fault=object())
    with pytest.raises(ValueError, match="invalid_test_transport_fault"):
        route_module._TestOnlyTransportFault(
            identity={**identity(), "request_attempt": True},
            source_node_id="node-a", command="infer_start",
            deadline_monotonic_ms=int(time.monotonic() * 1000) + 1_000,
        )


def test_route_infer_records_its_own_exact_a4_incident_after_fault(monkeypatch):
    """Local simulated command session; no gateway/browser or physical qualification."""
    import mycelium_router.serialization as serialization
    import mycelium_router.validation as validation
    import mycelium_request_gateway.contracts as gateway_contracts

    route, spec = make_route(monkeypatch)
    route._active_route_requests.clear()  # infer owns registration, not the fixture
    route._graph = SimpleNamespace(
        stages=(SimpleNamespace(placements=(SimpleNamespace(
            placement_id="placement-a", node_id="node-a"),)),),
        deployment_id="deployment-a", deployment_epoch=4, topology_version=8,
    )
    manifest = {"fixture": "local-only"}
    manifest_digest = "sha256:" + hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    route_identity = {
        **{field: spec.identity[field] for field in (
            "request_id", "request_attempt", "path_id", "path_attempt",
            "deployment_id", "deployment_epoch", "topology_generation")},
        "path_manifest_digest": manifest_digest,
    }
    command_identity = {**spec.identity, "path_digest": manifest_digest,
                        "absolute_deadline_ms": int(time.monotonic() * 1000) + 10_000}
    route._test_only_transport_fault = replace(
        spec, identity={**spec.identity, "path_digest": manifest_digest},
    )
    parsed = SimpleNamespace(
        ordered_hops=(SimpleNamespace(placement_id="placement-a"),),
        request_id="request-a", path_id="path-a", path_attempt=3,
    )
    monkeypatch.setattr(serialization, "path_manifest_from_dict", lambda _: parsed)
    monkeypatch.setattr(validation, "validate_manifest", lambda _manifest, _graph: None)
    route._deployment_qualification = SimpleNamespace(route_ready=True)
    monkeypatch.setattr(gateway_contracts, "qualification_digest", lambda _: DIGEST)
    monkeypatch.setattr(route, "_reject_if_affected_peer_quarantined", lambda _: None)
    monkeypatch.setattr(route, "_peer_counters", lambda: {})
    monkeypatch.setattr(route, "_verify_observation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(route, "_authorize_failed_request_cleanup", lambda *_args, **kwargs: time.monotonic() + 1)
    monkeypatch.setattr(route, "_wait_for_cancellation_cleanup", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(route, "_seal_and_publish_request_cleanup_receipt", lambda **_kwargs: {"sealed": True})
    subject = LivenessSubject("node-a", SubjectKind.PEER, 1)
    route._liveness.register_subject(subject, observed_at_ms=int(time.monotonic() * 1000))
    route._liveness_subjects["node-a"] = subject
    try:
        with pytest.raises(RuntimeError, match="^transport_not_running$"):
            route.infer(
                (1,), max_new_tokens=1, request_id="request-a",
                sink=SimpleNamespace(emit=lambda *_: None),
                route_identity=route_identity, locked_path_manifest=manifest,
                command_identity=command_identity,
                authorize_cleanup=lambda deadline: deadline,
                publish_cleanup_receipt=lambda _: None,
            )
        assert [call[1] for call in route._sessions["node-a"].calls] == ["bind_request_control"]
        incident, = route.a4_scoped_runtime_incidents()
        for field, value in route._test_only_transport_fault.identity.items():
            assert incident[field] == value
        assert incident["subject_id"] == "node-a"
        assert incident["scope"] == "request"
        assert incident["reason"] == "transport_not_running"
        assert incident["fatal_accepted"] is False
        assert route._incidents[-1]["state"] == "request_failed_closed"
        assert route._incidents[-1]["reason"] == "transport_not_running"
    finally:
        finish(route)
