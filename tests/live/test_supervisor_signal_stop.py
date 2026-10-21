# SPDX-License-Identifier: AGPL-3.0-only
"""The live supervisor's SIGINT/SIGTERM handler must take no thread lock.

CPython runs the handler on the main thread between bytecodes.  A handler that
calls ``threading.Event.set()`` takes the event's non-reentrant condition
lock, so a signal that lands while the main thread holds that lock (inside
any ``wait``/``set`` on the same event) deadlocks the supervisor on its own
thread.  The probe refuses the first lock the handler would take.
"""

from __future__ import annotations

from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

import mycelium_live.supervisor as supervisor
from tests.host_platform.signal_probe import guard_signal_handlers


def test_registry_server_stops_on_sigterm_without_taking_a_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>fixture</html>", encoding="utf-8")
    (tmp_path / "product").mkdir(mode=0o700)
    monkeypatch.setattr(
        supervisor,
        "native_deployment_projection",
        lambda plan: SimpleNamespace(
            deployment_id="fixture",
            model_id="fixture-model",
            model_revision="fixture-revision",
            quantization="int8-weight-only",
            topology_size=2,
        ),
    )

    def unavailable(*args, **kwargs):
        raise ValueError("fixture_model_execution_denied")

    monkeypatch.setattr(supervisor, "_qualified_runtime", unavailable)
    installed = guard_signal_handlers(monkeypatch)
    before = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    started: list[dict] = []

    def on_started(document: dict) -> None:
        started.append(document)
        # Deliver SIGTERM to the handler the supervisor installed, on the
        # main thread, exactly as CPython would between two bytecodes.
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    outcome = supervisor.run_registry_server(
        operator_plans=[tmp_path / "fixture-plan"],
        host="127.0.0.1",
        port=0,
        static_root=static,
        seed_state_root=tmp_path / "seed",
        product_state_root=tmp_path / "product",
        on_started=on_started,
        api_only=True,
        governance_root=None,
    )

    assert outcome == 0
    assert len(started) == 1
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    assert {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    } == before
