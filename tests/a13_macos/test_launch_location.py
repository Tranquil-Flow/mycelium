# SPDX-License-Identifier: AGPL-3.0-only
"""A bare launch of the packaged app from a place that is not the install must not start a session there.

On macOS 26 "Open Anyway" for the app launches the *bundle itself*, translocated and without ``--install``;
the session it started lived in a random read-only path and nothing was installed.  The launcher now
recognises that (frozen, macOS, no arguments, not running from ``programs/<version>``) and tells the user to
run ``Install Mycelium.command``.  Nothing here executes any app: the entry module is imported and called
in-process with the OS dialog and the app class replaced.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from mycelium_a13_macos import launch_location
from mycelium_a13_macos import __main__ as entry

EXE_TAIL = "Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo"
TRANSLOCATED = Path("/private/var/folders/ab/cd/T/AppTranslocation/6B1F0C8E-0000-0000-0000-000000000000/d") / EXE_TAIL


def test_a_translocated_executable_is_classified_translocated(tmp_path):
    assert launch_location.classify(TRANSLOCATED, tmp_path / "programs") == "translocated"


def test_an_executable_below_the_programs_root_is_installed_even_through_a_link(tmp_path):
    programs = tmp_path / "programs"
    exe = programs / "0.1.0" / EXE_TAIL
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")  # data only, never run
    link = tmp_path / "Applications" / "Mycelium A13.app"
    link.parent.mkdir()
    link.symlink_to(programs / "0.1.0" / "Mycelium A13 Demo.app")
    assert launch_location.classify(exe, programs) == "installed"
    assert launch_location.classify(link / "Contents/MacOS/Mycelium A13 Demo", programs) == "installed"


def test_a_lookalike_programs_folder_is_not_installed(tmp_path):
    other = tmp_path / "programs-evil" / "0.1.0" / EXE_TAIL
    assert launch_location.classify(other, tmp_path / "programs") == "elsewhere"
    assert launch_location.classify(tmp_path / "Downloads" / "b" / EXE_TAIL, tmp_path / "programs") == "elsewhere"


@pytest.mark.parametrize("kwargs, expected", [
    (dict(frozen=False), None),                                   # a source checkout / test run
    (dict(platform="linux"), None),
    (dict(argv=["x", "--headless"]), None),                       # any explicit flag keeps the old behaviour
    (dict(argv=["x", "--install", "--bundle", "/b"]), None),
    (dict(argv=["x", "-psn_0_12345"]), "not_installed"),          # Finder's own process-serial argument is not a flag
    (dict(argv=["x"]), "not_installed"),
])
def test_only_a_bare_frozen_mac_launch_outside_the_install_is_refused(tmp_path, kwargs, expected):
    base = dict(frozen=True, platform="darwin", argv=["x"], executable=TRANSLOCATED, programs_root=tmp_path / "programs")
    refusal = launch_location.not_installed_refusal(**{**base, **kwargs})
    assert (refusal["code"] if refusal else None) == expected
    if refusal:
        assert refusal["location"] == "translocated"
        assert "Install Mycelium.command" in refusal["message"]
        assert str(tmp_path) not in json.dumps(refusal)  # no paths in the public line


def test_the_installed_app_is_never_refused(tmp_path):
    programs = tmp_path / "programs"
    exe = programs / "0.1.0" / EXE_TAIL
    assert launch_location.not_installed_refusal(frozen=True, platform="darwin", argv=["x"], executable=exe,
                                                 programs_root=programs) is None


def test_main_refuses_before_any_session_starts(monkeypatch, capsys, tmp_path):
    shown = []

    def no_session(*args, **kwargs):
        raise AssertionError("a session was started from an uninstalled app")

    monkeypatch.setattr(sys, "argv", ["Mycelium A13 Demo"])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(TRANSLOCATED))
    monkeypatch.setattr(entry, "DemoApp", no_session)
    monkeypatch.setattr(entry, "preflight", no_session)
    monkeypatch.setattr(entry, "_show_refusal_dialog", shown.append)
    monkeypatch.setattr(entry.Path, "home", classmethod(lambda cls: tmp_path))
    assert entry.main() == 2
    event = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert event["event"] == "launcher_refused" and event["code"] == "not_installed"
    assert len(shown) == 1 and "Install Mycelium.command" in shown[0]
