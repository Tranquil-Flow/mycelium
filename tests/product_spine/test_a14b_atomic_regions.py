# SPDX-License-Identifier: AGPL-3.0-only
"""A14b atomic lane: coarse-region publishing pinned against the browser contract.

The gap lane (`runs/v2/a14b-route-explorer-gap`) verified the ordering hazard:
a backend that publishes coarse regions before `ui/web` widens its
route-explorer decoder makes `decodeProductSnapshot` throw on the live route
panel. These tests pin the backend half of the pair and the exact published
bytes the browser contract test decodes. They create no region authority and
claim no physical evidence: rows without an accepted authority publish
`unknown`.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "ui/web/src/test/a14bRegionConformance.json"
A14A_FIXTURE = ROOT / "ui/web/src/test/a14Conformance.json"
NOW = 1_000_000
WIDENED_PROJECTION_KEYS = {
    "protocol",
    "generation",
    "source_mode",
    "source_digest",
    "digest",
    "overlays",
    "nodes",
    "segments",
    "regions",
}


def published() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def accepted(snapshot: dict) -> frozenset:
    return frozenset(
        record["authority_digest"] for record in snapshot["coarse_region_evidence"]
    )


def resealed(value: dict) -> dict:
    from mycelium_product_spine.route_explorer import digest

    result = copy.deepcopy(value)
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    return result


def test_published_region_fixture_has_no_drift() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/generate_a14b_region_conformance.py", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_published_snapshot_binds_rows_to_nodes_and_revalidates() -> None:
    from mycelium_product_spine.contracts import validate_product_snapshot
    from mycelium_product_spine.route_explorer import (
        project_route_explorer,
        validate_route_explorer,
    )

    snapshot = published()
    assert validate_product_snapshot(snapshot) == snapshot
    projection = snapshot["route_explorer"]
    assert validate_route_explorer(projection) == projection
    assert project_route_explorer(snapshot) == projection
    assert projection["overlays"] == "unavailable"
    rows = {row["node"]: row for row in projection["regions"]}
    nodes = {node["id"]: node for node in projection["nodes"]}
    assert set(rows) == set(nodes)
    assert len(projection["regions"]) == len(projection["nodes"])
    for node_id, node in nodes.items():
        row = rows[node_id]
        assert (row["region"], row["region_source"]) == (
            node["region"],
            node["region_source"],
        )
    assert {node["region"] for node in projection["nodes"]} == {
        "europe",
        "asia",
        "unknown",
    }


def test_the_published_projection_exercises_the_widened_browser_shape() -> None:
    projection = published()["route_explorer"]
    # The A14a-era decoder enforces an exact eight-key projection; the published
    # bytes must carry the ninth key and non-unknown regions, so the frontend
    # widening is load-bearing rather than decorative.
    assert set(projection) == WIDENED_PROJECTION_KEYS
    assert any(node["region"] != "unknown" for node in projection["nodes"])
    assert any(
        node["region_source"] != "unavailable" for node in projection["nodes"]
    )


def test_a14a_era_projection_remains_byte_identical_without_evidence() -> None:
    from mycelium_product_spine.route_explorer import project_route_explorer

    legacy = json.loads(A14A_FIXTURE.read_text(encoding="utf-8"))
    projection = project_route_explorer(legacy)
    assert "regions" not in projection
    assert projection == legacy["route_explorer"]
    assert all(
        node["region"] == "unknown" and node["region_source"] == "unavailable"
        for node in projection["nodes"]
    )


def test_rows_without_an_accepted_authority_publish_unknown() -> None:
    from mycelium_product_spine.coarse_region import project_coarse_regions

    snapshot = published()
    nodes = snapshot["route_explorer"]["nodes"]
    evidence = snapshot["coarse_region_evidence"]
    # A narrower accepted set (for example after a withdrawal) is how
    # `unauthorized` is reached; publishing a record alone never grants
    # authority, and no signature field exists to be invented.
    rows = project_coarse_regions(
        nodes, evidence, now_unix_ms=NOW, authorized_authorities=frozenset()
    )
    by_node = {row["node"]: row for row in rows}
    assert set(by_node) == {node["id"] for node in nodes}
    with_evidence = {record["subject_ref"] for record in evidence}
    for node in nodes:
        row = by_node[node["id"]]
        assert row["region"] == "unknown" and row["region_source"] == "unavailable"
        # Frozen precedence: candidates without an accepted authority are
        # `unauthorized`; a node with no candidate at all stays `missing`.
        assert row["region_blocker"] == (
            "unauthorized" if node["id"] in with_evidence else "missing"
        )
    missing = project_coarse_regions(
        nodes, [], now_unix_ms=NOW, authorized_authorities=frozenset()
    )
    assert all(
        row["region"] == "unknown" and row["region_blocker"] == "missing"
        for row in missing
    )
    assert set(evidence[0]) == {
        "protocol",
        "subject_ref",
        "subject_kind",
        "source_kind",
        "region",
        "distance_class",
        "vocabulary_version",
        "model_version",
        "uncertainty",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
        "authority_digest",
        "digest",
    }
    accepted_rows = project_coarse_regions(
        nodes, evidence, now_unix_ms=NOW, authorized_authorities=accepted(snapshot)
    )
    assert any(row["region"] != "unknown" for row in accepted_rows)


def test_widened_route_explorer_accepts_rows_and_rejects_split_shapes() -> None:
    from mycelium_product_spine.route_explorer import validate_route_explorer

    projection = published()["route_explorer"]
    assert validate_route_explorer(projection) == projection

    disagreeing = copy.deepcopy(projection)
    disagreeing["regions"][0]["region"] = "polar"
    foreign = copy.deepcopy(projection)
    foreign["regions"][0]["node"] = "sha256:" + "c" * 64
    short = copy.deepcopy(projection)
    short["regions"] = short["regions"][:1]
    duplicated = copy.deepcopy(projection)
    duplicated["regions"] = duplicated["regions"] + [duplicated["regions"][0]]
    unsorted = copy.deepcopy(projection)
    unsorted["regions"] = list(reversed(unsorted["regions"]))
    coordinate = copy.deepcopy(projection)
    coordinate["regions"][0]["latitude"] = 52.5
    cell = copy.deepcopy(projection)
    cell["regions"][0]["cell"] = "cell_europe"
    bad_region = copy.deepcopy(projection)
    bad_region["nodes"][0]["region"] = "us-east-1"
    bad_source = copy.deepcopy(projection)
    bad_source["nodes"][0]["region_source"] = "signed"
    for value in (
        disagreeing,
        foreign,
        short,
        duplicated,
        unsorted,
        coordinate,
        cell,
        bad_region,
        bad_source,
    ):
        with pytest.raises(ValueError):
            validate_route_explorer(resealed(value))


def test_the_reduction_is_pure_deterministic_and_vocabulary_only() -> None:
    from mycelium_product_spine.coarse_region import project_coarse_regions

    snapshot = published()
    nodes = snapshot["route_explorer"]["nodes"]
    evidence = snapshot["coarse_region_evidence"]
    nodes_before = copy.deepcopy(nodes)
    evidence_before = copy.deepcopy(evidence)
    authorized = accepted(snapshot)
    first = project_coarse_regions(
        nodes, evidence, now_unix_ms=NOW, authorized_authorities=authorized
    )
    assert first == project_coarse_regions(
        nodes, evidence, now_unix_ms=NOW, authorized_authorities=authorized
    )
    assert nodes == nodes_before and evidence == evidence_before
    assert [row["node"] for row in first] == sorted(row["node"] for row in first)
    assert all(
        set(row) == {"node", "region", "region_source", "region_blocker", "distance_class"}
        for row in first
    )
    encoded = json.dumps(first)
    assert "cell_" not in encoded
    assert "latitude" not in encoded and "longitude" not in encoded
    assert not any(isinstance(item, float) for row in first for item in row.values())


def test_evidence_records_stay_closed_and_digest_bound() -> None:
    from mycelium_product_spine.coarse_region import (
        digest,
        validate_coarse_region_evidence,
    )

    record = published()["coarse_region_evidence"][0]
    assert validate_coarse_region_evidence(record) == record
    assert record["digest"] == digest(
        {key: value for key, value in record.items() if key != "digest"}
    )
    stale = dict(record)
    stale["region"] = "polar"
    with pytest.raises(ValueError):
        validate_coarse_region_evidence(stale)
    extra = dict(record)
    extra["hostname"] = "private.example"
    extra["digest"] = digest({k: v for k, v in extra.items() if k != "digest"})
    with pytest.raises(ValueError):
        validate_coarse_region_evidence(extra)
    returned = validate_coarse_region_evidence(record)
    returned["region"] = "polar"
    assert record["region"] == "europe"
