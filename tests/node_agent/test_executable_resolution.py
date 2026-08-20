# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executable identity regression coverage for physical command launchers."""

from __future__ import annotations

from pathlib import Path

from mycelium_node.process import capture_executable_identity


def test_capture_executable_identity_resolves_bare_path_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "physical-transport"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))

    identity = capture_executable_identity("physical-transport")

    assert identity.path == str(executable.resolve())


def test_capture_executable_identity_rejects_missing_bare_path_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    try:
        capture_executable_identity("missing-physical-transport")
    except ValueError as exc:
        assert str(exc) == "executable is unavailable"
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("missing PATH executable was accepted")
