# SPDX-License-Identifier: AGPL-3.0-only
"""The transport-probe sidecar wrapper's SIGTERM handler must take no lock.

The wrapper's main thread parks in its stop wait for the whole probe run; a
handler that calls ``Event.set()`` there deadlocks on the wait's own lock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys

import pytest

import scripts.run_transport_probe_sidecar as probe_sidecar
from tests.host_platform.signal_probe import guard_signal_handlers


def test_probe_sidecar_stops_on_sigterm_without_taking_a_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    endpoint_secret = private / "endpoint.secret"
    endpoint_secret.write_bytes(b"\x01" * 32)
    endpoint_secret.chmod(0o600)
    bootstrap = private / "bootstrap.secret"
    events: list[str] = []

    class FakeSidecar:
        def __init__(self, **kwargs: object) -> None:
            self.bootstrap_material = b"bootstrap"

        def start(self) -> dict[str, str]:
            events.append("start")
            # Deliver SIGTERM to the installed handler on the main thread.
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return {"endpoint_id": "endpoint", "endpoint_addr": "addr"}

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(probe_sidecar, "NativeSidecarProcess", FakeSidecar)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_transport_probe_sidecar.py",
            "--binary",
            str(tmp_path / "sidecar"),
            "--socket-root",
            str(private),
            "--bootstrap-secret-file",
            str(bootstrap),
            "--endpoint-secret-file",
            str(endpoint_secret),
            "--max-runtime-seconds",
            "60",
        ],
    )
    installed = guard_signal_handlers(monkeypatch)
    before = signal.getsignal(signal.SIGTERM)

    assert probe_sidecar.main() == 0

    assert events == ["start", "close"]
    ready = json.loads(capsys.readouterr().out)
    assert ready["protocol"] == "mycelium.transport_probe_sidecar_ready.v1"
    assert not os.path.exists(bootstrap)
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    assert signal.getsignal(signal.SIGTERM) is before
