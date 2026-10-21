# SPDX-License-Identifier: AGPL-3.0-only
"""The managed worker's native command/response budget must be derived, not 5 s.

Regression: ``--command-timeout`` was hardcoded to 5 and the app<->native
session response read to 10, while the canonical route uses
``NODE_COMMAND_TIMEOUT_SECONDS = 900`` / ``NODE_SESSION_TIMEOUT_SECONDS = 930``
and the managed session cap is 240.  The startup challenge's first *cold*
distributed prefill (``infer_start``) needs far more than 5 s, so it raised
``TimeoutError``, the native worker exited, and the route failed closed.

No model execution here: the session is replaced with a fake.
"""
import os

import pytest

import mycelium_a13_macos.worker as worker_module
from mycelium_a13_macos.control_session import MAX_SESSION_TIMEOUT_SECONDS
from mycelium_a13_macos.worker import ManagedWorker, WorkerLimits, _native_budgets
from test_worker import worker


def _limits(**changes):
    values = dict(
        max_rss_bytes=2_000_000_000,
        max_disk_bytes=10_000_000,
        max_wall_seconds=1800,
        max_commands=8,
    )
    values.update(changes)
    return WorkerLimits(**values)


def test_budget_stays_strictly_inside_the_managed_session_cap():
    """The native budget must leave transit room inside the owner's window.

    Round35 regression: with the native budget pinned to the cap (240 s) the
    native only answered at 192 s, the owner had already given up, and the run
    recorded ``managed_control_response_timeout`` with ``resp_code: None`` -- the
    native's real cause was discarded.  The budget must be strictly below the cap.
    """
    command, response = _native_budgets(_limits(max_wall_seconds=3600))
    assert command == response
    assert 0 < command < MAX_SESSION_TIMEOUT_SECONDS
    assert command == pytest.approx(
        MAX_SESSION_TIMEOUT_SECONDS * worker_module._NATIVE_RESPONSE_TRANSIT_MARGIN
    )


def test_command_budget_is_no_longer_five_seconds():
    command, response = _native_budgets(_limits(max_wall_seconds=1800))
    assert command > 5
    assert response > 5
    assert command == response


def test_prefill_wait_and_response_transit_fit_inside_the_owner_window():
    """The native's own prefill wait plus response transit must fit in the cap.

    The native prefill wait is ``command_timeout * 0.8``.  Pinning the budget to
    the 240 s cap makes that product 192 s, leaving only 48 s for the answer to
    travel back -- which round35 exceeded.  The invariant that prevents it:
    the native answers within its own budget, and the budget is small enough that
    the answer still transits before the owner's window closes.
    """
    command, _ = _native_budgets(_limits(max_wall_seconds=1800))
    prefill_wait = command * 0.8
    transit_margin = MAX_SESSION_TIMEOUT_SECONDS - command
    assert prefill_wait <= command            # native answers inside its budget
    assert command < MAX_SESSION_TIMEOUT_SECONDS
    assert transit_margin >= 30               # a real allowance, not rounding
    assert prefill_wait + transit_margin <= MAX_SESSION_TIMEOUT_SECONDS


def test_budget_honours_the_cleanup_reserve_below_the_cap():
    command, response = _native_budgets(_limits(max_wall_seconds=120))
    assert command == response == 110


def test_budget_stays_positive_and_finite():
    for wall in (11, 30, 120, 1800, 3600):
        command, response = _native_budgets(_limits(max_wall_seconds=wall))
        assert 0 < command <= MAX_SESSION_TIMEOUT_SECONDS
        assert command == response


def test_start_argv_carries_the_derived_command_timeout(tmp_path):
    instance = worker(tmp_path)
    argv = ManagedWorker._native_argv(instance)
    supplied = argv[argv.index('--command-timeout') + 1]
    assert float(supplied) == _native_budgets(instance.limits)[0]
    assert float(supplied) > 5


def test_start_uses_the_derived_response_budget(tmp_path, monkeypatch):
    captured = {}

    class _FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.pid = os.getpid()
            self.node_id = kwargs['node_id']

        @property
        def returncode(self):
            return None

        @property
        def stderr(self):
            return b''

        def send(self, **kwargs):
            return {'ok': True}

        def close(self):
            pass

    monkeypatch.setattr(worker_module, '_ConcurrentNativeSession', _FakeSession)
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid)
    instance = worker(tmp_path)
    try:
        instance.start()
        assert captured['timeout_seconds'] == _native_budgets(instance.limits)[1]
        assert captured['timeout_seconds'] > 5
    finally:
        instance.close()
