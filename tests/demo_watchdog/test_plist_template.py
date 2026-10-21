# SPDX-License-Identifier: AGPL-3.0-only
"""The LaunchAgent plist is a TEMPLATE: it must render correctly and never be loaded.

The packet gates deployment on the laptop returning, so this suite asserts the
template's contract with launchd (KeepAlive, RunAtLoad, WorkingDirectory, correct
paths, scrubbed environment) and that nothing in this lane can load it.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

import mycelium_demo_watchdog
from mycelium_demo_watchdog.plist import (
    PLACEHOLDERS,
    TEMPLATE_PATH,
    UnsatisfiedPlaceholder,
    render_plist,
)

REPO_ROOT = Path(mycelium_demo_watchdog.__file__).resolve().parents[1]

MAPPING = {
    "MYCELIUM_HOME": "/Users/owner",
    "MYCELIUM_PYTHON": "/opt/homebrew/bin/python3.14",
    "MYCELIUM_REPO_ROOT": str(REPO_ROOT),
    "MYCELIUM_STATE_DIR": "/Users/owner/mycelium-demo-watchdog",
    "MYCELIUM_LOG_DIR": "/Users/owner/mycelium-demo-watchdog/logs",
}


def test_template_ships_in_the_owned_deploy_path():
    assert TEMPLATE_PATH == REPO_ROOT / "deploy" / "demo-watchdog.plist"
    assert TEMPLATE_PATH.is_file()


def test_template_is_a_valid_plist_after_rendering():
    document = plistlib.loads(render_plist(MAPPING))
    assert document["Label"] == "org.mycelium.demo-watchdog"


def test_template_keeps_the_watchdog_alive_and_running_at_load():
    document = plistlib.loads(render_plist(MAPPING))
    assert document["KeepAlive"] is True, "auto-restart is the whole point of a watchdog"
    assert document["RunAtLoad"] is True
    assert document["ThrottleInterval"] >= 10


def test_working_directory_is_the_repository_root_not_a_relative_path():
    document = plistlib.loads(render_plist(MAPPING))
    assert document["WorkingDirectory"] == str(REPO_ROOT)
    assert Path(document["WorkingDirectory"]).is_absolute()
    assert Path(document["WorkingDirectory"]).is_dir()


def test_program_arguments_run_the_module_with_a_scrubbed_environment():
    document = plistlib.loads(render_plist(MAPPING))
    argv = document["ProgramArguments"]
    assert argv[0] == "/usr/bin/env"
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        assert name in argv, f"{name} must be unset before the watchdog starts"
    assert "PYTHONDONTWRITEBYTECODE=1" in argv
    assert MAPPING["MYCELIUM_PYTHON"] in argv
    assert argv[-3:] == ["-m", "mycelium_demo_watchdog", "--arm"]
    assert "--arm" in argv, "the deployed watchdog must be explicitly armed"


def test_logs_are_absolute_paths_under_the_state_dir():
    document = plistlib.loads(render_plist(MAPPING))
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = Path(document[key])
        assert path.is_absolute()
        assert str(path).startswith(MAPPING["MYCELIUM_LOG_DIR"])


def test_environment_variables_carry_no_git_or_python_path_poison():
    document = plistlib.loads(render_plist(MAPPING))
    environment = document.get("EnvironmentVariables", {})
    assert environment.get("HOME") == MAPPING["MYCELIUM_HOME"]
    for name in ("GIT_DIR", "GIT_WORK_TREE", "PYTHONPATH", "VIRTUAL_ENV"):
        assert name not in environment


def test_umask_and_process_type_are_restrictive():
    document = plistlib.loads(render_plist(MAPPING))
    assert document["Umask"] == 63
    assert document.get("ProcessType") == "Background"


def test_render_refuses_to_leave_a_placeholder_behind():
    for name in PLACEHOLDERS:
        partial = dict(MAPPING)
        del partial[name]
        with pytest.raises(UnsatisfiedPlaceholder):
            render_plist(partial)


def test_render_output_contains_no_placeholder_token():
    rendered = render_plist(MAPPING).decode()
    for name in PLACEHOLDERS:
        assert f"__{name}__" not in rendered


def test_no_lane_code_can_load_a_launch_agent():
    """Structural proof the template is only ever text in this lane."""
    package = Path(mycelium_demo_watchdog.__file__).parent
    text = "\n".join(path.read_text() for path in package.rglob("*.py"))
    for forbidden in ("launchctl", "launchd/bootstrap", "plistlib.dumps("):
        assert forbidden not in text, forbidden
