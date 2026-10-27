# SPDX-License-Identifier: AGPL-3.0-only
"""Cold-restart policy supersedes retained same-run configure/start replay.

No model/network execution: local worker doubles exercise the production boundary.
A pre-load process replacement is not a qualified route or old-request continuity.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime, RuntimeErrorCode
from mycelium_a13_macos.worker import WorkerError
from test_worker import worker
from test_worker_concurrency import FakeConcurrentSession, attach_fake, configure_payload


def _replacement_fixture(tmp_path, monkeypatch, *, permit_replacement=True, loaded=False):
    old_root = tmp_path / 'old'
    new_root = tmp_path / 'new'
    old_root.mkdir()
    new_root.mkdir()
    old = worker(old_root, authorize_load=lambda envelope: True)
    old_session = FakeConcurrentSession()
    attach_fake(old, old_session)
    if loaded:
        old.send(command_id='initial-configure', command='configure', payload=configure_payload())
        old.send(command_id='initial-start', command='start', payload={'peer': {}, 'local_generation': 1})
    else:
        for index in range(2):
            old.send(command_id=f'initial-hello-{index}', command='hello', payload={})
    old_session.returncode = 143
    authorized = []
    def grant(envelope):
        authorized.append(envelope['command'])
        return permit_replacement
    new = worker(new_root, authorize_load=grant)
    session = FakeConcurrentSession()
    def start():
        attach_fake(new, session)
        return session._success('hello', 'hello')
    monkeypatch.setattr(new, 'start', start)
    runtime = object.__new__(ManagedTargetRuntime)
    runtime.root = tmp_path / 'runtime'
    runtime.root.mkdir(mode=0o700)
    runtime.run_id = 'run-test'
    runtime._stop = threading.Event()
    runtime._lock = threading.RLock()
    runtime._state = 'running'
    runtime._error = None
    runtime._worker_replacements = 0
    runtime._worker_generation = 0
    runtime._replacement_worker = None
    runtime.worker = old
    runtime._authorize_load = grant  # a work runtime: the load authorizer is attached
    runtime.pump = SimpleNamespace(worker=old)
    monkeypatch.setattr(runtime, '_make_worker', lambda: new)
    return runtime, old, new, session, authorized


@pytest.mark.parametrize('permit', [True, False])
@pytest.mark.parametrize('attempts', [0, 3])
def test_loaded_worker_death_requires_cold_run_not_startup_replay(tmp_path, monkeypatch, permit, attempts):
    runtime, old, new, session, authorized = _replacement_fixture(
        tmp_path, monkeypatch, loaded=True, permit_replacement=permit)
    runtime._worker_replacements = attempts
    try:
        with pytest.raises(RuntimeErrorCode, match='runtime_cold_restart_required'):
            runtime._replace_worker_if_dead()
        assert runtime.worker is old and runtime.pump.worker is old
        assert runtime._stop.is_set()
        assert runtime.status()['route_ready'] is False
        assert runtime.status()['state'] == 'blocked'
        assert runtime._worker_replacements == attempts
        assert not session.calls and not authorized and new._session is None
        assert old.cleanup_proven()
        record = json.loads((runtime.root / 'worker-replacement.private.json').read_text())
        assert record['outcome'] == 'refused'
        assert record['error_code'] == 'runtime_cold_restart_required'
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize('ack', ['delivered', 'lost'])
def test_owner_requested_stop_of_a_loaded_worker_keeps_the_control_loop_alive(tmp_path, monkeypatch, ack):
    """F16 (device attempt10, 2026-10-27): the owner's route close sends `stop`; the native worker exits
    rc 0 as asked. That exit is the requested outcome, not a death: the runtime must stay up so its pump
    publishes the stop response and later cleanup receipts. Treating it as `runtime_cold_restart_required`
    killed the control loop, so the owner waited for the stop command to expire (~300 s) and reported
    `owner_route_stop_unproven` for every Stop and rollover."""
    runtime, old, new, session, authorized = _replacement_fixture(tmp_path, monkeypatch, loaded=True)
    old._session.returncode = None
    assert old.requires_cold_restart()
    if ack == 'lost':
        original_send = old._session.send
        def lost(**kwargs):
            original_send(**kwargs)
            raise TimeoutError('stop_result_unknown')
        monkeypatch.setattr(old._session, 'send', lost)
    try:
        if ack == 'lost':
            with pytest.raises(TimeoutError):
                old.send(command_id='owner-stop', command='stop', payload={})
        else:
            assert old.send(command_id='owner-stop', command='stop', payload={})['ok']
        old._session.returncode = 0  # the native process exits as asked
        for _ in range(3):  # the control loop calls this every tick
            runtime._replace_worker_if_dead()
        assert not runtime._stop.is_set(), 'a requested stop killed the control loop'
        assert runtime.status()['state'] == 'running'
        assert runtime.worker is old and runtime.pump.worker is old
        assert runtime._worker_replacements == 0
        assert not session.calls and not authorized and new._session is None
        assert old.cleanup_proven()
        record = json.loads((runtime.root / 'worker-replacement.private.json').read_text())
        assert record['outcome'] == 'refused'
        assert record['error_code'] == 'worker_owner_stopped'
        with pytest.raises(WorkerError, match='worker_owner_stopped'):
            old.send(command_id='late-inference', command='infer_start', payload={'request': {}})
    finally:
        old.close()
        new.close()


def test_preload_replacement_needs_fresh_exact_authorization(tmp_path, monkeypatch):
    runtime, old, new, session, authorized = _replacement_fixture(tmp_path, monkeypatch)
    try:
        runtime._replace_worker_if_dead()
        assert runtime.worker is new
        assert session.calls == [] and authorized == []
        assert not new.status()['model_load_authorized']
        assert not runtime.status()['route_ready']
        with pytest.raises(WorkerError, match='model_load_not_admitted'):
            new.send(command_id='too-early', command='infer_start', payload={'request': {}})
        new.send(command_id='fresh-configure', command='configure', payload=configure_payload())
        new.send(command_id='fresh-start', command='start', payload={'peer': {}, 'local_generation': 1})
        assert new.send(command_id='fresh-request', command='infer_start', payload={'request': {}})['ok']
        assert authorized == ['configure', 'start', 'infer_start']
        assert [call[1] for call in session.calls] == authorized
    finally:
        old.close()
        new.close()


def test_preload_replacement_refuses_revoked_grant(tmp_path, monkeypatch):
    runtime, old, new, session, authorized = _replacement_fixture(tmp_path, monkeypatch, permit_replacement=False)
    try:
        runtime._replace_worker_if_dead()
        with pytest.raises(WorkerError, match='model_execution_not_authorized'):
            new.send(command_id='configure', command='configure', payload=configure_payload())
        assert not new.status()['model_load_authorized']
        assert not session.calls and authorized == ['configure']
    finally:
        old.close()
        new.close()


def _blocked_start(monkeypatch, new, session):
    def start():
        attach_fake(new, session)
        session.entered.set()
        assert session.release.wait(2)
    monkeypatch.setattr(new, 'start', start)


def test_stop_during_start_cannot_publish_replacement(tmp_path, monkeypatch):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    _blocked_start(monkeypatch, new, session)
    thread = threading.Thread(target=runtime._replace_worker_if_dead)
    try:
        thread.start()
        assert session.entered.wait(1)
        assert runtime.worker is old and not runtime.status()['worker_running']
        runtime._stop.set()
        session.release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert runtime.worker is old and runtime.pump.worker is old
        assert session.returncode is not None
    finally:
        session.release.set()
        thread.join(2)
        old.close()
        new.close()


def test_close_interrupts_unpublished_replacement_before_join(tmp_path, monkeypatch):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    _blocked_start(monkeypatch, new, session)
    runtime._socket_parent = tmp_path / 'sockets'
    runtime._socket_parent.mkdir()
    runtime.pump.wake = lambda: None
    runtime.pump.close = old.close
    runtime._thread = threading.Thread(target=runtime._replace_worker_if_dead)
    try:
        runtime._thread.start()
        assert session.entered.wait(1)
        runtime.close()
        assert not runtime._thread.is_alive()
        assert runtime._state == 'closed'
        assert new.cleanup_proven() and session.returncode is not None
        assert runtime.worker is old
    finally:
        session.release.set()
        runtime._thread.join(2)
        old.close()
        new.close()


@pytest.mark.parametrize('close_outcome', ['raises', 'returns_live'])
def test_failed_replacement_cleanup_remains_owned_and_stops_runtime(tmp_path, monkeypatch, close_outcome):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    def failed_start():
        attach_fake(new, session)
        raise WorkerError('worker_native_start_failed')
    monkeypatch.setattr(new, 'start', failed_start)
    original_close = new.close
    def unproven_close():
        if close_outcome == 'raises':
            raise RuntimeErrorCode('runtime_stop_incomplete')
    monkeypatch.setattr(new, 'close', unproven_close)
    monkeypatch.setattr(new, 'process_group_present', lambda: True)
    try:
        with pytest.raises(RuntimeErrorCode, match='runtime_replacement_cleanup_unproven'):
            runtime._replace_worker_if_dead()
        assert runtime._replacement_worker is new and runtime._stop.is_set()
        assert runtime.worker is old and runtime.pump.worker is old
    finally:
        old.close()
        original_close()


def test_native_startup_refusal_never_publishes_replacement(tmp_path, monkeypatch):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    def failed_start():
        attach_fake(new, session)
        raise WorkerError('worker_native_start_failed')
    monkeypatch.setattr(new, 'start', failed_start)
    try:
        runtime._replace_worker_if_dead()
        assert runtime.pump.worker is old
        assert session.returncode is not None and new.cleanup_proven()
        assert json.loads((runtime.root / 'worker-replacement.private.json').read_text())['outcome'] == 'failed'
    finally:
        old.close()
        new.close()
