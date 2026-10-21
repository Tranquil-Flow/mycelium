# SPDX-License-Identifier: AGPL-3.0-only
"""Source-mode children (native node, staging) import the package regardless of the working directory."""

from __future__ import annotations

import os
from pathlib import Path

from mycelium_a13_macos import child_environment as worker

ROOT = str(Path(worker.__file__).resolve().parents[1])


def test_package_root_is_prepended_and_existing_entries_are_kept(monkeypatch):
    env = worker.source_mode_environment({"PYTHONPATH": os.pathsep.join(["/a", ROOT, "/b"]), "KEEP": "1"})
    assert env["PYTHONPATH"].split(os.pathsep) == [ROOT, "/a", "/b"]
    assert env["KEEP"] == "1"
    assert worker.source_mode_environment({})["PYTHONPATH"] == ROOT
    monkeypatch.setattr(worker.sys, "frozen", True, raising=False)
    assert worker.source_mode_environment({"PYTHONPATH": "/a"}) is None  # frozen: --managed-node


def test_real_native_node_starts_from_an_unrelated_working_directory(tmp_path, monkeypatch):
    from test_work_authorization import TestRealNativeBoundary

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the node inherits this cwd, which holds no package
    monkeypatch.delenv("PYTHONPATH", raising=False)
    case = TestRealNativeBoundary()
    authz, _manifest, _assignment = case._authorization(tmp_path)
    node = case._worker(tmp_path, authz)
    try:
        assert node.start()["ok"] is True
    finally:
        node.close()


def test_staging_child_imports_from_an_unrelated_working_directory(tmp_path, monkeypatch):
    import subprocess
    import sys

    monkeypatch.delenv("PYTHONPATH", raising=False)
    command = [sys.executable, "-B", "-c", "import mycelium_a13_macos.managed_staging"]
    bare = subprocess.run(command, cwd=tmp_path, capture_output=True, env=dict(os.environ))
    assert bare.returncode != 0  # what the staging child saw before the fix
    fixed = subprocess.run(command, cwd=tmp_path, capture_output=True, env=worker.source_mode_environment())
    assert fixed.returncode == 0, fixed.stderr
