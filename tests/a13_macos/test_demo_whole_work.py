# SPDX-License-Identifier: AGPL-3.0-only
"""Two-Mac whole-snapshot run through the REAL owner chain and REAL member agents.

Reproduces two-Mac rehearsal run 9 (2026-10-13): compile and publish passed, then
both participants -- this Mac's own contributor and the joined Mac -- stayed
``reported`` through the ``targets`` step until the published offers expired and
the workflow failed ``managed_workflow_activation_expired``.

Real: ``OwnerRun``, ``AppDemoChain`` (every step), ``RunExchange``, two
``DemoTargetAgent`` instances (the owner's local contributor under
``OwnerRunConsent``, a joined Mac under a standing-consent gate), the joined
Mac's ``holds_offer`` hand-off, and the native-control status shape
(``TargetApplication.status`` over ``ManagedTargetRuntime.status``).

Faked (heavy or physical): compile/staging/native process.  The fakes follow the
real contracts: work acceptance starts the native worker WITHOUT a model load
(``test_work_acceptance_parent``: ``model_load_authorized is False``); the owner
sends ``configure`` -- the only thing that admits the load -- after activation
(``ManagedServingWorkflow.run`` -> ``launch.run``); offers expire ``ttl`` after
publication and the workflow fails at expiry while awaiting activation
(workflow.py ``managed_workflow_activation_expired``).
"""
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_a13_macos.demo_target import DemoTargetAgent
from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime
from mycelium_a13_macos.owner_run import OwnerRun
from mycelium_a13_macos.run_exchange import RunExchange
from mycelium_a13_macos.serving import QWEN3_0_6B_MODEL
from mycelium_a13_macos.target_app import TargetApplication

LOCAL, REMOTE, OWN = 'node-local', 'node-remote', 'node-owner'
PROPOSAL = {'max_combined_rss_bytes': 16_000, 'max_additional_disk_bytes': 24_000,
            'max_wall_seconds': 1800}
DIGEST = 'sha256:' + '2' * 64


class Worker:
    """Native worker double: the load is admitted only by the owner's configure."""
    def __init__(self): self.load_admitted = False; self.running = True
    def status(self): return {'running': self.running, 'model_load_authorized': self.load_admitted}


class Actor:
    """A member's target application as the agent sees it (pending offers + status)."""
    def __init__(self, node):
        self.member = SimpleNamespace(node_id=node)
        self.offer = None
        self.cleanup = None
        self.staged = None
        self.runtime = None  # SimpleNamespace standing in for ManagedTargetRuntime
        self.peer = SimpleNamespace(state_root=None, status=lambda: {'membership_state': 'current'})

    def pending_stage(self): return self.offer
    def pending_cleanup(self): return self.cleanup

    def status(self):
        # The real status composition: TargetApplication.status over ManagedTargetRuntime.status.
        runtime = None
        if self.runtime is not None:
            runtime = SimpleNamespace(status=lambda: ManagedTargetRuntime.status(self.runtime))
        return TargetApplication.status(SimpleNamespace(
            peer=SimpleNamespace(status=lambda: {'membership_state': 'current', 'lease_state': 'fresh',
                                                 'membership_generation': 1}),
            capability=None, _runtime=runtime))

    # Target-side effects of the app routes the agent / standing acceptor drive.
    def start(self, *, authorize_load):
        assert self.runtime is None, 'runtime_already_attached'
        self.runtime = SimpleNamespace(_lock=threading.RLock(), _state='running', _error=None,
                                       worker=Worker(), _authorize_load=authorize_load)

    def stop(self): self.runtime = None

    def configure(self):
        """The owner's configure after activation (launch.run): admits the model load."""
        if self.runtime is not None and self.runtime._authorize_load is not None:
            self.runtime.worker.load_admitted = True


def _route(actor, path, body):
    if path == '/api/staging/accept':
        assert body['offer_id'] == actor.offer['offer_id']
    elif path == '/api/control/start':
        actor.start(authorize_load=None)  # bootstrap: hello only, no load authorizer
    elif path == '/api/control/stop':
        actor.stop()
    elif path == '/api/staging/cleanup/accept':
        actor.cleanup = None; actor.offer = None
    elif path == '/api/work/preview':
        actor.staged = actor.offer['offer_id']
        return {'consent_digest': 'sha256:' + '0' * 64}
    elif path == '/api/work/accept':
        assert actor.staged == actor.offer['offer_id']
        actor.start(authorize_load=lambda *_: True)
    return {}


