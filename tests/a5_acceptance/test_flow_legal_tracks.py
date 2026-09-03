"""Deterministic tests for A5 flow allocation over legal tracks (flow extension).

Spec: docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md
§4 (flow solver: measured rates, complete tracks, bounded finite fractions
that sum to one, no per-request splitting) and §10 (finite bounded flow,
resource fit).
"""

from __future__ import annotations

import math

import pytest

from mycelium_layer_planner.contracts import LayerRange, StagePlacement
from mycelium_layer_planner.flow import FlowEdge, assign_flow_over_legal_tracks
from mycelium_layer_planner.replication import enumerate_legal_tracks


def _placement(pid: str, node_id: str, start: int, end: int, capacity: float = 10.0, *, primary: bool = True) -> StagePlacement:
    return StagePlacement(
        placement_id=pid,
        replica_group_id=f"group-{start:03}",
        node_id=node_id,
        layer_range=LayerRange(start, end),
        primary=primary,
        service_capacity_rps=capacity,
    )


def _fixture():
    placements = {
        "s0p": _placement("s0p", "node-0", 0, 22, capacity=10.0),
        "s0r": _placement("s0r", "node-3", 0, 22, capacity=10.0, primary=False),
        "s1p": _placement("s1p", "node-0", 23, 24, capacity=20.0),
    }
    groups = (("s0p", "s0r"), ("s1p",))
    forward = (
        FlowEdge("s0p", "s1p", 20.0, 1.0),
        FlowEdge("s0r", "s1p", 20.0, 1.0),
    )
    loopbacks = (
        FlowEdge("s1p", "s0p", 20.0, 1.0),
        FlowEdge("s1p", "s0r", 20.0, 1.0),
    )
    return placements, groups, forward, loopbacks


def _tracks():
    placements, groups, forward, loopbacks = _fixture()
    enumeration = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    return placements, groups, forward, loopbacks, [t.placement_ids for t in enumeration.tracks]


def test_finite_bounded_flow_all_tracks_admitted():
    placements, groups, forward, loopbacks, tracks = _tracks()
    result = assign_flow_over_legal_tracks(
        groups, placements, forward, loopbacks,
        tracks=tracks, fractions=[0.5, 0.5], demand=10.0,
    )
    assert math.isfinite(result.admitted)
    assert result.admitted == pytest.approx(10.0)
    assert result.unmet_demand == pytest.approx(0.0)
    assert len(result.tracks) == 2
    assert all(track.amount == pytest.approx(5.0) for track in result.tracks)


def test_no_per_request_splitting_each_track_orders_groups_once():
    placements, groups, forward, loopbacks, tracks = _tracks()
    result = assign_flow_over_legal_tracks(
        groups, placements, forward, loopbacks,
        tracks=tracks, fractions=[0.5, 0.5], demand=10.0,
    )
    for track in result.tracks:
        assert len(track.placement_ids) == 2
        assert track.placement_ids[0] in groups[0]
        assert track.placement_ids[1] in groups[1]
        assert len(set(track.placement_ids)) == 2


def test_resource_fit_unmet_when_demand_exceeds_capacity():
    placements, groups, forward, loopbacks, tracks = _tracks()
    # stage0 total capacity = 20; edges 20 each side; demand 30 -> unmet 10
    result = assign_flow_over_legal_tracks(
        groups, placements, forward, loopbacks,
        tracks=tracks, fractions=[0.5, 0.5], demand=30.0,
    )
    assert result.admitted == pytest.approx(20.0)
    assert result.unmet_demand == pytest.approx(10.0)
    # utilization of each stage0 placement saturated
    assert result.stage_utilization["s0p"] == pytest.approx(1.0)
    assert result.stage_utilization["s0r"] == pytest.approx(1.0)


