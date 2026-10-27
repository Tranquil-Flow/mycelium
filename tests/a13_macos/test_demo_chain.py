# SPDX-License-Identifier: AGPL-3.0-only
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
    def __init__(self, tmp_path, *, remote=True, prepared=True, source_bytes=16_380_000_000):
        self.source_bytes = source_bytes
        self.root = tmp_path
        self.installation_role = 'owner'; self.running = True
        self.guard = threading.RLock()
        nodes = [OWN, LOCAL] + ([REMOTE] if remote else [])
        self.authority = SimpleNamespace(status=lambda: {'swarm_created': True},
            member=SimpleNamespace(node_id=OWN), coordinator=Coordinator(nodes),
            control_mailbox=SimpleNamespace(request_stage_cleanup=lambda offer: None))
        # source_bytes stands in for a slice-sized Qwen3 (8B bf16 source); the
        # 0.6B itself goes whole (test_small_model_goes_whole_to_each_mac).
        source = SimpleNamespace(model=QWEN3_0_6B_MODEL, source_bytes=self.source_bytes,
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
        return {REMOTE: {'generation': 3, 'expires_at': time.time() + 300,
                         'device': {'memory_bytes': 16 << 30}, 'caps': {'max_wall_seconds': 1800}}}


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
    def __init__(self, nodes, *, qualified=True):
        self.state = 'prepared'
        self.qualified = qualified
        self.node_slice_plans = {n: {'node_id': n, 'node_plan_digest': 'sha256:' + '4' * 64} for n in nodes}
    def status(self):
        # The supervisor's mycelium.live_registry_server_started.v1 document: the
        # registry serves its API even when the startup route challenge failed
        # (two-Mac run 14: placement_load_proof_mismatch on the joined Mac).
        startup = {'protocol': 'mycelium.live_registry_server_started.v1',
                   # The real supervisor reports its URL with a trailing slash
                   # (mycelium_live/supervisor.py); chat needs the bare origin.
                   'url': 'http://127.0.0.1:55555/', 'startup_qualification_complete': self.qualified,
                   'simulated': False, 'api_only': True, 'deployment_count': int(self.qualified),
                   'unavailable_deployment_count': int(not self.qualified)}
        return {'state': self.state, 'error_code': None,
                'startup': startup if self.state == 'api_running' else None}


PROPOSAL = {'max_combined_rss_bytes': 16_000, 'max_additional_disk_bytes': 24_000,
            'max_wall_seconds': 1800}


class Ops:
    def __init__(self, app, *, compile_error=None, collect_errors=(), qualified=True):
        self.app = app; self.calls = []; self.compile_error = compile_error
        self.collect_errors = collect_errors; self.qualified = qualified
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
        app._chain_workflow = Workflow(nodes, qualified=self.qualified)
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


def make(tmp_path, *, remote=True, prepared=True, compile_error=None, fail_at=None, collect_errors=(),
         qualified=True, local_wall=1800):
    from mycelium_a13_macos.standing_consent import grant_standing_consent
    from test_standing_consent import GRANT, POLICY, NOW
    if local_wall is not None:  # None = a real owner: it creates the swarm and holds no standing grant
        grant_standing_consent(tmp_path, {**GRANT, 'caps': {**GRANT['caps'], 'max_wall_seconds': local_wall}},
                               policy=POLICY, now=NOW)
    app = FakeApp(tmp_path, remote=remote, prepared=prepared)
    ops = Ops(app, compile_error=compile_error, collect_errors=collect_errors, qualified=qualified)
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
    assert compiled[2] is True  # slice-sized source: each Mac fetches only its slice
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


def test_failed_startup_qualification_is_a_failed_run_not_serving(tmp_path):
    # Two-Mac run 14: the registry came up api_running with
    # startup_qualification_complete false (the joined Mac refused configure
    # with placement_load_proof_mismatch) and the run still said 'serving'
    # while chat got qualification 503s for four minutes.
    app, ops, members, run = make(tmp_path, qualified=False)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert status['state'] == 'failed', status
    assert (status['failed_step'], status['error_code']) == ('serving', 'route_qualification_failed')
    serving = next(step for step in status['steps'] if step['name'] == 'serving')
    assert 'startup route challenge' in (serving['detail'] or '')
    assert ops.calls[-1] == ('stop',)
    assert 'serving' not in members['m'].phases


def test_startup_without_a_qualification_verdict_is_not_serving(tmp_path):
    app, ops, members, run = make(tmp_path)
    workflow_status = Workflow.status
    def legacy_status(self):
        document = workflow_status(self)
        if document['startup'] is not None:
            document['startup'] = {'url': document['startup']['url']}
        return document
    Workflow.status = legacy_status
    try:
        run.start()
        status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    finally:
        Workflow.status = workflow_status
    assert (status['state'], status['error_code']) == ('failed', 'route_qualification_failed')


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


def test_small_model_goes_whole_to_each_mac(tmp_path):
    # Qwen3-0.6B (~1.5 GB bf16 source) is below the slice window: whole snapshot.
    app = FakeApp(tmp_path, source_bytes=1_519_209_243)
    from mycelium_a13_macos.serving import slice_fetch_permitted
    assert not slice_fetch_permitted(app.acquisition.prepared_source().source_bytes,
                                     architecture=app.acquisition.prepared_source().model.architecture)


class SlowJoiner(Members):
    """The remote Mac downloads the model for `seconds` before it can describe itself
    (two-Mac run 16: a laptop on a phone hotspot needed > 180 s)."""
    def __init__(self, *args, seconds, stalled=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.until = time.monotonic() + seconds
        self.stalled = stalled
        self.done = 0
    def tick(self):
        a = self.exchange.announcement(REMOTE)
        if a['phase'] == 'collect' and time.monotonic() < self.until:
            for node in self.nodes:
                if node == REMOTE:
                    if not self.stalled:
                        self.done += 1
                    self.exchange.submit_local(node, {'protocol': MEMBER_REPORT, 'run_id': a['run_id'],
                        'deployment_id': a['deployment_id'], 'state': 'downloading_model', 'detail': None,
                        'model': None, 'progress': {'done_bytes': self.done, 'total_bytes': 10**9},
                        'descriptor': None, 'issued_at': time.time()})
            local = [n for n in self.nodes if n != REMOTE]
            saved, self.nodes = self.nodes, local
            try:
                super().tick()
            finally:
                self.nodes = saved
            return
        super().tick()


def make_slow(tmp_path, *, seconds, stalled=False):
    app, ops, members, run = make(tmp_path)
    def factory(app, local, exchange):
        members['m'] = SlowJoiner(app, exchange, [LOCAL, REMOTE], seconds=seconds, stalled=stalled)
        return members['m']
    run.chain._agent_factory = factory
    run.chain.budgets.update({'descriptors': 1.0, 'download_stall': 0.6, 'descriptors_download': 8.0})
    return app, ops, members, run


def test_a_mac_still_downloading_with_progress_is_waited_for(tmp_path):
    app, ops, members, run = make_slow(tmp_path, seconds=2.5)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'}, timeout=20)
    assert status['state'] == 'serving', status


def test_a_stalled_download_is_named_after_the_stall_window(tmp_path):
    # Still reporting, bytes flat: the run ends with its own code, and the page
    # keeps showing the joined Mac with its state (not 'two_macs_required').
    app, ops, members, run = make_slow(tmp_path, seconds=30, stalled=True)
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'member_download_stalled')
    joined = [p for p in status['participants'] if p['node_id'] == REMOTE]
    assert joined and joined[0]['state'] == 'downloading_model', status['participants']


class AgingMembers(Members):
    """Members whose signed descriptors carry their real expiry.  This Mac's card
    has only ``stale_for`` seconds of life left until it is re-signed at ``fresh_after``."""
    def __init__(self, *args, fresh_after, **kwargs):
        super().__init__(*args, **kwargs)
        self.start = time.monotonic(); self.fresh_after = fresh_after
    def tick(self):
        super().tick()
        a = self.exchange.announcement(LOCAL)
        if a['phase'] != 'collect':
            return
        now = time.time()
        fresh = time.monotonic() - self.start >= self.fresh_after
        report = dict(self.exchange.reports()[LOCAL])
        report.pop('received_at', None)
        report['descriptor'] = {'statement': {'target_node_id': LOCAL, 'issued_at': now - 299.9 + (299.9 if fresh else 0),
                                              'expires_at': now + (300 if fresh else 0.1)}}
        self.exchange.submit_local(LOCAL, report)


def make_aging(tmp_path, *, fresh_after):
    app, ops, members, run = make(tmp_path)
    def factory(app, local, exchange):
        members['m'] = AgingMembers(app, exchange, [LOCAL, REMOTE], fresh_after=fresh_after)
        return members['m']
    run.chain._agent_factory = factory
    run.chain.budgets.update({'descriptors': 1.0, 'download_stall': 0.6, 'descriptor_margin': 5.0})
    cards = []
    prepare = ops.prepare_observation
    def capture(app, **k):
        cards.extend(k['descriptors']); return prepare(app, **k)
    ops.prepare_observation = capture
    return app, ops, members, run, cards


def test_observe_waits_for_a_descriptor_that_outlives_its_use(tmp_path):
    app, ops, members, run, cards = make_aging(tmp_path, fresh_after=0.4)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'}, timeout=20)
    assert status['state'] == 'serving', status
    local = next(c for c in cards if c['statement']['target_node_id'] == LOCAL)
    assert local['statement']['expires_at'] - time.time() > 60  # the re-signed card, not the 0.1 s one


def test_a_descriptor_never_refreshed_is_named_not_sent_to_observe(tmp_path):
    app, ops, members, run, cards = make_aging(tmp_path, fresh_after=60)
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'member_descriptor_stale')
    assert cards == [] and not any(c[0] == 'prepare' for c in ops.calls)


