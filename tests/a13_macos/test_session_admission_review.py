# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only loopback/session and one-click proposal admission contracts.

Never construct/start DemoApp: the HTTP handler runs against a narrow synthetic app;
its action surface records only nonnative requests and raises for everything else.
"""
import json
import secrets
import socket
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.app import DemoApp, _Handler
from mycelium_a13_macos import demo_app
from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_a13_macos.owner_run import OwnerRunError
from scripts.a13_operator.app_rpc import AppClient

LIMITS = {'max_execution_download_bytes': 0, 'max_combined_rss_bytes': 8589934592,
          'max_additional_disk_bytes': 8589934592, 'max_wall_seconds': 1800, 'run_count': 1}


@pytest.fixture
def local_handler(tmp_path, monkeypatch):
    # All subprocess and foreign network activity are forbidden before handler/client use.
    def forbidden(*args, **kwargs):
        raise AssertionError('external_effect_forbidden')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    original_connect = socket.socket.connect
    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] != '127.0.0.1':
            raise AssertionError('non_loopback_connect_forbidden')
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', loopback_only)
    root = tmp_path / 'synthetic'; root.mkdir(mode=0o700)
    class Synthetic:
        make_launch_file = DemoApp.make_launch_file
        origin = DemoApp.origin
        port = DemoApp.port
        def __init__(self):
            self.root = root
            self.launch_file = root / 'open-demo.html'
            self.native_credential = secrets.token_urlsafe(32)
            self.guard = threading.RLock()
            self.closed = threading.Event()
            self.session = self.launch_code = None
            self.session_expires = self.launch_expires = 0
            self.effects = []
            self.installation_role = 'owner'
        def status(self):
            return {'running': True, 'synthetic': True}
        def read_status(self):
            # Same inert payload through DemoApp's published-reader interface.
            return self.status()
        def action(self, path, body):
            if path == '/api/demo/start':
                return DemoApp.action(self, path, body)
            if path != '/api/review/ping' or body != {'ping': True}:
                raise AssertionError('native_or_foreign_effect_forbidden')
            self.effects.append(path)
            return {'ack': True}
    app = Synthetic()
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    server.app = app; app.server = server
    (root / 'native-control.json').write_text(json.dumps({'port': app.port, 'credential': app.native_credential}))
    (root / 'native-control.json').chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield app, root
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
        assert not thread.is_alive()


def test_second_real_client_invalidates_first_and_single_client_remains_authorized(local_handler):
    app, root = local_handler
    first = AppClient(root)
    assert first.call('/api/status') == {'running': True, 'synthetic': True}
    assert first.call('/api/review/ping', {'ping': True}) == {'ack': True}
    second = AppClient(root)
    assert second.call('/api/status')['running'] is True
    with pytest.raises(RuntimeError, match=r'local_api_http_401:open_from_native_launcher'):
        first.call('/api/status')
    assert second.call('/api/review/ping', {'ping': True}) == {'ack': True}
    assert len(app.effects) == 2


def test_compiled_proposal_refused_before_work_announcement(tmp_path):
    class App:
        def __init__(self):
            self.root = tmp_path
            self._observation_context = SimpleNamespace(facts={'owner': {}, 'peer': {}})
            self._chain_compile_state = None
            self._member_capability_reports = {}
            self._chain_slice_fetch = False
            self.local_contributor = None
            self.authority = SimpleNamespace(coordinator=SimpleNamespace())
        def member_capability_reports(self): return {}
    class Ops:
        def __init__(self, proposal): self.proposal = proposal; self.calls = []
        def start_compile(self, app):
            self.calls.append('compile')
            app._chain_compile_state = {'state': 'compiled', 'execution_proposal': self.proposal,
                                        'layer_split': {'stages': []}}
        def start_serving(self, app, proposal):
            self.calls.append('serve'); raise AssertionError('work_was_admitted')
    class Ctx:
        def __init__(self, ceiling):
            self.state = {'slice_mode': False, 'execution_ceiling': ceiling,
                          'model': {'choice': 'synthetic'}, 'manifest_digest': 'synthetic'}
            self.result = {}
            self.run_id = 'synthetic'
        def progress(self, detail): pass
        def wait_until(self, predicate, **kwargs):
            assert predicate()
        def check_stopped(self): pass
    app = App()
    cases = [dict(LIMITS, max_combined_rss_bytes=LIMITS['max_combined_rss_bytes'] + 1),
             dict(LIMITS, max_execution_download_bytes=1),
             dict(LIMITS, max_wall_seconds=True),
             {k:v for k,v in LIMITS.items() if k != 'run_count'}]
    for proposal in cases:
        ops = Ops(proposal)
        chain = AppDemoChain(app, ops=ops, local_device=lambda: {'memory_bytes': 16 << 30})
        chain._announce = lambda *a, **kw: None
        ctx = Ctx(LIMITS)
        with pytest.raises(OwnerRunError, match='execution_proposal_outside_ceiling'):
            chain._step_compile(ctx)
        assert ops.calls == ['compile']
        assert 'proposal' not in ctx.state
    ops = Ops(dict(LIMITS, operator_plan_digest='sha256:'+'0'*64))
    chain = AppDemoChain(app, ops=ops, local_device=lambda: {'memory_bytes': 16 << 30})
    chain._announce = lambda *a, **kw: None
    chain._publish_participants = lambda ctx: None
    ctx = Ctx(LIMITS)
    chain._step_compile(ctx)
    assert ctx.state['proposal'] == ops.proposal
    assert ops.calls == ['compile']


def test_one_client_http_start_validates_checked_body_before_run(local_handler, monkeypatch):
    app, root = local_handler
    client = AppClient(root)
    calls = []
    class Run:
        def start(self, *, execution_ceiling=None):
            calls.append(execution_ceiling)
            return {'state': 'running'}
    monkeypatch.setattr(demo_app, 'ensure_exchange', lambda app: object())
    monkeypatch.setattr(demo_app, 'owner_run', lambda app: Run())
    for bad in [dict(LIMITS, max_execution_download_bytes=1),
                dict(LIMITS, run_count=True),
                {k:v for k,v in LIMITS.items() if k != 'run_count'},
                dict(LIMITS, max_wall_seconds=1801)]:
        with pytest.raises(RuntimeError, match=r'local_api_http_400:execution_ceiling_invalid'):
            client.call('/api/demo/start', {'execution_ceiling': bad})
    assert calls == [] and app.effects == []
    assert client.call('/api/status')['synthetic'] is True
    assert client.call('/api/demo/start', {'execution_ceiling': LIMITS}) == {'state': 'running'}
    assert calls == [LIMITS]
    assert client.call('/api/review/ping', {'ping': True}) == {'ack': True}


def test_owner_run_keeps_checked_ceiling_until_compile_then_fails_before_work(tmp_path, monkeypatch):
    # Real OwnerRun + AppDemoChain step sequencing; only compile/member I/O are
    # supplied by the existing nonnative fake in test_demo_chain.
    from test_demo_chain import make, wait, Ops
    original = Ops.start_compile
    proposal = dict(LIMITS, max_combined_rss_bytes=LIMITS['max_combined_rss_bytes'] + 1)
    def compiled(self, app):
        original(self, app)
        app._chain_compile_state['execution_proposal'] = dict(proposal)
    monkeypatch.setattr(Ops, 'start_compile', compiled)
    app, ops, members, run = make(tmp_path)
    run.start(execution_ceiling=LIMITS)
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('compile', 'execution_proposal_outside_ceiling')
    assert [c[0] for c in ops.calls] == ['prepare', 'compile', 'stop']
    assert 'work' not in members['m'].phases


def test_guarded_rebuild_does_not_drop_ceiling():
    from test_owner_run import FakeChain, wait_for
    from mycelium_a13_macos.owner_run import OwnerRun
    class Observed(FakeChain):
        def __init__(self):
            super().__init__(); self.ceilings = []
        def run_step(self, name, ctx):
            if name == 'compile': self.ceilings.append(ctx.state.get('execution_ceiling'))
            return super().run_step(name, ctx)
    chain = Observed(); chain.release_all()
    run = OwnerRun(chain)
    run.start(execution_ceiling=LIMITS)
    wait_for(run, lambda s: s['state'] == 'serving')
    run.rebuild()
    wait_for(run, lambda s: s['state'] == 'serving' and s['reason'] == 'rebuild')
    assert chain.ceilings == [LIMITS, LIMITS]
    run.stop(); wait_for(run, lambda s: s['state'] == 'stopped')
