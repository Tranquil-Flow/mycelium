from __future__ import annotations

import json
from pathlib import Path

from mycelium_product_spine import ProductProjector


ROOT = Path(__file__).resolve().parents[2]


def _member(node_id: str, *, peer_class: str = "mac_mlx_iroh") -> dict:
    mobile = peer_class == "android_termux_iroh"
    return {
        "node_id": node_id,
        "generation": 1,
        "incarnation": "incarnation-1",
        "lease_expires_at": 2_000.0,
        "last_liveness_at": 1_000.0,
        "lifecycle_state": "RUNNING",
        "peer_class": peer_class,
        "runtime_capability": {
            "runtime_backend": "pixel-stdlib" if mobile else "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        "activation_eligible": not mobile,
        "endpoint_id": "must-never-appear",
        "endpoint_addrs": ["https://10.0.0.1/control"],
    }


def _route() -> dict:
    return json.loads(
        (ROOT / "contracts/compatibility-fixtures/live-route-status-v1.json").read_text()
    )


def _qualification() -> dict:
    value = json.loads(
        (ROOT / "contracts/compatibility-fixtures/route-qualification-v1.json").read_text()
    )
    value.update(
        qualification_id="qualification-live",
        route_ready=True,
        issued_at_unix_ms=1_000_000,
        deployment_id=_route()["deployment_id"],
        deployment_epoch=1,
        topology_version=_route()["topology_version"],
        model_id=_route()["model_id"],
        placement_provenance="operator_selected",
        reason_codes=[],
    )
    return value


def _internet_native() -> dict:
    fixtures = ROOT / "contracts" / "compatibility-fixtures"
    return {
        "bootstrap_status": json.loads(
            (fixtures / "internet-bootstrap-status-v1.json").read_text()
        ),
        "activation_observation": json.loads(
            (fixtures / "internet-activation-observation-v1.json").read_text()
        ),
        "activation_history": [
            json.loads(
                (fixtures / "internet-activation-observation-v1.json").read_text()
            )
        ],
        "relay_projection": json.loads(
            (fixtures / "relay-projection-v1.json").read_text()
        ),
        "qualification": json.loads(
            (fixtures / "internet-native-qualification-v1.json").read_text()
        ),
    }


def test_snapshot_always_contains_closed_unknown_internet_native_projection() -> None:
    snapshot = ProductProjector(pseudonym_salt=b"i" * 32).project(
        members=[],
        route_status=None,
        qualification=None,
        now_unix_ms=1_500_000,
    )

    assert set(snapshot["internet_native"]) == {
        "bootstrap_status",
        "activation_observation",
        "activation_history",
        "relay_projection",
        "qualification",
    }
    assert snapshot["internet_native"]["bootstrap_status"]["freshness"] == "unknown"
    assert snapshot["internet_native"]["activation_observation"]["path_class"] == "unknown"
    assert snapshot["internet_native"]["relay_projection"] is None
    assert snapshot["internet_native"]["qualification"] is None


def test_snapshot_detaches_supplied_internet_native_projection() -> None:
    internet_native = _internet_native()
    snapshot = ProductProjector(pseudonym_salt=b"j" * 32).project(
        members=[],
        route_status=None,
        qualification=None,
        internet_native=internet_native,
        now_unix_ms=1_500_000,
    )

    assert snapshot["internet_native"] == internet_native
    assert snapshot["internet_native"] is not internet_native


def test_member_without_placement_is_visible_but_mobile_is_not_qualified() -> None:
    projector = ProductProjector(pseudonym_salt=b"p" * 32)
    snapshot = projector.project(
        members=[
            _member("node-a"),
            _member("node-b"),
            _member("private-pixel-hostname", peer_class="android_termux_iroh"),
        ],
        route_status=_route(),
        qualification=_qualification(),
        now_unix_ms=1_500_000,
    )

    mobile = next(
        item
        for item in snapshot["entities"]
        if item["kind"] == "device"
        and item["attributes"]["peer_class"] == "android_termux_iroh"
    )
    encoded = json.dumps(snapshot, sort_keys=True)
    assert mobile["attributes"]["placement_id"] is None
    assert mobile["attributes"]["activation_eligible"] is False
    assert "private-pixel-hostname" not in encoded
    assert "must-never-appear" not in encoded
    assert "10.0.0.1" not in encoded
    assert any(
        item["code"] == "member_without_placement"
        and item["scope_id"] == mobile["entity_id"]
        for item in snapshot["notices"]
    )


def test_placement_without_current_member_withholds_coherent_qualification() -> None:
    projector = ProductProjector(pseudonym_salt=b"q" * 32)
    snapshot = projector.project(
        members=[_member("node-a")],
        route_status=_route(),
        qualification=_qualification(),
        now_unix_ms=1_500_000,
    )

    route_id = next(
        item["entity_id"] for item in snapshot["entities"] if item["kind"] == "route"
    )
    readiness = {
        item["dimension"]: item
        for item in snapshot["readiness"]
        if item["scope_id"] == route_id
    }
    authority = next(
        item for item in snapshot["entities"] if item["kind"] == "qualification"
    )
    assert authority["attributes"]["route_ready"] is True
    assert readiness["membership"]["state"] == "not_ready"
    assert readiness["membership"]["reason_code"] == "placement_member_missing"
    assert readiness["qualification"]["state"] == "not_ready"


def test_revoked_member_is_visible_but_never_activation_eligible() -> None:
    revoked = _member("node-a")
    revoked.update(
        lifecycle_state="STOPPED",
        generation=2,
        lease_expires_at=1_000.0,
        last_liveness_at=1_400.0,
    )
    snapshot = ProductProjector(pseudonym_salt=b"r" * 32).project(
        members=[revoked, _member("node-b")],
        route_status=_route(),
        qualification=_qualification(),
        now_unix_ms=1_500_000,
    )

    device = next(
        entity
        for entity in snapshot["entities"]
        if entity["kind"] == "device"
        and entity["attributes"]["membership_generation"] == 2
    )
    assert device["attributes"]["lifecycle"] == "stopped"
    assert device["attributes"]["activation_eligible"] is False
    assert any(
        item["scope_id"] == device["entity_id"]
        and item["reason_code"] == "member_revoked"
        for item in snapshot["readiness"]
    )
    assert any(
        entity["kind"] == "incident"
        and entity["attributes"]["reason_code"] == "member_revoked"
        for entity in snapshot["entities"]
    )


def test_seed_rotation_projects_generation_and_incident_without_key_digests() -> None:
    members = [_member("node-a"), _member("node-b")]
    for member in members:
        member.update(
            authority_generation=2,
            rotation_status="completed",
            rotation_observed_at=1_450.0,
        )
    snapshot = ProductProjector(pseudonym_salt=b"k" * 32).project(
        members=members,
        route_status=_route(),
        qualification=_qualification(),
        now_unix_ms=1_500_000,
    )

    devices = [entity for entity in snapshot["entities"] if entity["kind"] == "device"]
    assert {device["attributes"]["authority_generation"] for device in devices} == {2}
    assert any(
        entity["kind"] == "incident"
        and entity["attributes"]["state"] == "seed_rotation_completed"
        for entity in snapshot["entities"]
    )
    assert "seed_key_digest" not in repr(snapshot)


def test_projector_module_has_no_mutating_subsystem_imports() -> None:
    source = (ROOT / "mycelium_product_spine/projector.py").read_text()
    for forbidden in (
        "mycelium_layer_planner",
        "mycelium_qualification.qualifier",
        "physical_inference",
        "subprocess",
        "provisioning",
    ):
        assert forbidden not in source


def test_mixed_qualification_binding_degrades_and_withholds_readiness() -> None:
    qualification = _qualification()
    qualification["topology_version"] += 1
    snapshot = ProductProjector(pseudonym_salt=b"b" * 32).project(
        members=[_member("node-a"), _member("node-b")],
        route_status=_route(),
        qualification=qualification,
        now_unix_ms=1_500_000,
    )

    assert snapshot["publication"]["source_mode"] == "degraded"
    qualification_source = next(
        source
        for source in snapshot["source_states"]
        if source["source_id"] == "qualification-source"
    )
    assert qualification_source["status"] == "conflict"
    assert qualification_source["reason_code"] == "qualification_binding_conflict"
    assert any(
        item["dimension"] == "qualification" and item["state"] == "not_ready"
        for item in snapshot["readiness"]
    )
    assert not any(
        relation["kind"] == "qualifies" for relation in snapshot["relations"]
    )


def test_route_order_exposes_unknown_directed_link_without_inventing_measurement() -> None:
    snapshot = ProductProjector(pseudonym_salt=b"l" * 32).project(
        members=[_member("node-a"), _member("node-b")],
        route_status=_route(),
        qualification=_qualification(),
        now_unix_ms=1_500_000,
    )

    links = [entity for entity in snapshot["entities"] if entity["kind"] == "directed_link"]
    assert len(links) == 1
    assert links[0]["attributes"]["connectivity"] == "unknown"
    assert links[0]["attributes"]["measurement_digest"] is None


def test_assignment_and_load_proof_bind_current_member_stage_and_qualification() -> None:
    assignments = [
        {
            "assignment_id": f"assignment-{index}",
            "node_id": f"node-{'a' if index == 0 else 'b'}",
            "stage_id": f"stage-00{index}-primary",
            "membership_generation": 1,
            "load_generation": 17,
            "assignment_digest": "sha256:" + ("a" if index == 0 else "b") * 64,
            "stage_pack_digest": "sha256:" + ("c" if index == 0 else "d") * 64,
            "load_proof_digest": "sha256:" + ("e" if index == 0 else "f") * 64,
        }
        for index in range(2)
    ]
    snapshot = ProductProjector(pseudonym_salt=b"a" * 32).project(
        members=[_member("node-a"), _member("node-b")],
        assignments=assignments,
        route_status=_route(),
        qualification=_qualification(),
        now_unix_ms=1_500_000,
    )

    projected_assignments = [
        entity for entity in snapshot["entities"] if entity["kind"] == "assignment"
    ]
    load_proofs = [
        entity for entity in snapshot["entities"] if entity["kind"] == "load_proof"
    ]
    assert len(projected_assignments) == 2
    assert len(load_proofs) == 2
    assert all(entity["attributes"]["ready"] is True for entity in load_proofs)
    assert all(
        entity["attributes"]["device_id"].startswith("device-")
        for entity in projected_assignments
    )
    assert any(
        item["dimension"] == "artifacts" and item["state"] == "ready"
        for item in snapshot["readiness"]
    )


def test_replica_placements_are_distinct_parallel_stage_entities() -> None:
    route = _route()
    primary = dict(route["stages"][0])
    replica = {
        **primary,
        "node_id": "node-b",
        "placement_id": "stage-000-replica-node-b",
    }
    route["stages"] = [primary, replica]
    route["peers"][0]["placements"] = [primary]
    route["peers"][1]["placements"] = [replica]

    snapshot = ProductProjector(pseudonym_salt=b"x" * 32).project(
        members=[_member("node-a"), _member("node-b")],
        route_status=route,
        qualification=_qualification(),
        now_unix_ms=1_500_000,
    )

    stages = [entity for entity in snapshot["entities"] if entity["kind"] == "stage"]
    assert {stage["entity_id"] for stage in stages} == {
        "stage-000-primary",
        "stage-000-replica-node-b",
    }
    assert not any(
        entity["kind"] == "directed_link" for entity in snapshot["entities"]
    )
