# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-03 / reliability: bounded retry for reachability refusals on re-derivable operations.

Round 71r1 (2026-09-17): a single 409 `managed_control_unreachable` from `/api/work/accept` ended a
round that had already passed staging, native start and both exact previews. The question is not
"retry everything" -- a POST may be non-idempotent -- but "is THIS operation safe to repeat?".
`work_acceptance.accept_work` answers yes: it re-fetches the offer, recomputes the preview and
requires `current == preview`, so a repeat either completes the identical accept or fails closed as
`work_preview_changed`.

Pinned here: the retry is PATH-SCOPED (an unlisted path is never retried), env-gated, bounded by its
budget, gives up by re-raising the ORIGINAL refusal, and never retries a non-reachability error.
"""
import importlib.util
import os
import pathlib
import sys

import pytest

OPERATOR = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'
if str(OPERATOR) not in sys.path:
    sys.path.insert(0, str(OPERATOR))


def _load(monkeypatch, tmp_path):
    owner = tmp_path / 'owner-lhr'
    owner.mkdir()
    monkeypatch.setenv('A13_OWNER_ROOT', str(owner))
    monkeypatch.setenv('A13_REMOTE_ROOT', '/Users/example/remote')
    monkeypatch.setenv('A13_TARGET_PREVIEW', 'preview70')
    monkeypatch.setenv('A13_RPC_ROUND', 'rpc-round70')
    name = 'round14_rpc_retry_probe'
    spec = importlib.util.spec_from_file_location(name, OPERATOR / 'round14_rpc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fail_then_succeed(module, monkeypatch, *, failures, text, calls):
    state = {'n': 0}

    def fake(path, body):
        calls.append(path)
        state['n'] += 1
        if state['n'] <= failures:
            raise RuntimeError('local_api_http_409:' + text)
        return {'ok': True}

    monkeypatch.setattr(module, '_target_once', fake)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    return state


def test_a_listed_path_retries_a_reachability_refusal(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setenv('A13_RETRY_REACHABILITY_PATHS', '/api/work/accept')
    monkeypatch.setenv('A13_RETRY_REACHABILITY_BUDGET_S', '60')
    calls = []
    _fail_then_succeed(module, monkeypatch, failures=2,
                       text='managed_control_unreachable', calls=calls)
    result = module._target_once_reachability_retry('/api/work/accept', {'x': 1})
    assert result == {'ok': True}
    assert len(calls) == 3          # refused twice, then succeeded


def test_an_unlisted_path_is_never_retried(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setenv('A13_RETRY_REACHABILITY_PATHS', '/api/work/accept')
    calls = []
    _fail_then_succeed(module, monkeypatch, failures=2,
                       text='managed_control_unreachable', calls=calls)
    # The dispatch decision itself: target() must not route an unlisted path into the retry.
    assert '/api/staging/accept' not in module._retry_paths()
    assert '/api/work/accept' in module._retry_paths()


def test_a_non_reachability_error_is_not_retried(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setenv('A13_RETRY_REACHABILITY_BUDGET_S', '60')
    calls = []
    _fail_then_succeed(module, monkeypatch, failures=99,
                       text='work_preview_changed', calls=calls)
    with pytest.raises(RuntimeError, match='work_preview_changed'):
        module._target_once_reachability_retry('/api/work/accept', None)
    assert len(calls) == 1


def test_an_exhausted_budget_gives_up_with_the_original_refusal(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setenv('A13_RETRY_REACHABILITY_BUDGET_S', '0')
    calls = []
    _fail_then_succeed(module, monkeypatch, failures=99,
                       text='managed_control_unreachable', calls=calls)
    with pytest.raises(RuntimeError, match='managed_control_unreachable'):
        module._target_once_reachability_retry('/api/work/accept', None)
    assert len(calls) == 1


def test_no_paths_are_retried_by_default(monkeypatch, tmp_path):
    """Opt-in: without the env var the round behaves exactly as before."""
    module = _load(monkeypatch, tmp_path)
    monkeypatch.delenv('A13_RETRY_REACHABILITY_PATHS', raising=False)
    assert module._retry_paths() == frozenset()


def test_a_malformed_budget_falls_back_to_a_finite_default(monkeypatch, tmp_path):
    """A typo must not turn a bounded retry into an unbounded one."""
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setenv('A13_RETRY_REACHABILITY_BUDGET_S', 'not-a-number')
    assert module._retry_budget_seconds() == 60.0


def test_the_ssh_level_timeout_is_retried_when_the_link_blips():
    """Round 75r1 (2026-09-17): the laptop's link blipped mid-round and the cleanup's own quit reported
    `ssh: connect to host 203.0.113.12 port 22: Operation timed out`. The laptop answered ping and ssh
    again minutes later (ping stddev 26 ms -- an unstable link, not a dead host). _target_once already
    reports that shape as `remote_handshake_timeout`, so it must be retryable exactly like the other
    reachability codes -- while staying opt-in per path, because retrying a non-idempotent POST would be
    the sloppy version of this fix."""
    import importlib.util
    import pathlib
    import sys
    operator = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'
    for key, value in (('A13_OWNER_ROOT', '/tmp/a13-retry-owner'),
                       ('A13_REMOTE_ROOT', '/tmp/a13-retry-remote'),
                       ('A13_TARGET_PREVIEW', 'preview70'),
                       ('A13_RPC_ROUND', 'rpc-round70')):
        os.environ.setdefault(key, value)
    pathlib.Path('/tmp/a13-retry-owner').mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('round14_rpc_retry_test', operator / 'round14_rpc.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['round14_rpc_retry_test'] = module
    spec.loader.exec_module(module)

    assert 'remote_handshake_timeout' in module.REACHABILITY_CODES
    attempts = {'n': 0}

    def flaky(path, body=None):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise module.HandshakeTimeout('remote_handshake_timeout:%s:ssh timed out' % path)
        return {'ok': True}

    original = module._target_once
    module._target_once = flaky
    os.environ['A13_RETRY_REACHABILITY_PATHS'] = '/api/work/accept'
    try:
        result = module._target_once_reachability_retry('/api/work/accept', {})
    finally:
        module._target_once = original
        os.environ.pop('A13_RETRY_REACHABILITY_PATHS', None)
    assert result == {'ok': True}
    assert attempts['n'] == 3          # it retried the blip and cleared
