# SPDX-License-Identifier: AGPL-3.0-only
"""The artifact source agent's SIGINT/SIGTERM handler must take no thread lock.

See ``tests/host_platform/signal_probe.py``: a handler that takes a lock the
main thread may already hold (``Event.set()`` under ``Event.wait()``)
deadlocks the process on its own thread.
"""

from __future__ import annotations

from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

import mycelium_live.artifact_agent as artifact_agent
from tests.host_platform.signal_probe import guard_signal_handlers


def test_artifact_agent_stops_on_sigterm_without_taking_a_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def handle_request() -> None:
        calls.append("handle_request")
        # The serving loop runs on the main thread: deliver SIGTERM to the
        # installed handler here, as CPython would between two bytecodes.
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    server = SimpleNamespace(
        timeout=None,
        handle_request=handle_request,
        server_close=lambda: calls.append("server_close"),
    )
    agent = SimpleNamespace(
        server=server,
        publish_availability=lambda: calls.append("publish"),
        reconcile=lambda: calls.append("reconcile"),
    )
    monkeypatch.setattr(artifact_agent, "load_artifact_source_agent", lambda _path: agent)
    installed = guard_signal_handlers(monkeypatch)
    before = signal.getsignal(signal.SIGTERM)

    assert artifact_agent.main(["--config", str(tmp_path / "config.json")]) == 0

    assert calls == ["publish", "reconcile", "handle_request", "server_close"]
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    assert signal.getsignal(signal.SIGTERM) is before
