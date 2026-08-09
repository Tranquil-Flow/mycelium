"""Fail-closed local laptop capability observations for M7 inventory."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform as platform_module
import re
import shutil
import subprocess
import sys
import time
from typing import Any, NoReturn, Sequence

from mycelium_membership import (
    CAPABILITY_REPORT_PROTOCOL,
    MembershipContractError,
    validate_membership_message,
)
from mycelium_qualification.evidence import canonical_json_bytes

from .plan_builder import PHYSICAL_RUNNER_INVENTORY_PROTOCOL
from .remote_probe import derive_local_run_scoped_identity

OBSERVATION_PROTOCOL = "mycelium.laptop_inventory_observation.v1"
VERIFICATION_PROTOCOL = "mycelium.laptop_inventory_verification.v1"
_HOST_ID_RE = re.compile(r"^host-[0-9a-f]{32}$")
_BOOT_ID_RE = re.compile(r"^boot-[0-9a-f]{32}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LINUX_LAPTOP_CHASSIS_TYPES = frozenset({8, 9, 10, 14, 30, 31, 32})
_FORBIDDEN_INVENTORY_CLAIM_KEY_MARKERS = frozenset(
    {
        "inventoryverified",
        "physicallyqualified",
        "qualification",
        "releaseready",
        "routeready",
        "readyforrelease",
        "readyforroute",
        "releaseisready",
        "routeisready",
    }
)
_READINESS_CONFIG_TOKENS = frozenset(
    {"deadline", "interval", "seconds", "timeout", "window"}
)


class LaptopInventoryError(ValueError):
    """Stable fail-closed laptop inventory error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LaptopFacts:
    host_name: str
    machine_model: str
    platform: str
    architecture: str
    memory_bytes: int
    available_storage_bytes: int
    backends: tuple[str, ...]
    precisions: tuple[str, ...]
    python_version: str
    is_laptop: bool


def _reject(code: str) -> NoReturn:
    raise LaptopInventoryError(code)


def _require_exact_json_shape(value: Any, code: str) -> None:
    if type(value) is dict:
        for key, nested in value.items():
            if type(key) is not str:
                _reject(code)
            _require_exact_json_shape(nested, code)
        return
    if type(value) is list:
        for nested in value:
            _require_exact_json_shape(nested, code)
        return
    if type(value) not in {str, int, float, bool, type(None)}:
        _reject(code)


def _canonical_snapshot(value: Any, code: str) -> Any:
    _require_exact_json_shape(value, code)
    try:
        detached = json.loads(
            json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True)
        )
    except (TypeError, ValueError) as exc:
        raise LaptopInventoryError(code) from exc
    if detached != value:
        _reject(code)
    return detached


