# SPDX-License-Identifier: AGPL-3.0-only
"""A closed native worker must report its recorded cause, not a generic code.

Regression: the serving native process exited, the guard recorded
``worker_native_exited``, but every later command was refused with a generic
``worker_not_running``. That weaker code is what the target pump turned into
``reconciliation_required`` and the owner into an opaque fail-closed route.
No model execution here.
"""
from threading import Lock
from types import SimpleNamespace as NS

import pytest

from mycelium_a13_macos.worker import ManagedWorker, WorkerError, _unavailable_code


def _accounting(**changes):
    values = dict(
        _account_lock=Lock(),
        _session=None,
        _closed=NS(is_set=lambda: True),
        _error=None,
        _commands=set(),
        _pending_commands=set(),
        _command_count=0,
        limits=NS(max_commands=8),
    )
    values.update(changes)
    return NS(**values)


def test_unavailable_code_prefers_recorded_native_exit():
    worker = _accounting(_error='worker_native_exited')
    assert _unavailable_code(worker) == 'worker_native_exited'


def test_unavailable_code_rejects_anything_that_is_not_a_bounded_code():
    for value in (None, '', 'Native one-liner with spaces', 'UpperCase', 'x' * 200, 123, b'bytes'):
        worker = _accounting(_error=value)
        assert _unavailable_code(worker) == 'worker_not_running'


def test_reserve_command_reports_the_recorded_cause():
    worker = _accounting(_error='worker_native_exited')
    with pytest.raises(WorkerError, match='^worker_native_exited$'):
        ManagedWorker._reserve_command(worker, 'managed-hello')


def test_reserve_command_reports_the_cause_when_the_session_is_still_attached():
    worker = _accounting(_session=object(), _error='worker_native_exited')
    with pytest.raises(WorkerError, match='^worker_native_exited$'):
        ManagedWorker._reserve_command(worker, 'managed-hello')


def test_reserve_command_still_admits_a_live_worker():
    session = object()
    worker = _accounting(_session=session, _closed=NS(is_set=lambda: False))
    assert ManagedWorker._reserve_command(worker, 'managed-hello') is session


def test_commit_command_reports_the_recorded_cause():
    worker = _accounting(_session=object(), _error='worker_native_exited')
    with pytest.raises(WorkerError, match='^worker_native_exited$'):
        ManagedWorker._commit_command(worker, 'managed-hello', worker._session)
