from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

from mycelium_router.fakes import (
    FakeTopologyProvider,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.routing import ProgressivePathBuilder, RoutePolicy, RoutingError
from mycelium_router.scoring import RouteScorer
from mycelium_router.contracts import RouterConfig
from physical_sqlite_capacity import SQLiteQualificationCapacityPort
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


def _capacity(database, *, clock, capacities=None):
    graph = graph_fixture()
    nodes = {
        placement.node_id
        for stage in graph.stages
        for placement in stage.placements
    }
    return SQLiteQualificationCapacityPort(
        database,
        FakeTopologyProvider(graph),
        (
            {node_id: 1_000_000 for node_id in nodes}
            if capacities is None
            else capacities
        ),
        clock=clock,
        id_source=SequenceIdSource(),
        maximum_imported_lease_seconds=35.0,
    )


def _builder(capacity):
    return ProgressivePathBuilder(
        policy=RoutePolicy(
            RouteScorer(RouterConfig(reservation_lease_seconds=10.0))
        ),
        capacity=capacity,
        id_source=SequenceIdSource(),
    )


def test_cross_host_lock_synchronizes_path_carried_reservations(tmp_path) -> None:
    clock = ManualClock()
    source_capacity = _capacity(tmp_path / "source.sqlite3", clock=clock)
    remote_capacity = _capacity(tmp_path / "remote.sqlite3", clock=clock)
    source_builder = _builder(source_capacity)
    remote_builder = _builder(remote_capacity)
    build = source_builder.start(
        request_fixture(),
        graph_fixture(),
        path_attempt=0,
    )
    while not source_builder.is_complete(build):
        build = source_builder.advance(build, state_table(), now=clock.now())

    manifest = remote_builder.lock(build, now=clock.now())
    replayed = remote_builder.lock(build, now=clock.now())

    assert manifest.ordered_hops == build.ordered_hops
    assert replayed == manifest
    snapshot = remote_capacity.snapshot()
    assert set(snapshot.reservations) == {
        hop.reservation_id for hop in build.ordered_hops
    }
    assert {record.status for record in snapshot.reservations.values()} == {"COMMITTED"}


def test_cross_host_synchronized_reservations_release_idempotently(tmp_path) -> None:
    clock = ManualClock()
    source_capacity = _capacity(tmp_path / "source.sqlite3", clock=clock)
    remote_capacity = _capacity(tmp_path / "remote.sqlite3", clock=clock)
    source_builder = _builder(source_capacity)
    remote_builder = _builder(remote_capacity)
    build = source_builder.start(
        request_fixture(request_id="remote-release"),
        graph_fixture(),
        path_attempt=0,
    )
    while not source_builder.is_complete(build):
        build = source_builder.advance(build, state_table(), now=clock.now())
    remote_builder.lock(build, now=clock.now())
    reservation_ids = tuple(hop.reservation_id for hop in build.ordered_hops)

    remote_capacity.release_synchronized_build(reservation_ids)
    remote_capacity.release_synchronized_build(reservation_ids)

    snapshot = remote_capacity.snapshot()
    assert all(
        snapshot.node_reserved_kv_bytes[node_id] == 0
        for node_id in snapshot.node_reserved_kv_bytes
    )
    assert {
        snapshot.reservations[reservation_id].status
        for reservation_id in reservation_ids
    } == {"RELEASED"}


def test_repeated_snapshots_close_every_sqlite_connection(
    tmp_path,
    monkeypatch,
) -> None:
    clock = ManualClock()
    capacity = _capacity(tmp_path / "capacity.sqlite3", clock=clock)
    real_connect = sqlite3.connect
    opened = []

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    for _ in range(32):
        capacity.snapshot()

    assert len(opened) == 32
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_cross_host_sync_rejects_oversized_lease_without_import(tmp_path) -> None:
    clock = ManualClock()
    source_capacity = _capacity(tmp_path / "source.sqlite3", clock=clock)
    remote_capacity = _capacity(tmp_path / "remote.sqlite3", clock=clock)
    source_builder = _builder(source_capacity)
    remote_builder = _builder(remote_capacity)
    build = source_builder.start(request_fixture(), graph_fixture(), path_attempt=0)
    while not source_builder.is_complete(build):
        build = source_builder.advance(build, state_table(), now=clock.now())
    oversized = replace(
        build,
        ordered_hops=(
            replace(build.ordered_hops[0], reservation_expires_at=60.0),
            *build.ordered_hops[1:],
        ),
    )

    with pytest.raises(RoutingError) as caught:
        remote_builder.lock(oversized, now=clock.now())

    assert caught.value.code == "path_reservation_sync_rejected"
    assert caught.value.detail == "lease_duration_exceeded"
    assert not remote_capacity.snapshot().reservations


def test_cross_host_sync_rolls_back_partial_capacity_import(tmp_path) -> None:
    graph = graph_fixture()
    clock = ManualClock()
    source_capacity = _capacity(tmp_path / "source.sqlite3", clock=clock)
    source_builder = _builder(source_capacity)
    build = source_builder.start(request_fixture(), graph, path_attempt=0)
    while not source_builder.is_complete(build):
        build = source_builder.advance(build, state_table(), now=clock.now())
    capacities = {
        placement.node_id: 1_000_000
        for stage in graph.stages
        for placement in stage.placements
    }
    second_placement_id = build.ordered_hops[1].placement_id
    second_node = next(
        placement.node_id
        for placement in graph.stages[1].placements
        if placement.placement_id == second_placement_id
    )
    capacities[second_node] = 0
    remote_capacity = _capacity(
        tmp_path / "remote.sqlite3",
        clock=clock,
        capacities=capacities,
    )
    remote_builder = _builder(remote_capacity)

    with pytest.raises(RoutingError) as caught:
        remote_builder.lock(build, now=clock.now())

    assert caught.value.code == "path_reservation_sync_rejected"
    assert caught.value.detail == "capacity_exceeded"
    assert not remote_capacity.snapshot().reservations