def _inventory_claim_key_forbidden(key: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    ordered_tokens = tuple(re.findall(r"[a-z0-9]+", separated.casefold()))
    tokens = frozenset(ordered_tokens)
    compact = "".join(ordered_tokens)
    if any(marker in compact for marker in _FORBIDDEN_INVENTORY_CLAIM_KEY_MARKERS):
        return True
    if "qualified" in tokens:
        if {"fully", "domain", "name"}.issubset(tokens):
            return False
        return True
    if "verified" in tokens and "inventory" in tokens:
        return True
    readiness = tokens & {"ready", "readiness"}
    readiness_domain = tokens & {"authority", "release", "route"}
    return bool(
        readiness
        and readiness_domain
        and not (tokens & _READINESS_CONFIG_TOKENS)
    )


def _reject_forbidden_inventory_claims(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                if _inventory_claim_key_forbidden(key):
                    _reject("physical_inventory_readiness_forbidden")
            _reject_forbidden_inventory_claims(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_inventory_claims(nested)


def _segment(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        _reject(code)
    return value


def _text(value: Any, code: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _reject(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _reject(code)
    if len(encoded) > maximum or any(character in value for character in "\x00\n\r\t"):
        _reject(code)
    return value


def _capability_message(
    *,
    node_id: str,
    host_id: str,
    boot_id: str,
    observed_at_unix_seconds: int,
    facts: LaptopFacts,
) -> dict[str, Any]:
    capability = {
        "platform": facts.platform,
        "architecture": facts.architecture,
        "memory_bytes": facts.memory_bytes,
        "available_storage_bytes": facts.available_storage_bytes,
        "backends": sorted(facts.backends),
        "precisions": sorted(facts.precisions),
    }
    message = {
        "protocol": CAPABILITY_REPORT_PROTOCOL,
        "message_id": "m7-inventory-observation",
        "swarm_id": "m7-inventory",
        "sender_node_id": node_id,
        "sender_endpoint_id": host_id,
        "recipient_node_id": "inventory-controller",
        "incarnation": boot_id,
        "generation": 1,
        "issued_at": observed_at_unix_seconds,
        "expires_at": observed_at_unix_seconds + 60,
        **capability,
    }
    try:
        validated = validate_membership_message(message)
    except MembershipContractError as exc:
        raise LaptopInventoryError("capability_invalid") from exc
    return {key: validated[key] for key in capability}


def build_laptop_observation(
    *,
    run_id: str,
    node_id: str,
    host_id: str,
    boot_id: str,
    observed_at_unix_seconds: int,
    facts: LaptopFacts,
) -> dict[str, Any]:
    """Validate and digest one physically collected laptop observation."""

    run_id = _segment(run_id, "run_id_invalid")
    node_id = _segment(node_id, "node_id_invalid")
    if not isinstance(host_id, str) or _HOST_ID_RE.fullmatch(host_id) is None:
        _reject("host_identity_invalid")
    if not isinstance(boot_id, str) or _BOOT_ID_RE.fullmatch(boot_id) is None:
        _reject("boot_identity_invalid")
    if (
        isinstance(observed_at_unix_seconds, bool)
        or not isinstance(observed_at_unix_seconds, int)
        or observed_at_unix_seconds <= 0
    ):
        _reject("observation_time_invalid")
    if not isinstance(facts, LaptopFacts):
        _reject("facts_invalid")
    if facts.is_laptop is not True:
        _reject("laptop_required")

    host_name = _text(facts.host_name, "host_name_invalid")
    machine_model = _text(facts.machine_model, "machine_model_invalid")
    python_version = _text(facts.python_version, "python_version_invalid", maximum=64)
    capability = _capability_message(
        node_id=node_id,
        host_id=host_id,
        boot_id=boot_id,
        observed_at_unix_seconds=observed_at_unix_seconds,
        facts=facts,
    )
    unsigned = {
        "protocol": OBSERVATION_PROTOCOL,
        "run_id": run_id,
        "node_id": node_id,
        "host_id": host_id,
        "boot_id": boot_id,
        "observed_at_unix_seconds": observed_at_unix_seconds,
        "host_name": host_name,
        "machine_model": machine_model,
        "is_laptop": True,
        "capability": capability,
        "python_version": python_version,
        "collection_method": "local_system_probe",
        "physical_qualification_executed": False,
        "route_ready": False,
        "release_ready": False,
    }
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    return {**unsigned, "observation_digest": digest}


def _validated_observation(document: Any) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        _reject("observation_invalid")
    value = dict(document)
    expected_fields = {
        "protocol",
        "run_id",
        "node_id",
        "host_id",
        "boot_id",
        "observed_at_unix_seconds",
        "host_name",
        "machine_model",
        "is_laptop",
        "capability",
        "python_version",
        "collection_method",
        "physical_qualification_executed",
        "route_ready",
        "release_ready",
        "observation_digest",
    }
    if set(value) != expected_fields or value.get("protocol") != OBSERVATION_PROTOCOL:
        _reject("observation_invalid")
    supplied_digest = value.get("observation_digest")
    unsigned = {key: item for key, item in value.items() if key != "observation_digest"}
    actual_digest = "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    if supplied_digest != actual_digest:
        _reject("observation_digest_invalid")
    capability = value.get("capability")
    if not isinstance(capability, Mapping):
        _reject("observation_invalid")
    try:
        facts = LaptopFacts(
            host_name=value["host_name"],
            machine_model=value["machine_model"],
            platform=capability["platform"],
            architecture=capability["architecture"],
            memory_bytes=capability["memory_bytes"],
            available_storage_bytes=capability["available_storage_bytes"],
            backends=tuple(capability["backends"]),
            precisions=tuple(capability["precisions"]),
            python_version=value["python_version"],
            is_laptop=value["is_laptop"],
        )
        rebuilt = build_laptop_observation(
            run_id=value["run_id"],
            node_id=value["node_id"],
            host_id=value["host_id"],
            boot_id=value["boot_id"],
            observed_at_unix_seconds=value["observed_at_unix_seconds"],
            facts=facts,
        )
    except (KeyError, TypeError, LaptopInventoryError) as exc:
        raise LaptopInventoryError("observation_invalid") from exc
    if rebuilt != value:
        _reject("observation_invalid")
    return rebuilt


def verify_laptop_inventory(
    observations: Sequence[Mapping[str, Any]],
    *,
    minimum_laptops: int = 3,
) -> dict[str, Any]:
    """Verify one unique, digest-bound observed laptop set."""

    if (
        isinstance(minimum_laptops, bool)
        or not isinstance(minimum_laptops, int)
        or minimum_laptops < 1
    ):
        _reject("inventory_minimum_invalid")
    if not isinstance(observations, Sequence) or isinstance(
        observations, (str, bytes, bytearray)
    ):
        _reject("inventory_invalid")
    validated = tuple(_validated_observation(document) for document in observations)
    if len(validated) < minimum_laptops:
        _reject("inventory_minimum_not_met")
    if len({item["run_id"] for item in validated}) != 1:
        _reject("inventory_run_mismatch")
    for field in ("node_id", "host_id", "boot_id"):
        if len({item[field] for item in validated}) != len(validated):
            _reject("inventory_identity_not_unique")

    ordered = tuple(sorted(validated, key=lambda item: item["node_id"]))
    unsigned = {
        "protocol": VERIFICATION_PROTOCOL,
        "run_id": ordered[0]["run_id"],
        "minimum_required_laptops": minimum_laptops,
        "observed_laptop_count": len(ordered),
        "node_ids": [item["node_id"] for item in ordered],
        "observation_digests": [item["observation_digest"] for item in ordered],
        "inventory_verified": True,
        "physical_qualification_executed": False,
        "route_ready": False,
        "release_ready": False,
    }
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    return {**unsigned, "verification_digest": digest}


def bind_verified_laptops_to_physical_inventory(
    inventory: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    minimum_laptops: int = 3,
) -> dict[str, Any]:
    """Identity-bind JSON-canonical inventory before full ``build_safe_plan`` validation."""

    if (
        isinstance(minimum_laptops, bool)
        or not isinstance(minimum_laptops, int)
        or minimum_laptops < 3
    ):
        _reject("inventory_minimum_invalid")
    try:
        raw_observations = list(observations)
    except (TypeError, ValueError) as exc:
        raise LaptopInventoryError("observation_invalid") from exc
    observation_snapshot = _canonical_snapshot(
        raw_observations,
        "observation_invalid",
    )
    inventory_snapshot = _canonical_snapshot(
        inventory,
        "physical_inventory_invalid",
    )
    verification = verify_laptop_inventory(
        observation_snapshot,
        minimum_laptops=minimum_laptops,
    )
    if not isinstance(inventory_snapshot, dict):
        _reject("physical_inventory_protocol_invalid")
    if inventory_snapshot.get("protocol") != PHYSICAL_RUNNER_INVENTORY_PROTOCOL:
        _reject("physical_inventory_protocol_invalid")
    _reject_forbidden_inventory_claims(inventory_snapshot)
    if inventory_snapshot.get("run_id") != verification["run_id"]:
        _reject("inventory_run_mismatch")
    hosts = inventory_snapshot.get("hosts")
    if not isinstance(hosts, list):
        _reject("inventory_node_set_mismatch")

    hosts_by_node: dict[str, Mapping[str, Any]] = {}
    for host in hosts:
        if not isinstance(host, Mapping):
            _reject("inventory_node_set_mismatch")
        node_id = host.get("node_id")
        if not isinstance(node_id, str) or node_id in hosts_by_node:
            _reject("inventory_node_set_mismatch")
        hosts_by_node[node_id] = host
    expected_nodes = set(verification["node_ids"])
    if set(hosts_by_node) != expected_nodes:
        _reject("inventory_node_set_mismatch")

    observations_by_node = {
        observation["node_id"]: _validated_observation(observation)
        for observation in observation_snapshot
    }
    for node_id, observation in observations_by_node.items():
        host = hosts_by_node[node_id]
        if (
            host.get("host_id") != observation["host_id"]
            or host.get("boot_id") != observation["boot_id"]
        ):
            _reject("inventory_identity_mismatch")
    return inventory_snapshot


def _command_output(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            tuple(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10.0,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaptopInventoryError("hardware_probe_failed") from exc
    if completed.returncode != 0:
        _reject("hardware_probe_failed")
    return completed.stdout.strip()


def _darwin_model() -> tuple[str, bool]:
    raw = _command_output(("system_profiler", "SPHardwareDataType", "-json"))
    try:
        record = json.loads(raw)["SPHardwareDataType"][0]
        machine_name = record["machine_name"]
        machine_model = record["machine_model"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise LaptopInventoryError("hardware_probe_failed") from exc
    _text(machine_name, "hardware_probe_failed")
    return _text(machine_model, "hardware_probe_failed"), "macbook" in machine_name.casefold()


def _linux_model() -> tuple[str, bool]:
    product_path = Path("/sys/class/dmi/id/product_name")
    chassis_path = Path("/sys/class/dmi/id/chassis_type")
    try:
        model = product_path.read_text(encoding="utf-8").strip()
        chassis_type = int(chassis_path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise LaptopInventoryError("hardware_probe_failed") from exc
    return _text(model, "hardware_probe_failed"), chassis_type in _LINUX_LAPTOP_CHASSIS_TYPES


def _memory_bytes(system: str) -> int:
    if system == "Darwin":
        try:
            value = int(_command_output(("sysctl", "-n", "hw.memsize")))
        except ValueError as exc:
            raise LaptopInventoryError("hardware_probe_failed") from exc
    else:
        try:
            value = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError) as exc:
            raise LaptopInventoryError("hardware_probe_failed") from exc
    if value <= 0:
        _reject("hardware_probe_failed")
    return value


def _backends(system: str, architecture: str) -> tuple[str, ...]:
    found: list[str] = []
    if (
        system == "Darwin"
        and architecture == "arm64"
        and importlib.util.find_spec("mlx") is not None
    ):
        found.append("mlx")
    if importlib.util.find_spec("numpy") is not None:
        found.append("numpy")
    if not found:
        _reject("runtime_backend_unavailable")
    return tuple(found)


def collect_local_laptop_observation(*, run_id: str, node_id: str) -> dict[str, Any]:
    """Measure this host and emit one privacy-scoped M7 laptop observation."""

    system = platform_module.system()
    architecture = platform_module.machine()
    if system == "Darwin":
        machine_model, is_laptop = _darwin_model()
        version = platform_module.mac_ver()[0]
        platform_name = f"macOS-{version}"
    elif system == "Linux":
        machine_model, is_laptop = _linux_model()
        platform_name = f"Linux-{platform_module.release()}"
    else:
        _reject("platform_unsupported")

    backends = _backends(system, architecture)
    precisions = ("float16", "float32")
    facts = LaptopFacts(
        host_name=_text(platform_module.node(), "host_name_invalid"),
        machine_model=machine_model,
        platform=platform_name,
        architecture=architecture,
        memory_bytes=_memory_bytes(system),
        available_storage_bytes=shutil.disk_usage(Path.home()).free,
        backends=backends,
        precisions=precisions,
        python_version=platform_module.python_version(),
        is_laptop=is_laptop,
    )
    host_id, boot_id = derive_local_run_scoped_identity(run_id)
    return build_laptop_observation(
        run_id=run_id,
        node_id=node_id,
        host_id=host_id,
        boot_id=boot_id,
        observed_at_unix_seconds=int(time.time()),
        facts=facts,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--canonical-json", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--node-id")
    try:
        arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if not arguments.canonical_json:
            return 2
        observation = collect_local_laptop_observation(
            run_id=arguments.run_id,
            node_id=arguments.node_id,
        )
    except (LaptopInventoryError, TypeError, ValueError):
        return 2
    sys.stdout.write(json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LaptopFacts",
    "LaptopInventoryError",
    "OBSERVATION_PROTOCOL",
    "VERIFICATION_PROTOCOL",
    "bind_verified_laptops_to_physical_inventory",
    "build_laptop_observation",
    "collect_local_laptop_observation",
    "main",
    "verify_laptop_inventory",
]
