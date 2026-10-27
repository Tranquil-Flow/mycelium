# SPDX-License-Identifier: AGPL-3.0-only
"""Resource refusals terminate publication on the first poll, not its timeout."""
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_a13_macos.owner_run import OwnerRunError
from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.serving import A9_ROUTE_MODEL


@pytest.mark.parametrize('node', ['local', 'remote'])
@pytest.mark.parametrize('state', ['error', 'reported'])
@pytest.mark.parametrize('code', ['member_storage_cap_below_share', 'member_memory_cap_below_share'])
def test_publish_surfaces_resource_refusal_before_timeout(node, state, code, tmp_path):
    reports = {'first-unready': {'state': 'reported', 'detail': 'work_not_accepted'},
               node: {'state': state, 'detail': code}}
    calls = []
    app = SimpleNamespace(root=tmp_path)
    chain = AppDemoChain(app, ops=SimpleNamespace(start_serving=lambda *args: {}))
    chain._reports = lambda ctx: reports
    chain._publish_participants = lambda ctx: None
    chain._workflow_state = lambda: {'state': 'staging'}
    chain._tick_local = lambda: None
    rows = R.plan_for_members(A9_ROUTE_MODEL, [
        {'node_id': 'first-unready', 'memory_bytes': 16 << 30},
        {'node_id': node, 'memory_bytes': 48 << 30}], slice_mode=False)['members']
    def wait_until(predicate, **kwargs):
        calls.append('poll')
        if not predicate():
            raise OwnerRunError('member_staging_timeout')
    ctx = SimpleNamespace(state={'participants': ['first-unready', node], 'local_node': 'local',
        'proposal': {}, 'prepared': SimpleNamespace(model=A9_ROUTE_MODEL),
        'compile_state': {'resource_plan': {'members': rows}}},
        progress=lambda value: None, wait_until=wait_until)
    with pytest.raises(OwnerRunError) as error:
        chain._step_publish(ctx)
    assert error.value.code == code
    assert 'refused its share' in error.value.message
    assert 'Qwen2.5-0.5B' in error.value.message
    assert ('storage' if 'storage' in code else 'memory') in error.value.message
    assert calls == ['poll']


def test_readiness_checks_later_errors_even_when_an_earlier_report_is_missing():
    chain = AppDemoChain(SimpleNamespace())
    chain._reports = lambda ctx: {'second': {'state': 'error', 'detail': 'member_storage_cap_below_share'}}
    ctx = SimpleNamespace(state={'participants': ['missing', 'second']})
    with pytest.raises(OwnerRunError, match='member_storage_cap_below_share'):
        chain._all_in(ctx, {'work_running'})
