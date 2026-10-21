# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic launchd/systemd/Windows-SCM packaging for durable Mycelium processes.

The Windows SCM descriptor is opt-in (``include_windows_member=True``): every
existing caller of :func:`build_service_package`/:func:`write_service_package`
(the M22 service package and the A13 Linux tranche) gets exactly the same
launchd+systemd payload set it always has. Only the A13 Windows tranche
(``mycelium_a13_windows.service_lifecycle``) asks for the Windows member.

:func:`build_windows_member_package` separately renders Windows-native members
a Windows host can consume (a Task Scheduler logon task for the per-user mode,
an ``sc.exe`` argv plan for the per-service mode) from Windows drive-absolute
paths.  It is pure rendering: nothing is executed, registered or signed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import plistlib
import re
import stat
import subprocess
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


def _posix_absolute(value: Any, code: str) -> str:
    """``_absolute`` for the launchd/systemd members, judged identically on every host.

    Those members always run on a POSIX host, so a Windows build host (the A13
    Windows ``finalize`` step) must not judge their paths with ``ntpath``.  On a
    POSIX host this is exactly ``_absolute``.
    """

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ServicePackageError(code)
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != posixpath.abspath(value):
        raise ServicePackageError(code)
    return value


def _argv(value: Any, *, absolute: Any = None) -> tuple[str, ...]:
    check = _absolute if absolute is None else absolute
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) == 0
        or any(not isinstance(item, str) or not item or "\x00" in item for item in value)
    ):
        raise ServicePackageError("service_argv_invalid")
    result = tuple(value)
    check(result[0], "service_executable_invalid")
    return result


def validate_service_config(value: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_config(value, absolute=_absolute)


def _validate_config(value: Mapping[str, Any], *, absolute: Any) -> dict[str, Any]:
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
        "argv": list(_argv(value["argv"], absolute=absolute)),
        "working_directory": absolute(value["working_directory"], "service_working_directory_invalid"),
        "state_directory": absolute(value["state_directory"], "service_state_directory_invalid"),
        "log_directory": absolute(value["log_directory"], "service_log_directory_invalid"),
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


def _systemd(config: Mapping[str, Any], config_path: Path, *, user_unit: bool = False) -> bytes:
    """One hardened unit.  ``user_unit`` renders for the per-user manager
    (``systemctl --user``): ``network-online.target`` is a system-manager target
    a user manager does not have, so a user unit must not order on it."""
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
        *(() if user_unit else ("After=network-online.target", "Wants=network-online.target")),
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


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _windows_service_xml(
    config: Mapping[str, Any], config_path: Path, *, install_mode: str
) -> bytes:
    """Declarative Windows SCM service descriptor -- never calls sc.exe/New-Service.

    ``windows_service`` installs a least-privilege per-service virtual account
    (``NT SERVICE\\<name>``); ``windows_service_per_user`` runs in the signed-in
    user's own session with no elevation. Both are ``SERVICE_WIN32_OWN_PROCESS``,
    carry a ``RestrictedToken``, open no public listener, and the environment is
    whatever the caller supplied to :func:`validate_service_config` -- already
    checked there for secret-bearing names.
    """
    if install_mode not in ("windows_service", "windows_service_per_user"):
        raise ServicePackageError("service_windows_install_mode_invalid")
    service_name = f"mycelium-{config['service_id']}"
    account = (
        f"NT SERVICE\\{service_name}"
        if install_mode == "windows_service"
        else "interactive_per_user_session"
    )
    argv = _runner_argv(config, config_path)
    environment_lines = "".join(
        f'    <Environment name="{_xml_escape(key)}">{_xml_escape(value)}</Environment>\n'
        for key, value in config["environment"].items()
    )
    argv_lines = "".join(
        f"    <Argument>{_xml_escape(item)}</Argument>\n" for item in argv
    )
    document = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<MyceliumWindowsServiceDescriptor version="1">\n'
        f"  <ServiceName>{_xml_escape(service_name)}</ServiceName>\n"
        f"  <DisplayName>Mycelium {_xml_escape(config['role'])} ({_xml_escape(config['service_id'])})</DisplayName>\n"
        f"  <InstallMode>{install_mode}</InstallMode>\n"
        "  <ServiceType>SERVICE_WIN32_OWN_PROCESS</ServiceType>\n"
        "  <StartType>SERVICE_DEMAND_START</StartType>\n"
        f"  <ObjectName>{_xml_escape(account)}</ObjectName>\n"
        "  <RestrictedToken>true</RestrictedToken>\n"
        "  <PublicListener>false</PublicListener>\n"
        "  <Argv>\n" + argv_lines + "  </Argv>\n"
        f"  <WorkingDirectory>{_xml_escape(config['working_directory'])}</WorkingDirectory>\n"
        f"  <RestartLimit>{int(config['restart_limit'])}</RestartLimit>\n"
        f"  <RestartDelaySeconds>{int(config['restart_delay_seconds'])}</RestartDelaySeconds>\n"
        f"  <StopTimeoutSeconds>{int(config['stop_timeout_seconds'])}</StopTimeoutSeconds>\n"
        "  <Environment>\n" + environment_lines + "  </Environment>\n"
        "</MyceliumWindowsServiceDescriptor>\n"
    )
    return document.encode("utf-8")


