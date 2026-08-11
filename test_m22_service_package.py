from __future__ import annotations

import json
from pathlib import Path
import plistlib
import stat

import pytest

from mycelium_service_package import (
    ServicePackageError,
    build_service_package,
    validate_service_config,
    write_service_package,
)


def config() -> dict[str, object]:
    return {
        "protocol": "mycelium.service_package.v1",
        "package_version": "m22-1",
        "service_id": "node-a",
        "role": "node",
        "argv": ["/usr/bin/python3", "-m", "mycelium_node", "--data-dir", "/var/lib/mycelium/node-a"],
        "working_directory": "/opt/mycelium/current",
        "state_directory": "/var/lib/mycelium/node-a",
        "log_directory": "/var/log/mycelium",
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "health_url": "http://127.0.0.1:8791/__mycelium/status",
        "restart_limit": 5,
        "restart_window_seconds": 300,
        "restart_delay_seconds": 5,
        "stop_timeout_seconds": 45,
    }


def test_service_package_is_deterministic_bounded_and_shell_free() -> None:
    package_root = Path("/opt/mycelium/packages/node-a")
    first = build_service_package(config(), package_root=package_root)
    second = build_service_package(config(), package_root=package_root)
    assert first == second
    plist = plistlib.loads(first["_payloads"]["org.mycelium.node-a.plist"])
    assert plist["ProgramArguments"] == [
        "/usr/bin/python3",
        "-B",
        "-m",
        "mycelium_service_runner",
        "--config",
        "/opt/mycelium/packages/node-a/service-config.json",
    ]
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    unit = first["_payloads"]["mycelium-node-a.service"].decode("utf-8")
    assert "mycelium_service_runner" in unit
    assert "Restart=on-failure" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "NoNewPrivileges=true" in unit
    assert "UMask=0077" in unit
    assert "PrivateTmp=true" not in unit
    assert "ProtectSystem=" not in unit
    assert "membership_is_not_route_eligibility" not in unit
    assert first["health"]["membership_is_not_route_eligibility"] is True
    assert first["lifecycle"]["persistent_restart_budget"] is True


def test_systemd_paths_use_directive_safe_unquoted_encoding() -> None:
    value = config()
    value["working_directory"] = "/opt/Mycelium Release/%current"
    value["state_directory"] = "/var/lib/mycelium/node ü"
    package = build_service_package(
        value,
        package_root=Path("/opt/mycelium/packages/node-a"),
    )
    unit = package["_payloads"]["mycelium-node-a.service"].decode("utf-8")
    assert "WorkingDirectory=/opt/Mycelium\\x20Release/%%current" in unit
    assert 'WorkingDirectory="' not in unit


def test_written_package_is_owner_only_and_manifest_matches(tmp_path: Path) -> None:
    root = (tmp_path / "package").resolve()
    result = write_service_package(config(), root)
    paths = tuple(root.iterdir())
    assert len(paths) == 4
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    manifest = json.loads((root / "service-package-manifest.json").read_text())
    assert manifest == result
    assert "_payloads" not in manifest


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("argv", ["python3", "-m", "mycelium_node"], "service_executable_invalid"),
        ("role", "coordinator", "service_identity_invalid"),
        ("environment", {"API_KEY": "secret"}, "service_environment_invalid"),
        ("health_url", "http://10.0.0.2:8791/status", "service_health_url_invalid"),
        ("restart_limit", 0, "service_restart_policy_invalid"),
    ],
)
def test_service_config_rejects_unsafe_or_unbounded_values(
    field: str, value: object, code: str
) -> None:
    invalid = config()
    invalid[field] = value
    with pytest.raises(ServicePackageError, match=code):
        validate_service_config(invalid)
