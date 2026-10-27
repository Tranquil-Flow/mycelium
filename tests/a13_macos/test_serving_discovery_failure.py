# SPDX-License-Identifier: AGPL-3.0-only
"""A failed discovery read is not evidence that an admitted native worker died.

CPU-only: real target agent, report validator and owner monitor; inert worker
and transport doubles. No app launch, external relay or native model execution.
"""
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_a13_macos.managed_control import ManagedControlClient, ManagedControlError
from mycelium_a13_macos.run_exchange import RunExchange
from test_demo_target import announcement, world

READS = [('pending_stage', '/managed-control/staging/read'),
         ('pending_cleanup', '/managed-control/staging/cleanup/read')]


def serving(world, local=False):
    app, agent, _, state, _ = world
    state['a'] = announcement('serving')
    actor = app.authority
    actor._runtime = SimpleNamespace(run_id=state['a']['run_id'],
                                     deployment_id=state['a']['deployment_id'])
    actor.native = {'state': 'running', 'worker_running': True,
                    'work_admitted': True, 'model_load_authorized': True}
    assert agent.tick()['state'] == 'serving'
    if local:
        app.local_contributor = actor
        agent.local = True
    # Exercise the actual report validator and the monitor that decides whether
    # the owner must stop both workers. No native/MLX dependencies are involved.
    exchange = RunExchange(SimpleNamespace())
    agent.sink = lambda report: exchange.submit_local('node-b', report)
    ctx = SimpleNamespace(run_id=state['a']['run_id'], deployment_id=state['a']['deployment_id'],
                          state={'participants': ['node-b']})
    chain = AppDemoChain(SimpleNamespace(run_exchange=exchange))
    return app, agent, state, actor, chain, ctx


@pytest.mark.parametrize('local', [False, True])
@pytest.mark.parametrize('method,path', READS)
def test_socket_failure_during_serving_is_not_a_dead_worker(world, monkeypatch, local, method, path):
    app, agent, state, actor, chain, ctx = serving(world, local)
    original = getattr(actor, method)
    calls = []
    # Exercise the shipped transport-to-error translation, not merely a matching
    # string exception: a timeout can occur after the last healthy native receipt.
    client = object.__new__(ManagedControlClient)
    client.base_url = 'https://relay.invalid'
    client._transport_timeout = lambda: 1.0
    def unavailable(*args, **kwargs):
        calls.append(path)
        raise URLError(TimeoutError('timed out'))
    client._opener = SimpleNamespace(open=unavailable)
    monkeypatch.setattr(actor, method, lambda: client._request_once(path, {}))

    result = agent.tick()

    assert calls == [path]  # no added retry loop or changed transport budget
    assert chain._dead_member(ctx) is None
    assert (result['state'], result['detail']) == ('reported', 'managed_control_unreachable')
    assert result['error_http'] == {'status': None, 'path': path, 'code': 'managed_control_unreachable'}
    assert app.actions == [] and actor.native['worker_running'] is True
    monkeypatch.setattr(actor, method, original)
    assert agent.tick()['state'] == 'serving'
    assert agent.status()['error_http'] is None
    assert chain._dead_member(ctx) is None


@pytest.mark.parametrize('method,path', READS)
def test_read_timeout_uses_same_observation_boundary(world, monkeypatch, method, path):
    app, agent, _, actor, chain, ctx = serving(world)
    def unavailable():
        raise ManagedControlError('managed_control_response_timeout', request_path=path)
    monkeypatch.setattr(actor, method, unavailable)
    assert agent.tick()['state'] == 'reported'
    assert chain._dead_member(ctx) is None
    assert app.actions == []


@pytest.mark.parametrize('error', [
    ManagedControlError('managed_control_signature_invalid', request_path=READS[0][1]),
    ManagedControlError('managed_control_unreachable', request_path=READS[0][1], cause_code='relay_dns_nonpublic'),
    ManagedControlError('managed_control_unreachable', request_path=READS[0][1], cause_code='unknown_guard'),
    ManagedControlError('managed_control_unreachable', status=400, request_path=READS[0][1]),
    ManagedControlError('managed_control_remote_error', status=403, request_path=READS[0][1]),
    ManagedControlError('managed_control_remote_error', status=503, request_path=READS[0][1]),
    ManagedControlError('managed_control_unreachable', request_path='/managed-control/staging/ack'),
    ManagedControlError('managed_control_unreachable', request_path=None),
    ValueError('managed_control_unreachable'),
])
def test_authority_unknown_and_mutating_failures_remain_fatal(world, monkeypatch, error):
    _, agent, _, actor, chain, ctx = serving(world)
    def fail():
        raise error
    monkeypatch.setattr(actor, 'pending_stage', fail)
    assert agent.tick()['state'] == 'error'
    assert chain._dead_member(ctx) is not None


