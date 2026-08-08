from __future__ import annotations

import hashlib
import json

import pytest

from mycelium_physical_runner import laptop_inventory
from mycelium_physical_runner.laptop_inventory import (
    LaptopFacts,
    LaptopInventoryError,
    build_laptop_observation,
)
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
