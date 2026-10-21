# SPDX-License-Identifier: AGPL-3.0-only
"""A14b region reduction, projection and product binding; specification sections 5-7.

RED at `b90edaf`: `mycelium_product_spine.coarse_region` does not exist,
`mycelium.route_explorer.v1` freezes node `region` to `unknown` and `region_source`
to `unavailable`, and the projector has no coarse-region evidence input. These cases
freeze the target behaviour; they are not a claim that anything works.
"""
from __future__ import annotations

import copy
import json

import pytest

from tests.a14b_acceptance import conformance

NOW = 1000000


def reduce_rows(value, evidence, *, now_unix_ms=NOW, authorized=None):
    coarse = conformance.contract()
    return coarse.project_coarse_regions(
        conformance.nodes(value),
        evidence,
        now_unix_ms=now_unix_ms,
        authorized_authorities=(
            conformance.accepted(*evidence) if authorized is None else authorized
        ),
    )


def by_node(rows):
    return {row["node"]: row for row in rows}


def test_reduction_labels_a_declared_member_and_the_relay_region_separately():
    value = conformance.snapshot()
    member, other = conformance.members(value)
    endpoint = conformance.node_of_kind(value, "endpoint")
    evidence = [
        conformance.region_evidence(member["id"], "member", "owner_declared", "europe"),
        conformance.region_evidence(
            endpoint["id"], "relay", "reviewed_relay_metadata", "asia"
        ),
    ]
    result = reduce_rows(value, evidence)
    assert len(result) == len(conformance.nodes(value))
    assert [row["node"] for row in result] == sorted(row["node"] for row in result)
    assert all(set(row) == conformance.ROW_FIELDS for row in result)
    assert by_node(result)[member["id"]] == {
        "node": member["id"],
        "region": "europe",
        "region_source": "owner_declared",
        "region_blocker": None,
        "distance_class": None,
    }
    assert by_node(result)[endpoint["id"]] == {
        "node": endpoint["id"],
        "region": "asia",
        "region_source": "reviewed_relay_metadata",
        "region_blocker": None,
        "distance_class": None,
    }
    assert by_node(result)[other["id"]] == {
        "node": other["id"],
        "region": "unknown",
        "region_source": "unavailable",
        "region_blocker": "missing",
        "distance_class": None,
    }


def test_relay_evidence_never_labels_a_member_and_member_evidence_never_labels_a_relay():
    value = conformance.snapshot()
    member, _ = conformance.members(value)
    endpoint = conformance.node_of_kind(value, "endpoint")
    evidence = [
        conformance.region_evidence(
            member["id"], "relay", "reviewed_relay_metadata", "europe"
        ),
        conformance.region_evidence(endpoint["id"], "member", "owner_declared", "africa"),
    ]
    result = by_node(reduce_rows(value, evidence))
    assert result[member["id"]]["region"] == "unknown"
    assert result[member["id"]]["region_blocker"] == "missing"
    assert result[endpoint["id"]]["region"] == "unknown"
    assert result[endpoint["id"]]["region_blocker"] == "missing"


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "unauthorized",
        "expired",
        "conflicting",
        "geography_beats_class",
        "expired_geography_blocks_class",
        "expired_class",
    ],
)
def test_unknown_blockers_follow_the_frozen_precedence(case):
    value = conformance.snapshot()
    member, _ = conformance.members(value)
    current = conformance.region_evidence(member["id"], "member", "owner_declared", "europe")
    expired = conformance.region_evidence(
        member["id"],
        "member",
        "owner_declared",
        "europe",
        expires_at_unix_ms=NOW - 1,
    )
    if case == "missing":
        evidence, authorized, blocker = [], None, "missing"
    elif case == "unauthorized":
        evidence = [current]
        authorized = frozenset(["sha256:" + "b" * 64])
        blocker = "unauthorized"
    elif case == "expired":
        evidence, authorized, blocker = [expired], None, "expired"
    elif case == "conflicting":
        other = conformance.region_evidence(member["id"], "member", "owner_declared", "asia")
        evidence, authorized, blocker = [current, other], None, "conflicting"
    elif case == "geography_beats_class":
        evidence = [current, conformance.latency_evidence(member["id"], "member", "far")]
        authorized, blocker = None, None
    elif case == "expired_class":
        evidence = [
            conformance.latency_evidence(
                member["id"], "member", "far", expires_at_unix_ms=NOW - 1
            )
        ]
        authorized, blocker = None, "expired"
    else:
        evidence = [expired, conformance.latency_evidence(member["id"], "member", "far")]
        authorized, blocker = None, "expired"
    row = by_node(reduce_rows(value, evidence, authorized=authorized))[member["id"]]
    assert row["region_blocker"] == blocker
    if blocker is None:
        assert row["region"] == "europe"
        assert row["region_source"] == "owner_declared"
    else:
        assert row["region"] == "unknown"
        assert row["region_source"] == "unavailable"


