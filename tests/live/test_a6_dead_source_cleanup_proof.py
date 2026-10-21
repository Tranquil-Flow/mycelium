# SPDX-License-Identifier: AGPL-3.0-only
"""A6 recovery when the killed source peer can never return a cleanup receipt.

``PhysicalLiveRoute`` seals a request cleanup receipt only after every
participating node returned an exact, request-scoped clean receipt.  A source
peer that was SIGKILLed returns nothing, so the route records
``request_cleanup_unproven`` and the router cannot record the source command's
terminal.  The router then has to accept the route's own observation that the
peer process exited (identity-bound ``node_process_exited`` incident and a
nonzero ``returncode``) as that peer's cleanup proof, while every surviving
participant still has to prove exact cleanup itself.  Without that, recovery
is unreachable for a killed source and the request strands in cleanup.
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from mycelium_live.router_port import LiveRouterPort
from mycelium_live.supervisor import ColdRouteRestartOwner
from tests.live.test_a6_cold_restart_owner_recovery import _KilledSourceRoute, _owner
from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute,
    _runtime,
    _submit_and_drain,
)

_IDENTITY_FIELDS = (
    "deployment_id", "deployment_epoch", "qualification_digest", "request_id",
    "request_attempt", "path_id", "path_attempt", "path_digest",
    "topology_generation", "command_id", "publisher_generation", "cancellation_generation",
)


class _Peer:
    def __init__(self) -> None:
        self.returncode = None


class _PhysicalCleanupMixin:
    """The physical route's cleanup-proof surface, as PhysicalLiveRoute keeps it.

    Receipts exist only for attempts whose every participant proved cleanup;
    the source attempt's dead peer never does.
    """

    is_simulated = False
    source_node = "node-source"
    survivor_node = "node-survivor"
    survivor_clean = True
    source_exits = True

    def _init_physical(self) -> None:
        self._sessions = {self.source_node: _Peer(), self.survivor_node: _Peer()}
        self._active_route_requests: dict[str, dict] = {}
        self._cleanup_snapshots_by_request: dict[str, dict] = {}
        self._request_cleanup_receipts: dict[str, dict] = {}
        self.scoped_releases: list[dict] = []

    def _begin(self, request_id, command_identity, cleanup_owner_id) -> dict:
        subject = {"request_id": request_id, **{
            field: command_identity[field] for field in _IDENTITY_FIELDS if field != "request_id"
        }, "absolute_deadline_ms": command_identity["absolute_deadline_ms"]}
        self._active_route_requests[request_id] = {
            **{field: command_identity[field] for field in _IDENTITY_FIELDS if field != "request_id"},
            "participating_node_ids": frozenset((self.source_node, self.survivor_node)),
            "cancellation_cleanup_subject": subject,
            "cleanup_owner_id": cleanup_owner_id,
        }
        return subject

    def _kill_source(self, request_id, command_identity, cleanup_owner_id,
                     authorize_cleanup=None) -> None:
        self._begin(request_id, command_identity, cleanup_owner_id)
        if self.source_exits:
            self._sessions[self.source_node].returncode = -9
        self._scoped_runtime_incidents.append({
            "protocol": "mycelium.scoped_runtime_incident.v1",
            "incident_id": f"scoped-runtime-{len(self._scoped_runtime_incidents) + 1}",
            **{field: command_identity[field] for field in _IDENTITY_FIELDS},
            "cleanup_owner_id": cleanup_owner_id,
            "subject_id": self.source_node,
            "scope": "request",
            "reason": "node_process_exited" if self.source_exits else "transport_not_running",
            "fatal_requested": False,
            "fatal_accepted": False,
        })
        # As PhysicalLiveRoute.cancel_request does after the failure: the
        # cleanup subject is frozen at the next cancellation generation, and
        # the gateway command is joined to that generation; the incident above
        # keeps the generation it was observed at.
        active = self._active_route_requests[request_id]
        active["cancellation_generation"] = command_identity["cancellation_generation"] + 1
        subject = {**active["cancellation_cleanup_subject"],
                   "cancellation_generation": active["cancellation_generation"]}
        active["cancellation_cleanup_subject"] = subject
        if authorize_cleanup is not None:
            authorize_cleanup(time.monotonic() + 5.0)
        # The survivor answers the cleanup fanout; the dead source cannot.
        receipt = {**subject, "complete": self.survivor_clean}
        self._cleanup_snapshots_by_request[request_id] = {
            self.survivor_node: {"details": {"request_cleanup": receipt}},
        }

    # -- PhysicalLiveRoute's own cleanup-proof methods (same semantics) -------

    def a4_scoped_runtime_incidents(self):
        return tuple(dict(item) for item in self._scoped_runtime_incidents)

    def _request_cleanup_subject(self, request_id):
        active = self._active_route_requests.get(request_id)
        if active is None:
            raise RuntimeError("request_control_identity_unavailable")
        return dict(active["cancellation_cleanup_subject"])

    def _cancellation_cleanup_complete(self, participating_node_ids=None, *, cleanup_subject=None):
        snapshots = self._cleanup_snapshots_by_request.get(cleanup_subject["request_id"], {})
        required = frozenset(participating_node_ids or ())
        if not required or not required <= set(snapshots):
            return False
        for node_id in required:
            receipt = snapshots[node_id]["details"].get("request_cleanup")
            if (
                not isinstance(receipt, dict)
                or any(receipt.get(f) != v for f, v in cleanup_subject.items())
                or receipt.get("complete") is not True
            ):
                return False
        return True

    def _store_request_cleanup_receipt(self, *, request_id, cleanup_subject,
                                       participating_node_ids, deployment_id):
        existing = self._request_cleanup_receipts.get(request_id)
        if existing is not None:
            if any(existing.get(f) != v for f, v in cleanup_subject.items()):
                raise RuntimeError("route_cleanup_receipt_identity_mismatch")
            return dict(existing)
        receipt = {
            **cleanup_subject,
            "cleanup_owner_id": f"physical-live-route:{deployment_id}",
            "node_ids": sorted(participating_node_ids),
            "completed_at_monotonic_ms": int(time.monotonic() * 1_000),
        }
        receipt["receipt_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._request_cleanup_receipts[request_id] = receipt
        self._cleanup_snapshots_by_request.pop(request_id, None)
        return dict(receipt)

    def request_cleanup_receipt_scoped(self, request_id, *, request_attempt, path_id,
                                       path_attempt, path_digest, cleanup_owner_id):
        receipt = self._request_cleanup_receipts.get(request_id)
        expected = {"request_id": request_id, "request_attempt": request_attempt,
                    "path_id": path_id, "path_attempt": path_attempt,
                    "path_digest": path_digest, "cleanup_owner_id": cleanup_owner_id}
        if receipt is None or any(receipt.get(f) != v for f, v in expected.items()):
            return None
        return dict(receipt)

    def release_request_scoped(self, request_id, **identity) -> None:
        if self.request_cleanup_receipt_scoped(request_id, **identity) is None:
            raise RuntimeError("request_scoped_release_cleanup_unproven")
        self.scoped_releases.append({"request_id": request_id, **identity})
        self._active_route_requests.pop(request_id, None)
        self._request_cleanup_receipts.pop(request_id, None)

    def _successor_completed(self, request_id, command_identity, cleanup_owner_id) -> None:
        # The successor ran only on survivors and proved its own cleanup.
        subject = self._begin(request_id, command_identity, cleanup_owner_id)
        self._active_route_requests[request_id]["participating_node_ids"] = frozenset(
            (self.survivor_node,)
        )
        self._request_cleanup_receipts.pop(request_id, None)
        self._cleanup_snapshots_by_request[request_id] = {
            self.survivor_node: {"details": {"request_cleanup": {**subject, "complete": True}}},
        }
        self._store_request_cleanup_receipt(
            request_id=request_id, cleanup_subject=subject,
            participating_node_ids=frozenset((self.survivor_node,)),
            deployment_id=command_identity["deployment_id"],
        )


class _DeadSourcePhysicalRoute(_PhysicalCleanupMixin, FailingThenRecoveringRoute):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._init_physical()

    def infer(self, *args, request_id, command_identity, **kwargs):
        owner = f"physical-live-route:{command_identity['deployment_id']}"
        source_attempt = not self.calls
        authorize_cleanup = kwargs.get("authorize_cleanup")
        try:
            return super().infer(
                *args, request_id=request_id, command_identity=command_identity, **kwargs,
            )
        except RuntimeError:
            if not source_attempt:
                raise
            self._scoped_runtime_incidents.pop()  # replaced by the physical incident
            self._kill_source(request_id, command_identity, owner, authorize_cleanup)
            raise RuntimeError("request_terminal_blocked_cleanup_unproven") from None
        finally:
            if not source_attempt:
                self._successor_completed(request_id, command_identity, owner)


def _terminals(events):
    return [event.kind for event in events if event.kind in {"completed", "failed", "cancelled"}]


def test_a_killed_source_with_no_cleanup_receipt_still_recovers(tmp_path) -> None:
    route = _DeadSourcePhysicalRoute()
    _coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert _terminals(events) == ["completed"], (
            router.recovery_failure_reason(request_id),
            router._pending.get(request_id),
        )
        assert [event.text for event in events if event.kind == "token"] == [
            "<101>", "<102>", "<103>", "<104>",
        ]
        assert len(route.calls) == 2
        # The source's cleanup proof was sealed by the route itself, so its
        # scoped state is no longer held for an attempt that cannot return.
        assert request_id not in route._cleanup_snapshots_by_request
    finally:
        service.close()


def test_the_source_failure_is_observed_for_the_a6_fault_control(tmp_path) -> None:
    route = _DeadSourcePhysicalRoute(block_successor=True)
    _coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding

        request_id = service.submit(InferenceSubmission(
            prompt="live recovery prompt", max_new_tokens=4,
            qualification=qualification_binding(qualification),
        ))
        assert route.successor_started.wait(timeout=3), router._pending.get(request_id)
        view = router.a6_request_view(request_id)
        assert view["source_failure_observed"] is True, view
        assert view["terminal_blocked_reason"] is None, view
        route.cancelled.set()
    finally:
        service.close()


@pytest.mark.parametrize(
    ("source_exits", "survivor_clean"),
    [(False, True), (True, False)],
    ids=["peer_alive_but_disconnected", "survivor_cleanup_unproven"],
)
def test_without_exact_proof_the_source_stays_terminal_blocked(
    tmp_path, source_exits, survivor_clean,
) -> None:
    route = _DeadSourcePhysicalRoute()
    route.source_exits = source_exits
    route.survivor_clean = survivor_clean
    _coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert "completed" not in _terminals(events)
        assert len(route.calls) == 1, "no successor without exact source cleanup"
        assert router.a6_request_view(request_id)["source_failure_observed"] is False
        assert route._request_cleanup_receipts.get(request_id) is None
    finally:
        service.close()


class _OwnedDeadSourceRoute(_PhysicalCleanupMixin, _KilledSourceRoute):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._init_physical()

    def infer(self, *args, request_id, command_identity, **kwargs):
        owner = f"physical-live-route:{command_identity['deployment_id']}"
        source_attempt = not self.calls
        authorize_cleanup = kwargs.get("authorize_cleanup")
        try:
            return FailingThenRecoveringRoute.infer(
                self, *args, request_id=request_id, command_identity=command_identity, **kwargs,
            )
        except RuntimeError:
            if not source_attempt:
                raise
            self._scoped_runtime_incidents.pop()
            self._kill_source(request_id, command_identity, owner, authorize_cleanup)
            self.source_killed = True
            raise RuntimeError("request_terminal_blocked_cleanup_unproven") from None
        finally:
            if not source_attempt:
                self._successor_completed(request_id, command_identity, owner)


def test_the_owner_replaces_the_degraded_route_so_later_requests_are_served(tmp_path) -> None:
    route = _OwnedDeadSourceRoute()
    spare = _OwnedDeadSourceRoute()
    _c, _r, _s, qualification = _runtime(tmp_path, FailingThenRecoveringRoute())
    owner = _owner([route, spare], qualification)
    owner.open()
    _coordinator, router, service, qualification = _runtime(
        tmp_path, owner, qualification=qualification, port_qualification_source=owner,
    )
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert _terminals(events) == ["completed"], router.recovery_failure_reason(request_id)
        assert route.scoped_releases, "the recovered request was never released"
        deadline = time.monotonic() + 3.0
        while owner.public_status()["cold_route_restart"]["generation"] != 2:
            assert time.monotonic() < deadline, owner.public_status()["cold_route_restart"]
            time.sleep(0.01)
        assert spare.open_count == 1 and owner.is_alive() is True
    finally:
        service.close()


def test_the_dead_peer_proof_reads_the_route_through_the_restart_owner() -> None:
    route = _OwnedDeadSourceRoute()
    owner = _owner([route], "qualification")
    owner.open()
    port = type("_Port", (), {"_route": owner})()
    identity = {
        "deployment_id": "deployment", "deployment_epoch": 1, "qualification_digest": "sha256:" + "a" * 64,
        "request_id": "r", "request_attempt": 1, "path_id": "p", "path_attempt": 0,
        "path_digest": "sha256:" + "b" * 64, "topology_generation": 1, "command_id": "request:r:attempt:1",
        "publisher_generation": 1, "cancellation_generation": 0, "absolute_deadline_ms": 10**12,
    }
    route._kill_source("r", identity, "physical-live-route:deployment")
    from mycelium_live.command_controller import CommandIdentity

    # The failure joined the gateway command to the route's cancelled generation.
    command = CommandIdentity(**{**identity, "cancellation_generation": 1})
    receipt = LiveRouterPort._dead_peer_cleanup_receipt(
        port, command, "physical-live-route:deployment",
    )
    assert receipt is not None and receipt["node_ids"] == ["node-source", "node-survivor"]
    assert route.request_cleanup_receipt_scoped(
        "r", request_attempt=1, path_id="p", path_attempt=0, path_digest="sha256:" + "b" * 64,
        cleanup_owner_id="physical-live-route:deployment",
    ) == receipt
    # A foreign owner or a stale attempt never borrows the exit as proof.
    assert LiveRouterPort._dead_peer_cleanup_receipt(port, command, "physical-live-route:other") is None
    assert ColdRouteRestartOwner  # the owner is the production wrapper under test
