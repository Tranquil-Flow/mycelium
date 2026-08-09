from __future__ import annotations

import hashlib
import json

import pytest

from mycelium_physical_runner import laptop_inventory
from mycelium_physical_runner.laptop_inventory import (
    LaptopFacts,
    LaptopInventoryError,
    build_laptop_observation,
    verify_laptop_inventory,
)
from mycelium_physical_runner.plan_builder import PHYSICAL_RUNNER_INVENTORY_PROTOCOL
from mycelium_qualification.evidence import canonical_json_bytes


def _facts(**overrides) -> LaptopFacts:
    values = {
        "host_name": "laptop-a",
        "machine_model": "MacBookPro18,3",
        "platform": "macOS-26.5.1",
        "architecture": "arm64",
        "memory_bytes": 16 * 1024**3,
        "available_storage_bytes": 80 * 1024**3,
        "backends": ("mlx",),
        "precisions": ("float16",),
        "python_version": "3.14.4",
        "is_laptop": True,
    }
    values.update(overrides)
    return LaptopFacts(**values)


def _observation(**overrides):
    values = {
        "run_id": "m7-inventory-001",
        "node_id": "node-laptop-a",
        "host_id": "host-" + "a" * 32,
        "boot_id": "boot-" + "b" * 32,
        "observed_at_unix_seconds": 1_786_230_000,
        "facts": _facts(),
    }
    values.update(overrides)
    return build_laptop_observation(**values)


def test_build_laptop_observation_reuses_membership_capability_shape() -> None:
    observation = _observation()

    assert observation["protocol"] == "mycelium.laptop_inventory_observation.v1"
    assert observation["node_id"] == "node-laptop-a"
    assert observation["host_id"] == "host-" + "a" * 32
    assert observation["boot_id"] == "boot-" + "b" * 32
    assert observation["host_name"] == "laptop-a"
    assert observation["machine_model"] == "MacBookPro18,3"
    assert observation["is_laptop"] is True
    assert observation["capability"] == {
        "platform": "macOS-26.5.1",
        "architecture": "arm64",
        "memory_bytes": 16 * 1024**3,
        "available_storage_bytes": 80 * 1024**3,
        "backends": ["mlx"],
        "precisions": ["float16"],
    }
    assert observation["python_version"] == "3.14.4"
    assert observation["collection_method"] == "local_system_probe"
    assert observation["physical_qualification_executed"] is False
    assert observation["route_ready"] is False
    assert observation["release_ready"] is False

    unsigned = {key: value for key, value in observation.items() if key != "observation_digest"}
    expected = "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    assert observation["observation_digest"] == expected


def test_build_laptop_observation_is_deterministic_and_binds_capability() -> None:
    first = _observation()
    second = _observation()
    changed = _observation(facts=_facts(memory_bytes=32 * 1024**3))

    assert first == second
    assert first["observation_digest"] != changed["observation_digest"]


def test_build_laptop_observation_rejects_non_laptop_and_invalid_aliases() -> None:
    with pytest.raises(LaptopInventoryError, match="laptop_required"):
        _observation(facts=_facts(is_laptop=False))
    with pytest.raises(LaptopInventoryError, match="host_identity_invalid"):
        _observation(host_id="raw-hardware-serial")


