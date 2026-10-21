# SPDX-License-Identifier: AGPL-3.0-only
"""Run the Linux managed host unattended as a hardened systemd *user* unit.

  mycelium -m ... (frozen) / python -m mycelium_a13_linux.service_install --package-root <dir> render \\
      --host-root <dir> --policy policy.json --limits limits.json --preview preview.json \\
      --acknowledge-unattended
  ... --package-root <dir> register|start|stop|status|unregister
  ... --package-root <dir> enable-linger --acknowledge-linger | disable-linger

The Linux twin of ``mycelium_a13_windows.service_install``:

* the unit comes from the shared renderer (``mycelium_service_package``) in its
  **user-unit** form (no ``network-online.target``, which only the system
  manager has), hardened and validated by ``service_lifecycle``; it runs
  ``mycelium_service_runner`` (bounded restart budget) which runs the host's
  ``run`` command;
* registration needs ``--acknowledge-unattended`` and binds the exact previewed
  work (policy, limits and preview are copied into the owner-only package
  directory); a restart re-runs ``accept_work`` against that preview, so stale
  work is refused and never re-previewed;
* everything is ``systemctl --user``: no root, no system unit, no polkit;
* **linger is a separate, explicit opt-in** (``enable-linger
  --acknowledge-linger``): only with linger does the unit run with nobody
  logged in.  The package records that *this* tool enabled it, so uninstall
  disables it only then -- never a linger the user had set themselves.

Every command goes through an injectable ``runner``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import mycelium_host_platform

from mycelium_service_package import PROTOCOL as SERVICE_PACKAGE_PROTOCOL

from .service_lifecycle import ServiceLifecycleError, build_linux_service_package

PROTOCOL = "mycelium.a13_linux_service_install.v1"
SERVICE_ID = "managed-host"
UNIT_NAME = f"mycelium-{SERVICE_ID}.service"
HOST_MODULE = "mycelium_a13_linux.managed_host"
_BOUND_FILES = ("policy.json", "limits.json", "preview.json")
_LINGER_RECORD = "linger.json"
_PASSTHROUGH_ENVIRONMENT = (
    "MYCELIUM_A9_INT8_PROVIDER_LIBRARY",
    "MYCELIUM_A9_INT8_PROVIDER_DIGEST",
    "MYCELIUM_A9_INT8_PROVIDER_THREADS",
    "MYCELIUM_A9_INT8_PROVIDER_REPORT",
    "MYCELIUM_A13_RUN_MODEL",
)

Runner = Callable[[Sequence[str]], int]


class ServiceInstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _default_runner(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def _config_home(environ: Any = None) -> Path:
    source = os.environ if environ is None else environ
    value = source.get("XDG_CONFIG_HOME")
    return Path(value) if value else Path.home() / ".config"


def default_package_root(environ: Any = None) -> Path:
    return _config_home(environ) / "mycelium" / "service"


def default_unit_directory(environ: Any = None) -> Path:
    return _config_home(environ) / "systemd" / "user"


def service_config(*, package_root: str, host_root: str, app_executable: str,
                   environment: dict[str, str] | None = None) -> dict[str, Any]:
    package = Path(package_root)
    argv = [
        app_executable, "-m", HOST_MODULE, "--root", host_root, "run",
        "--policy", str(package / "policy.json"),
        "--limits", str(package / "limits.json"),
        "--preview", str(package / "preview.json"),
        "--acknowledge",
    ]
    return {
        "protocol": SERVICE_PACKAGE_PROTOCOL,
        "package_version": "v0-1-0",
        "service_id": SERVICE_ID,
        "role": "node",
        "argv": argv,
        "working_directory": host_root,
        "state_directory": str(package / "state"),
        "log_directory": str(package / "logs"),
        "environment": dict(environment or {}),
        "health_url": None,
        "restart_limit": 5,
        "restart_window_seconds": 600,
        "restart_delay_seconds": 60,
        "stop_timeout_seconds": 30,
    }


def _lease(package_root: Path, *, create: bool):
    try:
        return mycelium_host_platform.private_directory(package_root, create=create)
    except mycelium_host_platform.HostPlatformError as exc:
        raise ServiceInstallError("service_package_root_unsafe") from exc


def render(*, package_root: str | os.PathLike[str], host_root: str, policy: bytes, limits: bytes,
           preview: bytes, acknowledged_unattended: bool, app_executable: str | None = None,
           environ: Any = None) -> dict[str, Any]:
    """Bind the previewed work and render the hardened user unit into an owner-only package."""

    if acknowledged_unattended is not True:
        raise ServiceInstallError("unattended_acknowledgement_required")
    for raw in (policy, limits, preview):
        try:
            if not isinstance(json.loads(raw), dict):
                raise ValueError
        except ValueError as exc:
            raise ServiceInstallError("bound_consent_invalid") from exc
    source = os.environ if environ is None else environ
    environment = {key: source[key] for key in _PASSTHROUGH_ENVIRONMENT if source.get(key)}
    root = Path(package_root)
    config = service_config(package_root=str(root), host_root=host_root,
                            app_executable=app_executable or sys.executable, environment=environment)
    try:
        package = build_linux_service_package(config, package_root=root, install_mode="systemd_user")
    except ServiceLifecycleError as exc:
        raise ServiceInstallError(exc.code) from None
    unit = package["systemd_unit"]
    if unit["name"] != UNIT_NAME or "network-online.target" in unit["text"]:
        raise ServiceInstallError("service_unit_not_a_user_unit")
    from mycelium_qualification.evidence import canonical_json_bytes
    from mycelium_service_package import validate_service_config

    with _lease(root, create=True) as lease:
        for name, raw in (("policy.json", policy), ("limits.json", limits), ("preview.json", preview)):
            lease.atomic_replace(name, raw)
        lease.atomic_replace("service-config.json", canonical_json_bytes(validate_service_config(config)))
        lease.atomic_replace(UNIT_NAME, unit["text"].encode("utf-8"))
    return {"protocol": PROTOCOL, "package_root": str(root), "unit": UNIT_NAME, "unit_sha256": unit["sha256"],
            "hardening": package["hardening"], "files": sorted([*_BOUND_FILES, "service-config.json", UNIT_NAME]),
            "executed": False}


def _read_package(package_root: Path) -> bytes:
    with _lease(package_root, create=False) as lease:
        try:
            for name in (*_BOUND_FILES, "service-config.json"):
                lease.read_file(name, max_bytes=4 * 1024 * 1024)
            return lease.read_file(UNIT_NAME, max_bytes=64 * 1024)
        except mycelium_host_platform.HostPlatformError as exc:
            raise ServiceInstallError("service_package_incomplete") from exc


def _linger_record(package_root: Path) -> dict[str, Any] | None:
    try:
        with mycelium_host_platform.private_directory(package_root, create=False) as lease:
            return json.loads(lease.read_file(_LINGER_RECORD, max_bytes=4096))
    except mycelium_host_platform.HostPlatformError:
        return None


def execute(action: str, package_root: str | os.PathLike[str], *, unit_directory: str | os.PathLike[str] | None = None,
            runner: Runner = _default_runner, user: str | None = None,
            acknowledged_linger: bool = False) -> dict[str, Any]:
    """Run one lifecycle action; only ``systemctl --user`` and ``loginctl`` for this user."""

    root = Path(package_root)
    units = Path(unit_directory) if unit_directory is not None else default_unit_directory()
    results: list[dict[str, Any]] = []

    def run(argv: list[str]) -> int:
        code = runner(argv)
        results.append({"argv": argv, "exit_code": code})
        return code

    def systemctl(*arguments: str) -> int:
        return run(["systemctl", "--user", *arguments])

    login = user or os.environ.get("USER") or ""
    if action == "register":
        unit = _read_package(root)
        units.mkdir(parents=True, exist_ok=True)
        temporary = units / (UNIT_NAME + ".tmp")
        temporary.write_bytes(unit)
        os.chmod(temporary, 0o600)
        os.replace(temporary, units / UNIT_NAME)
        if systemctl("daemon-reload") != 0 or systemctl("enable", UNIT_NAME) != 0:
            raise ServiceInstallError("service_register_failed")
    elif action in {"start", "stop"}:
        _read_package(root)
        if systemctl(action, UNIT_NAME) != 0:
            raise ServiceInstallError(f"service_{action}_failed")
    elif action == "status":
        if systemctl("status", "--no-pager", UNIT_NAME) not in (0, 3):  # 3 = inactive, still a status
            raise ServiceInstallError("service_status_failed")
    elif action == "unregister":
        systemctl("disable", "--now", UNIT_NAME)  # nothing running is fine before removal
        (units / UNIT_NAME).unlink(missing_ok=True)
        if systemctl("daemon-reload") != 0:
            raise ServiceInstallError("service_unregister_failed")
        record = _linger_record(root)
        if record and record.get("enabled_by_installer") is True and record.get("user") == login:
            run(["loginctl", "disable-linger", login])
            with _lease(root, create=False) as lease:
                lease.atomic_replace(_LINGER_RECORD, json.dumps({"enabled_by_installer": False, "user": login}).encode())
    elif action == "enable-linger":
        if acknowledged_linger is not True:
            raise ServiceInstallError("linger_acknowledgement_required")
        if not login:
            raise ServiceInstallError("linger_user_unavailable")
        _read_package(root)
        if run(["loginctl", "enable-linger", login]) != 0:
            raise ServiceInstallError("linger_enable_failed")  # e.g. polkit asked; never escalate
        with _lease(root, create=False) as lease:
            lease.atomic_replace(_LINGER_RECORD, json.dumps({"enabled_by_installer": True, "user": login}).encode())
    elif action == "disable-linger":
        record = _linger_record(root)
        if not (record and record.get("enabled_by_installer") is True and record.get("user") == login):
            raise ServiceInstallError("linger_not_enabled_by_installer")
        if run(["loginctl", "disable-linger", login]) != 0:
            raise ServiceInstallError("linger_disable_failed")
        with _lease(root, create=False) as lease:
            lease.atomic_replace(_LINGER_RECORD, json.dumps({"enabled_by_installer": False, "user": login}).encode())
    else:
        raise ServiceInstallError("service_action_invalid")
    return {"protocol": PROTOCOL, "action": action, "unit": UNIT_NAME, "commands": results}


def main(argv: list[str] | None = None, *, runner: Runner = _default_runner) -> int:
    parser = argparse.ArgumentParser(prog="mycelium-service", description=__doc__.split("\n\n")[0])
    parser.add_argument("--package-root", type=Path, default=None)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("render")
    p.add_argument("--host-root", required=True)
    for name in ("--policy", "--limits", "--preview"):
        p.add_argument(name, required=True)
    p.add_argument("--acknowledge-unattended", action="store_true")
    for name in ("register", "start", "stop", "status", "unregister", "disable-linger"):
        sub.add_parser(name)
    p = sub.add_parser("enable-linger")
    p.add_argument("--acknowledge-linger", action="store_true")
    args = parser.parse_args(argv)
    root = args.package_root or default_package_root()
    try:
        if args.action == "render":
            result = render(package_root=root, host_root=args.host_root,
                            policy=Path(args.policy).read_bytes(), limits=Path(args.limits).read_bytes(),
                            preview=Path(args.preview).read_bytes(),
                            acknowledged_unattended=args.acknowledge_unattended)
        else:
            result = execute(args.action, root, runner=runner,
                             acknowledged_linger=getattr(args, "acknowledge_linger", False))
    except ServiceInstallError as exc:
        print(json.dumps({"protocol": PROTOCOL, "refused": exc.code}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
