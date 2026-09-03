"""Deterministic tests for A5 legal-track enumeration (planner extension).

Spec: docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md
§3, §4, §10 (planner tests: exact multi-stage ranges, legal-track
enumeration, missing/reversed edges, stable ties, finite bounded flow,
resource fit, no per-request splitting).
"""

from __future__ import annotations

import pytest

from mycelium_layer_planner.contracts import LayerRange, StagePlacement
from mycelium_layer_planner.flow import FlowEdge
from mycelium_layer_planner.replication import (
    TRACK_FRACTION_TOLERANCE,
    enumerate_legal_tracks,
)


def _placement(pid: str, node_id: str, start: int, end: int, *, primary: bool = True) -> StagePlacement:
    return StagePlacement(
        placement_id=pid,
        replica_group_id=f"group-{start:03}",
        node_id=node_id,
        layer_range=LayerRange(start, end),
        primary=primary,
        service_capacity_rps=10.0,
    )


def _two_group_fixture():
    """stage0 (layers 0-22) replicated on two placements, stage1 (23-24)."""
    placements = {
        "s0p": _placement("s0p", "node-0", 0, 22),
        "s0r": _placement("s0r", "node-3", 0, 22, primary=False),
        "s1p": _placement("s1p", "node-0", 23, 24),
    }
    groups = (("s0p", "s0r"), ("s1p",))
    forward = (
        FlowEdge("s0p", "s1p", 5.0, 1.0),
        FlowEdge("s0r", "s1p", 5.0, 1.0),
    )
    loopbacks = (
        FlowEdge("s1p", "s0p", 5.0, 1.0),
        FlowEdge("s1p", "s0r", 5.0, 1.0),
    )
    return placements, groups, forward, loopbacks


def test_enumerates_two_complete_legal_tracks():
    placements, groups, forward, loopbacks = _two_group_fixture()
    result = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    assert result.total_combinations == 2
    assert {track.placement_ids for track in result.tracks} == {
        ("s0p", "s1p"),
        ("s0r", "s1p"),
    }
    assert result.missing_edges == ()
    assert result.reversed_edges == ()


def test_track_ids_are_deterministic_and_distinct():
    placements, groups, forward, loopbacks = _two_group_fixture()
    first = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    second = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    ids = [track.track_id for track in first.tracks]
    assert ids == [track.track_id for track in second.tracks]
    assert len(set(ids)) == len(ids)


def test_tracks_cover_exact_multi_stage_ranges_once_in_order():
    placements, groups, forward, loopbacks = _two_group_fixture()
    result = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    for track in result.tracks:
        ranges = [placements[pid].layer_range for pid in track.placement_ids]
        assert len(ranges) == 2
        # exact coverage: contiguous, non-overlapping, full span 0..24
        assert ranges[0].start == 0 and ranges[0].end == 22
        assert ranges[1].start == 23 and ranges[1].end == 24
        # group order preserved: one placement per group
        group0_ids = set(groups[0])
        group1_ids = set(groups[1])
        assert track.placement_ids[0] in group0_ids
        assert track.placement_ids[1] in group1_ids


def test_missing_forward_edge_drops_track_and_reports_pair():
    placements, groups, forward, loopbacks = _two_group_fixture()
    forward = (FlowEdge("s0p", "s1p", 5.0, 1.0),)  # s0r -> s1p missing
    result = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    assert result.tracks and all(
        track.placement_ids == ("s0p", "s1p") for track in result.tracks
    )
    assert ("s0r", "s1p") in result.missing_edges


def test_reversed_edge_reported_when_reverse_exists():
    placements, groups, _, loopbacks = _two_group_fixture()
    # forward edge exists only in the reversed direction
    forward = (FlowEdge("s1p", "s0r", 5.0, 1.0),)
    result = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    assert ("s0r", "s1p") in result.missing_edges
    assert ("s0r", "s1p") in result.reversed_edges


