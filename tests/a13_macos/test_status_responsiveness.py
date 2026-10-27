# SPDX-License-Identifier: AGPL-3.0-only
"""Real loopback reads during event-held work; no native models or devices."""
import http.client
import json
import threading
import time
from types import SimpleNamespace as NS

import pytest

from test_app import app, login
from test_chain_compile_parent import fixture_app
from mycelium_a13_macos import chain_compile
from mycelium_a13_macos.owner_run import OwnerRun, RunContext


def read(app, headers, path='/api/status', *, timeout=0.5):
    connection = http.client.HTTPConnection('127.0.0.1', app.port, timeout=timeout)
    start = time.monotonic()
    try:
        if path == '/chat/api/state':
            connection.request('POST', path, '{}', {
                'Content-Type': 'application/json', 'X-Mycelium-Chat': '1'})
        else:
            connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        assert response.status == 200, value
        return value, time.monotonic() - start
    finally:
        connection.close()


def run_at(app, step, *, monitor=lambda ctx: None):
    run = OwnerRun(NS(monitor=monitor))
    run._ctx = RunContext(run, 'status-run', 'status-deployment', 'start')
    run._state, run._step = 'running', step
    run._steps[step]['state'] = 'active'
    app.owner_run = run
    return run


def assert_shape(value, step):
    assert value['owner_run']['state'] == 'running'
    assert value['owner_run']['step'] == step
    assert value['owner_run']['run_id'] == 'status-run'
    assert value['owner_run']['error_code'] is None
    assert value['owner_run']['route_ready'] is False
    assert value['chat']['available'] is False
    assert value['network']['state'] == 'private'
    assert value['swarm']['enabled'] is False
    assert isinstance(value['acquisition'], dict)
    assert isinstance(value['run_model'], dict)


def test_status_during_real_compile_guard(app, tmp_path, monkeypatch, record_property):
    headers = login(app)
    read(app, headers)  # establish the normal response before work
    run = run_at(app, 'compile')
    compiler, _ = fixture_app(tmp_path / 'compiler', monkeypatch)
    compiler.guard = app.guard
    entered, release = threading.Event(), threading.Event()
    prepare = chain_compile.prepare_serving_workflow

    def held(**kwargs):
        entered.set()
        assert release.wait(5), 'compiler seam not released'
        return prepare(**kwargs)

    monkeypatch.setattr(chain_compile, 'prepare_serving_workflow', held)
    try:
        chain_compile.start_compile(compiler, acknowledged=True)
        assert entered.wait(3)
        value, elapsed = read(app, headers)
        record_property('status_seconds', elapsed)
        print('compile-held status_seconds=%.6f' % elapsed)
        assert_shape(value, 'compile')
        # Progress cannot freeze at the cached run/step while app.guard is held.
        run._progress(run._ctx, 'latest compile progress')
        value, _ = read(app, headers)
        assert next(s for s in value['owner_run']['steps'] if s['name'] == 'compile')['detail'] == 'latest compile progress'
        assert not release.is_set()
    finally:
        release.set()
        compiler._chain_compile_thread.join(5)
        app.owner_run = None
    assert not compiler._chain_compile_thread.is_alive()
    assert compiler._chain_compile_state['state'] == 'compiled'


@pytest.mark.parametrize('role,step', [('owner', 'publish'), ('owner', 'targets'), ('target', 'targets')])
def test_status_during_owner_or_member_work(app, role, step, record_property):
    headers = login(app)
    read(app, headers)
    run = run_at(app, step) if role == 'owner' else None
    entered, release = threading.Event(), threading.Event()

    def work():
        # Same critical section as DemoApp._standing_loop / guarded actions.
        with app.guard:
            app.installation_role = role
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert entered.wait(3)
        value, elapsed = read(app, headers)
        record_property('status_seconds', elapsed)
        print('%s-%s-held status_seconds=%.6f' % (role, step, elapsed))
        assert value['installation_role'] == role
        if run is not None:
            assert_shape(value, step)
        else:
            assert value['owner_run'] is None
            assert value['chat'] is None
        assert not release.is_set()
    finally:
        release.set()
        thread.join(5)
        app.owner_run = None
        app.installation_role = 'owner'
    assert not thread.is_alive()