class Acquisition:
    def __init__(self, source):
        self.source = source
        self.state_root = None
    def status(self): return {'state': 'prepared'}
    def prepared_source(self): return self.source


class Coordinator:
    def __init__(self, nodes): self.nodes = nodes
    def members(self):
        return [{'node_id': n, 'generation': 1, 'lifecycle_state': 'RUNNING',
                 'lease_expires_at': time.time() + 600} for n in self.nodes]
    def member(self, node): return {'node_id': node, 'generation': 1}


class OwnerApp:
    def __init__(self, root, actors):
        self.root = root
        self.installation_role = 'owner'; self.running = True
        self.guard = threading.RLock()
        self.actors = actors
        self.authority = SimpleNamespace(status=lambda: {'swarm_created': True},
            member=SimpleNamespace(node_id=OWN), coordinator=Coordinator([OWN, LOCAL, REMOTE]),
            control_mailbox=SimpleNamespace(request_stage_cleanup=lambda offer: None))
        source = SimpleNamespace(model=QWEN3_0_6B_MODEL, source_bytes=1_519_209_243,
                                 manifest_digest='sha256:' + '1' * 64, source_root=root)
        self.acquisition = Acquisition(source)
        self.local_contributor = None
        self.run_exchange = RunExchange(SimpleNamespace())
        self._observation_context = None
        self._chain_compile_state = None
        self._chain_workflow = None
    def _start_local_contributor(self): self.local_contributor = self.actors[LOCAL]
    def member_capability_reports(self):
        return {REMOTE: {'generation': 1, 'expires_at': time.time() + 300, 'device': {'memory_bytes': 8 << 30}}}
    def action(self, path, body): return _route(self.actors[LOCAL], path, body)
    def _resource_check(self): return None


class JoinedApp:
    """The joined Mac: its agent plus the standing acceptor's hand-off (app.auto_accept)."""
    def __init__(self, actor, source):
        self.authority = actor
        self.local_contributor = None
        self.acquisition = Acquisition(source)
        self.demo_target = None
        self.handled = set()
    def action(self, path, body): return _route(self.authority, path, body)
    def _resource_check(self): return None

    def auto_accept(self):
        # app.auto_accept: hold while the agent holds; else StandingAcceptor.tick
        # previews (stages) and accepts each pending offer once.
        from mycelium_a13_macos.demo_app import holds_offer
        offer = self.authority.pending_stage()
        if holds_offer(self) or offer is None or offer['offer_id'] in self.handled:
            return None
        self.handled.add(offer['offer_id'])
        _route(self.authority, '/api/work/preview', {})
        _route(self.authority, '/api/work/accept', {})
        return {'offer_id': offer['offer_id'], 'state': 'accepted'}


class Context:
    """Observation context double: bootstrap offers, collection, cleanup."""
    def __init__(self, actors, run_id):
        self.actors = actors
        self.facts = {n: {} for n in actors}
        self.offers = {n: {'offer_id': 'boot-' + n, 'archive_digest': DIGEST, 'run_id': run_id}
                       for n in actors}
        for node, actor in actors.items():
            actor.offer = dict(self.offers[node])
        self.observations = None
    def collect(self):
        if any(a.runtime is None for a in self.actors.values()):
            raise ValueError('managed_observation_session_open_failed')
        self.observations = {'observations': [{'host_id': 'h-' + n} for n in self.facts]}
    def retire_bootstrap(self):
        for actor in self.actors.values():
            if actor.offer is not None and actor.offer['offer_id'].startswith('boot-'):
                actor.cleanup = actor.offer
        if any(a.cleanup is not None or a.runtime is not None for a in self.actors.values()):
            raise ValueError('offer_cleanup_unconfirmed')
        return {}


