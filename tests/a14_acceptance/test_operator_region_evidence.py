# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-declared coarse-region evidence reaches the live route explorer."""
from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

import pytest

from mycelium_demo import cli
from mycelium_live import supervisor as supervisor_module
from mycelium_product_spine import ProductProjector
from mycelium_product_spine.app import ProductEvidenceApplication
from mycelium_product_spine.region_declarations import (
    PROTOCOL,
    OperatorRegionEvidenceSource,
    RegionDeclarationError,
    parse_region_declaration,
)
from mycelium_product_spine.route_explorer import digest, project_route_explorer

ROOT = Path(__file__).resolve().parents[2]
_conformance = runpy.run_path(str(ROOT / "tests/a14_acceptance/conformance.py"))
NOW = 1_000_000


def _declaration(path: Path, statements, *, expires=NOW + 60_000, mode=0o600, **extra):
    document = {"protocol": PROTOCOL, "operator": "owner", "issued_at_unix_ms": NOW - 1_000,
                "expires_at_unix_ms": expires,
                "declarations": [{"node_id": node, "region": region} for node, region in statements]}
    document.update(extra)
    path.write_text(json.dumps(document))
    os.chmod(path, mode)
    return path


def _snapshot(source):
    members, route, internet = _conformance["inputs"](NOW)
    projector = ProductProjector(pseudonym_salt=b"c" * 32)
    app = ProductEvidenceApplication(
        projector=projector, membership_source=lambda: members, route_source=lambda: route,
        qualification_source=lambda: None, internet_native_source=lambda: internet,
        coarse_region_source=None if source is None else source(projector),
        clock_unix_ms=lambda: NOW)
    return members, app._publish()


def _source(path):
    return lambda projector: OperatorRegionEvidenceSource(
        path, member_entity_id=projector._pseudonym)


def test_declared_regions_publish_owner_declared_rows_and_keep_unknown_peers(tmp_path):
    members, _ = _snapshot(None)
    first, second = sorted(member["node_id"] for member in members)[:2]
    path = _declaration(tmp_path / "regions.json", [(first, "europe"), (second, "us-east-1")])
    _members, snapshot = _snapshot(_source(path))
    explorer = snapshot["route_explorer"]
    assert explorer == project_route_explorer(
        {k: v for k, v in snapshot.items() if k != "route_explorer"})
    rows = {row["node"]: row for row in explorer["regions"]}
    projector = ProductProjector(pseudonym_salt=b"c" * 32)
    europe = rows[digest(["member", projector._pseudonym(first)])]
    provider = rows[digest(["member", projector._pseudonym(second)])]
    assert (europe["region"], europe["region_source"], europe["region_blocker"]) == (
        "europe", "owner_declared", None)
    # A provider region name is not vocabulary: an explicit unknown, off-globe.
    assert (provider["region"], provider["region_source"]) == ("unknown", "owner_declared")
    undeclared = [row for node, row in rows.items() if node not in (
        digest(["member", projector._pseudonym(first)]),
        digest(["member", projector._pseudonym(second)]))]
    assert undeclared and all(row["region"] == "unknown" and row["region_blocker"] == "missing"
                              for row in undeclared)
    record = snapshot["coarse_region_evidence"][0]
    assert record["uncertainty"] == "declared_unverified"
    for canary in (first, second, "192.0.2.23", "RAW-ENDPOINT-CANARY"):
        assert canary not in json.dumps(explorer)


def test_expired_declaration_is_published_but_reduces_to_expired_unknown(tmp_path):
    members, _ = _snapshot(None)
    node = sorted(member["node_id"] for member in members)[0]
    path = _declaration(tmp_path / "regions.json", [(node, "asia")], expires=NOW - 1)
    _members, snapshot = _snapshot(_source(path))
    rows = [row for row in snapshot["route_explorer"]["regions"] if row["region_blocker"] == "expired"]
    assert len(rows) == 1 and rows[0]["region"] == "unknown"


@pytest.mark.parametrize("problem", ["missing", "group_readable", "extra_key", "coordinates",
                                     "duplicate", "bad_node", "symlink"])
def test_a_refused_declaration_publishes_no_evidence_and_keeps_the_a14a_projection(
        tmp_path, problem):
    members, baseline = _snapshot(None)
    node = sorted(member["node_id"] for member in members)[0]
    path = tmp_path / "regions.json"
    if problem == "group_readable":
        _declaration(path, [(node, "europe")], mode=0o640)
    elif problem == "extra_key":
        _declaration(path, [(node, "europe")], hostname="host.example")
    elif problem == "coordinates":
        document = {"protocol": PROTOCOL, "operator": "owner", "issued_at_unix_ms": 1,
                    "expires_at_unix_ms": NOW * 2,
                    "declarations": [{"node_id": node, "region": "europe", "latitude": 52.5}]}
        path.write_text(json.dumps(document))
        os.chmod(path, 0o600)
    elif problem == "duplicate":
        _declaration(path, [(node, "europe"), (node, "asia")])
    elif problem == "bad_node":
        _declaration(path, [("../etc/passwd", "europe")])
    elif problem == "symlink":
        target = _declaration(tmp_path / "real.json", [(node, "europe")])
        path.symlink_to(target)
    source = OperatorRegionEvidenceSource(path, member_entity_id=lambda raw: raw)
    assert source() == [] and source.last_error is not None
    _members, snapshot = _snapshot(_source(path))
    assert "regions" not in snapshot["route_explorer"]
    assert "coarse_region_evidence" not in snapshot
    assert snapshot["route_explorer"]["nodes"] == baseline["route_explorer"]["nodes"]


def test_editing_the_declaration_replaces_the_authority():
    one = parse_region_declaration(json.dumps({
        "protocol": PROTOCOL, "operator": "owner", "issued_at_unix_ms": 1,
        "expires_at_unix_ms": 2, "declarations": [{"node_id": "n", "region": "europe"}]}).encode())
    two = parse_region_declaration(json.dumps({
        "protocol": PROTOCOL, "operator": "owner", "issued_at_unix_ms": 1,
        "expires_at_unix_ms": 2, "declarations": [{"node_id": "n", "region": "asia"}]}).encode())
    assert one["authority_digest"] != two["authority_digest"]
    assert one["statements"] == {"n": "europe"} and two["statements"] == {"n": "asia"}
    with pytest.raises(RegionDeclarationError):
        parse_region_declaration(b'{"protocol": "' + PROTOCOL.encode() + b'", "operator": NaN}')


def test_supervisor_and_demo_forward_the_declaration_file(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(supervisor_module, "run_physical_server",
                        lambda **kwargs: calls.append(kwargs) or 0)
    base = ["--operator-plan", str(tmp_path / "one.json"),
            "--seed-state-root", str(tmp_path / "seed")]
    assert supervisor_module.main(base) == 0
    assert "coarse_region_declaration_file" not in calls[-1]
    declaration = tmp_path / "regions.json"
    assert supervisor_module.main([*base, "--coarse-region-declaration-file",
                                   str(declaration)]) == 0
    assert calls[-1]["coarse_region_declaration_file"] == declaration
    forwarded: list[list[str]] = []
    argv = ["serve", "--mode", "live", "--operator-plan", str(tmp_path / "plan.json"),
            "--seed-state-root", str(tmp_path / "seed"),
            "--coarse-region-declaration-file", str(declaration)]
    assert cli.main(argv, live_server_main=lambda a: forwarded.append(list(a)) or 0,
                    environ={}) == 0
    assert forwarded[0][-2:] == ["--coarse-region-declaration-file", str(declaration)]
