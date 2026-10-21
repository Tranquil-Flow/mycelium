# SPDX-License-Identifier: AGPL-3.0-only
"""Status-side idle linearization using real owner/exchange methods; no live app."""
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest
from mycelium_a13_macos import demo_app, run_model
from mycelium_a13_macos.owner_run import OwnerRun
from mycelium_a13_macos.run_exchange import RunExchange


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    # Install before any app/exchange/owner construction.
    def forbidden(*args, **kwargs):
        raise AssertionError('forbidden_transport_or_process')
    for name in ('connect', 'connect_ex', 'bind', 'listen'):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex',
                 'gethostbyaddr', 'getnameinfo', 'create_connection'):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def fixture_app(tmp_path, monkeypatch, *, owner=True):
    mailbox = SimpleNamespace()
    exchange = RunExchange(mailbox)
    app = SimpleNamespace(installation_role='owner', authority=SimpleNamespace(control_mailbox=mailbox),
                          run_exchange=exchange, model_source_server=SimpleNamespace(mailbox=mailbox, hint=lambda: None),
                          root=tmp_path, guard=threading.RLock(), owner_run=None)
    stopped = threading.Event()
    class Chain:
        def run_step(self, name, ctx):
            if name == 'model':
                assert stopped.wait(5), 'owner_step_release_timeout'
        def teardown(self, ctx, *, failed):
            pass
    if owner:
        app.owner_run = OwnerRun(Chain(), identities=lambda: ('run', 'deployment'))
    choice = ['old']
    def lookup(root):
        assert not app.guard._is_owned(), 'model_lookup_under_app_guard'
        if app.owner_run is not None:
            assert not app.owner_run._lock._is_owned(), 'model_lookup_under_owner_lock'
        return choice[0], 'synthetic', SimpleNamespace(model_id='synthetic-model', resolved_commit='a'*40)
    monkeypatch.setattr(run_model, 'app_run_model', lookup)
    return app, exchange, choice, stopped


def test_no_owner_idle_refresh_and_same_model_noop(tmp_path, monkeypatch):
    app, exchange, choice, _ = fixture_app(tmp_path, monkeypatch, owner=False)
    demo_app.ensure_exchange(app)
    assert exchange.announcement()['model']['choice'] == 'old'
    before = exchange.announcement()['issued_at']
    demo_app.ensure_exchange(app)
    assert exchange.announcement()['issued_at'] == before
    choice[0] = 'new'
    demo_app.ensure_exchange(app)
    assert exchange.announcement()['phase'] == 'idle'
    assert exchange.announcement()['model']['choice'] == 'new'


def test_stopped_owner_model_refresh(tmp_path, monkeypatch):
    app, exchange, choice, stopped = fixture_app(tmp_path, monkeypatch)
    owner = app.owner_run
    try:
        demo_app.ensure_exchange(app)
        owner.start()
        owner.stop()
        stopped.set()
        owner.join(5)
        assert not owner.active() and owner.status()['state'] == 'stopped'
        exchange.announce(phase='stopped')
        choice[0] = 'new'
        demo_app.ensure_exchange(app)
        assert exchange.announcement()['phase'] == 'idle'
        assert exchange.announcement()['model']['choice'] == 'new'
    finally:
        stopped.set();owner.stop();owner.join(5)
        assert owner._thread is None or not owner._thread.is_alive()


@pytest.mark.parametrize('retire', ['exchange', 'owner', 'mailbox'])
def test_delayed_lookup_cannot_write_retired_identity(tmp_path, monkeypatch, retire):
    app, exchange, choice, stopped = fixture_app(tmp_path, monkeypatch)
    old_owner = app.owner_run
    entered, release = threading.Event(), threading.Event()
    errors = []
    def lookup(root):
        entered.set()
        assert release.wait(5), 'lookup_release_timeout'
        return 'fresh', 'synthetic', SimpleNamespace(model_id='synthetic-model', resolved_commit='a'*40)
    monkeypatch.setattr(run_model, 'app_run_model', lookup)
    def refresh():
        try:demo_app.ensure_exchange(app)
        except BaseException as exc:errors.append(repr(exc))
    reader=threading.Thread(target=refresh,name='phase-retired-reader')
    reader.start()
    try:
        assert entered.wait(5)
        if retire == 'exchange':
            app.run_exchange = RunExchange(app.authority.control_mailbox)
        elif retire == 'owner':
            app.owner_run = OwnerRun(old_owner.chain, identities=lambda: ('successor','deployment'))
        else:
            app.authority.control_mailbox = SimpleNamespace()
        release.set();reader.join(5)
        assert not reader.is_alive() and not errors, errors
        assert exchange.announcement()['model'] is None, 'retired_exchange_mutated'
    finally:
        release.set();stopped.set();old_owner.stop()
        if reader.ident is not None:reader.join(5)
        old_owner.join(5)
        assert not reader.is_alive() and not errors, errors


