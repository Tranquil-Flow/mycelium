# SPDX-License-Identifier: AGPL-3.0-only
"""reconnect_target's real retry behaviour, at the operator's own seam.

The policy module is tested next door; this pins that the operator actually uses
it. 65r1 died 17 s into a 180 s budget on `seed_http_seed_time_invalid` at
attempt 1 -- a rejection that re-mints on the next request -- while ~283 s were
still left on the card.
"""
import importlib.util
import pathlib
import sys
import tempfile
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / 'scripts/a13_operator/round14_bootstrap_compile.py'


def _load_operator():
    stub = types.ModuleType('round14_rpc')
    stub.owner = lambda *a, **k: {}
    stub.target = lambda *a, **k: {}
    stub.PRIVATE = pathlib.Path(tempfile.mkdtemp())
    stub.REMOTE = '/tmp/a13-remote-stub'
    saved = sys.modules.get('round14_rpc')
    sys.modules['round14_rpc'] = stub
    try:
        spec = importlib.util.spec_from_file_location('a13_bootstrap_compile', TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop('round14_rpc', None)
        else:
            sys.modules['round14_rpc'] = saved
    return module


@pytest.fixture()
def operator(monkeypatch):
    module = _load_operator()
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(module, 'RECONNECT_BUDGET_SECONDS', 30.0)
    return module


def _target_raising(module, sequence, calls):
    def target(path, payload=None):
        calls.append(path)
        item = sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    module.target = target


def test_a_window_rejection_is_retried_and_the_attempt_is_recorded(operator):
    calls = []
    _target_raising(operator, [RuntimeError('local_api_http_409:seed_http_seed_time_invalid'),
                               {'connected': True}], calls)
    observations = []
    operator.reconnect_target({'statement': 'card'}, observations)

    assert len(calls) == 2                      # retried, then connected
    first = observations[0]
    assert first['attempt'] == 1 and first['outcome'] == 'failed'
    assert first['codes'] == ['seed_http_seed_time_invalid']
    assert first['transient'] is True
    assert 'seed_http_seed_time_invalid' in first['error']   # raw text kept
    assert observations[1]['outcome'] == 'connected'


def test_a_product_refusal_still_fails_closed_on_the_first_attempt(operator):
    calls = []
    _target_raising(operator, [RuntimeError('managed_control_reconciliation_required')], calls)
    with pytest.raises(RuntimeError, match='managed_control_reconciliation_required'):
        operator.reconnect_target({'statement': 'card'}, [])
    assert len(calls) == 1


def test_an_exhausted_budget_stops_even_a_retryable_code(operator, monkeypatch):
    calls = []
    monkeypatch.setattr(operator, 'RECONNECT_BUDGET_SECONDS', 0.0)
    _target_raising(operator, [RuntimeError('seed_http_seed_time_invalid')], calls)
    with pytest.raises(RuntimeError, match='seed_http_seed_time_invalid'):
        operator.reconnect_target({'statement': 'card'}, [])
    assert len(calls) == 1
