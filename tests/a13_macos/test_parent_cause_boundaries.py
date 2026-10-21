# SPDX-License-Identifier: AGPL-3.0-only
"""Discriminating privacy and competing-actor controls for private diagnostics."""
import json
import socket
import subprocess
import threading

from test_worker_replacement_readiness import _replacement_fixture
from mycelium_a13_macos.worker import WorkerError
from mycelium_a13_macos.managed_runtime import RuntimeErrorCode
import pytest


def fenced_fixture(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('external_effect_refused')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    return _replacement_fixture(tmp_path, monkeypatch, loaded=True)


def test_unknown_regex_shaped_cause_is_not_an_allowlisted_code(tmp_path, monkeypatch):
    runtime, old, new, _, _ = fenced_fixture(tmp_path, monkeypatch)
    try:
        old._error = 'private_canary_123'
        with pytest.raises(RuntimeErrorCode, match='runtime_cold_restart_required'):
            runtime._replace_worker_if_dead()
        text = (runtime.root / 'worker-replacement.private.json').read_text()
        record = json.loads(text)
        assert record['previous_error_code'] is None
        assert 'private_canary_123' not in text
    finally:
        old.close()
        new.close()


def test_first_actor_keeps_its_own_code_when_other_writer_changes_global_error(tmp_path, monkeypatch):
    runtime, old, new, _, _ = fenced_fixture(tmp_path, monkeypatch)
    old._session.returncode = None
    mark = old._mark_shutdown_actor
    expiry_at_mark = threading.Event()
    guard_at_mark = threading.Event()
    expiry_release = threading.Event()
    guard_release = threading.Event()
    failures = []

    def gated(actor, *args, **kwargs):
        if actor == 'expiry':
            expiry_at_mark.set()
            assert expiry_release.wait(2)
        elif actor == 'guard':
            guard_at_mark.set()
            assert guard_release.wait(2)
        return mark(actor, *args, **kwargs)

    def checked(operation):
        try:
            operation()
        except BaseException as exc:
            failures.append(type(exc).__name__)

    monkeypatch.setattr(old, '_mark_shutdown_actor', gated)
    monkeypatch.setattr(old, '_check', lambda *_: (_ for _ in ()).throw(WorkerError('worker_membership_not_current')))
    expiry = threading.Thread(target=lambda: checked(old._expire))
    guard = threading.Thread(target=lambda: checked(old._watch))
    try:
        expiry.start()
        assert expiry_at_mark.wait(2)
        guard.start()
        assert guard_at_mark.wait(2)
        # Both writers assigned _error. Let expiry publish the first actor snapshot.
        expiry_release.set()
        expiry.join(2)
        assert not expiry.is_alive()
        guard_release.set()
        guard.join(2)
        assert not guard.is_alive() and not failures
        snapshot = old.private_cause_snapshot()
        assert snapshot['previous_shutdown_actor'] == 'expiry'
        assert snapshot['previous_error_code'] == 'worker_wall_limit_exceeded'
        assert snapshot['previous_returncode'] is None
    finally:
        expiry_release.set()
        guard_release.set()
        if expiry.ident is not None:
            expiry.join(2)
        if guard.ident is not None:
            guard.join(2)
        old.close()
        new.close()
