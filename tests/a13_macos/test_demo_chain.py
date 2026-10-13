"""The real owner chain (demo_chain.AppDemoChain) over fake chain ops and simulated members."""
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.demo_chain import AppDemoChain, member_capability_records, work_limits
from mycelium_a13_macos.owner_run import OwnerRun
from mycelium_a13_macos.run_exchange import MEMBER_REPORT, RunExchange
from mycelium_a13_macos.serving import QWEN3_0_6B_MODEL

LOCAL, REMOTE, OWN = 'node-local', 'node-remote', 'node-owner'


class Coordinator:
    def __init__(self, nodes): self.nodes = nodes
    def members(self):
        return [{'node_id': n, 'generation': 3, 'lifecycle_state': 'RUNNING',
                 'lease_expires_at': time.time() + 600} for n in self.nodes]
    def member(self, node): return {'node_id': node, 'generation': 3}


class FakeApp:
    def __init__(self, tmp_path, *, remote=True, prepared=True):
        self.root = tmp_path
        self.installation_role = 'owner'; self.running = True
        self.guard = threading.RLock()
        nodes = [OWN, LOCAL] + ([REMOTE] if remote else [])
        self.authority = SimpleNamespace(status=lambda: {'swarm_created': True},
            member=SimpleNamespace(node_id=OWN), coordinator=Coordinator(nodes),
            control_mailbox=SimpleNamespace(request_stage_cleanup=lambda offer: None))
        source = SimpleNamespace(model=QWEN3_0_6B_MODEL, source_bytes=1_519_209_243,
                                 manifest_digest='sha256:' + '1' * 64, source_root=tmp_path)
        self.acquisition = SimpleNamespace(status=lambda: {'state': 'prepared' if prepared else 'not_started'},
                                           prepared_source=lambda: source)
        self.local_contributor = None
        self.run_exchange = RunExchange(SimpleNamespace())
        self._observation_context = None
        self._chain_compile_state = None
        self._chain_workflow = None
    def _start_local_contributor(self):
        self.local_contributor = SimpleNamespace(member=SimpleNamespace(node_id=LOCAL))
    def member_capability_reports(self):
        return {REMOTE: {'generation': 3, 'expires_at': time.time() + 300, 'device': {'memory_bytes': 16 << 30}}}


class Context:
    def __init__(self, nodes, collect_errors=()):
        self.facts = {n: {} for n in nodes}
        self.offers = {n: {'offer_id': 'boot-' + n} for n in nodes}
        self.observations = None
        self.collect_errors = list(collect_errors)
        self.collects = 0
    def collect(self):
        self.collects += 1
        if self.collect_errors:
            error = self.collect_errors.pop(0) if len(self.collect_errors) > 1 else self.collect_errors[0]
            if error is not None: raise error
        self.observations = {'observations': [{'host_id': 'h-' + n} for n in self.facts]}
    def retire_bootstrap(self): return {}


class Workflow:
    def __init__(self, nodes):
        self.state = 'prepared'
        self.node_slice_plans = {n: {'node_id': n, 'node_plan_digest': 'sha256:' + '4' * 64} for n in nodes}
    def status(self):
        return {'state': self.state, 'error_code': None,
                'startup': {'url': 'http://127.0.0.1:55555'} if self.state == 'api_running' else None}


PROPOSAL = {'max_combined_rss_bytes': 16_000, 'max_additional_disk_bytes': 24_000,
            'max_wall_seconds': 1800}


class Ops:
    def __init__(self, app, *, compile_error=None, collect_errors=()):
        self.app = app; self.calls = []; self.compile_error = compile_error
        self.collect_errors = collect_errors
    def prepare_observation(self, app, *, run_id, deployment_id, descriptors):
        self.calls.append(('prepare', [c['statement']['target_node_id'] for c in descriptors]))
        app._observation_context = Context([c['statement']['target_node_id'] for c in descriptors],
                                           self.collect_errors)
    def rebuild(self, app, *, run_id, deployment_id, descriptors):
        self.calls.append(('rebuild', [c['statement']['target_node_id'] for c in descriptors]))
        app._observation_context = Context([c['statement']['target_node_id'] for c in descriptors])
        return {'state': 'awaiting_bootstrap_workers'}
    def start_compile(self, app):
        self.calls.append(('compile', dict(app._member_capability_reports), app._chain_slice_fetch))
        if self.compile_error: raise ValueError(self.compile_error)
        nodes = sorted(app._observation_context.facts)
        app._chain_workflow = Workflow(nodes)
        app._chain_compile_state = {'state': 'compiled', 'execution_proposal': PROPOSAL,
            'layer_split': {'stages': [{'node_id': n, 'start': i * 14, 'end': (i + 1) * 14} for i, n in enumerate(nodes)]}}
    def start_serving(self, app, proposal):
        self.calls.append(('serve', proposal)); app._chain_workflow.state = 'awaiting_activation'
        return {'registry_origin': 'http://127.0.0.1:55555'}
    def activate(self, app, proposal):
        self.calls.append(('activate',)); app._chain_workflow.state = 'api_running'
    def stop(self, app):
        self.calls.append(('stop',)); app._chain_workflow = None; app._chain_compile_state = None


