# SPDX-License-Identifier: AGPL-3.0-only
"""Review-only CPU controls: in-memory issuer/route; no model, network, or fleet."""
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import demo_chain, serving, workflow as workflow_module
from mycelium_a13_macos.owner_run import OwnerRunError
from mycelium_a13_macos.workflow import ManagedServingWorkflow
from mycelium_live import supervisor

REQUEST = 'startup-' + 'a' * 32


def bound():
    wf = ManagedServingWorkflow(authority=None, controller=None, launch=None)
    wf.bind_progress_identity(run_id='run', deployment_id='deployment', epoch=2)
    wf._state = 'starting'
    return wf


def event(wf, index, *, kind='token', deployment='deployment', request=REQUEST, limit=4):
    wf._challenge_event(deployment, request, kind, index, limit)


def test_correlated_indices_only_and_bounded_public_status():
    wf = bound()
    event(wf, 0)
    assert wf.status()['challenge_progress'] is None
    event(wf, -1, kind='begin')
    for bad in (True, 2, -1, 4097, '0'):
        event(wf, bad)
    event(wf, 0, request='startup-' + 'b' * 32)
    event(wf, 0, deployment='other')
    event(wf, 0, limit=5)
    assert wf.status()['challenge_progress'] is None
    event(wf, 0)
    event(wf, 0)
    event(wf, 2)
    event(wf, 1)
    event(wf, 1)
    event(wf, -1, kind='begin', request='startup-' + 'b' * 32)
    event(wf, 2, request='startup-' + 'b' * 32)
    status = wf.status()
    assert status['challenge_progress'] == {'verified_token_count': 2}
    assert REQUEST not in repr(status) and 'token_id' not in repr(status)
    wf.stop()
    event(wf, 2)
    assert wf.status()['challenge_progress'] is None


def test_actual_supervisor_sink_and_request_release(monkeypatch):
    seen = []
    class Route:
        startup_challenge = ((11,), (21, 22))
        execution_graph = SimpleNamespace(deployment_id='deployment')
        def infer_startup_challenge(self, *, request_id, sink):
            sink.emit(0, 21)
            sink.emit(1, 22)
            return SimpleNamespace(token_ids=self.startup_challenge[1])
        def counters(self): return SimpleNamespace(fatal=None)
        def live_attestation(self, *, request_id): return {'in_memory': True}
        def release_request(self, request_id): seen.append('released')
    monkeypatch.setattr(supervisor, 'issue_live_route_qualification', lambda *a, **k: 'qualified')
    wf = bound()
    assert supervisor._qualify_open_route(Route(), on_challenge_event=wf._challenge_event) == 'qualified'
    assert seen == ['released']
    assert wf.status()['challenge_progress'] == {'verified_token_count': 2}
    assert supervisor._qualify_open_route(Route(), on_challenge_event=lambda *a: (_ for _ in ()).throw(ValueError('private'))) == 'qualified'
    assert seen == ['released', 'released']


