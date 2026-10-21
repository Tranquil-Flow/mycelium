# SPDX-License-Identifier: AGPL-3.0-only
"""The busy-app read timeout: bounded, derived from measurement, and fail-closed.

74r1 died at the stream step with `TimeoutError: timed out` at `self._sock.recv_into(b)` right after
ACTIVATION was requested. The cause is a latency mismatch on one channel: app_rpc.py:15's FIXED 10 s
handshake timeout (a PINNED file, unraisable) against calls that legitimately take 50-104 s while the
model loads -- target WORK_ACCEPTED measured 79.4 / 78.6 / 104.1 s across the three passing rounds.

These tests pin three things: that a busy timeout is retried, that the retry is BOUNDED, and that the
budget stays derived from the measurement rather than drifting below it. The last one is the point --
a future edit that quietly shrinks the budget back under the observed maximum would reinstate exactly
the failure this fix exists for.
"""
import importlib.util
import os
import pathlib
import time

import pytest

OPERATOR = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'
for key, value in (('A13_OWNER_ROOT', '/tmp/a13-owner-test'),
                   ('A13_REMOTE_ROOT', '/tmp/a13-remote-test'),
                   ('A13_TARGET_PREVIEW', 'preview70'),
                   ('A13_RPC_ROUND', 'rpc-round70')):
    os.environ.setdefault(key, value)
pathlib.Path('/tmp/a13-owner-test').mkdir(parents=True, exist_ok=True)

_spec = importlib.util.spec_from_file_location('round14_rpc', OPERATOR / 'round14_rpc.py')
rpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rpc)

# The slowest legitimate single call actually observed (72r1, target WORK_ACCEPTED).
MEASURED_MAX_S = 104.123


def test_the_budget_is_derived_from_the_measured_maximum():
    """The derivation is the evidence. If someone lowers this below the observation, it is a bug."""
    assert rpc._OWNER_CALL_BUDGET_S >= MEASURED_MAX_S * 1.10
    assert rpc._OWNER_CALL_BUDGET_S <= MEASURED_MAX_S * 4        # and it stays a deadline


def test_a_busy_timeout_is_retried_and_then_succeeds(monkeypatch):
    calls = {'n': 0}

    class Flaky:
        def call(self, path, body=None):
            calls['n'] += 1
            if calls['n'] < 3:
                raise TimeoutError('timed out')
            return {'state': 'api_running'}

    monkeypatch.setattr(rpc, '_client', lambda root: Flaky())
    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    monkeypatch.setattr(rpc.time, 'sleep', lambda s: None)
    assert rpc.owner('/api/serving/status', {}) == {'state': 'api_running'}
    assert calls['n'] == 3


def test_a_non_busy_failure_fails_closed_immediately(monkeypatch):
    """A refused connection or bad credential must NOT be retried into the deadline."""
    calls = {'n': 0}

    class Refused:
        def call(self, path, body=None):
            calls['n'] += 1
            raise ConnectionRefusedError('connection refused')

    monkeypatch.setattr(rpc, '_client', lambda root: Refused())
    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    with pytest.raises(ConnectionRefusedError):
        rpc.owner('/api/serving/status', {})
    assert calls['n'] == 1


def test_a_permanently_busy_app_still_gives_up(monkeypatch):
    """Bounded, not infinite: the budget has to end somewhere, and it must end."""
    calls = {'n': 0}
    clock = {'t': 0.0}

    class AlwaysBusy:
        def call(self, path, body=None):
            calls['n'] += 1
            clock['t'] += 30.0          # each attempt burns real budget
            raise TimeoutError('timed out')

    monkeypatch.setattr(rpc, '_client', lambda root: AlwaysBusy())
    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    monkeypatch.setattr(rpc.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(rpc.time, 'sleep', lambda s: None)
    with pytest.raises(TimeoutError):
        rpc.owner('/api/serving/status', {})
    assert calls['n'] <= 6              # it stopped rather than looping forever