class Members:
    """Simulates both members answering the announcement (local + remote)."""
    def __init__(self, app, exchange, nodes, *, fail_at=None):
        self.app, self.exchange, self.nodes, self.fail_at = app, exchange, nodes, fail_at
        self.phases = []
    def tick(self):
        for node in self.nodes:
            a = self.exchange.announcement(node)
            phase = a['phase']
            if not self.phases or self.phases[-1] != phase: self.phases.append(phase)
            if phase in {'idle', 'stopped'}: continue
            workflow = self.app._chain_workflow
            state = {'collect': 'reported', 'observe': 'bootstrap_running', 'compile': 'reported',
                     'work': 'work_running' if workflow is not None and workflow.state != 'prepared' else 'slice_ready',
                     'serving': 'serving'}[phase]
            detail = None
            if self.fail_at == phase: state, detail = 'error', 'slice_digest_mismatch'
            self.exchange.submit_local(node, {'protocol': MEMBER_REPORT, 'run_id': a['run_id'],
                'deployment_id': a['deployment_id'], 'state': state, 'detail': detail, 'model': None,
                'progress': None, 'descriptor': {'statement': {'target_node_id': node}}, 'issued_at': time.time()})
    def close(self): pass


def make(tmp_path, *, remote=True, prepared=True, compile_error=None, fail_at=None, collect_errors=()):
    app = FakeApp(tmp_path, remote=remote, prepared=prepared)
    ops = Ops(app, compile_error=compile_error, collect_errors=collect_errors)
    members = {}
    def factory(app, local, exchange):
        members['m'] = Members(app, exchange, [LOCAL] + ([REMOTE] if remote else []), fail_at=fail_at)
        return members['m']
    chain = AppDemoChain(app, ops=ops, interval=0.005, agent_factory=factory,
                         local_device=lambda: {'memory_bytes': 48 << 30},
                         budgets={k: 2.0 for k in ('model', 'descriptors', 'bootstrap', 'collect', 'cleanup',
                                                   'compile', 'slices', 'publish', 'targets', 'serving')})
    run = OwnerRun(chain); chain.run = run
    return app, ops, members, run


