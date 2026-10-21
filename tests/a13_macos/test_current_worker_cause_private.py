# SPDX-License-Identifier: AGPL-3.0-only
"""A13 private cold-refusal must name the *current* worker, not a stale exit.

No native/model/network child: external process and connect are fenced before setup.
"""
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest
from test_worker_replacement_readiness import _replacement_fixture
from mycelium_a13_macos.managed_runtime import RuntimeErrorCode
from mycelium_a13_macos.worker import WorkerError, _ConcurrentNativeSession


def test_session_uses_captured_birth_token_only():
    session = object.__new__(_ConcurrentNativeSession)
    session._process = SimpleNamespace(_pre_exec_ownership=SimpleNamespace(start_token='1760000000:12345'))
    assert session.birth_token == '1760000000:12345'
    session._process._pre_exec_ownership.start_token = '/unbounded/path'
    assert session.birth_token is None
    session._process._pre_exec_ownership = None
    assert session.birth_token is None


@pytest.mark.parametrize('actor,expected_code,expected_rc', [
    ('guard', 'worker_membership_not_current', None),
    ('expiry', 'worker_wall_limit_exceeded', None),
    ('explicit_close', None, None),
    ('native_exit', 'worker_native_exited', 0),
])
def test_cold_refusal_identifies_current_worker_and_terminal_actor(
    tmp_path, monkeypatch, actor, expected_code, expected_rc,
):
    def forbidden(*args, **kwargs):
        raise AssertionError('external_call_forbidden')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    runtime, old, new, session, grants = _replacement_fixture(tmp_path, monkeypatch, loaded=True)
    try:
        old._session.returncode = None
        if actor == 'guard':
            monkeypatch.setattr(old, '_check', lambda *_: (_ for _ in ()).throw(
                WorkerError('worker_membership_not_current')))
            old._watch()
        elif actor == 'expiry':
            old._expire()
        elif actor == 'explicit_close':
            old.close()
        else:
            old._session.returncode = 0
            monkeypatch.setattr(old, '_check', lambda *_: None)
            old._watch()
        with pytest.raises(RuntimeErrorCode, match='runtime_cold_restart_required'):
            runtime._replace_worker_if_dead()
        record = json.loads((runtime.root / 'worker-replacement.private.json').read_text())
        assert record['outcome'] == 'refused'
        assert record['error_code'] == 'runtime_cold_restart_required'
        assert record['previous_error_code'] == expected_code
        assert record['previous_shutdown_actor'] == actor
        assert record['previous_returncode'] == expected_rc
        assert record['previous_pid'] is None  # fake session has no native process
        assert record['previous_birth_token'] is None
        assert record['previous_started_at_utc_ms'] is None  # no real handshake
        assert isinstance(record['recorded_at_utc_ms'], int)
        assert runtime.worker is old and runtime.pump.worker is old
        assert not grants and not session.calls and new._session is None
    finally:
        old.close()
        new.close()


def test_cold_refusal_does_not_copy_unbounded_error_or_launch_native(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('external_call_forbidden')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch, loaded=True)
    try:
        old._error = 'private path /not-for-recording or arbitrary error'
        with pytest.raises(RuntimeErrorCode, match='runtime_cold_restart_required'):
            runtime._replace_worker_if_dead()
        text = (runtime.root / 'worker-replacement.private.json').read_text()
        record = json.loads(text)
        assert record['previous_error_code'] is None
        assert '/not-for-recording' not in text
        assert record['previous_returncode'] == 143
        assert record['previous_shutdown_actor'] == 'native_exit_observed_without_guard'
        assert new._session is None and session.calls == []
    finally:
        old.close()
        new.close()


def test_private_snapshot_bounds_actor_birth_and_timestamp(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('external_call_forbidden')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    runtime, old, new, _, _ = _replacement_fixture(tmp_path, monkeypatch, loaded=True)
    try:
        old._session.returncode = None
        old._shutdown_actor = '/unsafe actor'
        old._birth_token = '/unsafe birth'
        old._started_at_utc_ms = 'unsafe timestamp'
        snapshot = old.private_cause_snapshot()
        assert snapshot['previous_shutdown_actor'] is None
        assert snapshot['previous_birth_token'] is None
        assert snapshot['previous_started_at_utc_ms'] is None
        assert '/unsafe' not in json.dumps(snapshot)
    finally:
        old.close()
        new.close()


def test_committed_native_send_exit_keeps_precleanup_rc_and_worker_identity(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('external_call_forbidden')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch, loaded=True)
    try:
        old.pid = 32145  # synthetic identity; no process is launched
        old._birth_token = '1700000000:123456'
        old._started_at_utc_ms = 1700000000123
        def die(**kwargs):
            old._session.returncode = 7
            raise WorkerError('worker_native_exited')
        monkeypatch.setattr(old._session, 'send', die)
        with pytest.raises(WorkerError, match='worker_native_exited'):
            old.send(command_id='last-hello', command='hello', payload={})
        with pytest.raises(RuntimeErrorCode, match='runtime_cold_restart_required'):
            runtime._replace_worker_if_dead()
        record = json.loads((runtime.root / 'worker-replacement.private.json').read_text())
        assert record['previous_shutdown_actor'] == 'native_send_exit'
        assert record['previous_error_code'] == 'worker_native_exited'
        assert record['previous_returncode'] == 7
        assert record['previous_pid'] == 32145
        assert record['previous_birth_token'] == '1700000000:123456'
        assert record['previous_started_at_utc_ms'] == 1700000000123
        assert old.cleanup_proven() and new._session is None and session.calls == []
    finally:
        old.close()
        new.close()