def test_track_starved_by_capacity_gets_partial_amount():
    placements, groups, forward, loopbacks, tracks = _tracks()
    # track0 has only 2 units of placement capacity on s0p -> capped
    starved = dict(placements)
    starved["s0p"] = _placement("s0p", "node-0", 0, 22, capacity=2.0)
    result = assign_flow_over_legal_tracks(
        groups, starved, forward, loopbacks,
        tracks=tracks, fractions=[0.5, 0.5], demand=10.0,
    )
    amounts = {track.placement_ids: track.amount for track in result.tracks}
    assert amounts[("s0p", "s1p")] == pytest.approx(2.0)
    assert amounts[("s0r", "s1p")] == pytest.approx(5.0)
    assert result.unmet_demand == pytest.approx(3.0)


def test_fractions_must_sum_to_one():
    placements, groups, forward, loopbacks, tracks = _tracks()
    with pytest.raises(ValueError, match="sum to one"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=tracks, fractions=[0.6, 0.6], demand=10.0,
        )


def test_negative_demand_rejected():
    placements, groups, forward, loopbacks, tracks = _tracks()
    with pytest.raises(ValueError, match="demand"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=tracks, fractions=[0.5, 0.5], demand=-1.0,
        )


def test_nonfinite_demand_rejected():
    placements, groups, forward, loopbacks, tracks = _tracks()
    with pytest.raises(ValueError, match="demand"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=tracks, fractions=[0.5, 0.5], demand=math.inf,
        )


def test_nonfinite_fraction_rejected():
    placements, groups, forward, loopbacks, tracks = _tracks()
    with pytest.raises(ValueError, match="finite"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=tracks, fractions=[math.nan, 1.0], demand=10.0,
        )


def test_fraction_count_mismatch_rejected():
    placements, groups, forward, loopbacks, tracks = _tracks()
    with pytest.raises(ValueError, match="one fraction per track"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=tracks, fractions=[1.0], demand=10.0,
        )


def test_track_with_unqualified_edge_rejected():
    placements, groups, forward, loopbacks, tracks = _tracks()
    forward = (FlowEdge("s0p", "s1p", 20.0, 1.0),)  # s0r -> s1p unqualified
    with pytest.raises(ValueError, match="unqualified"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=tracks, fractions=[0.5, 0.5], demand=10.0,
        )


def test_track_with_unknown_placement_rejected():
    placements, groups, forward, loopbacks, _ = _tracks()
    with pytest.raises(ValueError, match="unknown placement"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=[("s0p", "bogus")], fractions=[1.0], demand=10.0,
        )


def test_track_missing_a_group_rejected():
    placements, groups, forward, loopbacks, _ = _tracks()
    with pytest.raises(ValueError, match="one placement per group"):
        assign_flow_over_legal_tracks(
            groups, placements, forward, loopbacks,
            tracks=[("s0p",)], fractions=[1.0], demand=10.0,
        )


def test_deterministic_repeat_runs_match():
    placements, groups, forward, loopbacks, tracks = _tracks()
    a = assign_flow_over_legal_tracks(
        groups, placements, forward, loopbacks,
        tracks=tracks, fractions=[0.3, 0.7], demand=13.0,
    )
    b = assign_flow_over_legal_tracks(
        groups, placements, forward, loopbacks,
        tracks=tracks, fractions=[0.3, 0.7], demand=13.0,
    )
    assert a.admitted == b.admitted
    assert [t.amount for t in a.tracks] == [t.amount for t in b.tracks]


def test_zero_demand_yields_zero_admitted():
    placements, groups, forward, loopbacks, tracks = _tracks()
    result = assign_flow_over_legal_tracks(
        groups, placements, forward, loopbacks,
        tracks=tracks, fractions=[0.5, 0.5], demand=0.0,
    )
    assert result.admitted == 0.0
    assert result.unmet_demand == 0.0


def test_single_group_flow_works_without_loopback():
    placements = {
        "g0p": _placement("g0p", "node-0", 0, 24, capacity=10.0),
    }
    groups = (("g0p",),)
    result = assign_flow_over_legal_tracks(
        groups, placements, (), (),
        tracks=[("g0p",)], fractions=[1.0], demand=7.0,
    )
    assert result.admitted == pytest.approx(7.0)
    assert result.tracks[0].placement_ids == ("g0p",)