def test_chat_state_does_not_run_blocked_monitor(app, record_property):
    headers = login(app)
    read(app, headers)
    release = threading.Event()
    calls = []

    def monitor(ctx):
        calls.append('monitor')
        assert release.wait(5)
        return 'member_reported_error'

    # Isolate the reader from independently scheduled maintenance.
    publisher = getattr(app, 'status_publisher', None)
    if publisher is not None:
        publisher.stop()
    with app.guard:
        run = run_at(app, 'serving', monitor=monitor)
        run._state = 'serving'
        run._ctx.result['registry_origin'] = 'http://127.0.0.1:12345'
        try:
            value, elapsed = read(app, headers, '/chat/api/state')
            record_property('chat_seconds', elapsed)
            print('chat-held state_seconds=%.6f' % elapsed)
            assert value['available'] is True
            assert calls == []
            assert run._state == 'serving'
        finally:
            release.set()
            app.owner_run = None


def manual_publisher(app):
    from mycelium_a13_macos.published_status import PublishedStatus
    app.status_publisher.stop()
    publisher = PublishedStatus(app)
    app.status_publisher = publisher
    publisher.refresh()
    assert publisher._published is not None
    return publisher


def test_slow_authority_refresh_keeps_last_complete_view(app, monkeypatch, record_property):
    headers = login(app)
    publisher = manual_publisher(app)
    before = app.read_status()
    entered, release = threading.Event(), threading.Event()
    original = app.authority.status

    def held():
        entered.set()
        assert release.wait(5)
        return {**original(), 'membership': 'updated'}

    monkeypatch.setattr(app.authority, 'status', held)
    run = run_at(app, 'targets')
    thread = threading.Thread(target=publisher.refresh)
    thread.start()
    try:
        assert entered.wait(3)
        run._fail(run._ctx, 'targets', 'member_work_not_running')
        value, elapsed = read(app, headers)
        record_property('authority_held_status_seconds', elapsed)
        assert value['authority'] == before['authority']
        assert value['owner_run']['state'] == 'failing'
        assert value['owner_run']['error_code'] == 'member_work_not_running'
        assert value['owner_run']['route_ready'] is False
        assert not release.is_set()
    finally:
        release.set()
        thread.join(5)
        app.owner_run = None
    assert not thread.is_alive()
    assert app.read_status()['authority']['membership'] == 'updated'


def test_published_reads_detach_and_never_trigger_work(app, monkeypatch):
    publisher = manual_publisher(app)
    run = run_at(app, 'compile')
    run._ctx.result['participants'] = [{'node_id': 'member', 'layers': [0, 1]}]
    try:
        def forbidden(*args, **kwargs):
            raise AssertionError('read invoked maintenance or I/O')
        monkeypatch.setattr(app, 'status', forbidden)
        monkeypatch.setattr(app.authority, 'status', forbidden)
        monkeypatch.setattr(run, 'status', forbidden)
        first = app.read_status()
        first['authority'].clear()
        first['owner_run']['participants'][0]['layers'][0] = 99
        second = app.read_status()
        assert second['authority']
        assert second['owner_run']['participants'][0]['layers'] == [0, 1]
        assert run._ctx.result['participants'][0]['layers'] == [0, 1]
        # A failing collector must retain a complete snapshot and disclose failure.
        publisher.refresh()
        assert app.read_status()['status_snapshot']['refresh_failed'] is True
    finally:
        app.owner_run = None
    assert app.read_status()['owner_run']['state'] == 'idle'


def test_maintenance_monitor_runs_without_app_guard(app):
    publisher = manual_publisher(app)
    acquired = threading.Event()
    helpers = []

    def monitor(ctx):
        def acquire():
            with app.guard:
                acquired.set()
        helper = threading.Thread(target=acquire)
        helpers.append(helper)
        helper.start()
        assert acquired.wait(0.5), 'monitor called while publisher holds app.guard'
        return 'member_reported_error'

    run = run_at(app, 'serving', monitor=monitor)
    run._state = 'serving'
    try:
        publisher.refresh()
        assert acquired.is_set()
        assert app.read_status()['owner_run']['error_code'] == 'member_reported_error'
    finally:
        app.owner_run = None
        for helper in helpers:
            helper.join(2)
            assert not helper.is_alive()


