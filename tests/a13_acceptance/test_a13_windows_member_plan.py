# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows-native member plans from the shared service renderer (gap A3).

The shared renderer's older Windows member is a declarative XML descriptor that
nothing on a Windows host consumes, rendered from a POSIX-path config.  These
tests pin the Windows-native members: a Task Scheduler 1.2 logon task for the
per-user mode and an ``sc.exe`` argv plan for the per-service mode, rendered
from Windows drive-absolute paths judged identically on every host.  Pure
rendering: nothing is executed, registered or signed.
"""

from __future__ import annotations

import copy
import json
import subprocess
import xml.etree.ElementTree as ET

import pytest

import mycelium_service_package as package
from mycelium_service_package import (
    PROTOCOL,
    WINDOWS_MEMBER_PLAN_PROTOCOL,
    ServicePackageError,
    build_service_package,
    build_windows_member_package,
    validate_windows_service_config,
)

ROOT = "C:\\Users\\pilot\\AppData\\Local\\Mycelium\\package"
PYTHON = "C:\\Program Files\\Mycelium\\python\\python.exe"
PRINCIPAL = "DESKTOP-PILOT\\pilot"
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _config(**overrides):
    value = {
        "protocol": PROTOCOL,
        "package_version": "v0-1-0",
        "service_id": "win-1-agent",
        "role": "node",
        "argv": [PYTHON, "-m", "mycelium_node"],
        "working_directory": "C:\\Users\\pilot\\AppData\\Local\\Mycelium",
        "state_directory": "C:\\Users\\pilot\\AppData\\Local\\Mycelium\\state",
        "log_directory": "C:\\Users\\pilot\\AppData\\Local\\Mycelium\\logs",
        "environment": {"MYCELIUM_ROLE": "node"},
        "health_url": "http://127.0.0.1:14001/health",
        "restart_limit": 5,
        "restart_window_seconds": 60,
        "restart_delay_seconds": 5,
        "stop_timeout_seconds": 30,
    }
    value.update(overrides)
    return value


def _per_user(**overrides):
    return build_windows_member_package(
        _config(**overrides),
        package_root=ROOT,
        install_mode="windows_service_per_user",
        principal=PRINCIPAL,
    )


def _service(**overrides):
    return build_windows_member_package(
        _config(**overrides), package_root=ROOT, install_mode="windows_service"
    )


def _code(call) -> str:
    with pytest.raises(ServicePackageError) as caught:
        call()
    return str(caught.value)


def _command_line_to_argv(line: str) -> list[str]:
    """Reference decoder for the MSVC ``CommandLineToArgvW`` argument rules."""

    arguments: list[str] = []
    current: list[str] = []
    in_quotes = False
    have_argument = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == "\\":
            run = 0
            while index < len(line) and line[index] == "\\":
                run += 1
                index += 1
            if index < len(line) and line[index] == '"':
                current.append("\\" * (run // 2))
                if run % 2:
                    current.append('"')
                    index += 1
                have_argument = True
            else:
                current.append("\\" * run)
                have_argument = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            have_argument = True
        elif character in " \t" and not in_quotes:
            if have_argument:
                arguments.append("".join(current))
                current, have_argument = [], False
        else:
            current.append(character)
            have_argument = True
        index += 1
    if have_argument:
        arguments.append("".join(current))
    return arguments


def _task(result) -> ET.Element:
    raw = result["_payloads"]["mycelium-win-1-agent.task.xml"]
    assert raw[:2] == b"\xff\xfe", "schtasks /XML task files are UTF-16 with a BOM"
    text = raw[2:].decode("utf-16-le")
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    return ET.fromstring(text.split("?>", 1)[1])


def test_rendering_is_deterministic_and_executes_nothing(monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("the renderer must never execute anything")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    for build in (_per_user, _service):
        first, second = build(), build()
        assert first["_payloads"] == second["_payloads"]
        assert first["files"] == second["files"]
        assert first["plan"]["executed"] is False
        assert first["plan"]["signed"] is False
        assert first["plan"]["public_listener"] is False
        assert first["protocol"] == WINDOWS_MEMBER_PLAN_PROTOCOL
        for name, record in first["files"].items():
            assert record["sha256"] == package._digest(first["_payloads"][name])


def test_the_per_user_member_is_a_least_privilege_logon_task() -> None:
    result = _per_user()
    assert sorted(result["_payloads"]) == [
        "mycelium-win-1-agent.task.xml",
        "mycelium-win-1-agent.windows-member-plan.json",
        "service-config.json",
    ]
    task = _task(result)
    assert task.get("version") == "1.2"
    principal = task.find("t:Principals/t:Principal", TASK_NS)
    assert principal.find("t:UserId", TASK_NS).text == PRINCIPAL
    assert principal.find("t:LogonType", TASK_NS).text == "InteractiveToken"
    assert principal.find("t:RunLevel", TASK_NS).text == "LeastPrivilege"
    trigger = task.find("t:Triggers/t:LogonTrigger", TASK_NS)
    assert trigger.find("t:UserId", TASK_NS).text == PRINCIPAL
    settings = task.find("t:Settings", TASK_NS)
    assert settings.find("t:MultipleInstancesPolicy", TASK_NS).text == "IgnoreNew"
    assert settings.find("t:RestartOnFailure/t:Count", TASK_NS).text == "5"
    # Task Scheduler refuses sub-minute restart intervals; 5s rounds up to 1 minute.
    assert settings.find("t:RestartOnFailure/t:Interval", TASK_NS).text == "PT1M"
    exec_ = task.find("t:Actions/t:Exec", TASK_NS)
    assert exec_.find("t:Command", TASK_NS).text == PYTHON
    config_path = ROOT + "\\service-config.json"
    assert _command_line_to_argv(exec_.find("t:Arguments", TASK_NS).text) == [
        "-B",
        "-m",
        "mycelium_service_runner",
        "--config",
        config_path,
    ]
    plan = result["plan"]
    assert plan["mechanism"] == "task_scheduler_logon_task"
    assert plan["elevation_required"] is False
    assert plan["install_argv"] == [
        "schtasks.exe",
        "/Create",
        "/TN",
        "\\Mycelium\\mycelium-win-1-agent",
        "/XML",
        ROOT + "\\mycelium-win-1-agent.task.xml",
        "/F",
    ]
    assert "service_runner_stop_path_uses_posix_killpg" not in plan["known_blockers"]
    assert plan["stop_mechanism"] == "named_event_then_job_terminate"
    rendered_plan = json.loads(result["_payloads"]["mycelium-win-1-agent.windows-member-plan.json"])
    assert rendered_plan == plan


def test_the_per_service_member_is_a_restricted_virtual_account_scm_plan() -> None:
    result = _service()
    assert sorted(result["_payloads"]) == [
        "mycelium-win-1-agent.windows-member-plan.json",
        "service-config.json",
    ]
    plan = result["plan"]
    assert plan["mechanism"] == "service_control_manager"
    assert plan["elevation_required"] is True
    assert plan["service_account"] == "NT SERVICE\\mycelium-win-1-agent"
    create, sidtype, privs, failure, failureflag = plan["install_argv"]
    assert create[:3] == ["sc.exe", "create", "mycelium-win-1-agent"]
    options = dict(zip(create[3::2], create[4::2]))
    assert options["obj="] == "NT SERVICE\\mycelium-win-1-agent"
    assert options["type="] == "own"
    assert _command_line_to_argv(options["binPath="]) == plan["runner_argv"]
    assert plan["runner_argv"][0] == PYTHON
    assert sidtype == ["sc.exe", "sidtype", "mycelium-win-1-agent", "restricted"]
    assert privs == ["sc.exe", "privs", "mycelium-win-1-agent", "SeChangeNotifyPrivilege"]
    assert failure[5:] == ["actions=", "/".join(["restart", "5000"] * 5)]
    assert failureflag == ["sc.exe", "failureflag", "mycelium-win-1-agent", "1"]
    # Honest: a plain python.exe implements no SCM control handler.
    assert "python_runner_implements_no_scm_control_handler" in plan["known_blockers"]


def test_windows_paths_are_judged_identically_on_every_host() -> None:
    config = validate_windows_service_config(_config())
    assert config["argv"][0] == PYTHON
    for bad in (
        "/usr/bin/python3",
        "python.exe",
        "C:python.exe",
        "C:/Program Files/python.exe",
        "C:\\Mycelium\\..\\Windows\\python.exe",
        'C:\\Program Files\\"quoted"\\python.exe',
        "C:\\Mycelium\\python.exe:stream",
        "\\\\server\\share\\python.exe",
    ):
        assert _code(lambda bad=bad: _per_user(argv=[bad])) == "service_executable_invalid"
    assert _code(lambda: _per_user(working_directory="/var/lib/mycelium")) == (
        "service_working_directory_invalid"
    )
    assert _code(
        lambda: build_windows_member_package(
            _config(), package_root="/var/lib/mycelium", install_mode="windows_service"
        )
    ) == "service_package_root_invalid"


def test_refusals_keep_the_shared_boundaries() -> None:
    assert _code(lambda: _per_user(environment={"MYCELIUM_API_KEY": "x"})) == (
        "service_environment_invalid"
    )
    assert _code(lambda: _per_user(health_url="http://0.0.0.0:14001/health")) == (
        "service_health_url_invalid"
    )
    assert _code(
        lambda: build_windows_member_package(
            _config(), package_root=ROOT, install_mode="windows_driver"
        )
    ) == "service_windows_install_mode_invalid"
    for principal in (
        None,
        "pilot",
        "BUILTIN\\Users",
        "NT AUTHORITY\\SYSTEM",
        "NT SERVICE\\anything",
        "\\Everyone",
        "DESKTOP\\pi<lot",
    ):
        assert _code(
            lambda principal=principal: build_windows_member_package(
                _config(),
                package_root=ROOT,
                install_mode="windows_service_per_user",
                principal=principal,
            )
        ) == "service_windows_principal_invalid"
    # The per-service member runs as its own virtual account, never a named user.
    assert _code(
        lambda: build_windows_member_package(
            _config(),
            package_root=ROOT,
            install_mode="windows_service",
            principal=PRINCIPAL,
        )
    ) == "service_windows_principal_invalid"


def test_xml_escaping_cannot_inject_task_elements() -> None:
    """Defence in depth below validation: every interpolated value is escaped.

    Validation already refuses markup in every rendered field, so this drives
    the task renderer directly with a hostile (unvalidated) config.
    """

    hostile = validate_windows_service_config(_config())
    hostile["working_directory"] = "C:\\x</WorkingDirectory></Exec></Actions><Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals><Actions><Exec><WorkingDirectory>"
    raw = package._windows_task_xml(hostile, ROOT + "\\service-config.json", principal=PRINCIPAL)
    task = ET.fromstring(raw[2:].decode("utf-16-le").split("?>", 1)[1])
    assert len(task.findall(".//t:RunLevel", TASK_NS)) == 1
    assert task.find("t:Principals/t:Principal/t:RunLevel", TASK_NS).text == "LeastPrivilege"
    assert task.find("t:Actions/t:Exec/t:WorkingDirectory", TASK_NS).text == hostile["working_directory"]


def test_the_posix_service_package_is_unchanged_by_the_windows_renderer() -> None:
    posix = {
        **_config(),
        "argv": ["/usr/bin/python3", "-m", "mycelium_node"],
        "working_directory": "/var/lib/mycelium",
        "state_directory": "/var/lib/mycelium/state",
        "log_directory": "/var/log/mycelium",
    }
    before = copy.deepcopy(posix)
    from pathlib import Path

    result = build_service_package(posix, package_root=Path("/var/lib/mycelium/packages"))
    assert posix == before
    assert sorted(result["_payloads"]) == [
        "mycelium-win-1-agent.service",
        "org.mycelium.win-1-agent.plist",
        "service-config.json",
    ]