@pytest.mark.parametrize('change', ['old_run', 'old_deployment', 'no_runtime', 'worker_dead', 'not_admitted'])
def test_no_healthy_current_runtime_means_no_observation_exception(world, monkeypatch, change):
    _, agent, _, actor, chain, ctx = serving(world)
    if change == 'old_run': actor._runtime.run_id = 'previous-run'
    if change == 'old_deployment': actor._runtime.deployment_id = 'previous-deployment'
    if change == 'no_runtime': actor._runtime = None
    if change == 'worker_dead': actor.native['worker_running'] = False
    if change == 'not_admitted':
        actor.native.update(work_admitted=False, model_load_authorized=False)
    def fail():
        raise ManagedControlError('managed_control_unreachable', request_path=READS[0][1])
    monkeypatch.setattr(actor, 'pending_stage', fail)
    assert agent.tick()['state'] == 'error'
    assert chain._dead_member(ctx) == 'managed_control_unreachable'


def test_repeated_read_failure_cannot_hide_a_blocked_native_pump(world, monkeypatch):
    _, agent, _, actor, chain, ctx = serving(world)
    calls = []
    def fail():
        calls.append(True)
        raise ManagedControlError('managed_control_unreachable', request_path=READS[1][1])
    monkeypatch.setattr(actor, 'pending_cleanup', fail)
    for _ in range(3):
        assert agent.tick()['state'] == 'reported'
        assert chain._dead_member(ctx) is None
    actor.native.update(state='blocked', error_code='authority_state_write_failed', worker_running=False)
    result = agent.tick()
    assert (result['state'], result['detail']) == ('error', 'authority_state_write_failed')
    assert chain._dead_member(ctx) == 'authority_state_write_failed'
    assert len(calls) == 4


def test_owner_monitor_keeps_healthy_run_but_still_fails_a_dead_worker(world, monkeypatch):
    from mycelium_a13_macos.owner_run import OwnerRun, RunContext

    _, agent, state, actor, chain, _ = serving(world)
    run = OwnerRun(chain)
    ctx = RunContext(run, state['a']['run_id'], state['a']['deployment_id'], 'start')
    ctx.state['participants'] = ['node-b']
    run._ctx, run._state = ctx, 'serving'
    chain.app._chain_workflow = SimpleNamespace(status=lambda: {'state': 'api_running'})
    monkeypatch.setattr(chain, '_tick_local', agent.tick)
    def fail():
        raise ManagedControlError('managed_control_unreachable', request_path=READS[0][1])
    monkeypatch.setattr(actor, 'pending_stage', fail)
    for _ in range(3):
        status = run.status()
        assert (status['state'], status['error_code'], status['route_ready']) == ('serving', None, True)
        assert not ctx.stop_event.is_set()
    actor.native.update(state='blocked', error_code='control_poll_reconciliation_required')
    status = run.status()
    assert (status['state'], status['error_code'], status['route_ready']) == (
        'failing', 'control_poll_reconciliation_required', False)
    assert ctx.stop_event.is_set()


def test_unreadable_native_status_is_fatal_not_an_escaped_monitor_exception(world, monkeypatch):
    _, agent, _, actor, chain, ctx = serving(world)
    def fail_read():
        raise ManagedControlError('managed_control_unreachable', request_path=READS[0][1])
    def fail_status():
        raise ValueError('runtime_member_stale')
    monkeypatch.setattr(actor, 'pending_stage', fail_read)
    monkeypatch.setattr(actor, 'status', fail_status)
    assert agent.tick()['state'] == 'error'
    assert chain._dead_member(ctx) == 'runtime_member_stale'


@pytest.mark.parametrize('phase', ['collect', 'work', 'stopped'])
def test_other_phases_keep_fail_closed_discovery_behavior(world, monkeypatch, phase):
    _, agent, state, actor, chain, ctx = serving(world)
    state['a']['phase'] = phase
    def fail():
        raise ManagedControlError('managed_control_unreachable', request_path=READS[1][1])
    monkeypatch.setattr(actor, 'pending_cleanup', fail)
    assert agent.tick()['state'] == 'error'
    assert chain._dead_member(ctx) == 'managed_control_unreachable'


def test_cleanup_after_a_missed_read_still_stops_and_detaches(world, monkeypatch):
    app, agent, _, actor, _, _ = serving(world)
    original = actor.pending_cleanup
    def fail():
        raise ManagedControlError('managed_control_unreachable', request_path=READS[1][1])
    monkeypatch.setattr(actor, 'pending_cleanup', fail)
    assert agent.tick()['state'] == 'reported'
    monkeypatch.setattr(actor, 'pending_cleanup', original)
    actor.cleanup = {'offer_id': 'work-1', 'archive_digest': 'sha256:' + '2' * 64}
    assert agent.tick()['state'] == 'cleaning_up'
    assert [p for p, _ in app.actions] == ['/api/control/stop', '/api/staging/cleanup/accept']
