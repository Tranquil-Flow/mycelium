# SPDX-License-Identifier: AGPL-3.0-only
"""A fatal data transport is not proof of request-scoped resource cleanup."""
import threading
import time

import pytest

from mycelium_live.route import PhysicalLiveRoute


def _route():
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": object()}
    route._last_health = {"node-a": {"details": {
        "transport_fatal_error": {"code": "sidecar_closed"},
        "runtime": {"active_state_count": 1},
    }}}
    route._last_snapshots = dict(route._last_health)
    route._request_cleanup_receipts = {}
    route._cleanup_snapshots_by_request = {}
    route._active_route_requests = {"request-a": {
        "request_attempt": 2, "path_id": "path-a", "path_attempt": 3,
        "path_digest": "sha256:" + "a" * 64,
        "deployment_id": "deployment-a", "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "b" * 64,
        "topology_generation": 7, "command_id": "command-a",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1000) + 60_000,
        "entry_node_id": "node-a", "participating_node_ids": frozenset({"node-a"}),
        "cancellation_generation": 0, "cancellation_requested": False,
        "cancellation_started_at": None, "cancellation_deadline": None, "terminal": False,
    }}
    route._command_id = lambda node, action: f"{node}:{action}"
    # These unit tests exercise receipt-policy, not signature cryptography.
    route._verify_observation = lambda node, response, **kw: response
    route._ingest_transport_scoped_events = lambda *args: None
    return route


@pytest.mark.parametrize("receipt", [None, {"request_id": "other", "complete": True},
                                    {"request_id": "request-a", "complete": False}])
def test_fatal_health_cannot_replace_exact_cleanup_receipt(receipt):
    route = _route()
    observation = {"details": {**route._last_health["node-a"]["details"]}}
    if receipt is not None:
        observation["details"]["request_cleanup"] = receipt
    route._cleanup_snapshots_by_request = {"request-a": {"node-a": observation}}
    assert not route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject={"request_id": "request-a"})


def test_fatal_health_cannot_replace_unscoped_cleanup_state():
    route = _route()
    assert not route._cancellation_cleanup_complete(frozenset({"node-a"}))


def test_fatal_transport_does_not_preclude_a_real_exact_receipt():
    route = _route()
    route._cleanup_snapshots_by_request = {"request-a": {"node-a": {"details": {
        **route._last_health["node-a"]["details"],
        "request_cleanup": {"request_id": "request-a", "complete": True},
    }}}}
    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject={"request_id": "request-a"})


def test_fatal_data_transport_still_queries_control_snapshot():
    route = _route()
    calls = []
    def send(node_id, **kwargs):
        calls.append((node_id, kwargs))
        return {"details": {"request_cleanup": {
            **kwargs["payload"]["cleanup_subject"], "complete": True}}}
    route._send_before = send
    route._snapshot_nodes_before(frozenset({"node-a"}),
        deadline_monotonic_s=time.monotonic()+1,
        cleanup_subject={"request_id": "request-a"}, receipt_only=True)
    assert len(calls) == 1
    assert calls[0][1]["command"] == "snapshot"
    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject={"request_id": "request-a"})


def test_fatal_data_transport_still_fans_out_control_cancellation():
    route = _route()
    calls = []
    def send(node_id, **kwargs):
        calls.append((node_id, kwargs))
        return {"details": {"request_cleanup": {
            **{k: v for k, v in kwargs["payload"].items() if k != "deadline_budget_ms"},
            "complete": True}}}
    route._send_before = send
    assert route.cancel_request("request-a", deadline_monotonic_s=time.monotonic()+1)
    assert route._active_route_requests["request-a"]["cancellation_fanout_complete"].wait(1)
    assert len(calls) == 1
    assert calls[0][1]["command"] == "infer_cancel_wait"
    assert route._cancellation_cleanup_complete(frozenset({"node-a"}),
        cleanup_subject=route._request_cleanup_subject("request-a"))
