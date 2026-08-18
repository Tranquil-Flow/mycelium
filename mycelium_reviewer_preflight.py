"""Privacy-reduced, read-only preflight for an invited external Mac reviewer."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from typing import Any

from mycelium_invite import verify_invite_bundle
from mycelium_node.durable_membership import load_membership_state
from mycelium_seed.http import SeedHTTPClient


PROTOCOL = "mycelium.external_reviewer_preflight.v1"


def _memory_bytes() -> int:
    if sysctl := shutil.which("sysctl"):
        completed = subprocess.run(
            [sysctl, "-n", "hw.memsize"], text=True, capture_output=True, check=False
        )
        try:
            value = int(completed.stdout.strip())
        except ValueError:
            value = 0
        if value > 0:
            return value
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(page_size)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, "sha256:" + digest.hexdigest()


def _artifact_preflight(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "required_file_count": 0,
            "verified_file_count": 0,
            "required_bytes": 0,
            "missing_bytes": 0,
            "cache_reused": False,
        }
    if (
        not isinstance(value, dict)
        or set(value) != {"files"}
        or not isinstance(value["files"], list)
    ):
        raise ValueError("reviewer_artifact_requirements_invalid")
    required = verified = required_bytes = missing_bytes = 0
    for item in value["files"]:
        if not isinstance(item, dict) or set(item) != {
            "logical_name",
            "path",
            "size_bytes",
            "sha256",
        }:
            raise ValueError("reviewer_artifact_requirements_invalid")
        if (
            not isinstance(item["logical_name"], str)
            or not item["logical_name"]
            or type(item["size_bytes"]) is not int
            or item["size_bytes"] < 0
            or not isinstance(item["sha256"], str)
        ):
            raise ValueError("reviewer_artifact_requirements_invalid")
        path = Path(item["path"])
        required += 1
        required_bytes += item["size_bytes"]
        try:
            size, digest = _digest_file(path)
        except OSError:
            missing_bytes += item["size_bytes"]
            continue
        if size == item["size_bytes"] and digest == item["sha256"]:
            verified += 1
        else:
            missing_bytes += item["size_bytes"]
    return {
        "required_file_count": required,
        "verified_file_count": verified,
        "required_bytes": required_bytes,
        "missing_bytes": missing_bytes,
        "cache_reused": required > 0 and required == verified,
    }


def reviewer_preflight(
    *,
    invite_bundle: dict[str, Any],
    state_root: Path | None,
    artifact_requirements: dict[str, Any] | None,
    required_memory_bytes: int,
    required_disk_bytes: int,
    now: float | None = None,
) -> dict[str, Any]:
    observed = time.time() if now is None else now
    verified = verify_invite_bundle(invite_bundle, now=observed)
    client = SeedHTTPClient.from_invite_bundle(invite_bundle, now=observed)
    coordinator_reachable = False
    coordinator_identity_verified = False
    coordinator_reason = "unreachable"
    try:
        client.identity(now=observed + 1.0)
        coordinator_reachable = coordinator_identity_verified = True
        coordinator_reason = "verified"
    except Exception:
        pass
    membership = (
        load_membership_state(state_root)
        if state_root is not None and state_root.exists()
        else None
    )
    memory_bytes = _memory_bytes()
    disk_root = (
        state_root if state_root is not None and state_root.exists() else Path.home()
    )
    disk_free = shutil.disk_usage(disk_root).free
    artifacts = _artifact_preflight(artifact_requirements)
    system = platform.system().lower()
    architecture = platform.machine().lower()
    supported_platform = system == "darwin" and architecture in {"arm64", "aarch64"}
    mlx_available = importlib.util.find_spec("mlx") is not None
    resources_pass = (
        memory_bytes >= required_memory_bytes and disk_free >= required_disk_bytes
    )
    eligible = (
        supported_platform
        and mlx_available
        and resources_pass
        and coordinator_identity_verified
    )
    failures: list[str] = []
    if not supported_platform:
        failures.append("supported_apple_silicon_mac_required")
    if not mlx_available:
        failures.append("mlx_runtime_missing")
    if memory_bytes < required_memory_bytes:
        failures.append("insufficient_memory")
    if disk_free < required_disk_bytes:
        failures.append("insufficient_disk")
    if not coordinator_identity_verified:
        failures.append("coordinator_unreachable_or_unverified")
    return {
        "protocol": PROTOCOL,
        "observed_at_unix_ms": int(observed * 1_000),
        "platform": {
            "os": system,
            "architecture": architecture,
            "supported": supported_platform,
        },
        "coordinator": {
            "reachable": coordinator_reachable,
            "identity_verified": coordinator_identity_verified,
            "reason": coordinator_reason,
        },
        "invitation": {
            "cryptographically_verified": True,
            "single_use": True,
            "swarm_id_digest": "sha256:"
            + hashlib.sha256(verified["payload"]["swarm_id"].encode()).hexdigest(),
        },
        "identity": {
            "state": "resumable" if membership is not None else "new",
            "generation": None
            if membership is None
            else membership["membership_generation"],
            "duplicate_principal_created": False,
        },
        "resources": {
            "memory_bytes": memory_bytes,
            "disk_free_bytes": disk_free,
            "required_memory_bytes": required_memory_bytes,
            "required_disk_bytes": required_disk_bytes,
            "pass": resources_pass,
        },
        "runtime": {
            "peer_class": "mac_mlx_iroh",
            "backend": "mlx",
            "available": mlx_available,
            "model_adapters": ["qwen2", "qwen3"],
        },
        "artifacts": artifacts,
        "qualification": {
            "membership_ready": coordinator_identity_verified,
            "activation_eligible": eligible,
            "route_qualified": False,
            "reason": "preflight_only_qualification_required"
            if eligible
            else "preflight_failed",
        },
        "failures": failures,
        "state_mutated": False,
        "privacy": "no invite token, endpoint id, coordinator url, private path, username, or credential",
    }


__all__ = ["PROTOCOL", "reviewer_preflight"]