class ScriptedJoiner(Members):
    """The remote Mac follows ``script`` [(seconds, mode)] while the owner collects:
    'progress' reports rising bytes, 'flat' reports the same bytes, 'silent' sends
    nothing (a phone-hotspot link drop: two-Mac run 21 lost every report and seed
    heartbeat from the laptop ~110 s in).  After the script it reports normally."""
    def __init__(self, *args, script, **kwargs):
        super().__init__(*args, **kwargs)
        self.script = list(script)
        self.start = None
        self.done = 0
    def mode(self):
        elapsed = time.monotonic() - self.start
        for seconds, mode in self.script:
            if elapsed < seconds:
                return mode
            elapsed -= seconds
        return None
    def tick(self):
        a = self.exchange.announcement(REMOTE)
        if a['phase'] != 'collect':
            return super().tick()
        if self.start is None:
            self.start = time.monotonic()
        mode = self.mode()
        if mode is None:
            return super().tick()
        if mode == 'progress':
            self.done = min(self.done + 10**5, 10**9 - 1)
        if mode in {'progress', 'flat'}:
            self.exchange.submit_local(REMOTE, {'protocol': MEMBER_REPORT, 'run_id': a['run_id'],
                'deployment_id': a['deployment_id'], 'state': 'downloading_model', 'detail': None,
                'model': None, 'progress': {'done_bytes': self.done, 'total_bytes': 10**9},
                'descriptor': None, 'issued_at': time.time()})
        saved, self.nodes = self.nodes, [LOCAL]
        try:
            super().tick()
        finally:
            self.nodes = saved


