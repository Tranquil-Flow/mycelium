# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic launchd/systemd packaging for durable Mycelium processes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes


PROTOCOL = "mycelium.service_package.v1"
ROLES = frozenset({"artifact_source", "seed", "node", "supervisor"})
_ID = re.compile(r"^[a-z][a-z0-9.-]{0,126}[a-z0-9]$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_ENV = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PRIVATE|CREDENTIAL|API_KEY)")


class ServicePackageError(ValueError):
    pass


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _absolute(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ServicePackageError(code)
    path = Path(value)
    if not path.is_absolute() or str(path) != os.path.abspath(value):
        raise ServicePackageError(code)
    return value


def _argv(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) == 0
        or any(not isinstance(item, str) or not item or "\x00" in item for item in value)
    ):
        raise ServicePackageError("service_argv_invalid")
    result = tuple(value)
    _absolute(result[0], "service_executable_invalid")
    return result


def validate_service_config(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "protocol",
        "package_version",
        "service_id",
        "role",
        "argv",
        "working_directory",
        "state_directory",
        "log_directory",
        "environment",
        "health_url",
        "restart_limit",
        "restart_window_seconds",
        "restart_delay_seconds",
        "stop_timeout_seconds",
    }
    if set(value) != fields or value.get("protocol") != PROTOCOL:
        raise ServicePackageError("service_config_shape_invalid")
    service_id = value["service_id"]
    role = value["role"]
    version = value["package_version"]
    if (
        not isinstance(service_id, str)
        or _ID.fullmatch(service_id) is None
        or role not in ROLES
        or not isinstance(version, str)
        or _ID.fullmatch(version) is None
    ):
        raise ServicePackageError("service_identity_invalid")
    environment = value["environment"]
    if not isinstance(environment, Mapping):
        raise ServicePackageError("service_environment_invalid")
    normalized_environment: dict[str, str] = {}
    for key, item in sorted(environment.items()):
        if (
            not isinstance(key, str)
            or _ENV.fullmatch(key) is None
            or _SECRET_ENV.search(key)
            or not isinstance(item, str)
            or "\x00" in item
            or "\n" in item
        ):
            raise ServicePackageError("service_environment_invalid")
        normalized_environment[key] = item
    health_url = value["health_url"]
    if (
        health_url is not None
        and (
            not isinstance(health_url, str)
            or not health_url.startswith("http://127.0.0.1:")
            or "\n" in health_url
        )
    ):
        raise ServicePackageError("service_health_url_invalid")
    limits: dict[str, int | float] = {}
    for field in (
        "restart_limit",
        "restart_window_seconds",
        "restart_delay_seconds",
        "stop_timeout_seconds",
    ):
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item <= 0:
            raise ServicePackageError("service_restart_policy_invalid")
        limits[field] = item
    if not isinstance(limits["restart_limit"], int):
        raise ServicePackageError("service_restart_policy_invalid")
    normalized = {
        "protocol": PROTOCOL,
        "package_version": version,
        "service_id": service_id,
        "role": role,
        "argv": list(_argv(value["argv"])),
        "working_directory": _absolute(value["working_directory"], "service_working_directory_invalid"),
        "state_directory": _absolute(value["state_directory"], "service_state_directory_invalid"),
        "log_directory": _absolute(value["log_directory"], "service_log_directory_invalid"),
        "environment": normalized_environment,
        "health_url": health_url,
        **limits,
    }
    return json.loads(canonical_json_bytes(normalized))


def _runner_argv(config: Mapping[str, Any], config_path: Path) -> list[str]:
    return [
        str(config["argv"][0]),
        "-B",
        "-m",
        "mycelium_service_runner",
        "--config",
        str(config_path),
    ]


