from __future__ import annotations

import copy

import pytest

from mycelium_product_spine import (
    ENTITY_KINDS,
    ProductContractError,
    validate_product_event,
    validate_product_snapshot,
)


def snapshot_fixture() -> dict:
    source_id = "membership-source"
    binding = {
        "deployment_id": None,
        "deployment_epoch": None,
        "route_id": None,
        "route_generation": None,
        "topology_version": None,
    }
    freshness = {
        "status": "current",
        "observed_at_unix_ms": 1_000,
        "valid_until_unix_ms": 2_000,
    }
    return {
        "protocol": "mycelium.product_snapshot.v1",
        "publication": {
            "snapshot_id": "snapshot-1",
            "generation": 1,
            "cursor": 1,
            "published_at_unix_ms": 1_000,
            "source_mode": "live",
        },
        "supported_entity_kinds": list(ENTITY_KINDS),
        "source_states": [
            {
                "source_id": source_id,
                "authority": "seed_coordinator",
                "status": "current",
                "observed_at_unix_ms": 1_000,
                "valid_until_unix_ms": 2_000,
                "generation": 1,
                "reason_code": None,
            }
        ],
        "entities": [
            {
                "entity_id": "mobile-conformance-001",
                "kind": "device",
                "label": "Mobile conformance device",
                "source_id": source_id,
                "binding": binding,
                "freshness": freshness,
                "attributes": {
                    "peer_class": "android_termux_iroh",
                    "membership_generation": 1,
                    "authority_generation": 1,
                    "incarnation": "mobile-incarnation-1",
                    "lifecycle": "running",
                    "lease_freshness": "fresh",
                    "runtime_backend": "pixel-stdlib",
                    "transport": "iroh",
                    "activation_protocol": "mycelium.router_wire.v1",
                    "activation_eligible": False,
                    "placement_id": None,
                },
            }
        ],
        "relations": [],
        "readiness": [
            {
                "scope_id": "mobile-conformance-001",
                "dimension": "membership",
                "state": "ready",
                "reason_code": None,
                "source_id": source_id,
            },
            {
                "scope_id": "mobile-conformance-001",
                "dimension": "qualification",
                "state": "not_ready",
                "reason_code": "mobile_qualification_required",
                "source_id": source_id,
            },
        ],
        "notices": [],
        "provenance": {
            "projector": "mycelium_product_spine",
            "projector_version": "m12-v1",
            "source_mode": "live",
        },
    }


def test_snapshot_is_closed_detached_and_keeps_mobile_membership_unqualified() -> None:
    fixture = snapshot_fixture()
    validated = validate_product_snapshot(fixture)

    assert validated == fixture
    assert validated is not fixture
    assert validated["entities"][0]["attributes"]["activation_eligible"] is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("prompt", "private prompt"),
        ("endpoint_addrs", ["https://10.0.0.1/control"]),
        ("private_key", "not-a-key"),
        ("kv_cache", {"bytes": 1}),
        ("reservation_id", "reservation-private"),
    ],
)
def test_snapshot_rejects_every_private_payload_lane(key: str, value: object) -> None:
    fixture = snapshot_fixture()
    fixture["entities"][0]["attributes"][key] = value

    with pytest.raises(ProductContractError):
        validate_product_snapshot(fixture)


def test_snapshot_rejects_unknown_fields_and_unbound_sources() -> None:
    unknown = {**snapshot_fixture(), "planner_output": {}}
    with pytest.raises(ProductContractError, match="product_snapshot_invalid"):
        validate_product_snapshot(unknown)

    unbound = snapshot_fixture()
    unbound["entities"][0]["source_id"] = "missing-source"
    with pytest.raises(ProductContractError, match="product_snapshot_source_unbound"):
        validate_product_snapshot(unbound)


def test_event_requires_contiguous_cursor_and_snapshot_binding() -> None:
    snapshot = snapshot_fixture()
    event = {
        "protocol": "mycelium.product_event.v1",
        "cursor": 1,
        "previous_cursor": 0,
        "event_kind": "snapshot_published",
        "snapshot": snapshot,
    }
    assert validate_product_event(event) == event

    gap = copy.deepcopy(event)
    gap["cursor"] = 3
    gap["snapshot"]["publication"]["cursor"] = 3
    with pytest.raises(ProductContractError, match="product_event_invalid"):
        validate_product_event(gap)

    mixed = copy.deepcopy(event)
    mixed["snapshot"]["publication"]["cursor"] = 2
    with pytest.raises(ProductContractError, match="product_event_snapshot_unbound"):
        validate_product_event(mixed)
