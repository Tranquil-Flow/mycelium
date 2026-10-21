# SPDX-License-Identifier: AGPL-3.0-only
"""Offline source-composition controls; no native/model/physical qualification.

Preparation and commit are separate seams: a check merely moved to after
preparation would fail the first interleaving. The second proves the stop and
in-memory serving commit share the workflow lock, not announcement withdrawal.
"""
import threading

import pytest
from mycelium_a13_macos import demo_chain, chain_compile
from test_serving_publication_race_review import _fixture


@pytest.mark.parametrize('replacement', [False, True])
def test_retirement_after_preparation_before_commit(monkeypatch, replacement):
    wf, app, exchange, ctx, started = _fixture(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = exchange.prepare_announcement
    def held(**kwargs):
        result = original(**kwargs)
        if kwargs['phase'] == 'serving':
            entered.set()
            assert release.wait(5), 'preparation_release_timeout'
        return result
    monkeypatch.setattr(exchange, 'prepare_announcement', held)
    def produce():
        try:
            from types import SimpleNamespace
            wf.run(approval=SimpleNamespace(operator_plan_digest='digest', physical_host_ids=()), port=45678)
        except BaseException as exc:
            errors.append(('producer', repr(exc)))
            started.set()
    def observe():
        try: demo_chain.AppDemoChain(app)._step_serving(ctx)
        except BaseException as exc: errors.append(('observer', repr(exc)))
    producer = threading.Thread(target=produce, name='a13-fence-producer')
    observer = threading.Thread(target=observe, name='a13-fence-observer')
    producer.start()
    try:
        assert started.wait(5), 'producer_start_timeout'
        observer.start()
        assert entered.wait(5), 'preparation_not_reached'
        if replacement:
            from mycelium_a13_macos.workflow import ManagedServingWorkflow
            app._chain_workflow = ManagedServingWorkflow(authority=None, controller=None, launch=None)
        wf.stop()
        producer.join(5)
        assert not producer.is_alive(), 'producer_not_reaped_before_commit'
        assert exchange.announcement()['phase'] == 'idle'
        release.set()
        observer.join(5)
        assert not observer.is_alive(), 'observer_not_reaped'
        assert errors == [('observer', "OwnerRunError('serving_workflow_identity_changed')")], errors
        assert exchange.announcement()['phase'] != 'serving'
    finally:
        release.set()
        wf.stop()
        for thread in (observer, producer):
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive(), 'owned_thread_leaked:' + thread.name


def test_stop_cannot_complete_inside_publication_commit(monkeypatch):
    wf, app, exchange, ctx, started = _fixture(monkeypatch)
    entered, release, stop_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = exchange.commit_announcement
    def held(prepared):
        if prepared[0]['phase'] == 'serving':
            entered.set()
            assert release.wait(5), 'commit_release_timeout'
        return original(prepared)
    monkeypatch.setattr(exchange, 'commit_announcement', held)
    def produce():
        try:
            from types import SimpleNamespace
            wf.run(approval=SimpleNamespace(operator_plan_digest='digest', physical_host_ids=()), port=45678)
        except BaseException as exc:
            errors.append(('producer', repr(exc)))
            started.set()
    def observe():
        try: demo_chain.AppDemoChain(app)._step_serving(ctx)
        except BaseException as exc: errors.append(('observer', repr(exc)))
    def retire():
        try: wf.stop()
        except BaseException as exc: errors.append(('stop', repr(exc)))
        finally: stop_done.set()
    producer = threading.Thread(target=produce, name='a13-fence-producer')
    observer = threading.Thread(target=observe, name='a13-fence-observer')
    stopper = threading.Thread(target=retire, name='a13-fence-stopper')
    producer.start()
    try:
        assert started.wait(5), 'producer_start_timeout'
        observer.start()
        assert entered.wait(5), 'publication_commit_not_reached'
        stopper.start()
        # Stop cannot pass the commit lock. This is not a scheduler liveness proof;
        # release in finally so every owned thread is reaped even on assertion failure.
        assert not stop_done.wait(.05), 'stop_overtook_commit'
        release.set()
        observer.join(5)
        stopper.join(5)
        producer.join(5)
        assert all(not t.is_alive() for t in (observer, stopper, producer))
        assert stop_done.is_set() and not errors, errors
        assert exchange.announcement()['phase'] == 'serving'
        assert wf.status()['state'] == 'stopped'
    finally:
        release.set()
        wf.stop()
        for thread in (observer, stopper, producer):
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive(), 'owned_thread_leaked:' + thread.name


def test_real_stop_serving_retirement_precedes_commit(monkeypatch, tmp_path):
    """Use the actual compiler retirement writer, not a forged stopped status."""
    from types import SimpleNamespace
    wf, app, exchange, ctx, started = _fixture(monkeypatch)
    app.guard = threading.RLock()
    app.root = tmp_path
    app._observation_context = None
    app._chain_compile_thread = None
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = exchange.prepare_announcement
    def held(**kwargs):
        prepared = original(**kwargs)
        if kwargs['phase'] == 'serving':
            entered.set()
            assert release.wait(5), 'preparation_release_timeout'
        return prepared
    monkeypatch.setattr(exchange, 'prepare_announcement', held)
    def produce():
        try:
            wf.run(approval=SimpleNamespace(operator_plan_digest='digest', physical_host_ids=()), port=45678)
        except BaseException as exc:
            errors.append(('producer', repr(exc)))
            started.set()
    def observe():
        try: demo_chain.AppDemoChain(app)._step_serving(ctx)
        except BaseException as exc: errors.append(('observer', repr(exc)))
    producer = threading.Thread(target=produce, name='a13-fence-producer')
    observer = threading.Thread(target=observe, name='a13-fence-observer')
    app._chain_serving_thread = producer
    producer.start()
    try:
        assert started.wait(5), 'producer_start_timeout'
        observer.start()
        assert entered.wait(5), 'preparation_not_reached'
        assert chain_compile.stop_serving(app, timeout=5)['state'] == 'stopped'
        assert not producer.is_alive(), 'producer_not_reaped_by_retirement'
        assert app._chain_workflow is None
        release.set()
        observer.join(5)
        assert not observer.is_alive(), 'observer_not_reaped'
        assert errors == [('observer', "OwnerRunError('serving_workflow_identity_changed')")], errors
        assert exchange.announcement()['phase'] == 'idle'
    finally:
        release.set()
        wf.stop()
        for thread in (observer, producer):
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive(), 'owned_thread_leaked:' + thread.name
