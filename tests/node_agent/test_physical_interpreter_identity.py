# SPDX-License-Identifier: AGPL-3.0-only
"""macOS keeps its interpreter order: configured, legacy Homebrew framework image, then sys.executable.

An owner-configured ``MYCELIUM_PHYSICAL_SERVICE_PYTHON`` is tried first (opt-in); the Apple Silicon
Homebrew ``Python.app`` image comes next exactly as before; the plain running interpreter is only the
last fallback.  Linux and Windows use ``sys.executable``.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

from mycelium_node import process

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixtures")


def _executable(path, body=b"#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


def _framework(root, tag):
    versions = root / "Python.framework" / "Versions" / "3.14"
    cli = _executable(versions / "bin" / "python3.14", b"#!/bin/sh\nexit 0\n# cli " + tag)
    app = _executable(versions / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python",
                      b"#!/bin/sh\nexit 0\n# app " + tag)
    return cli, app


def _identity(**kw):
    return process.physical_service_interpreter_identity(platform="darwin", **kw)


def test_macos_legacy_homebrew_framework_image_wins_over_the_running_interpreter(tmp_path, monkeypatch):
    run_cli, _ = _framework(tmp_path / "running", b"running")
    legacy_cli, legacy_app = _framework(tmp_path / "legacy", b"legacy")
    monkeypatch.setattr(process, "_MACOS_LEGACY_PYTHON", str(legacy_cli))
    assert _identity(executable=str(run_cli), environ={}).path == os.path.realpath(legacy_app)


def test_macos_configured_interpreter_wins_over_the_legacy_path(tmp_path, monkeypatch):
    run_cli, _ = _framework(tmp_path / "running", b"running")
    legacy_cli, _ = _framework(tmp_path / "legacy", b"legacy")
    conf_cli, conf_app = _framework(tmp_path / "configured", b"configured")
    monkeypatch.setattr(process, "_MACOS_LEGACY_PYTHON", str(legacy_cli))
    got = _identity(executable=str(run_cli), environ={"MYCELIUM_PHYSICAL_SERVICE_PYTHON": str(conf_cli)})
    assert got.path == os.path.realpath(conf_app)


def test_macos_unusable_configured_interpreter_falls_through_to_the_legacy_path(tmp_path, monkeypatch):
    run_cli, _ = _framework(tmp_path / "running", b"running")
    legacy_cli, legacy_app = _framework(tmp_path / "legacy", b"legacy")
    monkeypatch.setattr(process, "_MACOS_LEGACY_PYTHON", str(legacy_cli))
    got = _identity(executable=str(run_cli), environ={"MYCELIUM_PHYSICAL_SERVICE_PYTHON": str(tmp_path / "nope")})
    assert got.path == os.path.realpath(legacy_app)


def test_macos_running_interpreter_is_only_the_last_fallback_when_the_legacy_path_is_absent(tmp_path, monkeypatch):
    plain = _executable(tmp_path / "venv" / "bin" / "python")
    monkeypatch.setattr(process, "_MACOS_LEGACY_PYTHON", str(tmp_path / "absent" / "python3"))
    assert _identity(executable=str(plain), environ={}).path == os.path.realpath(plain)
    run_cli, _ = _framework(tmp_path / "running", b"running")
    # the pre-change fallback was the plain running executable, not a derived framework image
    assert _identity(executable=str(run_cli), environ={}).path == os.path.realpath(run_cli)


def test_other_platforms_use_the_running_interpreter_and_ignore_the_macos_paths(tmp_path, monkeypatch):
    plain = _executable(tmp_path / "venv" / "bin" / "python")
    legacy_cli, _ = _framework(tmp_path / "legacy", b"legacy")
    monkeypatch.setattr(process, "_MACOS_LEGACY_PYTHON", str(legacy_cli))
    got = process.physical_service_interpreter_identity(platform="linux", executable=str(plain), environ={})
    assert got.path == os.path.realpath(plain)


def test_default_call_still_resolves_the_current_interpreter():
    assert process.physical_service_interpreter_identity().path
