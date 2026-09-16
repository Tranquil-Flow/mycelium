from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import signal
import sys
import time

import pytest

import mycelium_node.process as process_module


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc regression")
def test_cleanup_accepts_owned_macos_exec_transition(tmp_path: Path) -> None:
    """Cleanup retains the incarnation while an owned Mac executable changes."""

    ready = tmp_path / "ready"
    release = tmp_path / "release"
    target = tmp_path / "exec-transition.sh"
    launcher_path = process_module._inventory_process(os.getpid()).executable.path
    target.write_text(
        "#!/bin/sh\n"
        f"printf ready > {str(ready)!r}\n"
        f"while test ! -e {str(release)!r}; do /bin/sleep 0.01; done\n"
        f"exec {launcher_path!r} -I -S -c "
        "'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()'\n",
        encoding="utf-8",
    )

    supervised = process_module.PhysicalNodeProcess(
        command=("/bin/sh", str(target)),
        node_id="node-native-cleanup",
        run_id="run-native-cleanup",
        deployment_id="deployment-native-cleanup",
        response_timeout_seconds=0.05,
        shutdown_timeout_seconds=1.0,
    )
    ownership = supervised._pre_exec_ownership
    launch = supervised._launch_identity
    assert ownership is not None
    assert launch is not None
    try:
        _wait_until(ready.exists)
        release.touch()
        _wait_until(
            lambda: process_module._inventory_process(supervised.pid).executable
            == ownership.launcher_executable
        )
        current = process_module._inventory_process(supervised.pid)
        assert current.start_token == ownership.start_token
        assert current.process_group == ownership.process_group == supervised.pid
        assert current.session_id == ownership.session_id == supervised.pid
        assert current.executable == ownership.launcher_executable
        assert current.executable != launch.executable

        supervised.close()
        assert supervised.returncode == -signal.SIGKILL
    finally:
        if supervised._process.poll() is None:
            current = process_module._inventory_process(supervised.pid)
            assert current.pid == ownership.pid
            assert current.parent_pid == ownership.parent_pid == os.getpid()
            assert current.start_token == ownership.start_token
            assert current.process_group == ownership.process_group
            assert current.session_id == ownership.session_id
            assert process_module._identity_matches_pre_exec_ownership(
                current,
                ownership,
                supervised._target_executable_identity,
            )
            os.killpg(ownership.process_group, signal.SIGKILL)
            supervised._process.wait(timeout=2.0)
        # Complete pipe/thread cleanup even when the assertion above is RED.
        supervised.close()
