"""Member side of the one-click run: deterministic agent over fake app/actor/consent."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from mycelium_a13_macos import demo_target
from mycelium_a13_macos.demo_target import DemoTargetAgent, slice_progress


class Actor:
    def __init__(self, root):
        self.member = type('M', (), {'node_id': 'node-b'})()
        self.offer = None
        self.cleanup = None
        self.native = {'state': 'stopped', 'model_load_authorized': False}
        self.peer = type('P', (), {'state_root': root})()
    def pending_stage(self): return self.offer
    def pending_cleanup(self): return self.cleanup
    def status(self): return {'native_control': dict(self.native)}


class App:
    def __init__(self, root):
        self.root = root
        self.authority = Actor(root / 'target')
        self.local_contributor = None
        self.actions = []
    def action(self, path, body):
        self.actions.append((path, body))
        a = self.authority
        if path == '/api/control/start': a.native = {'state': 'running', 'model_load_authorized': False,
                                                     'work_admitted': False}
        if path == '/api/control/stop': a.native = {'state': 'stopped', 'model_load_authorized': False}
        if path == '/api/staging/cleanup/accept': a.cleanup = None
        if path == '/api/work/preview': return {'consent_digest': 'sha256:' + '0' * 64}
        # Real accept starts the worker for work but does not load: the owner's
        # configure after activation admits the load (test_work_acceptance_parent).
        if path == '/api/work/accept': a.native = {'state': 'running', 'model_load_authorized': False,
                                                    'work_admitted': True}
        return {}
    def _resource_check(self): return None


class Gate:
    def __init__(self): self.reason = None; self.cap = 10_000_000
    def allows(self, app, action): return self.reason
    def storage_cap(self, app): return self.cap


def announcement(phase, **extra):
    value = {'protocol': 'mycelium.a13_demo_run_announcement.v1', 'phase': phase,
             'run_id': 'a13-aa-qwen-20261013T150000Z', 'deployment_id': 'dep-1',
             'model': {'choice': 'qwen3-0.6b', 'model_id': 'Qwen/Qwen3-0.6B', 'resolved_commit': 'c' * 40},
             'slice_mode': True, 'manifest_digest': 'sha256:' + '1' * 64, 'work_limits': None,
             'issued_at': 1.0, 'bootstrap_offer_id': None, 'node_slice_plan': None}
    value.update(extra)
    return value


def offer(offer_id, run_id='a13-aa-qwen-20261013T150000Z'):
    return {'offer_id': offer_id, 'archive_digest': 'sha256:' + '2' * 64, 'run_id': run_id}


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / 'target').mkdir()
    app = App(tmp_path)
    state = {'a': None, 'reports': []}
    calls = []
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor',
                        lambda app, **k: calls.append(k) or {'statement': {'target_node_id': 'node-b'}, 'signed': True})
    gate = Gate()
    agent = DemoTargetAgent(app, consent=gate, source=lambda: state['a'],
                            sink=state['reports'].append, report_interval=0)
    return app, agent, gate, state, calls


def test_without_consent_nothing_is_clicked(world):
    app, agent, gate, state, calls = world
    gate.reason = 'standing_consent_missing'
    state['a'] = announcement('collect')
    assert agent.tick()['state'] == 'waiting_for_consent'
    assert app.actions == [] and calls == []


def test_collect_discloses_slice_descriptor_once_and_reports_it(world):
    app, agent, gate, state, calls = world
    state['a'] = announcement('collect')
    assert agent.tick()['state'] == 'reported'
    agent.tick()
    assert len(calls) == 1
    root = Path(calls[0]['slice_source']['source_root'])
    assert root == app.root / 'target' / 'serving' / 'slices' / 'a13-aa-qwen-20261013T150000Z' and root.is_dir()
    assert calls[0]['slice_source']['manifest_digest'] == 'sha256:' + '1' * 64
    assert any(r['descriptor'] == {'statement': {'target_node_id': 'node-b'}, 'signed': True} for r in state['reports'])
    assert state['reports'][-1]['state'] == 'reported'


def test_bootstrap_offer_is_staged_and_started_but_others_are_held(world):
    app, agent, gate, state, calls = world
    state['a'] = announcement('observe', bootstrap_offer_id='boot-1')
    app.authority.offer = offer('other')
    agent.tick()
    assert app.actions == [] and agent.holds_offer() is True
    app.authority.offer = offer('boot-1')
    assert agent.tick()['state'] == 'bootstrap_running'
    assert [p for p, _ in app.actions] == ['/api/staging/accept', '/api/control/start']
    assert app.actions[0][1] == {'offer_id': 'boot-1', 'archive_digest': 'sha256:' + '2' * 64, 'stage_acknowledged': True}
    agent.tick()
    assert len(app.actions) == 2  # never twice


def test_requested_cleanup_stops_control_then_cleans(world):
    app, agent, gate, state, calls = world
    state['a'] = announcement('observe', bootstrap_offer_id='boot-1')
    app.authority.offer = offer('boot-1'); agent.tick()
    app.authority.offer = None
    app.authority.cleanup = offer('boot-1')
    assert agent.tick()['state'] == 'cleaning_up'
    assert [p for p, _ in app.actions][-2:] == ['/api/control/stop', '/api/staging/cleanup/accept']


def node_plan(data=b'slice-bytes-0123456789'):
    sha = lambda b: 'sha256:' + hashlib.sha256(b).hexdigest()
    body = {'protocol': 'mycelium.a13_node_slice_plan.v1', 'plan_digest': 'sha256:' + '3' * 64,
            'model_id': 'Qwen/Qwen3-0.6B', 'resolved_commit': 'c' * 40, 'node_id': 'node-b',
            'source_files': [],
            'files': [{'path': 'deployment/stage-1.safetensors', 'size_bytes': len(data), 'content_digest': sha(data),
                       'segments': [{'offset': 0, 'length': len(data), 'sha256': sha(data),
                                     'inline_base64': base64.b64encode(data).decode()}]}],
            'download_bytes': 0, 'inline_bytes': len(data)}
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    from mycelium_a13_macos.slice_acquisition import _digest_value
    return {**body, 'node_plan_digest': _digest_value(body)}


def test_slice_fetch_verifies_only_this_members_slice_then_releases_the_offer(world):
    app, agent, gate, state, calls = world
    plan = node_plan()
    state['a'] = announcement('collect'); agent.tick()
    state['a'] = announcement('work', node_slice_plan=plan, work_limits={'max_rss_bytes': 1})
    app.authority.offer = offer('work-1')
    status = agent.tick()
    assert status['state'] == 'downloading_slice' and agent.holds_offer() is True
    agent.join_slice(5)
    status = agent.tick()
    assert status['state'] == 'slice_ready' and agent.holds_offer() is False
    root = app.root / 'target' / 'serving' / 'slices' / 'a13-aa-qwen-20261013T150000Z'
    assert (root / 'deployment/stage-1.safetensors').read_bytes() == b'slice-bytes-0123456789'
    assert slice_progress(plan, root) == {'done_bytes': 22, 'total_bytes': 22}
    assert ('/api/work/preview' not in [p for p, _ in app.actions])  # remote: standing acceptor accepts


def test_slice_over_storage_cap_fails_closed(world):
    app, agent, gate, state, calls = world
    gate.cap = 5
    state['a'] = announcement('collect'); agent.tick()
    state['a'] = announcement('work', node_slice_plan=node_plan())
    agent.tick(); agent.join_slice(5)
    status = agent.tick()
    assert status['state'] == 'error' and status['detail'] == 'slice_download_limit_exceeded' or status['detail'] == 'slice_disk_limit_exceeded'
    assert agent.holds_offer() is True


def test_local_contributor_accepts_work_and_reports_serving(world, tmp_path):
    app, _, gate, state, calls = world
    app.local_contributor, app.authority = app.authority, None
    app.acquisition = type('Q', (), {'state_root': tmp_path / 'model-source'})()
    (tmp_path / 'model-source').mkdir()
    def action(path, body, _orig=App.action):
        app.authority = app.local_contributor
        try: return _orig(app, path, body)
        finally: app.authority = None
    app.action = action
    agent = DemoTargetAgent(app, consent=gate, source=lambda: state['a'], sink=state['reports'].append,
                            local=True, report_interval=0)
    state['a'] = announcement('collect'); agent.tick()
    assert Path(calls[-1]['slice_source']['source_root']).parent == tmp_path / 'model-source' / 'slices'
    limits = {'max_rss_bytes': 1, 'max_disk_bytes': 2, 'max_wall_seconds': 600, 'max_commands': 5000}
    state['a'] = announcement('work', node_slice_plan=node_plan(), work_limits=limits)
    app.local_contributor.offer = offer('work-1')
    agent.tick(); agent.join_slice(5)
    assert agent.tick()['state'] == 'work_running'
    assert [p for p, _ in app.actions] == ['/api/work/preview', '/api/work/accept']
    assert app.actions[0][1] == {'limits': limits, 'stage_acknowledged': True}
    state['a'] = announcement('serving', node_slice_plan=node_plan(), work_limits=limits)
    assert agent.tick()['state'] == 'serving'
    state['a'] = announcement('stopped')
    assert agent.tick()['state'] == 'stopped'
    assert app.actions[-1][0] == '/api/control/stop'


def test_idle_announcement_is_idle_and_holds_nothing(world):
    app, agent, gate, state, calls = world
    state['a'] = announcement('idle', run_id=None, deployment_id=None)
    assert agent.tick()['state'] == 'idle' and agent.holds_offer() is False
    state['a'] = None
    assert agent.tick()['state'] == 'idle'


class Acquisition:
    """Whole-snapshot acquisition double: not started -> downloading -> prepared."""
    def __init__(self, model_id='Qwen/Qwen3-0.6B', commit='c' * 40):
        self.state = 'not_started'; self.error = None
        self.model = type('Mo', (), {'model_id': model_id, 'resolved_commit': commit})()
        self.downloaded = 0; self.total = 1_519_209_243
    def status(self):
        return {'state': self.state, 'downloaded_bytes': self.downloaded, 'total_bytes': self.total,
                'error_code': self.error}
    def prepared_source(self):
        if self.state != 'prepared':
            raise ValueError('source_not_prepared')
        return type('S', (), {'model': self.model})()


@pytest.fixture
def whole(world):
    app, agent, gate, state, calls = world
    app.acquisition = Acquisition()
    orig = app.action
    def action(path, body):
        out = orig(path, body)
        if path == '/api/model/download':
            app.acquisition.state = 'downloading'
        return out
    app.action = action
    return app, agent, gate, state, calls


def test_whole_mode_adopts_the_owners_model_and_downloads_under_the_cap(whole):
    app, agent, gate, state, calls = whole
    gate.cap = 8_000_000_000
    state['a'] = announcement('collect', slice_mode=False)
    status = agent.tick()
    assert status['state'] == 'downloading_model'
    assert [p for p, _ in app.actions] == ['/api/run-model', '/api/model/download']
    assert app.actions[0][1] == {'choice': 'qwen3-0.6b'}
    assert app.actions[1][1] == {'consent': {'download_acknowledged': True,
        'max_download_bytes': 8_000_000_000, 'storage_limit_bytes': 8_000_000_000}}
    assert calls == []  # no descriptor before the model is on this Mac
    app.acquisition.downloaded = 700_000_000
    status = agent.tick()
    assert status['state'] == 'downloading_model' and len(app.actions) == 2  # never twice
    assert status['progress'] == {'done_bytes': 700_000_000, 'total_bytes': 1_519_209_243}
    assert state['reports'][-1]['state'] == 'downloading_model'
    app.acquisition.state = 'prepared'
    assert agent.tick()['state'] == 'reported'
    assert len(calls) == 1 and 'slice_source' not in calls[0]


def test_whole_mode_download_needs_standing_consent(whole):
    app, agent, gate, state, calls = whole
    gate.reason = 'standing_consent_missing'
    state['a'] = announcement('collect', slice_mode=False)
    assert agent.tick()['state'] == 'waiting_for_consent'
    assert app.actions == []


def test_whole_mode_failed_download_reports_its_code(whole):
    app, agent, gate, state, calls = whole
    state['a'] = announcement('collect', slice_mode=False)
    agent.tick()
    app.acquisition.state, app.acquisition.error = 'failed', 'storage_limit_exceeded'
    status = agent.tick()
    assert (status['state'], status['detail']) == ('error', 'storage_limit_exceeded')
    assert len([p for p, _ in app.actions if p == '/api/model/download']) == 1


def test_whole_mode_with_the_model_already_here_downloads_nothing(whole):
    app, agent, gate, state, calls = whole
    app.acquisition.state = 'prepared'
    state['a'] = announcement('collect', slice_mode=False)
    assert agent.tick()['state'] == 'reported'
    assert app.actions == [] and len(calls) == 1


def test_whole_mode_refuses_a_different_prepared_model(whole):
    app, agent, gate, state, calls = whole
    app.acquisition = Acquisition(model_id='Qwen/Qwen3-8B')
    app.acquisition.state = 'prepared'
    state['a'] = announcement('collect', slice_mode=False)
    status = agent.tick()
    assert (status['state'], status['detail']) == ('error', 'demo_model_mismatch')
    assert calls == []


def test_whole_mode_member_reports_work_running_before_the_owner_configures(whole):
    # Run 9: a joined Mac whose standing acceptor had started the work worker
    # still reported 'reported' (it waited for model_load_authorized, which only
    # the owner's configure after activation sets), so 'targets' never finished.
    app, agent, gate, state, calls = whole
    app.acquisition.state = 'prepared'
    limits = {'max_rss_bytes': 1, 'max_disk_bytes': 2, 'max_wall_seconds': 600, 'max_commands': 5000}
    state['a'] = announcement('collect', slice_mode=False); agent.tick()
    state['a'] = announcement('work', slice_mode=False, work_limits=limits)
    assert (agent.tick()['state'], agent.status()['detail']) == ('reported', 'work_offer_not_published')
    app.authority.offer = offer('work-1')
    assert agent.holds_offer() is False  # whole mode: the standing acceptor takes it
    assert (agent.tick()['state'], agent.status()['detail']) == ('reported', 'work_not_accepted')
    app.authority.native = {'state': 'running', 'model_load_authorized': False, 'work_admitted': False}
    assert agent.tick()['detail'] == 'bootstrap_control_running'
    app.action('/api/work/accept', {})  # what StandingAcceptor.tick does on this Mac
    status = agent.tick()
    assert (status['state'], status['detail']) == ('work_running', None)
    assert state['reports'][-1]['state'] == 'work_running'
    app.authority.native = {'state': 'blocked', 'error_code': 'runtime_member_stale',
                            'model_load_authorized': False, 'work_admitted': True}
    assert (agent.tick()['state'], agent.status()['detail']) == ('reported', 'runtime_member_stale')


def test_collect_reissues_its_descriptor_before_it_expires(tmp_path, monkeypatch):
    """Two-Mac run 18b: this Mac reported at 51 s, the joined Mac downloaded for
    25 min, and the owner then verified a descriptor that had expired at 351 s.
    While the run still collects, a member keeps its signed descriptor fresh;
    each one keeps its own short expiry."""
    (tmp_path / 'target').mkdir()
    app = App(tmp_path)
    now = [1000.0]
    issued = []
    def card(app, **k):
        issued.append(now[0])
        return {'statement': {'target_node_id': 'node-b', 'issued_at': now[0], 'expires_at': now[0] + 300},
                'n': len(issued)}
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor', card)
    reports = []
    state = {'a': announcement('collect')}
    agent = DemoTargetAgent(app, consent=Gate(), source=lambda: state['a'], sink=reports.append,
                            clock=lambda: now[0], report_interval=10)
    assert agent.tick()['state'] == 'reported' and issued == [1000.0]
    now[0] = 1140.0; agent.tick()
    assert issued == [1000.0]                     # most of its life left: not re-signed
    now[0] = 1160.0; agent.tick()                 # under half its life left: re-signed now
    assert issued == [1000.0, 1160.0]
    assert reports[-1]['descriptor']['n'] == 2 and reports[-1]['state'] == 'reported'
    now[0] = 1600.0; agent.tick()                 # a long wait keeps it fresh
    assert issued[-1] == 1600.0
    count = len(issued)
    state['a'] = announcement('observe', bootstrap_offer_id='boot-1')
    now[0] = 2000.0; agent.tick()                 # the owner has verified it: never re-signed after collect
    assert len(issued) == count
