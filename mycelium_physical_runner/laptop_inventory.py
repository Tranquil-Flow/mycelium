"""Fail-closed local laptop capability observations for M7 inventory."""
from __future__ import annotations

import argparse
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

from .remote_probe import derive_local_run_scoped_identity

OBSERVATION_PROTOCOL = "mycelium.laptop_inventory_observation.v1"
_HOST_ID_RE = re.compile(r"^host-[0-9a-f]{32}$")
_BOOT_ID_RE = re.compile(r"^boot-[0-9a-f]{32}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LINUX_LAPTOP_CHASSIS_TYPES = frozenset({8, 9, 10, 14, 30, 31, 32})


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
    "build_laptop_observation",
    "collect_local_laptop_observation",
    "main",
]