def test_an_explicit_unknown_statement_stays_unknown_with_its_source_visible():
    value = conformance.snapshot()
    member, _ = conformance.members(value)
    evidence = [
        conformance.region_evidence(member["id"], "member", "owner_declared", "unknown")
    ]
    row = by_node(reduce_rows(value, evidence))[member["id"]]
    assert row == {
        "node": member["id"],
        "region": "unknown",
        "region_source": "owner_declared",
        "region_blocker": None,
        "distance_class": None,
    }
    assert row["region"] not in conformance.DISPLAY_CELLS


def test_a_latency_class_is_off_globe_and_never_geography():
    value = conformance.snapshot()
    member, _ = conformance.members(value)
    evidence = [conformance.latency_evidence(member["id"], "member", "far")]
    row = by_node(reduce_rows(value, evidence))[member["id"]]
    assert row == {
        "node": member["id"],
        "region": "unknown",
        "region_source": "latency_distance_class",
        "region_blocker": None,
        "distance_class": "far",
    }
    assert row["region"] in conformance.REGIONS
    assert row["region"] not in conformance.DISPLAY_CELLS


def test_conflicting_latency_classes_reduce_to_conflicting():
    value = conformance.snapshot()
    member, _ = conformance.members(value)
    evidence = [
        conformance.latency_evidence(member["id"], "member", "far"),
        conformance.latency_evidence(member["id"], "member", "near"),
    ]
    row = by_node(reduce_rows(value, evidence))[member["id"]]
    assert (row["region"], row["region_blocker"]) == ("unknown", "conflicting")
    assert row["region_source"] == "unavailable"


def test_an_agreeing_declaration_and_reviewed_relay_metadata_report_the_declaration():
    value = conformance.snapshot()
    endpoint = conformance.node_of_kind(value, "endpoint")
    evidence = [
        conformance.region_evidence(endpoint["id"], "relay", "owner_declared", "asia"),
        conformance.region_evidence(
            endpoint["id"], "relay", "reviewed_relay_metadata", "asia"
        ),
    ]
    row = by_node(reduce_rows(value, evidence))[endpoint["id"]]
    assert (row["region"], row["region_source"]) == ("asia", "owner_declared")


def test_reduction_is_pure_deterministic_bounded_and_vocabulary_only():
    value = conformance.snapshot()
    member, _ = conformance.members(value)
    evidence = [conformance.region_evidence(member["id"], "member", "owner_declared", "europe")]
    nodes_before = copy.deepcopy(conformance.nodes(value))
    evidence_before = copy.deepcopy(evidence)
    first = reduce_rows(value, evidence)
    assert first == reduce_rows(value, evidence)
    assert conformance.nodes(value) == nodes_before
    assert evidence == evidence_before
    assert len(first) == len(nodes_before)
    assert all(row["region"] in conformance.REGIONS for row in first)
    assert all(
        row["region_source"] in {"unavailable", *conformance.SOURCE_KINDS} for row in first
    )
    assert all(row["region_blocker"] in (None, *conformance.BLOCKERS) for row in first)
    assert not any(isinstance(item, float) for row in first for item in row.values())
    encoded = json.dumps(first)
    assert "cell_" not in encoded
    assert "latitude" not in encoded and "longitude" not in encoded
    assert json.loads(encoded) == first


def test_every_peer_keeps_a_row_even_without_any_region_evidence():
    value = conformance.snapshot()
    result = reduce_rows(value, [])
    assert {row["node"] for row in result} == {
        item["id"] for item in conformance.nodes(value)
    }
    assert all(row["region"] == conformance.UNKNOWN for row in result)
    assert all(row["region_blocker"] == "missing" for row in result)