class Workflow:
    """ManagedServingWorkflow double: publish -> staging -> awaiting_activation -> api."""
    def __init__(self, actors, run_id, ttl):
        self.actors, self.run_id, self.ttl = actors, run_id, ttl
        self.state, self.error = 'prepared', None
        self.node_slice_plans = None  # whole snapshot: no slices
        self.expires = None
    def publish(self):
        self.expires = time.time() + self.ttl
        for node, actor in self.actors.items():
            actor.offer = {'offer_id': 'work-' + node, 'archive_digest': DIGEST, 'run_id': self.run_id,
                           'expires_at': self.expires}
        self.state = 'staging'
    def status(self):
        if self.state == 'staging' and all(a.staged == a.offer['offer_id'] for a in self.actors.values()):
            self.state = 'awaiting_activation'
        if self.state == 'awaiting_activation' and time.time() >= self.expires:
            self.state, self.error = 'failed', 'managed_workflow_activation_expired'
        return {'state': self.state, 'error_code': self.error,
                'startup': {'url': 'http://127.0.0.1:55555', 'startup_qualification_complete': True,
                            'deployment_count': 1, 'unavailable_deployment_count': 0}
                if self.state == 'api_running' else None}
    def activate(self):
        assert self.state == 'awaiting_activation', self.state
        for actor in self.actors.values():
            actor.configure()
        self.state = 'api_running'


class Ops:
    def __init__(self, actors, ttl):
        self.actors, self.ttl, self.calls = actors, ttl, []
    def prepare_observation(self, app, *, run_id, deployment_id, descriptors):
        self.calls.append('prepare'); self.run_id = run_id
        app._observation_context = Context(self.actors, run_id)
    def start_compile(self, app):
        self.calls.append('compile')
        app._chain_workflow = Workflow(self.actors, self.run_id, self.ttl)
        app._chain_compile_state = {'state': 'compiled', 'execution_proposal': PROPOSAL,
            'layer_split': {'stages': [{'node_id': REMOTE, 'start': 0, 'end': 1},
                                       {'node_id': LOCAL, 'start': 1, 'end': 28}]}}
    def start_serving(self, app, proposal):
        self.calls.append('serve'); app._chain_workflow.publish()
        return {'registry_origin': 'http://127.0.0.1:55555'}
    def activate(self, app, proposal):
        self.calls.append('activate'); app._chain_workflow.activate()
    def stop(self, app):
        self.calls.append('stop')


class Chain(AppDemoChain):
    """The real chain; each wait tick also runs the joined Mac's standing loop once."""
    remote = None
    def _tick_local(self):
        super()._tick_local()
        if self.remote is not None:
            self.remote.auto_accept()
            self.remote.demo_target.tick()


class Allow:
    def allows(self, app, action): return None
    def storage_cap(self, app): return 8 << 30


def make(tmp_path, monkeypatch, *, ttl=3.0, targets=10.0, publish=5.0):
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor',
                        lambda app, **k: {'statement': {'target_node_id': app_node(app)}})
    actors = {LOCAL: Actor(LOCAL), REMOTE: Actor(REMOTE)}
    owner = OwnerApp(tmp_path, actors)
    ops = Ops(actors, ttl)
    chain = Chain(owner, ops=ops, interval=0.005, local_device=lambda: {'memory_bytes': 48 << 30},
                  budgets={**{k: 5.0 for k in ('model', 'descriptors', 'bootstrap', 'collect', 'cleanup',
                                               'compile', 'slices', 'serving')},
                           'publish': publish, 'targets': targets})
    joined = JoinedApp(actors[REMOTE], owner.acquisition.source)
    exchange = owner.run_exchange
    joined.demo_target = DemoTargetAgent(joined, consent=Allow(),
        source=lambda: exchange.announcement(REMOTE),
        sink=lambda report: exchange.submit_local(REMOTE, report), report_interval=0)
    chain.remote = joined
    run = OwnerRun(chain); chain.run = run
    return owner, ops, actors, run


def app_node(app):
    actor = app.local_contributor if getattr(app, 'local_contributor', None) is not None else app.authority
    return actor.member.node_id