#: Windows SCM member names, keyed by the install mode they declare.
WINDOWS_MEMBER_NAMES: dict[str, str] = {
    "windows_service": "windows-service.xml",
    "windows_service_per_user": "windows-service-per-user.xml",
}


# ------------------------------------------------ Windows-native member plans
#
# ``_windows_service_xml`` above is a declarative descriptor that nothing on a
# Windows host consumes, and it is rendered from a POSIX-path config.  The
# functions below render members a Windows host CAN consume -- a Task
# Scheduler 1.2 task definition for the per-user mode and an ``sc.exe`` argv
# plan for the per-service mode -- from a config whose paths are validated as
# Windows drive-absolute paths on every host (``PureWindowsPath``), so the
# rendering is byte-identical on macOS, Linux and Windows.  Pure rendering:
# nothing here runs ``schtasks.exe``, ``sc.exe`` or anything else.

WINDOWS_MEMBER_PLAN_PROTOCOL = "mycelium.windows_member_plan.v1"
WINDOWS_INSTALL_MODES: tuple[str, ...] = ("windows_service", "windows_service_per_user")
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TASK_FOLDER = "\\Mycelium\\"
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:\\")
_WINDOWS_FORBIDDEN_PATH_CHARACTERS = frozenset('"<>|?*\n\r\t\x00')
_WINDOWS_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,62}\\[A-Za-z0-9][A-Za-z0-9._ $-]{0,63}$")
_WINDOWS_NON_USER_DOMAINS = ("NT AUTHORITY\\", "BUILTIN\\", "NT SERVICE\\", "NT VIRTUAL MACHINE\\")
#: The only privilege the per-service member keeps (``sc.exe privs``).
WINDOWS_SERVICE_PRIVILEGES: tuple[str, ...] = ("SeChangeNotifyPrivilege",)