def wait(run, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = run.status()
        if predicate(s): return s
        time.sleep(0.01)
    raise AssertionError(run.status())


def test_start_walks_the_real_chain_to_serving_then_stop_tears_down(tmp_path):
    app, ops, members, run = make(tmp_path)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert status['state'] == 'serving', status
    assert status['registry_origin'] == 'http://127.0.0.1:55555'
    assert [c[0] for c in ops.calls] == ['prepare', 'compile', 'serve', 'activate']
    assert ops.calls[0][1] == [LOCAL, REMOTE]
    compiled = ops.calls[1]
    assert compiled[2] is True  # Qwen3-0.6B is a slice-fetch model
    assert compiled[1][LOCAL]['memory_bytes'] == 48 << 30 and compiled[1][REMOTE]['memory_bytes'] == 16 << 30
    assert {p['node_id']: p['layers'] for p in status['participants']} == {LOCAL: [0, 14], REMOTE: [14, 28]}
    run.status()  # a status poll keeps this Mac's agent reporting while serving
    assert members['m'].phases == ['collect', 'observe', 'compile', 'work', 'serving']
    remote_view = app.run_exchange.announcement(REMOTE)
    assert remote_view['node_slice_plan']['node_id'] == REMOTE
    assert remote_view['work_limits'] == {'max_rss_bytes': 8000, 'max_disk_bytes': 1,
                                          'max_wall_seconds': 1800, 'max_commands': 5000}
    run.stop()
    wait(run, lambda s: s['state'] == 'stopped')
    assert ops.calls[-1] == ('stop',)
    assert app.run_exchange.announcement(REMOTE)['phase'] == 'stopped'
    assert app._observation_context is None


def test_compile_refusal_is_reported_with_its_code(tmp_path):
    app, ops, members, run = make(tmp_path, compile_error='two_distinct_hosts_required')
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('compile', 'two_distinct_hosts_required')
    assert ops.calls[-1] == ('stop',)


@pytest.mark.parametrize('code', ['managed_observation_hosts_not_distinct',
                                  'managed_observation_boots_not_distinct'])
def test_same_host_observation_fails_at_once_with_its_own_code(tmp_path, code):
    # Two app instances on one Mac share a host id: that is final, not a
    # member that is still starting, so it must not wait out the budget.
    from mycelium_a13_macos.managed_observations import ManagedObservationsError
    app, ops, members, run = make(tmp_path, collect_errors=[ManagedObservationsError(code)])
    started = time.monotonic()
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('observe', code)
    assert time.monotonic() - started < 1.5
    assert [c[0] for c in ops.calls] == ['prepare', 'stop']


def test_transient_collect_failures_are_retried_then_named_on_timeout(tmp_path):
    from mycelium_a13_macos.managed_observations import ManagedObservationsError
    app, ops, members, run = make(tmp_path, collect_errors=[
        ManagedObservationsError('managed_observation_session_open_failed'), None])
    run.start()
    assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'
    assert app._observation_context is not None and app._observation_context.collects == 2
    run.stop(); wait(run, lambda s: s['state'] == 'stopped')

    app, ops, members, run = make(tmp_path, collect_errors=[
        ManagedObservationsError('managed_observation_session_open_failed')])
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('observe', 'member_observation_timeout')
    observe = next(step for step in status['steps'] if step['name'] == 'observe')
    assert 'managed_observation_session_open_failed' in (observe['detail'] or '')


def test_only_this_mac_fails_two_macs_required(tmp_path):
    app, ops, members, run = make(tmp_path, remote=False)
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'two_macs_required')


def test_model_not_downloaded_fails_first_step(tmp_path):
    app, ops, members, run = make(tmp_path, prepared=False)
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('model', 'owner_model_not_prepared')


def test_member_error_report_fails_the_run_with_its_code(tmp_path):
    app, ops, members, run = make(tmp_path, fail_at='work')
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('slices', 'slice_digest_mismatch')


def test_rebuild_uses_rebuild_with_current_members(tmp_path):
    app, ops, members, run = make(tmp_path)
    run.start(); wait(run, lambda s: s['state'] == 'serving')
    run.rebuild()
    status = wait(run, lambda s: s['state'] == 'serving' and s['reason'] == 'rebuild')
    assert [c[0] for c in ops.calls] == ['prepare', 'compile', 'serve', 'activate', 'stop',
                                         'rebuild', 'compile', 'serve', 'activate']
    assert ops.calls[5][1] == [LOCAL, REMOTE]


def test_monitor_turns_a_dead_workflow_into_a_failed_run(tmp_path):
    app, ops, members, run = make(tmp_path)
    run.start(); wait(run, lambda s: s['state'] == 'serving')
    app._chain_workflow.state = 'failed'
    status = wait(run, lambda s: s['state'] == 'failed')
    assert status['failed_step'] == 'serving' and status['error_code'] == 'managed_workflow_stopped'


def test_capability_records_glue_join_reports_and_local_observation(tmp_path):
    app = FakeApp(tmp_path); app._start_local_contributor()
    records = member_capability_records(app, [LOCAL, REMOTE, 'node-unreported'], now=100.0,
                                        local_device={'memory_bytes': 48 << 30})
    assert records[LOCAL] == {'memory_bytes': 48 << 30, 'generation': 3, 'expires_at': 1000.0}
    assert records[REMOTE]['memory_bytes'] == 16 << 30 and records[REMOTE]['generation'] == 3
    assert 'node-unreported' not in records


def test_work_limits_derive_from_the_proposal_and_policy(tmp_path):
    app = FakeApp(tmp_path)
    assert work_limits(app, PROPOSAL)['max_rss_bytes'] == 8000