def test_projection_accepts_widened_regions_only_when_rows_agree_with_nodes():
    from mycelium_product_spine.route_explorer import validate_route_explorer

    value = conformance.snapshot()
    member, _ = conformance.members(value)
    evidence = [conformance.region_evidence(member["id"], "member", "owner_declared", "europe")]
    globe = conformance.with_regions(value, reduce_rows(value, evidence))
    assert validate_route_explorer(globe) == globe
    assert globe["overlays"] == "unavailable"
    assert {item["region"] for item in globe["nodes"]} == {"europe", "unknown"}
    assert {item["node"] for item in globe["regions"]} == {
        item["id"] for item in globe["nodes"]
    }

    disagreeing = json.loads(json.dumps(globe))
    disagreeing["regions"][0]["region"] = "polar"
    with pytest.raises(ValueError):
        validate_route_explorer(conformance.reseal(disagreeing))

    foreign = json.loads(json.dumps(globe))
    foreign["regions"][0]["node"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError):
        validate_route_explorer(conformance.reseal(foreign))

    short = json.loads(json.dumps(globe))
    short["regions"] = short["regions"][:1]
    with pytest.raises(ValueError):
        validate_route_explorer(conformance.reseal(short))

    duplicated = json.loads(json.dumps(globe))
    duplicated["regions"] = duplicated["regions"] + [duplicated["regions"][0]]
    with pytest.raises(ValueError):
        validate_route_explorer(conformance.reseal(duplicated))


def test_a14a_era_projection_and_a_globe_free_projection_lose_no_fact():
    from mycelium_product_spine.route_explorer import validate_route_explorer

    value = conformance.snapshot()
    legacy = conformance.projection(value)
    assert validate_route_explorer(legacy) == legacy
    assert legacy["overlays"] == "unavailable"

    member, _ = conformance.members(value)
    evidence = [conformance.region_evidence(member["id"], "member", "owner_declared", "europe")]
    globe = conformance.with_regions(value, reduce_rows(value, evidence))
    assert validate_route_explorer(globe) == globe

    flat = conformance.without_regions(globe)
    assert validate_route_explorer(flat) == flat
    assert flat["nodes"] == globe["nodes"]
    assert flat["segments"] == globe["segments"]
    assert flat["overlays"] == globe["overlays"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("latitude", 52.5),
        ("longitude", 13.4),
        ("cell", "cell_europe"),
        ("display_cell", "cell_europe"),
        ("endpoint_id", "RAW-ENDPOINT-CANARY"),
        ("x", 1),
        ("y", 1),
    ],
)
def test_projection_rejects_coordinates_cells_and_identity_in_region_rows(field, value):
    from mycelium_product_spine.route_explorer import validate_route_explorer

    snapshot = conformance.snapshot()
    member, _ = conformance.members(snapshot)
    evidence = [conformance.region_evidence(member["id"], "member", "owner_declared", "europe")]
    globe = conformance.with_regions(snapshot, reduce_rows(snapshot, evidence))
    assert validate_route_explorer(globe) == globe

    tampered = json.loads(json.dumps(globe))
    tampered["regions"][0][field] = value
    with pytest.raises(ValueError):
        validate_route_explorer(conformance.reseal(tampered))


def test_the_product_snapshot_binds_reviewed_region_evidence():
    from mycelium_product_spine.contracts import validate_product_snapshot
    from mycelium_product_spine.route_explorer import (
        project_route_explorer,
        validate_route_explorer,
    )

    reference = conformance.snapshot()
    member, _ = conformance.members(reference)
    endpoint = conformance.node_of_kind(reference, "endpoint")
    evidence = [
        conformance.region_evidence(member["id"], "member", "owner_declared", "europe"),
        conformance.region_evidence(
            endpoint["id"], "relay", "reviewed_relay_metadata", "asia"
        ),
    ]
    members, route, internet = conformance.a14a_inputs(now=NOW)
    projector = conformance.projector(pseudonym_salt=b"c" * 32)
    snapshot = projector.project(
        members=members,
        route_status=route,
        qualification=None,
        internet_native=internet,
        now_unix_ms=NOW,
        coarse_region_evidence=evidence,
    )
    projection = snapshot["route_explorer"]
    assert validate_product_snapshot(snapshot) == snapshot
    assert validate_route_explorer(projection) == projection
    assert project_route_explorer(snapshot) == projection
    assert by_node(projection["regions"])[member["id"]]["region"] == "europe"
    assert by_node(projection["regions"])[endpoint["id"]]["region"] == "asia"
    assert by_node(projection["regions"])[endpoint["id"]]["region_source"] == (
        "reviewed_relay_metadata"
    )
    assert {item["region"] for item in projection["nodes"]} == {"europe", "asia", "unknown"}