def test_workflow_launch_keywords_and_terminal_not_progress(monkeypatch):
    from mycelium_a13_macos import serving as s
    monkeypatch.setattr(s, '_validate_execution_approval', lambda *a: None)
    monkeypatch.setattr(s, '_plan_digest', lambda *a: 'digest')
    monkeypatch.setattr(s, '_validate_dynamic_port', lambda *a: None)
    monkeypatch.setattr(s, '_assert_loopback_port_free', lambda *a: None)
    monkeypatch.setattr(workflow_module, 'publish_controller_offers', lambda *a, **k: ({}, object()))
    calls = []
    late = []
    class Launch:
        prepared = None
        operator_plan = 'synthetic-plan'
        operator_plan_digest = 'digest'
        physical_host_ids = ()
        def run(self, *, approval, port, api_only, stop_event, on_started, on_challenge_event, on_startup_failure):
            assert callable(on_startup_failure)
            calls.append((api_only, callable(on_challenge_event)))
            late.append((on_started, on_challenge_event))
            on_challenge_event('deployment', REQUEST, 'begin', -1, 4)
            on_challenge_event('deployment', REQUEST, 'token', 0, 4)
            assert wf.status()['challenge_progress'] == {'verified_token_count': 1}
            on_started({'startup_qualification_complete': False, 'unavailable_deployment_count': 1})
            return 0
    controller = SimpleNamespace(_run_plan={'run_id': 'run', 'deployment_id': 'deployment'}, execute=lambda phase: None)
    wf = ManagedServingWorkflow(authority=SimpleNamespace(control_mailbox=SimpleNamespace(request_stage_cleanup=lambda x: None)), controller=controller, launch=Launch())
    wf.bind_progress_identity(run_id='run', deployment_id='deployment', epoch=2)
    approval = SimpleNamespace(operator_plan_digest='digest', physical_host_ids=())
    assert wf.run(approval=approval, port=40000) == 0
    assert calls == [(True, True)]
    assert wf.status()['challenge_progress'] is None
    assert wf.status()['startup']['startup_qualification_complete'] is False
    wf.stop()
    late[0][0]({'startup_qualification_complete': True, 'unavailable_deployment_count': 0})
    late[0][1]('deployment', REQUEST, 'token', 1, 4)
    assert wf.status()['startup']['startup_qualification_complete'] is False
    assert wf.status()['challenge_progress'] is None


def test_registry_wrapper_keyword_forwarding(monkeypatch):
    monkeypatch.setattr(serving, '_validate_execution_approval', lambda approval, prepared: approval)
    monkeypatch.setattr(serving, '_plan_digest', lambda path: 'digest')
    monkeypatch.setattr(serving, '_validate_operator_plan', lambda *a: None)
    monkeypatch.setattr(serving, '_canonical_host_ids', lambda *a: ())
    monkeypatch.setattr(serving, '_bind_existing_seed', lambda *a: (None, ('seed', 'binding', 1)))
    monkeypatch.setattr(serving, '_validate_dynamic_port', lambda *a: None)
    monkeypatch.setattr(serving, '_assert_loopback_port_free', lambda *a: None)
    received = []
    launch = serving.RegistryLaunch(prepared=None, operator_plan='synthetic-plan', private_state_root='scratch',
        registry_state='scratch', seed_state_root='scratch', product_state_root='scratch',
        operator_plan_digest='digest', physical_host_ids=(), seed_binding=('seed','binding',1),
        _runner=lambda **kwargs: received.append(kwargs) or 0)
    approval = SimpleNamespace(operator_plan_digest='digest', physical_host_ids=())
    callback = lambda *args: None
    launch.run(approval=approval, port=40000, on_challenge_event=callback)
    assert received[0]['on_challenge_event'] is callback
    assert 'on_started' not in received[0]


