# SPDX-License-Identifier: AGPL-3.0-only
"""A16 live admission point: the node's KV reservation port honours the adaptive gate.

``SQLiteQualificationCapacityPort.reserve`` is where this host admits a new request's
whole-context KV.  With no gate (A16 off, the default) it must behave exactly as before.
"""
from __future__ import annotations

import sqlite3

import pytest

from mycelium_router.contracts import ReservationRequest
from mycelium_router.fakes import FakeTopologyProvider, ManualClock, SequenceIdSource
from physical_sqlite_capacity import SQLiteQualificationCapacityPort
from test_router_contracts import graph_fixture

CAP = 1_000_000


def _port(database, *, gate=None, clock=None, node="node-a", **extra):
    graph = graph_fixture()
    nodes = {p.node_id for s in graph.stages for p in s.placements}
    kwargs = {} if gate is None and not extra else {"adaptive_gate": gate, "adaptive_node_id": node, **extra}
    return SQLiteQualificationCapacityPort(
        database, FakeTopologyProvider(graph), {n: CAP for n in nodes},
        clock=clock or ManualClock(), id_source=SequenceIdSource(),
        maximum_imported_lease_seconds=35.0, **kwargs,
    )


def _placement(node_id):
    graph = graph_fixture()
    return next(p.placement_id for s in graph.stages for p in s.placements if p.node_id == node_id)


def _request(request_id, kv, node_id="node-a"):
    return ReservationRequest(
        request_id=request_id, path_id="path-" + request_id, path_attempt=0,
        placement_id=_placement(node_id), kv_bytes=kv, deployment_epoch=1,
        lease_expires_at=ManualClock().now() + 30.0,
    )


def _gate(**fields):
    base = {"admit": True, "refusal": None, "new_kv_budget_bytes": None, "epoch": 1.0}
    base.update(fields)
    return lambda: dict(base)


def test_opt_out_is_byte_identical_to_a_port_without_a_gate(tmp_path):
    plain = _port(tmp_path / "plain.sqlite3")
    absent = _port(tmp_path / "absent.sqlite3", gate=lambda: None, node="node-a")
    for port in (plain, absent):
        first = port.reserve(_request("r1", 400_000))
        over = port.reserve(_request("r2", 700_000))
        replay = port.reserve(_request("r1", 400_000))
        assert (first.accepted, over.accepted, over.reason, replay.accepted) == (
            True, False, "capacity_exceeded", True)
    a, b = plain.snapshot(), absent.snapshot()
    assert a.node_capacity_kv_bytes == b.node_capacity_kv_bytes
    assert a.node_reserved_kv_bytes == b.node_reserved_kv_bytes
    assert a.node_available_kv_bytes == b.node_available_kv_bytes
    assert [r.kv_bytes for r in a.reservations.values()] == [r.kv_bytes for r in b.reservations.values()]


def test_gate_refusal_blocks_new_reservation_but_not_idempotent_replay(tmp_path):
    state = {"admit": True}
    port = _port(tmp_path / "c.sqlite3", gate=lambda: {
        "admit": state["admit"], "refusal": None if state["admit"] else "adaptive_host_draining",
        "new_kv_budget_bytes": None, "epoch": 1.0})
    first = port.reserve(_request("r1", 100_000))
    assert first.accepted
    state["admit"] = False
    refused = port.reserve(_request("r2", 100_000))
    assert (refused.accepted, refused.reason) == (False, "adaptive_host_draining")
    replay = port.reserve(_request("r1", 100_000))  # committed/in-flight work is never revoked
    assert replay.accepted and replay.reservation_id == first.reservation_id
    snap = port.snapshot()
    assert snap.node_reserved_kv_bytes["node-a"] == 100_000
    assert snap.node_available_kv_bytes["node-a"] == 0  # published capacity is zero while fenced
    assert snap.node_capacity_kv_bytes["node-a"] == CAP  # the qualified capacity is never rewritten


def test_budget_refuses_whole_context_and_accumulates_per_epoch(tmp_path):
    epoch = {"v": 1.0}
    port = _port(tmp_path / "c.sqlite3", gate=lambda: {
        "admit": True, "refusal": None, "new_kv_budget_bytes": 300_000, "epoch": epoch["v"]})
    too_big = port.reserve(_request("big", 400_000))
    assert (too_big.accepted, too_big.reason) == (False, "adaptive_context_exceeds_budget")
    assert port.reserve(_request("a", 200_000)).accepted
    second = port.reserve(_request("b", 200_000))
    assert (second.accepted, second.reason) == (False, "adaptive_capacity_reduced")
    assert port.snapshot().node_available_kv_bytes["node-a"] == 100_000  # min(static, budget - spent)
    epoch["v"] = 2.0  # a fresh observation re-measures headroom
    assert port.reserve(_request("c", 200_000)).accepted
    # the earlier reservation is untouched
    assert port.snapshot().node_reserved_kv_bytes["node-a"] == 400_000


def test_gate_never_raises_capacity_above_the_static_limit(tmp_path):
    port = _port(tmp_path / "c.sqlite3", gate=_gate(new_kv_budget_bytes=10 * CAP))
    assert port.snapshot().node_available_kv_bytes["node-a"] == CAP
    over = port.reserve(_request("x", CAP + 1))
    assert not over.accepted


def test_gate_only_applies_to_the_local_node(tmp_path):
    port = _port(tmp_path / "c.sqlite3", gate=_gate(admit=False, refusal="adaptive_host_withdrawn"), node="node-a")
    assert not port.reserve(_request("a", 10)).accepted
    assert port.reserve(_request("b", 10, node_id="node-c")).accepted  # another host's placement
    assert port.snapshot().node_available_kv_bytes["node-c"] == CAP - 10


def test_gate_failure_fails_closed(tmp_path):
    def broken():
        raise RuntimeError("boom")
    port = _port(tmp_path / "c.sqlite3", gate=broken)
    refused = port.reserve(_request("a", 10))
    assert (refused.accepted, refused.reason) == (False, "adaptive_controller_stale")
    with sqlite3.connect(tmp_path / "c.sqlite3") as db:
        assert db.execute("select count(*) from reservations").fetchone()[0] == 0