def _windows_absolute(value: Any, code: str) -> str:
    """A Windows drive-absolute path, judged identically on every host."""

    if (
        not isinstance(value, str)
        or not value
        or _WINDOWS_DRIVE_PATH.match(value) is None
        or any(character in _WINDOWS_FORBIDDEN_PATH_CHARACTERS for character in value)
        or ":" in value[2:]
        or "/" in value
    ):
        raise ServicePackageError(code)
    path = PureWindowsPath(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ServicePackageError(code)
    return value


def validate_windows_service_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """The shared service config, with every path a Windows drive-absolute path."""

    return _validate_config(value, absolute=_windows_absolute)


def _windows_principal(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _WINDOWS_PRINCIPAL.fullmatch(value) is None
        # Groups and machine identities are never "the signed-in user".
        or value.upper().startswith(_WINDOWS_NON_USER_DOMAINS)
    ):
        raise ServicePackageError("service_windows_principal_invalid")
    return value


def _windows_command_line(argv: Sequence[str]) -> str:
    """The exact CreateProcess command line (MSVC ``CommandLineToArgvW`` rules)."""

    return subprocess.list2cmdline(list(argv))


def _task_duration_minutes(seconds: int | float) -> str:
    # Task Scheduler refuses a RestartOnFailure interval under one minute.
    return f"PT{max(1, math.ceil(float(seconds) / 60))}M"


def _windows_task_xml(
    config: Mapping[str, Any], config_path: str, *, principal: str
) -> bytes:
    """Task Scheduler 1.2 definition for ``windows_service_per_user``.

    Runs at the named user's logon, in that user's own interactive token, at
    ``LeastPrivilege`` (never elevated), one instance at a time, with a bounded
    restart budget.  Encoded UTF-16 with a BOM, as ``schtasks /XML`` expects.
    """

    runner = _runner_argv(config, config_path)
    task_name = f"mycelium-{config['service_id']}"
    document = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{_TASK_NAMESPACE}">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>Mycelium {_xml_escape(config['role'])} ({_xml_escape(config['service_id'])})</Description>\n"
        f"    <URI>{_xml_escape(_TASK_FOLDER + task_name)}</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{_xml_escape(principal)}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{_xml_escape(principal)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <RestartOnFailure>\n"
        f"      <Interval>{_task_duration_minutes(config['restart_delay_seconds'])}</Interval>\n"
        f"      <Count>{int(config['restart_limit'])}</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(runner[0])}</Command>\n"
        f"      <Arguments>{_xml_escape(_windows_command_line(runner[1:]))}</Arguments>\n"
        f"      <WorkingDirectory>{_xml_escape(config['working_directory'])}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )
    return b"\xff\xfe" + document.encode("utf-16-le")


def _windows_member_plan(
    config: Mapping[str, Any],
    config_path: str,
    *,
    install_mode: str,
    task_file: str | None,
) -> dict[str, Any]:
    name = f"mycelium-{config['service_id']}"
    runner = _runner_argv(config, config_path)
    common = {
        "protocol": WINDOWS_MEMBER_PLAN_PROTOCOL,
        "install_mode": install_mode,
        "service_id": config["service_id"],
        "runner_argv": runner,
        "runner_command_line": _windows_command_line(runner),
        "working_directory": config["working_directory"],
        "public_listener": False,
        "executed": False,
        "signed": False,
    }
    if install_mode == "windows_service_per_user":
        task_name = _TASK_FOLDER + name
        return {
            **common,
            "mechanism": "task_scheduler_logon_task",
            "elevation_required": False,
            "run_level": "LeastPrivilege",
            "task_name": task_name,
            "task_definition_file": task_file,
            "install_argv": ["schtasks.exe", "/Create", "/TN", task_name, "/XML", task_file, "/F"],
            "start_argv": ["schtasks.exe", "/Run", "/TN", task_name],
            # The runner signals its named stop event, the child gets its own
            # stop event, and after stop_timeout_seconds the runner terminates
            # the child's job object.  schtasks /End only kills the runner
            # (whose kill-on-close job then kills the child) and stays the
            # fallback when the runner no longer answers.
            "stop_mechanism": "named_event_then_job_terminate",
            "stop_argv": [*runner, "--stop"],
            "stop_fallback_argv": ["schtasks.exe", "/End", "/TN", task_name],
            "uninstall_argv": ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
            "known_blockers": [],
        }
    delay_ms = int(math.ceil(float(config["restart_delay_seconds"]) * 1000))
    actions = "/".join(["restart", str(delay_ms)] * int(config["restart_limit"]))
    start = "demand"
    return {
        **common,
        "mechanism": "service_control_manager",
        "elevation_required": True,
        "service_account": f"NT SERVICE\\{name}",
        "service_sid_type": "restricted",
        "privileges": list(WINDOWS_SERVICE_PRIVILEGES),
        "install_argv": [
            [
                "sc.exe", "create", name,
                "binPath=", _windows_command_line(runner),
                "type=", "own",
                "start=", start,
                "obj=", f"NT SERVICE\\{name}",
                "DisplayName=", f"Mycelium {config['role']} ({config['service_id']})",
            ],
            ["sc.exe", "sidtype", name, "restricted"],
            ["sc.exe", "privs", name, "/".join(WINDOWS_SERVICE_PRIVILEGES)],
            [
                "sc.exe", "failure", name,
                "reset=", str(int(math.ceil(float(config["restart_window_seconds"])))),
                "actions=", actions,
            ],
            ["sc.exe", "failureflag", name, "1"],
        ],
        "start_argv": ["sc.exe", "start", name],
        "stop_argv": ["sc.exe", "stop", name],
        "uninstall_argv": ["sc.exe", "delete", name],
        # A plain ``python.exe`` never calls StartServiceCtrlDispatcher, so the
        # SCM would kill it (error 1053) -- this plan is not registrable as is.
        # The restricted NT SERVICE account is also a different SID from the
        # user who paired the host, so it cannot open owner-only host state.
        # (The runner's stop path is no longer POSIX-only: on win32 it uses a
        # named stop event and a Job Object.)
        "known_blockers": [
            "python_runner_implements_no_scm_control_handler",
            "virtual_service_account_cannot_open_owner_only_host_state",
        ],
    }


def build_windows_member_package(
    value: Mapping[str, Any],
    *,
    package_root: str,
    install_mode: str,
    principal: str | None = None,
) -> dict[str, Any]:
    """Render one Windows-native member plan (and, per-user, its task XML).

    ``package_root`` is the Windows directory the package will live in on the
    target (validated with Windows path rules on every host); ``principal`` is
    the signed-in ``DOMAIN\\user`` the per-user task runs as (observed on the
    device, e.g. by ``whoami``) and is refused for the per-service mode, where
    the account is the service's own virtual account.
    """

    if install_mode not in WINDOWS_INSTALL_MODES:
        raise ServicePackageError("service_windows_install_mode_invalid")
    config = validate_windows_service_config(value)
    root = _windows_absolute(package_root, "service_package_root_invalid")
    config_path = str(PureWindowsPath(root) / "service-config.json")
    config_raw = canonical_json_bytes(config)
    payloads: dict[str, bytes] = {"service-config.json": config_raw}
    task_file = None
    if install_mode == "windows_service_per_user":
        user = _windows_principal(principal)
        task_name = f"mycelium-{config['service_id']}.task.xml"
        payloads[task_name] = _windows_task_xml(config, config_path, principal=user)
        task_file = str(PureWindowsPath(root) / task_name)
    elif principal is not None:
        raise ServicePackageError("service_windows_principal_invalid")
    plan = _windows_member_plan(
        config, config_path, install_mode=install_mode, task_file=task_file
    )
    plan_name = f"mycelium-{config['service_id']}.windows-member-plan.json"
    payloads[plan_name] = canonical_json_bytes(plan)
    return {
        "protocol": WINDOWS_MEMBER_PLAN_PROTOCOL,
        "install_mode": install_mode,
        "service_id": config["service_id"],
        "plan": plan,
        "files": {
            name: {"mode": "0600", "sha256": _digest(raw), "bytes": len(raw)}
            for name, raw in sorted(payloads.items())
        },
        "_payloads": payloads,
    }


def build_service_package(
    value: Mapping[str, Any], *, package_root: Path, include_windows_member: bool = False,
    systemd_user_unit: bool = False,
) -> dict[str, Any]:
    config = _validate_config(value, absolute=_posix_absolute)
    if not package_root.is_absolute():
        raise ServicePackageError("service_package_root_invalid")
    config_path = package_root / "service-config.json"
    config_raw = canonical_json_bytes(config)
    launchd_raw = _launchd(config, config_path)
    systemd_raw = _systemd(config, config_path, user_unit=systemd_user_unit)
    files = {
        "service-config.json": {"mode": "0600", "sha256": _digest(config_raw), "bytes": len(config_raw)},
        f"org.mycelium.{config['service_id']}.plist": {"mode": "0600", "sha256": _digest(launchd_raw), "bytes": len(launchd_raw)},
        f"mycelium-{config['service_id']}.service": {"mode": "0600", "sha256": _digest(systemd_raw), "bytes": len(systemd_raw)},
    }
    payloads = {
        "service-config.json": config_raw,
        f"org.mycelium.{config['service_id']}.plist": launchd_raw,
        f"mycelium-{config['service_id']}.service": systemd_raw,
    }
    if include_windows_member:
        for install_mode, suffix in WINDOWS_MEMBER_NAMES.items():
            name = f"mycelium-{config['service_id']}.{suffix}"
            raw = _windows_service_xml(config, config_path, install_mode=install_mode)
            files[name] = {"mode": "0600", "sha256": _digest(raw), "bytes": len(raw)}
            payloads[name] = raw
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
        "files": files,
        "_payloads": payloads,
    }


def write_service_package(
    value: Mapping[str, Any], output_root: Path, *, include_windows_member: bool = False
) -> dict[str, Any]:
    if not output_root.is_absolute():
        raise ServicePackageError("service_package_root_invalid")
    package = build_service_package(
        value, package_root=output_root, include_windows_member=include_windows_member
    )
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
    "WINDOWS_INSTALL_MODES",
    "WINDOWS_MEMBER_PLAN_PROTOCOL",
    "WINDOWS_SERVICE_PRIVILEGES",
    "ServicePackageError",
    "build_service_package",
    "build_windows_member_package",
    "validate_service_config",
    "validate_windows_service_config",
    "write_service_package",
]
