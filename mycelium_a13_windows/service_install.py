# SPDX-License-Identifier: AGPL-3.0-only
"""Register the Windows managed host as an unattended per-user service.

  python -m mycelium_a13_windows.service_install --package-root <dir> render \\
      --host-root <dir> --policy policy.json --limits limits.json --preview preview.json \\
      --acknowledge-unattended
  python -m mycelium_a13_windows.service_install --package-root <dir> register|start|stop|status|unregister

Design (gap 7, WINDOWS-PARITY.md):

* **Mechanism: per-user Task Scheduler logon task**, rendered by the shared
  ``mycelium_service_package.build_windows_member_package`` (Task Scheduler 1.2
  XML, ``InteractiveToken``, ``LeastPrivilege``, one instance, bounded restart).
  It runs ``mycelium_service_runner`` (Job Object child, named stop event,
  persistent restart budget), which runs the managed host's ``run`` command.
* **The SCM per-service mode is refused** (``windows_service_scm_mode_unsupported``):
  a plain ``python.exe`` implements no SCM control handler, and a restricted
  ``NT SERVICE\\<name>`` virtual account is a different SID from the user who
  paired the host, so it cannot open the owner-only host state (identity key,
  membership) that ``mycelium_host_platform`` protects.  Supporting it means
  pairing *as* the service account; that is a design change, not a flag.
* **Consent is never broadened by unattended operation.**  Registration
  requires ``--acknowledge-unattended`` and binds the exact previewed work
  (``preview.json``, policy and limits are copied into the owner-only package
  directory).  Every restart re-runs ``accept_work`` against that preview, so a
  stale or changed offer is refused and the runner's restart budget ends the
  loop; nothing re-previews or accepts new work by itself.
* **Stop** signals the runner's named stop event (``mycelium_service_runner
  --stop``); the runner forwards it to the host, which drains exactly like
  Ctrl+C and keeps staging.  ``schtasks /End`` is the fallback.

Every external command goes through an injectable ``runner`` so the plan can be
tested without Windows.  Nothing here elevates, opens a listener, or stores a
secret: the service config refuses secret-bearing environment names.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
from typing import Any

import mycelium_host_platform

from mycelium_service_package import (
    PROTOCOL as SERVICE_PACKAGE_PROTOCOL,
    ServicePackageError,
    build_windows_member_package,
)

PROTOCOL = "mycelium.a13_windows_service_install.v1"
SERVICE_ID = "mycelium-managed-host"
INSTALL_MODE = "windows_service_per_user"
_BOUND_FILES = ("policy.json", "limits.json", "preview.json")
_PLAN_FILE = f"mycelium-{SERVICE_ID}.windows-member-plan.json"
#: Environment the host may carry into the service (non-secret by construction).
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


def signed_in_principal(environ: Any = None) -> str:
    """``DOMAIN\\user`` of the signed-in user (validated by the renderer)."""

    source = os.environ if environ is None else environ
    domain, user = source.get("USERDOMAIN"), source.get("USERNAME")
    if not domain or not user:
        raise ServiceInstallError("windows_principal_unavailable")
    return f"{domain}\\{user}"


def service_config(*, package_root: str, host_root: str, python_executable: str,
                   environment: dict[str, str] | None = None) -> dict[str, Any]:
    """The shared service config that runs the managed host's ``run`` command."""

    package = PureWindowsPath(package_root)
    argv = [
        python_executable, "-m", "mycelium_a13_windows.managed_host", "--root", host_root, "run",
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
           preview: bytes, acknowledged_unattended: bool, principal: str,
           python_executable: str | None = None, install_mode: str = INSTALL_MODE,
           environ: Any = None, windows_package_root: str | None = None) -> dict[str, Any]:
    """Write the bound consent files and the rendered member into an owner-only directory.

    ``windows_package_root`` is the same directory as the Windows path the plan
    names (defaults to ``package_root``; tests on a POSIX host pass both).
    """

    if install_mode != INSTALL_MODE:
        raise ServiceInstallError("windows_service_scm_mode_unsupported")
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
    root = windows_package_root or str(Path(package_root))
    config = service_config(package_root=root, host_root=host_root,
                            python_executable=python_executable or sys.executable,
                            environment=environment)
    try:
        package = build_windows_member_package(config, package_root=root,
                                               install_mode=install_mode, principal=principal)
    except ServicePackageError as exc:
        raise ServiceInstallError(str(exc)) from None
    with _lease(Path(package_root), create=True) as lease:
        for name, raw in (("policy.json", policy), ("limits.json", limits), ("preview.json", preview)):
            lease.atomic_replace(name, raw)
        for name, raw in sorted(package["_payloads"].items()):
            lease.atomic_replace(name, raw)
    return {"protocol": PROTOCOL, "package_root": root, "plan": package["plan"],
            "files": sorted([*_BOUND_FILES, *package["_payloads"]]), "executed": False}