def make_scripted(tmp_path, script, **budgets):
    app, ops, members, run = make(tmp_path)
    def factory(app, local, exchange):
        members['m'] = ScriptedJoiner(app, exchange, [LOCAL, REMOTE], script=script)
        return members['m']
    run.chain._agent_factory = factory
    run.chain.budgets.update({'descriptors': 1.0, 'download_stall': 0.6, 'descriptors_download': 1.5,
                              'report_silence': 0.3, 'member_silence': 3.0, **budgets})
    return app, ops, members, run


def test_a_download_back_after_a_link_drop_longer_than_the_stall_window_is_waited_for(tmp_path):
    # Run 21: the joined Mac went silent mid-download for longer than the stall
    # window while its seed lease was still live; the owner must keep waiting.
    app, ops, members, run = make_scripted(tmp_path, [(0.5, 'progress'), (1.8, 'silent'), (0.5, 'progress')])
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'}, timeout=20)
    assert status['state'] == 'serving', status


def test_a_progressing_download_renews_its_budget_past_the_download_total(tmp_path):
    # Budgets scale with measured progress: a download that keeps advancing is
    # waited for past 'descriptors_download' (run 18b needed 1585 s of 1800).
    app, ops, members, run = make_scripted(tmp_path, [(3.0, 'progress')])
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'}, timeout=20)
    assert status['state'] == 'serving', status


def test_a_member_silent_past_the_silence_window_is_named_unreachable(tmp_path):
    app, ops, members, run = make_scripted(tmp_path, [(0.3, 'progress'), (60, 'silent')], member_silence=1.5)
    run.start()
    started = time.monotonic()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'member_unreachable')
    assert time.monotonic() - started < 10  # bounded, not the download total
    joined = [p for p in status['participants'] if p['node_id'] == REMOTE]
    assert joined and joined[0]['state'] == 'downloading_model', status['participants']


def test_a_silent_member_whose_seed_lease_lapsed_is_not_waited_for(tmp_path):
    app, ops, members, run = make_scripted(tmp_path, [(0.3, 'progress'), (60, 'silent')], member_silence=30.0)
    lapsed = app.authority.coordinator
    real = lapsed.members
    def members_with_lapsed_lease():
        rows = real()
        if members.get('m') is not None and members['m'].start is not None \
                and time.monotonic() - members['m'].start > 0.6:
            rows = [dict(r, lease_expires_at=time.time() - 1) if r['node_id'] == REMOTE else r for r in rows]
        return rows
    lapsed.members = members_with_lapsed_lease
    run.start()
    started = time.monotonic()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'member_unreachable')
    assert time.monotonic() - started < 10


def test_the_descriptors_step_shows_download_bytes_rate_and_time_left(tmp_path):
    app, ops, members, run = make_scripted(tmp_path, [(1.2, 'progress')])
    run.start()
    seen = []
    def look(s):
        step = next(x for x in s['steps'] if x['name'] == 'descriptors')
        if step['detail']:
            seen.append(step['detail'])
        return s['state'] in {'serving', 'failed'}
    wait(run, look, timeout=20)
    shown = [d for d in seen if ' MB' in d]
    assert shown, seen
    assert any(' of 1000 MB' in d and 'MB/s' in d and 'left' in d for d in shown), shown
