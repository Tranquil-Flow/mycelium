# SPDX-License-Identifier: AGPL-3.0-only
"""79r1's family: a refusal at STARTUP is not a refusal MID-RUN.

79r1 failed at bootstrap_compile with `URLError: <urlopen error [Errno 61] Connection refused>` while the
owner's native-control.json already existed -- the surface was written before the listener bound. The
operator saw the surface and called; the port was not open yet.

_busy_reason deliberately treats a refusal as NOT retryable, and that is right mid-run: a refusal then
means the app died. So the grace must be narrow, and these tests pin exactly how narrow: it applies only
until the channel has answered once, it is bounded, and everything else still fails closed.
"""
import importlib.util
import os
import pathlib

import pytest

OPERATOR = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'
for key, value in (('A13_OWNER_ROOT', '/tmp/a13-grace-owner'),
                   ('A13_REMOTE_ROOT', '/tmp/a13-grace-remote'),
                   ('A13_TARGET_PREVIEW', 'preview70'),
                   ('A13_RPC_ROUND', 'rpc-round70')):
    os.environ.setdefault(key, value)
pathlib.Path('/tmp/a13-grace-owner').mkdir(parents=True, exist_ok=True)

_spec = importlib.util.spec_from_file_location('round14_rpc_grace', OPERATOR / 'round14_rpc.py')
rpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rpc)


def _wire(monkeypatch, client, *, clock=None):
    monkeypatch.setattr(rpc, 'require_local', lambda root: None)
    monkeypatch.setattr(rpc, '_client', lambda root: client)
    monkeypatch.setattr(rpc.time, 'sleep', lambda s: None)
    rpc._OWNER_ANSWERED.clear()
    if clock is not None:
        monkeypatch.setattr(rpc.time, 'monotonic', lambda: clock['t'])


def test_a_refusal_before_the_first_reply_is_tolerated_then_succeeds(monkeypatch):
    calls = {'n': 0}

    class Warming:
        def call(self, path, body=None):
            calls['n'] += 1
            if calls['n'] < 3:
                raise ConnectionRefusedError(61, 'Connection refused')
            return {'state': 'api_running'}

    _wire(monkeypatch, Warming())
    assert rpc.owner('/api/serving/status', {}) == {'state': 'api_running'}
    assert calls['n'] == 3


def test_a_refusal_AFTER_the_channel_has_answered_fails_closed(monkeypatch):
    """The mid-run case. A refusal here means the app died; retrying would hide that."""
    calls = {'n': 0}

    class AliveThenDead:
        def call(self, path, body=None):
            calls['n'] += 1
            if calls['n'] == 1:
                return {'ok': True}
            raise ConnectionRefusedError(61, 'Connection refused')

    _wire(monkeypatch, AliveThenDead())
    assert rpc.owner('/api/serving/status', {}) == {'ok': True}
    with pytest.raises(ConnectionRefusedError):
        rpc.owner('/api/serving/status', {})
    assert calls['n'] == 2          # exactly one attempt on the dead call


def test_the_grace_is_bounded(monkeypatch):
    calls = {'n': 0}
    clock = {'t': 0.0}

    class NeverBinding:
        def call(self, path, body=None):
            calls['n'] += 1
            clock['t'] += 5.0
            raise ConnectionRefusedError(61, 'Connection refused')

    _wire(monkeypatch, NeverBinding(), clock=clock)
    with pytest.raises(ConnectionRefusedError):
        rpc.owner('/api/serving/status', {})
    assert calls['n'] <= 8          # it stopped rather than waiting forever


def test_a_non_refusal_error_is_never_graced(monkeypatch):
    calls = {'n': 0}

    class BadCredential:
        def call(self, path, body=None):
            calls['n'] += 1
            raise PermissionError('bad credential')

    _wire(monkeypatch, BadCredential())
    with pytest.raises(PermissionError):
        rpc.owner('/api/serving/status', {})
    assert calls['n'] == 1


def test_a_url_error_wrapping_refused_is_recognised(monkeypatch):
    import urllib.error
    assert rpc._is_refused(ConnectionRefusedError(61, 'Connection refused'))
    assert rpc._is_refused(urllib.error.URLError('[Errno 61] Connection refused'))
    assert not rpc._is_refused(TimeoutError('timed out'))