def test_observer_progress_only_current_epoch_and_actual_stall(monkeypatch):
    class Clock:
        now = 0
        def monotonic(self): return self.now
        def time(self): return 1000 + self.now
    clock = Clock()
    monkeypatch.setattr(demo_chain.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(demo_chain.time, 'time', clock.time)
    wf = bound()
    app = SimpleNamespace(_chain_workflow=wf, _chain_epoch=2,
        _chain_compile_state={'epoch': 2, 'state': 'compiled'},
        run_exchange=SimpleNamespace(reports=lambda: {}))
    chain = demo_chain.AppDemoChain(app, budgets={'serving': 3}, interval=1)
    class Ctx:
        run_id = 'run'
        deployment_id = 'deployment'
        state = {'participants': [], 'local_node': 'owner', 'compile_state': {'epoch': 2}}
        result = {}
        def progress(self, message): pass
        def wait_until(self, predicate, *, budget, code, interval):
            for _ in range(12):
                if clock.now == 2:
                    event(wf, -1, kind='begin'); event(wf, 0)
                if clock.now == 4: event(wf, 1)
                if clock.now == 5: event(wf, 1)  # duplicate must not renew
                if predicate(): return True
                clock.now += interval
            pytest.fail('observer did not expire')
    with pytest.raises(OwnerRunError) as caught:
        chain._step_serving(Ctx())
    assert caught.value.code == 'route_qualification_timeout'
    assert clock.now == 7  # verified milestones extend, duplicate cannot
    clock.now = 0
    app._chain_epoch = 3
    with pytest.raises(OwnerRunError) as caught:
        chain._step_serving(Ctx())
    assert caught.value.code == 'serving_workflow_identity_changed'
    assert clock.now == 0  # stale epoch refuses before observing progress
    clock.now = 0
    app._chain_epoch = 2
    Ctx.run_id = 'wrong-run'
    with pytest.raises(OwnerRunError) as caught:
        chain._step_serving(Ctx())
    assert caught.value.code == 'serving_workflow_identity_changed'
    assert clock.now == 0  # wrong run refuses before observing progress


def test_unqualified_terminal_still_refused(monkeypatch):
    wf = bound()
    wf._state = 'api_running'
    wf._startup = {'startup_qualification_complete': False, 'unavailable_deployment_count': 1}
    app = SimpleNamespace(_chain_workflow=wf, _chain_epoch=2,
        _chain_compile_state={'epoch':2,'state':'compiled'},
        run_exchange=SimpleNamespace(reports=lambda: {}))
    chain = demo_chain.AppDemoChain(app)
    ctx = SimpleNamespace(run_id='run', deployment_id='deployment',
        state={'participants': [], 'compile_state':{'epoch':2}},
        result={}, progress=lambda x: None, wait_until=lambda predicate, **kwargs: predicate())
    with pytest.raises(OwnerRunError) as caught:
        chain._step_serving(ctx)
    assert caught.value.code == 'route_qualification_failed'


def test_real_supervisor_producer_to_serving_observer_prevents_false_expiry(monkeypatch):
    class Clock:
        now = 0
        def monotonic(self): return self.now
        def time(self): return 1000 + self.now
    clock = Clock()
    monkeypatch.setattr(demo_chain.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(demo_chain.time, 'time', clock.time)
    monkeypatch.setattr(supervisor, 'issue_live_route_qualification', lambda *a, **k: 'qualified')
    class Route:
        startup_challenge = ((11,), (21,))
        execution_graph = SimpleNamespace(deployment_id='deployment')
        def infer_startup_challenge(self, *, request_id, sink):
            sink.emit(0, 21)
            return SimpleNamespace(token_ids=(21,))
        def counters(self): return SimpleNamespace(fatal=None)
        def live_attestation(self, *, request_id): return {'in_memory': True}
        def release_request(self, request_id): pass
    announcements = []
    exchange = SimpleNamespace(reports=lambda: {}, announce=lambda **kw: announcements.append(kw))
    wf = bound()
    app = SimpleNamespace(_chain_workflow=wf, _chain_epoch=2,
        _chain_compile_state={'epoch':2,'state':'compiled'}, run_exchange=exchange)
    chain = demo_chain.AppDemoChain(app, budgets={'serving':3}, interval=1)
    class Ctx:
        run_id='run'
        deployment_id='deployment'
        state={'participants': [], 'local_node':'owner', 'compile_state':{'epoch':2},
               'model':'synthetic', 'slice_mode':False, 'manifest_digest':'synthetic'}
        result={}
        def progress(self, message): pass
        def wait_until(self, predicate, *, budget, code, interval):
            for _ in range(10):
                if clock.now == 2:
                    assert supervisor._qualify_open_route(Route(), on_challenge_event=wf._challenge_event) == 'qualified'
                if clock.now == 4:
                    wf._startup={'startup_qualification_complete':True,
                                 'unavailable_deployment_count':0,'url':'http://127.0.0.1:45678/'}
                    wf._state='api_running'
                if predicate(): return True
                clock.now += interval
            pytest.fail('no qualified terminal')
    chain._step_serving(Ctx())
    assert clock.now == 4
    assert len(announcements) == 1 and announcements[0]['phase'] == 'serving'