def test_member_read_uses_completed_agent_snapshot_not_io_lock(app):
    from mycelium_a13_macos.demo_target import DemoTargetAgent
    headers = login(app)
    manual_publisher(app)
    agent = DemoTargetAgent(app, consent=None, source=lambda: None, sink=lambda value: None)
    agent._set('work_running', 'last completed tick')
    agent._record_status()
    app.demo_target = agent
    app.installation_role = 'target'
    try:
        with agent._lock:
            value, elapsed = read(app, headers)
            assert value['demo_target']['state'] == 'work_running'
            assert value['demo_target']['detail'] == 'last completed tick'
            assert elapsed < 0.5
    finally:
        app.demo_target = None
        app.installation_role = 'owner'


def test_member_idle_snapshot_retains_joined_label(app, monkeypatch):
    from mycelium_a13_macos.demo_target import DemoTargetAgent
    publisher = manual_publisher(app)
    app.installation_role = 'target'
    agent = DemoTargetAgent(app, consent=None, source=lambda: None, sink=lambda value: None)
    app.demo_target = agent
    monkeypatch.setattr(app.authority, 'status', lambda: {'membership_state': 'current'})
    try:
        publisher.refresh()
        assert app.read_status()['demo_target']['state'] == 'joined'
    finally:
        app.demo_target = None
        app.installation_role = 'owner'


def test_completed_action_publishes_without_running_monitor(app, monkeypatch):
    from test_app import request
    headers = login(app)
    manual_publisher(app)
    run = run_at(app, 'compile')
    run._state = 'stopped'
    calls = []
    monkeypatch.setattr(run, 'status', lambda: calls.append('monitor'))
    try:
        assert request(app, '/api/fast-mode', 'POST', {'enabled': True}, headers)[0] == 200
        assert app.status_publisher._published[1]['fast_mode']['enabled'] is True
        value, _ = read(app, headers)
        assert value['fast_mode']['enabled'] is True
        assert calls == []
    finally:
        app.owner_run = None


def test_completed_setting_survives_busy_publication_guard(app):
    from mycelium_a13_macos.app import DemoApp
    headers = login(app)
    manual_publisher(app)
    entered, release = threading.Event(), threading.Event()

    def save_then_hold():
        with app.guard:
            # A saved setting can precede a long aggregate refresh. The reader
            # must see it without acquiring that aggregate's guard.
            DemoApp._action(app, '/api/fast-mode', {'enabled': True})
            entered.set()
            assert release.wait(5)

    helper = threading.Thread(target=save_then_hold)
    helper.start()
    try:
        assert entered.wait(3)
        value, elapsed = read(app, headers)
        assert value['fast_mode']['enabled'] is True
        assert elapsed < 0.5
        assert not release.is_set()
    finally:
        release.set()
        helper.join(5)
        assert not helper.is_alive()


def test_remote_roster_is_current_without_waiting_for_seed_lock(app, monkeypatch):
    headers = login(app)
    manual_publisher(app)
    lock = threading.RLock()
    calls = []
    member = {'node_id': 'remote', 'generation': 2, 'lifecycle_state': 'RUNNING'}
    coordinator = NS(_lock=lock, members=lambda: calls.append('read') or (dict(member),))
    monkeypatch.setattr(app.authority, 'coordinator', coordinator, raising=False)
    value, _ = read(app, headers)
    assert value['authority']['members'] == [{**member, 'clock': None}]
    assert value['authority']['member_count'] == 1
    calls.clear()
    entered, release = threading.Event(), threading.Event()
    def held():
        with lock:
            entered.set()
            assert release.wait(5)
    helper = threading.Thread(target=held)
    helper.start()
    try:
        assert entered.wait(3)
        _, elapsed = read(app, headers)
        assert elapsed < 0.5
        assert calls == []
        assert not release.is_set()
    finally:
        release.set()
        helper.join(5)
        monkeypatch.setattr(app.authority, 'coordinator', None)
        assert not helper.is_alive()


def test_status_publisher_exits_on_app_close(app):
    publisher = app.status_publisher
    app.close()
    assert not publisher.thread.is_alive()
