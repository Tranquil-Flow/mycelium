# SPDX-License-Identifier: AGPL-3.0-only
"""A failed native start must leave its bounded cause on the target.

Two-Mac run #10: the joined Air's accepted work failed with
``node_process_start_failed`` and the target kept nothing but the bootstrap
worker's native-exit/worker-replacement files, so the launch step that failed
was unknowable.  ``ManagedWorker.start`` now persists the closed
``NodeStartFailure`` cause next to ``native-exit.private.json`` and records the
successful launch's phase timing, so the next run shows how much of the 2 s
launch budget each Mac actually uses.
"""

import json
import os
import stat
import sys
import time

import pytest

from test_worker import worker

import mycelium_node.process as native
from mycelium_a13_macos import worker as worker_module


def _capacity(tmp_path):
    capacity = tmp_path / 'capacity'
    capacity.mkdir()
    capacity.chmod(0o700)
    return capacity


def _failure():
    return native.NodeStartFailure.from_exception(
        phase='launcher_ready', exception=TimeoutError('launcher handshake timed out'),
        started=time.monotonic() - 2.1, budget_seconds=2.0, popen_ms=40.0, phase_ms={},
        launcher_returncode=-15, launcher_exited=False)


def test_start_failure_cause_is_persisted_next_to_native_exit(tmp_path, monkeypatch):
    capacity = _capacity(tmp_path)
    instance = worker(tmp_path, capacity_root=capacity)

    def refuse(**_kwargs):
        raise native.NodeProcessError('node_process_start_failed', start_failure=_failure())

    monkeypatch.setattr(worker_module, '_ConcurrentNativeSession', refuse)
    with pytest.raises(native.NodeProcessError) as caught:
        instance.start()
    instance.close()
    assert caught.value.code == 'node_process_start_failed'  # the raised error is unchanged
    assert instance.cleanup_proven() is True
    path = capacity / 'node-start-failure.private.json'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    record = json.loads(path.read_text())
    assert record['protocol'] == 'mycelium.a13.node_start_failure_diagnostic.v1'
    assert (record['run_id'], record['node_id']) == ('run-test', 'mac-test')
    assert record['error_code'] == 'node_process_start_failed'
    assert record['public_code'] == 'node_process_start_failed_launcher_ready_timeout'
    assert record['cause']['phase'] == 'launcher_ready'
    assert record['cause']['kind'] == 'timeout'
    assert record['cause']['budget_ms'] == 2000
    assert record['cause']['elapsed_ms'] >= 2000
    assert record['cause']['launcher_returncode'] == -15
    assert str(tmp_path) not in path.read_text()


def test_a_failure_without_a_cause_writes_no_start_record(tmp_path, monkeypatch):
    capacity = _capacity(tmp_path)
    instance = worker(tmp_path, capacity_root=capacity)

    def refuse(**_kwargs):
        raise native.NodeProcessError('node_process_executable_changed')

    monkeypatch.setattr(worker_module, '_ConcurrentNativeSession', refuse)
    with pytest.raises(native.NodeProcessError, match='node_process_executable_changed'):
        instance.start()
    instance.close()
    assert not (capacity / 'node-start-failure.private.json').exists()


def test_successful_start_records_the_launch_budget_headroom(tmp_path, monkeypatch):
    capacity = _capacity(tmp_path)
    timing = {'budget_ms': 2000, 'popen_ms': 12,
              'phase_ms': {'launcher_ready': 310, 'pre_exec_ownership': 330,
                           'launcher_release': 331, 'target_exec': 360,
                           'inventory': 380, 'identity': 381}}

    class Session:
        def __init__(self, **kwargs):
            self.pid = os.getpid()
            self.launch_timing = timing

        returncode = None
        stderr = b''

        def send(self, **_kwargs):
            return {'ok': True}

        def close(self):
            pass

    monkeypatch.setattr(worker_module, '_ConcurrentNativeSession', Session)
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    instance = worker(tmp_path, capacity_root=capacity)
    try:
        instance.start()
    finally:
        instance.close()
    record = json.loads((capacity / 'node-start-timing.private.json').read_text())
    assert record['protocol'] == 'mycelium.a13.node_start_timing_diagnostic.v1'
    assert record['timing'] == timing
    assert record['headroom_ms'] == 2000 - 381


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX launcher handshake')
def test_real_launcher_timeout_reaches_the_target_record(tmp_path, monkeypatch):
    """End to end through the real PhysicalNodeProcess, without a model."""

    capacity = _capacity(tmp_path)
    monkeypatch.setattr(native, '_LAUNCH_HANDSHAKE_SECONDS', 0.3)
    monkeypatch.setattr(native, '_LAUNCHER_SOURCE',
                        'import time\ntime.sleep(30)\n' + native._LAUNCHER_SOURCE)
    instance = worker(tmp_path, capacity_root=capacity)
    with pytest.raises(native.NodeProcessError) as caught:
        instance.start()
    instance.close()
    assert caught.value.public_code == 'node_process_start_failed_launcher_ready_timeout'
    record = json.loads((capacity / 'node-start-failure.private.json').read_text())
    assert record['public_code'] == 'node_process_start_failed_launcher_ready_timeout'
    assert record['cause']['launcher_exited'] is False
