# SPDX-License-Identifier: AGPL-3.0-only
"""Stop announcement remains mandatory even when the drain fails closed."""
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.demo_chain import AppDemoChain


@pytest.mark.parametrize('rollover', [False, True])
def test_failed_drain_announces_stop_without_claiming_cleanup(tmp_path, rollover):
    order = []
    app = SimpleNamespace(root=tmp_path, run_exchange=SimpleNamespace(
        announce=lambda **kwargs: order.append(kwargs['phase'])),
        chat=SimpleNamespace(cancel_all=lambda code: order.append(code)))
    def stop_before(app, **kwargs):
        order.append('drain')
        raise ValueError('rebuild_stop_timeout')
    errors = []
    ctx = SimpleNamespace(state={'rollover_requested': rollover, 'model': {},
        'slice_mode': True, 'manifest_digest': 'sha256:'+'a'*64}, result={},
        reason='rollover' if rollover else 'manual', run_id='desk-run', deployment_id='desk-deployment',
        record_exception=lambda code, error: errors.append(code))
    chain = AppDemoChain(app, ops=SimpleNamespace(stop_before=stop_before))
    chain.teardown(ctx, failed=False)
    assert order == ['chat_switching_runs' if rollover else 'chat_cancelled', 'drain', 'stopped']
    assert errors == ['rebuild_stop_timeout']
    assert ctx.result['cleanup_unproven'] == {
        'code': 'owner_route_stop_unproven', 'owner_lock_released': False,
        'stop_error': 'rebuild_stop_timeout'}