def test_missing_loopback_drops_track():
    placements, groups, forward, _ = _two_group_fixture()
    loopbacks = (FlowEdge("s1p", "s0p", 5.0, 1.0),)  # s1p -> s0r missing
    result = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    assert result.tracks and all(
        track.placement_ids == ("s0p", "s1p") for track in result.tracks
    )
    assert ("s1p", "s0r") in result.missing_edges


def test_stable_tie_ordering_by_placement_tuple():
    placements, groups, forward, loopbacks = _two_group_fixture()
    result = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    ordered = [track.placement_ids for track in result.tracks]
    assert ordered == sorted(ordered)
    # equal costs: tie does not disturb the deterministic order
    costs = {track.cost_ms for track in result.tracks}
    assert len(costs) == 1
    again = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    assert [t.placement_ids for t in again.tracks] == ordered


def test_default_equal_split_fractions_sum_to_one():
    placements, groups, forward, loopbacks = _two_group_fixture()
    result = enumerate_legal_tracks(groups, placements, forward, loopbacks)
    total = sum(track.traffic_fraction for track in result.tracks)
    assert abs(total - 1.0) < TRACK_FRACTION_TOLERANCE
    assert all(track.traffic_fraction > 0 for track in result.tracks)


def test_pinned_traffic_fractions_applied():
    placements, groups, forward, loopbacks = _two_group_fixture()
    result = enumerate_legal_tracks(
        groups,
        placements,
        forward,
        loopbacks,
        traffic_fractions={("s0p", "s1p"): 0.6, ("s0r", "s1p"): 0.4},
    )
    by_path = {track.placement_ids: track.traffic_fraction for track in result.tracks}
    assert abs(by_path[("s0p", "s1p")] - 0.6) < 1e-9
    assert abs(by_path[("s0r", "s1p")] - 0.4) < 1e-9


def test_bad_fraction_sum_rejected():
    placements, groups, forward, loopbacks = _two_group_fixture()
    with pytest.raises(ValueError, match="sum to one"):
        enumerate_legal_tracks(
            groups,
            placements,
            forward,
            loopbacks,
            traffic_fractions={("s0p", "s1p"): 0.6, ("s0r", "s1p"): 0.6},
        )


def test_partial_fraction_mapping_rejected_before_track_construction():
    placements, groups, forward, loopbacks = _two_group_fixture()
    with pytest.raises(ValueError, match="cover every legal track"):
        enumerate_legal_tracks(
            groups,
            placements,
            forward,
            loopbacks,
            traffic_fractions={("s0p", "s1p"): 1.0},
        )


def test_fraction_for_non_legal_track_rejected():
    placements, groups, forward, loopbacks = _two_group_fixture()
    with pytest.raises(ValueError, match="non-legal"):
        enumerate_legal_tracks(
            groups,
            placements,
            forward,
            loopbacks,
            traffic_fractions={("s0p", "s1p"): 0.5, ("bogus", "s1p"): 0.5},
        )


def test_negative_fraction_rejected():
    placements, groups, forward, loopbacks = _two_group_fixture()
    with pytest.raises(ValueError, match="non-negative"):
        enumerate_legal_tracks(
            groups,
            placements,
            forward,
            loopbacks,
            traffic_fractions={("s0p", "s1p"): 1.2, ("s0r", "s1p"): -0.2},
        )


def test_empty_groups_rejected():
    placements, _, forward, loopbacks = _two_group_fixture()
    with pytest.raises(ValueError):
        enumerate_legal_tracks(((),), placements, forward, loopbacks)


def test_duplicate_placement_across_groups_rejected():
    placements, groups, forward, loopbacks = _two_group_fixture()
    with pytest.raises(ValueError):
        enumerate_legal_tracks((("s0p", "s0p"), ("s1p",)), placements, forward, loopbacks)


def test_single_group_track_needs_no_loopback():
    placements = {
        "g0p": _placement("g0p", "node-0", 0, 24),
    }
    result = enumerate_legal_tracks(
        (("g0p",),), placements, (), (),
    )
    assert [track.placement_ids for track in result.tracks] == [("g0p",)]
    assert result.missing_edges == ()
