# SPDX-License-Identifier: AGPL-3.0-only
"""Actual OwnerRun stop blocked at the real publication fence, without sleep oracles."""
import sys
import threading
import time
from types import SimpleNamespace
from mycelium_a13_macos import demo_chain
from mycelium_a13_macos.owner_run import OwnerRun
from test_serving_publication_race_review import _fixture

def test_actual_owner_stop_waits_for_in_memory_commit(monkeypatch):
    wf, app, exchange, template, started = _fixture(monkeypatch)
    app.guard = threading.RLock()
    prep_entered, prep_release = threading.Event(), threading.Event()
    commit_entered, commit_release, stop_done = threading.Event(), threading.Event(), threading.Event()
    errors, held_locks = [], []
    real_chain = demo_chain.AppDemoChain(app)
    class Chain:
        def run_step(self, name, ctx):
            ctx.state.update(template.state)
            if name == 'serving': real_chain._step_serving(ctx)
        def teardown(self, ctx, *, failed): wf.stop()
    owner = OwnerRun(Chain(), identities=lambda: ('run', 'deployment'))
    prepare, commit = exchange.prepare_announcement, exchange.commit_announcement
    def held_prepare(**kwargs):
        result = prepare(**kwargs)
        prep_entered.set()
        assert prep_release.wait(5)
        return result
    def held_commit(prepared):
        held_locks.append((app.guard._is_owned(), owner._lock._is_owned(), wf._lock._is_owned()))
        commit_entered.set()
        assert commit_release.wait(5)
        return commit(prepared)
    monkeypatch.setattr(exchange, 'prepare_announcement', held_prepare)
    monkeypatch.setattr(exchange, 'commit_announcement', held_commit)
    def produce():
        try: wf.run(approval=SimpleNamespace(operator_plan_digest='digest', physical_host_ids=()), port=45678)
        except BaseException as exc:
            errors.append(('producer', type(exc).__name__))
            started.set()
    def stop():
        try: owner.stop()
        except BaseException as exc: errors.append(('stop', type(exc).__name__))
        finally: stop_done.set()
    producer = threading.Thread(target=produce, name='a13-parent-commit-producer')
    stopper = threading.Thread(target=stop, name='a13-parent-commit-stopper')
    producer.start()
    try:
        assert started.wait(5) and not errors
        owner.start()  # Preparation deliberately waits outside all lifecycle locks.
        assert prep_entered.wait(5)
        prep_release.set()
        assert commit_entered.wait(5)
        assert held_locks == [(True, True, True)]
        stopper.start()
        deadline = time.monotonic() + 5
        blocked_at_stop_lock = False
        while time.monotonic() < deadline:
            frame = sys._current_frames().get(stopper.ident)
            while frame is not None:
                if (frame.f_code.co_name == 'stop'
                        and frame.f_globals.get('__name__') == 'mycelium_a13_macos.owner_run'
                        and frame.f_lineno == OwnerRun.stop.__code__.co_firstlineno + 1):
                    blocked_at_stop_lock = True
                    break
                frame = frame.f_back
            if blocked_at_stop_lock or stop_done.is_set(): break
            threading.Event().wait(.001)
        assert blocked_at_stop_lock, 'actual_owner_stop_did_not_reach_held_lock'
        assert not stop_done.is_set()
        commit_release.set()
        stopper.join(5)
        owner.join(5)
        producer.join(5)
        assert not stopper.is_alive() and not producer.is_alive() and not owner._thread.is_alive()
        assert not errors and stop_done.is_set()
        assert owner.status()['state'] == 'stopped'
        # Commit linearized before stop; withdrawal is deliberately not claimed.
        assert exchange.announcement()['phase'] == 'serving'
    finally:
        prep_release.set()
        commit_release.set()
        owner.stop()
        wf.stop()
        for thread in (stopper, producer):
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive()
        owner.join(5)
        assert owner._thread is None or not owner._thread.is_alive()
