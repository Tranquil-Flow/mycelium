# SPDX-License-Identifier: AGPL-3.0-only
"""The physical node's SIGINT/SIGTERM handler must take no thread lock.

The handler used to close the service from inside the handler.  Closing the
transport, runtime and sidecar takes their locks; CPython runs the handler on
the main thread between bytecodes, so a signal that lands while the main
thread holds one of those locks deadlocks the node on its own thread.  The
handler must only request the stop; the service is closed by the main thread
once the stop has unwound it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import threading

import pytest

import physical_inference_node as node
from tests.host_platform.signal_probe import guard_signal_handlers


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handlers")
def test_physical_node_stops_on_sigterm_without_taking_a_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class FakeService:
        def __init__(self, **_kwargs: object) -> None:
            self._lock = threading.Lock()

        def close(self) -> None:
            # Like the real close: transport/runtime/sidecar teardown locks.
            with self._lock:
                events.append("close")

    class Stdin:
        @property
        def buffer(self) -> "Stdin":
            return self

        def __iter__(self) -> "Stdin":
            return self

        def __next__(self) -> bytes:
            events.append("read")
            # Deliver SIGTERM to the installed handler on the main thread,
            # while the node waits for its next command.
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            raise StopIteration

    monkeypatch.setattr(node, "PhysicalNodeService", FakeService)
    monkeypatch.setattr(
        node,
        "_parse_args",
        lambda: argparse.Namespace(
            run_id="run",
            deployment_id="deployment",
            node_id="node",
            artifact_root=tmp_path,
            socket_root=tmp_path,
            capacity_root=None,
            sidecar_binary=tmp_path / "sidecar",
            sidecar_local_only=True,
            sidecar_force_relay=False,
            command_timeout=1.0,
            endpoint_secret_file=None,
            decode_mode=None,
        ),
    )
    monkeypatch.setattr(sys, "stdin", Stdin())
    monkeypatch.setattr(node.mycelium_host_platform, "is_win32", lambda: False)
    before = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    installed = guard_signal_handlers(monkeypatch)

    with pytest.raises(SystemExit) as stopped:
        node.main()

    assert stopped.value.code == 128 + signal.SIGTERM
    assert events[0] == "read"
    assert "close" in events
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    assert {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    } == before
