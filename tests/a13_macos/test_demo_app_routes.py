# SPDX-License-Identifier: AGPL-3.0-only
"""One-click run through the real owner app (single Mac): routes, exchange, local agent."""
import http.client
import json
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.app import DemoApp, DemoError
from mycelium_a13_macos.serving import QWEN3_0_6B_MODEL
from test_app import login, request
from test_app_authority import CONSENT
from test_worker import SAFE


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL', raising=False)
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    try:
        yield app
    finally:
        app.close()


def prepared(app):
    source = app.acquisition.state_root / 'fixture-snapshot'
    source.mkdir()
    # source_bytes stands in for a slice-sized Qwen3 (8B bf16 source).
    value = SimpleNamespace(model=QWEN3_0_6B_MODEL, source_bytes=16_380_000_000, source_root=source,
                            manifest_digest='sha256:' + '1' * 64, manifest={'files': []})
    app.acquisition.status = lambda: {'state': 'prepared'}
    app.acquisition.prepared_source = lambda: value
    return value


def wait(app, predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = app.status()['owner_run']
        if predicate(status): return status
        time.sleep(0.05)
    raise AssertionError(app.status()['owner_run'])


def test_demo_routes_need_an_owner_swarm(owner):
    with pytest.raises(DemoError, match='owner_swarm_required'):
        owner.action('/api/demo/start', {})
    owner.action('/api/create', {'consent': CONSENT})
    with pytest.raises(DemoError, match='invalid_request'):
        owner.action('/api/demo/start', {'x': 1})
    with pytest.raises(DemoError, match='owner_run_rebuild_requires_run'):
        owner.action('/api/demo/rebuild', {})
    assert owner.status()['owner_run']['state'] == 'idle'


def test_start_without_the_model_fails_with_a_clear_code(owner):
    owner.action('/api/create', {'consent': CONSENT})
    owner.action('/api/demo/start', {})
    status = wait(owner, lambda s: s['state'] == 'failed', timeout=30)
    assert (status['failed_step'], status['error_code']) == ('model', 'owner_model_not_prepared')


def test_single_mac_start_reaches_descriptors_and_stops_two_macs_required(native_sidecar_required, owner):
    owner.action('/api/create', {'consent': CONSENT})
    owner._policy_observer = lambda: dict(SAFE)
    source = prepared(owner)
    run = owner.status() and __import__('mycelium_a13_macos.demo_app', fromlist=['x']).owner_run(owner)
    run.chain.budgets['descriptors'] = 3.0
    run.chain.interval = 0.1
    owner.action('/api/demo/start', {})
    status = wait(owner, lambda s: s['state'] in {'failed', 'serving'})
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'two_macs_required')
    assert [s['state'] for s in status['steps'][:3]] == ['done', 'done', 'failed']
    local = owner.local_contributor.member.node_id
    report = owner.run_exchange.reports()[local]
    # This Mac's own agent disclosed a real signed slice-mode descriptor.
    facts = report['descriptor']['statement']['facts']
    assert facts['node_id'] == local and facts['manifest_digest'] == source.manifest_digest
    assert facts['source_root'].startswith(str(owner.acquisition.state_root / 'slices'))
    assert status['participants'][0]['label'] == 'This Mac (owner)'
    assert owner.run_exchange.announcement(local)['phase'] in {'stopped', 'idle'}
    assert owner.run_exchange.announcement(local)['model']['model_id'] == owner.run_model_contract().model_id


def test_idle_announcement_carries_the_owner_model_choice(owner):
    owner.action('/api/create', {'consent': CONSENT})
    owner.action('/api/run-model', {'choice': 'qwen3-0.6b'})
    owner.status()
    assert owner.run_exchange.announcement()['model']['model_id'] == 'Qwen/Qwen3-0.6B'
    owner.action('/api/run-model', {'choice': 'qwen3-8b'})
    owner.status()
    assert owner.run_exchange.announcement()['model']['model_id'] == 'Qwen/Qwen3-8B'


def test_http_status_and_chat_page_on_the_loopback_server(owner):
    headers = login(owner)
    status, _, raw = request(owner, '/api/status', headers=headers)
    value = json.loads(raw)
    assert status == 200 and value['owner_run']['state'] == 'idle'
    assert value['chat']['available'] is False and value['chat']['local_url'].endswith('/chat')
    status, h, raw = request(owner, '/chat')
    assert status == 200 and b'/chat/chat.js' in raw and "script-src 'self'" in h['Content-Security-Policy']
    status, _, raw = request(owner, '/chat/api/send', 'POST', {'prompt': 'hi'},
                             headers={'X-Mycelium-Chat': '1'})
    assert status == 503 and json.loads(raw)['error'] == 'chat_not_ready'
    status, _, raw = request(owner, '/api/demo/start', 'POST', {}, headers={'Origin': owner.origin})
    assert status == 401


def test_joined_mac_registers_the_demo_auto_step(tmp_path):
    from mycelium_a13_macos import demo_app
    app = DemoApp(tmp_path / 'x', check_platform=False)
    assert demo_app.auto_step in app.auto_steps
