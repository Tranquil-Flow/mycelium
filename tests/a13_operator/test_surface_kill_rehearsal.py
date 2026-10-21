# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-03 item 4, last piece: a REAL process kill, end to end, offline.

Two properties, both measured rather than asserted:

 1. DETACHMENT -- a surface process started the way the round starts its daemons (its own
    session) is not in the launcher's process group, which is the same evidence every round
    record carries (pgid == pid, reparented, no controlling tty). This is why a chat session's
    teardown cannot reap a round.
 2. A REAL KILL MAKES `surface_lost` FIRE, promptly, with the daemon's own exit evidence. The
    stub unlinks native-control.json on SIGTERM exactly as the app does on shutdown
    (app.py:771), so this is the real event rather than a simulated one -- and the operator's
    check must name it instead of letting a bare FileNotFoundError surface as
    `compiler_not_ready` (the ERRATA-1 failure that cost a physical attempt).

Scope, stated plainly: the stub is not the app, so this does not exercise the app's own
shutdown code -- it exercises the operator's reaction to the app's documented unlink contract.
"""
import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
OPERATOR = ROOT / 'scripts/a13_operator'
if str(OPERATOR) not in sys.path:
    sys.path.insert(0, str(OPERATOR))

STUB = OPERATOR / 'wp8_03_surface_stub.py'

_spec = importlib.util.spec_from_file_location('round14_surface', OPERATOR / 'round14_surface.py')
surface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(surface)


def _start(tmp_path, port=54321):
    root = tmp_path / 'owner-lhr'
    root.mkdir()
    log = tmp_path / 'owner-daemon.log'
    process = subprocess.Popen(
        [sys.executable, str(STUB), '--root', str(root), '--log', str(log), '--port', str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    control = root / 'native-control.json'
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not control.exists() and process.poll() is None:
        time.sleep(0.05)
    return root, log, process, control


def _reap(process):
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            pass
        process.wait(timeout=10)


def test_a_started_surface_process_is_detached_from_its_launcher(tmp_path):
    root, log, process, control = _start(tmp_path)
    try:
        assert control.exists()
        assert surface.require_local(root, label='owner')['present'] is True
        # The detachment evidence: its own session, so our process group dying cannot reap it.
        assert os.getpgid(process.pid) == process.pid
        assert os.getsid(process.pid) == process.pid
        assert os.getpgid(process.pid) != os.getpgid(0)
    finally:
        _reap(process)


def test_a_real_kill_makes_surface_lost_fire_promptly_with_evidence(tmp_path):
    root, log, process, control = _start(tmp_path)
    try:
        assert surface.require_local(root, label='owner')['present'] is True

        killed_at = time.monotonic()
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=15)

        assert not control.exists(), 'the stub must unlink its control file on shutdown'

        fired = None
        with pytest.raises(RuntimeError) as caught:
            fired = time.monotonic()
            surface.require_local(root, label='owner', log_path=log)
        assert str(caught.value).startswith('surface_lost:owner:')
        assert 'present' in str(caught.value)

        # Promptly: the check must not need a timeout to notice a dead app.
        assert fired - killed_at < 10.0

        # And with the daemon's own last markers as evidence.
        message = str(caught.value)
        assert 'packaged_app_closed' in message
        assert 'OWNER_DAEMON_EXIT' in message
        assert 'compiler_not_ready' not in message
    finally:
        _reap(process)


def test_a_kill_before_the_surface_is_written_is_still_a_named_surface_lost(tmp_path):
    """The race that matters: the app dies before writing its control file at all."""
    root = tmp_path / 'owner-lhr'
    root.mkdir()
    log = tmp_path / 'owner-daemon.log'
    log.write_text('launcher_started\npackaged_app_closed\n')
    with pytest.raises(RuntimeError) as caught:
        surface.require_local(root, label='owner', log_path=log)
    assert str(caught.value).startswith('surface_lost:owner:')
    assert 'packaged_app_closed' in str(caught.value)
