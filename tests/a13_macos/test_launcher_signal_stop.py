# SPDX-License-Identifier: AGPL-3.0-only
"""The A13 launcher's SIGINT/SIGTERM handler must take no thread lock.

The launcher's main thread parks in ``done.wait(.25)`` for its whole life; a
handler that calls ``Event.set()`` there can land inside that wait's locked
section and deadlock the launcher on its own thread.
"""

from __future__ import annotations

from pathlib import Path
import signal
import sys

import pytest

import mycelium_a13_macos.__main__ as launcher
from tests.host_platform.signal_probe import guard_signal_handlers


def test_launcher_stops_on_sigterm_without_taking_a_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []

    class Closed:
        def is_set(self) -> bool:
            if "sigterm" not in events:
                events.append("sigterm")
                # Deliver SIGTERM to the installed handler on the main thread.
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return False

    class FakeApp:
        closed = Closed()

        def __init__(self, _state_root: Path) -> None:
            self.root = _state_root
            self.installation_role = 'owner'

        def start(self) -> "FakeApp":
            events.append("start")
            return self

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(launcher, "preflight", lambda: {"supported": True})
    monkeypatch.setattr(launcher, "DemoApp", FakeApp)
    monkeypatch.setattr(launcher, "model_contract", lambda: {"fixture": True})
    monkeypatch.setattr(
        sys, "argv", ["mycelium-a13", "--headless", "--state-root", str(tmp_path)]
    )
    before = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    installed = guard_signal_handlers(monkeypatch)
    try:
        outcome = launcher.main()
    finally:
        # The launcher leaves its handlers installed until process exit.
        for signum, handler in before.items():
            signal.signal(signum, handler)

    assert outcome == 0, capsys.readouterr().err
    assert events == ["start", "sigterm", "close"]
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