def _launchd(config: Mapping[str, Any], config_path: Path) -> bytes:
    label = f"org.mycelium.{config['service_id']}"
    log_directory = str(config["log_directory"])
    document = {
        "Label": label,
        "ProgramArguments": _runner_argv(config, config_path),
        "WorkingDirectory": config["working_directory"],
        "EnvironmentVariables": dict(config["environment"]),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": int(config["restart_delay_seconds"]),
        "ProcessType": "Background",
        "StandardOutPath": f"{log_directory}/{config['service_id']}.stdout.log",
        "StandardErrorPath": f"{log_directory}/{config['service_id']}.stderr.log",
        "SoftResourceLimits": {"NumberOfFiles": 4096},
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def _systemd_escape(value: str) -> str:
    return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""


def _systemd_path(value: str) -> str:
    """Encode one absolute path without relying on directive-specific quoting."""

    safe = frozenset(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789/_.:+@=-"
    )
    encoded: list[str] = []
    for character in value:
        if character in safe:
            encoded.append(character)
        elif character == "%":
            encoded.append("%%")
        else:
            encoded.extend(f"\\x{byte:02x}" for byte in character.encode("utf-8"))
    return "".join(encoded)


def _systemd(config: Mapping[str, Any], config_path: Path) -> bytes:
    environment = "\n".join(
        f"Environment={_systemd_escape(f'{key}={value}')}"
        for key, value in config["environment"].items()
    )
    command = " ".join(
        _systemd_escape(item) for item in _runner_argv(config, config_path)
    )
    lines = [
        "[Unit]",
        f"Description=Mycelium {config['role']} ({config['service_id']})",
        "After=network-online.target",
        "Wants=network-online.target",
        f"StartLimitIntervalSec={config['restart_window_seconds']}",
        f"StartLimitBurst={config['restart_limit']}",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={_systemd_path(config['working_directory'])}",
        environment,
        f"ExecStart={command}",
        "Restart=on-failure",
        f"RestartSec={config['restart_delay_seconds']}",
        "KillSignal=SIGTERM",
        f"TimeoutStopSec={config['stop_timeout_seconds']}",
        "NoNewPrivileges=true",
        "UMask=0077",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def build_service_package(
    value: Mapping[str, Any], *, package_root: Path
) -> dict[str, Any]:
    config = validate_service_config(value)
    if not package_root.is_absolute():
        raise ServicePackageError("service_package_root_invalid")
    config_path = package_root / "service-config.json"
    config_raw = canonical_json_bytes(config)
    launchd_raw = _launchd(config, config_path)
    systemd_raw = _systemd(config, config_path)
    return {
        "protocol": PROTOCOL,
        "package_version": config["package_version"],
        "service_id": config["service_id"],
        "role": config["role"],
        "health": {
            "url": config["health_url"],
            "membership_is_not_route_eligibility": True,
        },
        "lifecycle": {
            "bounded_restart": True,
            "persistent_restart_budget": True,
            "restart_budget_owner": "mycelium_service_runner",
            "graceful_signal": "SIGTERM",
            "stop_timeout_seconds": config["stop_timeout_seconds"],
            "log_rotation": "platform_journal_or_10MiB_x5_operator_policy",
            "upgrade": "install_new_version_then_health_check",
            "rollback": "restore_previous_digest_pinned_package",
        },
        "files": {
            "service-config.json": {"mode": "0600", "sha256": _digest(config_raw), "bytes": len(config_raw)},
            f"org.mycelium.{config['service_id']}.plist": {"mode": "0600", "sha256": _digest(launchd_raw), "bytes": len(launchd_raw)},
            f"mycelium-{config['service_id']}.service": {"mode": "0600", "sha256": _digest(systemd_raw), "bytes": len(systemd_raw)},
        },
        "_payloads": {
            "service-config.json": config_raw,
            f"org.mycelium.{config['service_id']}.plist": launchd_raw,
            f"mycelium-{config['service_id']}.service": systemd_raw,
        },
    }


def write_service_package(value: Mapping[str, Any], output_root: Path) -> dict[str, Any]:
    if not output_root.is_absolute():
        raise ServicePackageError("service_package_root_invalid")
    package = build_service_package(value, package_root=output_root)
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_root, 0o700)
    payloads = package.pop("_payloads")
    for name, raw in payloads.items():
        path = output_root / name
        path.write_bytes(raw)
        os.chmod(path, 0o600)
    manifest_raw = canonical_json_bytes(package)
    manifest_path = output_root / "service-package-manifest.json"
    manifest_path.write_bytes(manifest_raw)
    os.chmod(manifest_path, 0o600)
    if any(stat.S_IMODE(path.stat().st_mode) != 0o600 for path in output_root.iterdir()):
        raise ServicePackageError("service_package_mode_invalid")
    return package


__all__ = [
    "PROTOCOL",
    "ServicePackageError",
    "build_service_package",
    "validate_service_config",
    "write_service_package",
]