def test_clock_model_and_provider_callbacks_outside_lifecycle_locks(tmp_path, monkeypatch):
    app, exchange, choice, stopped = fixture_app(tmp_path, monkeypatch)
    owner=app.owner_run
    def outside():
        assert not app.guard._is_owned(), 'callback_under_app_guard'
        assert not owner._lock._is_owned(), 'callback_under_owner_lock'
        return 123.0
    exchange._clock=outside
    def provider():
        outside()
        return None
    app.model_source_server.hint=provider
    try:
        demo_app.ensure_exchange(app)  # model callback checks locks in fixture
        assert exchange.announcement()['issued_at']==123.0
        exchange.announce(phase='work')
        assert exchange.announcement()['phase']=='work'  # invokes provider outside locks
        demo_app.ensure_exchange(app)
        assert exchange.announcement()['phase']=='work'  # idle may not replace work
    finally:
        stopped.set();owner.stop();owner.join(5)


def test_no_owner_install_waits_for_idle_commit(tmp_path, monkeypatch):
    app, exchange, choice, stopped = fixture_app(tmp_path, monkeypatch, owner=False)
    from mycelium_a13_macos import demo_chain
    class Steps:
        def run_step(self, name, ctx):
            assert stopped.wait(5), 'owner_step_release_timeout'
        def teardown(self, ctx, *, failed):pass
    monkeypatch.setattr(demo_chain, 'AppDemoChain', lambda app: Steps())
    entered, release, attempting, installed = (threading.Event() for _ in range(4))
    errors=[]
    original=exchange.commit_announcement
    def held(prepared, **kwargs):
        entered.set()
        assert release.wait(5), 'commit_release_timeout'
        return original(prepared, **kwargs)
    monkeypatch.setattr(exchange, 'commit_announcement', held)
    def refresh():
        try:demo_app.ensure_exchange(app)
        except BaseException as exc:errors.append(('refresh',repr(exc)))
    def install():
        attempting.set()
        try:
            demo_app.owner_run(app)
            installed.set()
        except BaseException as exc:errors.append(('install',repr(exc)))
    reader=threading.Thread(target=refresh,name='phase-empty-reader')
    installer=threading.Thread(target=install,name='phase-empty-installer')
    reader.start()
    try:
        assert entered.wait(5)
        installer.start();assert attempting.wait(5)
        assert not installed.wait(.15), 'owner_install_slipped_between_idle_authorization_and_swap'
        release.set();reader.join(5);installer.join(5)
        assert not reader.is_alive() and not installer.is_alive()
        assert installed.is_set() and not errors, errors
        assert exchange.announcement()['phase']=='idle'
        assert app.owner_run is not None
    finally:
        release.set();stopped.set()
        for thread in (reader,installer):
            if thread.ident is not None:thread.join(5)
        if app.owner_run is not None:
            app.owner_run.stop();app.owner_run.join(5)
        assert all(not t.is_alive() for t in (reader,installer)) and not errors,errors


def test_final_idle_commit_orders_owner_start(tmp_path, monkeypatch):
    app, exchange, choice, stopped = fixture_app(tmp_path, monkeypatch)
    owner = app.owner_run
    entered, release, attempting, started = (threading.Event() for _ in range(4))
    errors = []
    original = exchange.commit_announcement
    def held_commit(prepared, **kwargs):
        if prepared[0]['phase'] == 'idle':
            entered.set()
            assert release.wait(5), 'idle_commit_release_timeout'
        return original(prepared, **kwargs)
    monkeypatch.setattr(exchange, 'commit_announcement', held_commit)
    def refresh():
        try:
            demo_app.ensure_exchange(app)
        except BaseException as exc:
            errors.append(('refresh', repr(exc)))
    def start():
        attempting.set()
        try:
            owner.start()
            started.set()
        except BaseException as exc:
            errors.append(('start', repr(exc)))
    reader = threading.Thread(target=refresh, name='phase-idle-commit-reader')
    starter = threading.Thread(target=start, name='phase-idle-commit-starter')
    reader.start()
    try:
        assert entered.wait(5), 'final_idle_commit_not_reached'
        starter.start()
        assert attempting.wait(5)
        assert not started.wait(.15), 'owner_start_slipped_between_idle_authorization_and_swap'
        release.set()
        reader.join(5);starter.join(5)
        assert not reader.is_alive() and not starter.is_alive()
        assert started.is_set() and not errors, errors
        assert exchange.announcement()['phase'] == 'idle'  # ordered before owner Start, not after it
    finally:
        release.set();stopped.set();owner.stop()
        for thread in (reader, starter):
            if thread.ident is not None:thread.join(5)
        owner.join(5)
        assert all(not t.is_alive() for t in (reader, starter))
        assert owner._thread is None or not owner._thread.is_alive()
        assert not errors, errors
