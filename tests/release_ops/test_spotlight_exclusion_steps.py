# SPDX-License-Identifier: AGPL-3.0-only
"""The Spotlight helper only prints steps: it never runs sudo and names every run dir exactly."""
from __future__ import annotations

import ast
import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "spotlight_exclusion_steps.py"


def _load():
    spec = importlib.util.spec_from_file_location("spotlight_exclusion_steps", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_run_dirs_are_derived_from_the_home_not_a_developer_path(tmp_path):
    mod = _load()
    dirs = mod.default_run_dirs(home=tmp_path)
    assert dirs == [
        tmp_path / "Documents" / "mycelium-core-program" / "runs",
        tmp_path / "Projects",
        tmp_path / ".hermes" / "cache" / "scratch",
        tmp_path / "Documents" / "playground",
    ]
    assert "evinova-self" not in SCRIPT.read_text()


def test_every_path_gets_the_exact_privacy_marker_and_volume_commands(tmp_path):
    mod = _load()
    run_dir = tmp_path / "run dir with space"
    run_dir.mkdir()
    text = mod.render_steps([run_dir], mount_of=lambda path: Path("/System/Volumes/Data"))
    assert f"== {run_dir}" in text
    assert f"touch {shlex.quote(str(run_dir / '.metadata_never_index'))}" in text
    assert "System Settings" in text and "Search Privacy" in text
    assert "sudo mdutil -i off /System/Volumes/Data" in text
    assert "sudo mdutil -i on /System/Volumes/Data" in text      # the undo is printed with the command
    assert "mdutil -s /System/Volumes/Data" in text              # the read-only check
    assert "whole volume" in text.lower()                        # the caveat: mdutil acts on volumes, not folders
    assert f"sudo mdutil -i off {run_dir}" not in text           # the folder form is not offered as if it worked


def test_a_missing_path_is_named_not_silently_skipped(tmp_path):
    mod = _load()
    text = mod.render_steps([tmp_path / "nope"], mount_of=lambda path: Path("/"))
    assert "(does not exist yet)" in text


def test_the_script_never_runs_sudo_and_prints_only_by_default(tmp_path):
    tree = ast.parse(SCRIPT.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for call in calls:
        if isinstance(call.func, ast.Attribute) and call.func.attr in {"run", "Popen", "check_output", "call"}:
            argv = ast.unparse(call)
            assert "sudo" not in argv, argv
    done = subprocess.run([sys.executable, str(SCRIPT), "--path", str(tmp_path)], capture_output=True, text=True,
                          timeout=30, env={**os.environ, "HOME": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert f"== {tmp_path}" in done.stdout
    assert "sudo mdutil -i off" in done.stdout
    assert "Marker" in done.stdout


def test_explicit_paths_replace_the_defaults(tmp_path):
    done = subprocess.run([sys.executable, str(SCRIPT), "--path", str(tmp_path)], capture_output=True, text=True,
                          timeout=30, env={**os.environ, "HOME": str(tmp_path / "home")})
    assert done.returncode == 0
    assert "mycelium-core-program" not in done.stdout


def test_relative_paths_are_refused():
    done = subprocess.run([sys.executable, str(SCRIPT), "--path", "relative/dir"], capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 2 and "absolute" in done.stderr


@pytest.mark.skipif(not Path("/System/Volumes/Data").is_dir(), reason="macOS Data volume only")
def test_user_folders_resolve_to_the_data_volume_not_the_sealed_system_volume(tmp_path):
    mod = _load()
    assert mod.mount_point(Path.home() / "Documents") == Path("/System/Volumes/Data")
    assert mod.mount_point(tmp_path / "not-created-yet" / "deeper") == Path("/System/Volumes/Data")