def load_plan(package_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read the rendered plan back through the owner-only check."""

    with _lease(Path(package_root), create=False) as lease:
        try:
            plan = json.loads(lease.read_file(_PLAN_FILE, max_bytes=64 * 1024))
            for name in _BOUND_FILES:
                lease.read_file(name, max_bytes=4 * 1024 * 1024)
        except mycelium_host_platform.HostPlatformError as exc:
            raise ServiceInstallError("service_package_incomplete") from exc
    if plan.get("install_mode") != INSTALL_MODE or plan.get("known_blockers"):
        raise ServiceInstallError("service_plan_invalid")
    return plan


def execute(action: str, package_root: str | os.PathLike[str], *, runner: Runner = _default_runner) -> dict[str, Any]:
    """Run one lifecycle action of the rendered plan; never anything else."""

    plan = load_plan(package_root)
    commands: list[list[str]]
    if action == "register":
        commands = [plan["install_argv"]]
    elif action == "start":
        commands = [plan["start_argv"]]
    elif action == "stop":
        commands = [plan["stop_argv"]]
    elif action == "status":
        commands = [["schtasks.exe", "/Query", "/TN", plan["task_name"], "/V", "/FO", "LIST"]]
    elif action == "unregister":
        commands = [plan["stop_argv"], plan["uninstall_argv"]]
    else:
        raise ServiceInstallError("service_action_invalid")
    results: list[dict[str, Any]] = []

    def run(argv: Sequence[str]) -> int:
        code = runner(list(argv))
        results.append({"argv": list(argv), "exit_code": code})
        return code

    if action == "stop":
        # Graceful first (named stop event -> drain); schtasks /End only if the
        # runner no longer answers (its kill-on-close job then ends the host).
        if run(plan["stop_argv"]) != 0 and run(plan["stop_fallback_argv"]) != 0:
            raise ServiceInstallError("service_stop_failed")
    elif action == "unregister":
        run(plan["stop_argv"])  # nothing running is fine before removal
        if run(plan["uninstall_argv"]) != 0:
            raise ServiceInstallError("service_unregister_failed")
    else:
        for argv in commands:
            if run(argv) != 0:
                raise ServiceInstallError(f"service_{action}_failed")
    return {"protocol": PROTOCOL, "action": action, "task_name": plan["task_name"], "commands": results}


def main(argv: list[str] | None = None, *, runner: Runner = _default_runner) -> int:
    parser = argparse.ArgumentParser(prog="python -m mycelium_a13_windows.service_install",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--package-root", required=True, help="owner-only directory for the service package")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("render")
    p.add_argument("--host-root", required=True)
    for name in ("--policy", "--limits", "--preview"):
        p.add_argument(name, required=True)
    p.add_argument("--acknowledge-unattended", action="store_true")
    p.add_argument("--mode", default=INSTALL_MODE, choices=[INSTALL_MODE, "windows_service"])
    for name in ("register", "start", "stop", "status", "unregister"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    try:
        if args.action == "render":
            result = render(package_root=args.package_root, host_root=args.host_root,
                            policy=Path(args.policy).read_bytes(), limits=Path(args.limits).read_bytes(),
                            preview=Path(args.preview).read_bytes(),
                            acknowledged_unattended=args.acknowledge_unattended,
                            principal=signed_in_principal(), install_mode=args.mode)
        else:
            result = execute(args.action, args.package_root, runner=runner)
    except ServiceInstallError as exc:
        print(json.dumps({"protocol": PROTOCOL, "refused": exc.code}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