def wait(run, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = run.status()
        if predicate(s): return s
        time.sleep(0.01)
    raise AssertionError(run.status())


def test_whole_mode_run_reaches_serving_once_both_macs_accept_work(tmp_path, monkeypatch):
    owner, ops, actors, run = make(tmp_path, monkeypatch)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert (status['state'], status['failed_step'], status['error_code']) == ('serving', None, None), status
    assert ops.calls == ['prepare', 'compile', 'serve', 'activate']
    # Both workers were started for work (load authorizer attached) and the
    # owner's configure after activation admitted the load on each.
    assert all(a.runtime.worker.load_admitted for a in actors.values())
    states = {p['node_id']: p['state'] for p in status['participants']}
    assert set(states) == {LOCAL, REMOTE} and set(states.values()) <= {'work_running', 'serving'}
    run.stop(); wait(run, lambda s: s['state'] == 'stopped')



@pytest.mark.parametrize('reason', [None, 'work_offer_changed'])
def test_a_member_that_stages_but_never_starts_work_is_named_when_the_offers_expire(
        tmp_path, monkeypatch, reason):
    owner, ops, actors, run = make(tmp_path, monkeypatch, ttl=1.0)
    joined = run.chain.remote
    def stage_only():  # the standing acceptor previews (stages) but the accept never starts the worker
        offer = joined.authority.pending_stage()
        if joined.demo_target.holds_offer() or offer is None or offer['offer_id'] in joined.handled:
            return
        joined.handled.add(offer['offer_id'])
        _route(joined.authority, '/api/work/preview', {})
        if reason is not None:
            joined.standing = SimpleNamespace(status=lambda: {'last': {
                'offer_id': offer['offer_id'], 'state': 'failed', 'reason': reason}})
    joined.auto_accept = stage_only
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('targets', 'member_work_not_running')
    expected = reason or 'work_not_accepted'
    parts = {p['node_id']: (p['state'], p['detail']) for p in status['participants']}
    assert parts[REMOTE] == ('reported', expected)
    assert parts[LOCAL][0] == 'work_running'  # this Mac did start its work worker
    targets = next(step for step in status['steps'] if step['name'] == 'targets')
    assert targets['detail'].startswith('managed_workflow_activation_expired; still waiting for Joined Mac')
    assert targets['detail'].endswith(': ' + expected)
    assert 'This Mac (owner)' not in targets['detail']
    assert 'activate' not in ops.calls


def test_a_member_that_never_stages_is_named_when_publish_times_out(tmp_path, monkeypatch):
    owner, ops, actors, run = make(tmp_path, monkeypatch, publish=1.0)
    run.chain.remote.auto_accept = lambda: None  # the offer is never taken
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('publish', 'member_staging_timeout')
    publish = next(step for step in status['steps'] if step['name'] == 'publish')
    assert publish['detail'].startswith('timed out; still waiting for Joined Mac')
    assert publish['detail'].endswith(': work_not_accepted')


class Downloading(Acquisition):
    """The joined Mac's acquisition: downloading (bytes rising) until ``seconds`` pass."""
    def __init__(self, source, seconds):
        super().__init__(source)
        self.started, self.seconds = time.monotonic(), seconds
    def status(self):
        elapsed = time.monotonic() - self.started
        if elapsed >= self.seconds:
            return {'state': 'prepared'}
        return {'state': 'downloading', 'downloaded_bytes': int(elapsed * 1000) + 1, 'total_bytes': 10**9}


def test_a_long_joiner_download_does_not_expire_this_macs_descriptor(tmp_path, monkeypatch):
    """Two-Mac run 18b (2026-10-14): this Mac signed its installation descriptor
    51 s in; the joined Mac downloaded Qwen3-0.6B over a phone hotspot for ~25 min;
    observe then verified the owner's own descriptor, expired since 351 s, and the
    run failed ``installation_descriptor_invalid``.  Scaled: descriptors live 1 s,
    the joined Mac downloads for 2.5 s."""
    ttl = 1.0
    owner, ops, actors, run = make(tmp_path, monkeypatch)
    def card(app, **k):
        now = time.time()
        return {'statement': {'target_node_id': app_node(app), 'issued_at': now, 'expires_at': now + ttl}}
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor', card)
    run.chain.budgets['descriptor_margin'] = 0.2
    joined = run.chain.remote
    joined.acquisition = Downloading(owner.acquisition.source, 2.5)
    joined.action = lambda path, body: {} if path in {'/api/run-model', '/api/model/download'} else _route(
        joined.authority, path, body)
    seen = []
    prepare = ops.prepare_observation
    def verify_then_prepare(app, *, run_id, deployment_id, descriptors):
        now = time.time()
        seen.extend(c['statement']['expires_at'] - now for c in descriptors)
        if any(c['statement']['expires_at'] <= now for c in descriptors):
            raise ValueError('installation_descriptor_expired')  # verify_installation_descriptor
        return prepare(app, run_id=run_id, deployment_id=deployment_id, descriptors=descriptors)
    ops.prepare_observation = verify_then_prepare
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert (status['state'], status['failed_step'], status['error_code']) == ('serving', None, None), status
    assert len(seen) == 2 and min(seen) >= 0.2, seen
    run.stop(); wait(run, lambda s: s['state'] == 'stopped')