def test_canonical_cli_emits_only_one_observation(monkeypatch, capsys) -> None:
    expected = _observation()
    monkeypatch.setattr(
        laptop_inventory,
        "collect_local_laptop_observation",
        lambda *, run_id, node_id: expected,
    )

    assert (
        laptop_inventory.main(
            ["--canonical-json", "--run-id", "m7-inventory-001", "--node-id", "node-laptop-a"]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == expected
    assert captured.out == json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"


def _unique_observation(index: int):
    hex_character = "abcdef"[index]
    return _observation(
        node_id=f"node-laptop-{index}",
        host_id="host-" + hex_character * 32,
        boot_id="boot-" + hex_character * 32,
        facts=_facts(host_name=f"laptop-{index}"),
    )


def test_verify_laptop_inventory_accepts_three_unique_observations() -> None:
    observations = [_unique_observation(index) for index in (2, 0, 1)]

    verification = verify_laptop_inventory(observations)

    assert verification["protocol"] == "mycelium.laptop_inventory_verification.v1"
    assert verification["minimum_required_laptops"] == 3
    assert verification["observed_laptop_count"] == 3
    assert verification["node_ids"] == ["node-laptop-0", "node-laptop-1", "node-laptop-2"]
    assert verification["inventory_verified"] is True
    assert verification["physical_qualification_executed"] is False
    assert verification["route_ready"] is False
    assert verification["release_ready"] is False
    assert verification["verification_digest"].startswith("sha256:")


def test_verify_laptop_inventory_rejects_current_two_observation_state() -> None:
    with pytest.raises(LaptopInventoryError, match="inventory_minimum_not_met"):
        verify_laptop_inventory([_unique_observation(0), _unique_observation(1)])


def test_verify_laptop_inventory_rejects_tampering_and_duplicate_host() -> None:
    tampered = _unique_observation(0)
    tampered["capability"]["memory_bytes"] += 1
    with pytest.raises(LaptopInventoryError, match="observation_digest_invalid"):
        verify_laptop_inventory([tampered, _unique_observation(1), _unique_observation(2)])

    duplicate = _unique_observation(1)
    duplicate["host_id"] = _unique_observation(0)["host_id"]
    duplicate_without_digest = {
        key: value for key, value in duplicate.items() if key != "observation_digest"
    }
    duplicate["observation_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(duplicate_without_digest)
    ).hexdigest()
    with pytest.raises(LaptopInventoryError, match="inventory_identity_not_unique"):
        verify_laptop_inventory(
            [_unique_observation(0), duplicate, _unique_observation(2)]
        )


def _physical_inventory(observations):
    return {
        "protocol": PHYSICAL_RUNNER_INVENTORY_PROTOCOL,
        "run_id": observations[0]["run_id"],
        "deployment_id": "deployment-m7-001",
        "hosts": [
            {
                "node_id": observation["node_id"],
                "host_id": observation["host_id"],
                "boot_id": observation["boot_id"],
                "probe_transport": "local" if index == 0 else "ssh",
                "opaque_operator_field": f"preserve-{index}",
            }
            for index, observation in enumerate(observations)
        ],
        "opaque_root_field": {"preserve": True},
    }


def test_bind_verified_laptops_returns_detached_canonical_inventory() -> None:
    observations = [_unique_observation(index) for index in (0, 1, 2)]
    inventory = _physical_inventory(observations)

    bound = laptop_inventory.bind_verified_laptops_to_physical_inventory(
        inventory,
        observations,
    )

    assert bound == inventory
    assert bound is not inventory
    assert bound["hosts"] is not inventory["hosts"]
    assert "route_ready" not in bound
    assert "release_ready" not in bound


def test_bind_verified_laptops_cannot_lower_minimum_below_three() -> None:
    observations = [_unique_observation(index) for index in (0, 1)]
    inventory = _physical_inventory(observations)

    with pytest.raises(LaptopInventoryError, match="inventory_minimum_invalid"):
        laptop_inventory.bind_verified_laptops_to_physical_inventory(
            inventory,
            observations,
            minimum_laptops=2,
        )


def test_bind_verified_laptops_rejects_json_normalizing_inventory() -> None:
    observations = [_unique_observation(index) for index in (0, 1, 2)]
    inventory = _physical_inventory(observations)
    inventory["opaque_root_field"] = {"must_remain_tuple": ("a", "b")}

    with pytest.raises(LaptopInventoryError, match="physical_inventory_invalid"):
        laptop_inventory.bind_verified_laptops_to_physical_inventory(
            inventory,
            observations,
        )


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda inventory: inventory.__setitem__("protocol", "parallel.inventory.v1"),
            "physical_inventory_protocol_invalid",
        ),
        (
            lambda inventory: inventory.__setitem__("run_id", "different-run"),
            "inventory_run_mismatch",
        ),
        (
            lambda inventory: inventory.__setitem__("route_ready", True),
            "physical_inventory_readiness_forbidden",
        ),
        (
            lambda inventory: inventory["hosts"][0].__setitem__(
                "physical_qualification_executed", True
            ),
            "physical_inventory_readiness_forbidden",
        ),
        (
            lambda inventory: inventory["hosts"].pop(),
            "inventory_node_set_mismatch",
        ),
        (
            lambda inventory: inventory["hosts"][1].__setitem__(
                "host_id", "host-" + "f" * 32
            ),
            "inventory_identity_mismatch",
        ),
        (
            lambda inventory: inventory["hosts"][1].__setitem__(
                "boot_id", "boot-" + "f" * 32
            ),
            "inventory_identity_mismatch",
        ),
    ],
)
def test_bind_verified_laptops_rejects_inventory_mismatch(
    mutate,
    expected_code: str,
) -> None:
    observations = [_unique_observation(index) for index in (0, 1, 2)]
    inventory = _physical_inventory(observations)
    mutate(inventory)

    with pytest.raises(LaptopInventoryError, match=expected_code):
        laptop_inventory.bind_verified_laptops_to_physical_inventory(
            inventory,
            observations,
        )
