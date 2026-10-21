# SPDX-License-Identifier: AGPL-3.0-only
"""Independent real OwnerRun stop controls for the proposed publication fence."""
import threading
from types import SimpleNamespace
import pytest
from mycelium_a13_macos import demo_chain
from mycelium_a13_macos.owner_run import OwnerRun
from test_serving_publication_race_review import _fixture

@pytest.mark.parametrize('stop_during_prepare', [False, True])
def test_real_owner_run_stop_is_cancellation_not_failure(monkeypatch, stop_during_prepare):
    wf, app, exchange, template, started = _fixture(monkeypatch)
    app.guard = threading.RLock()
    entered, release, serving_done = threading.Event(), threading.Event(), threading.Event()
    real_prepare = exchange.prepare_announcement
    def prepare(**kwargs):
        result = real_prepare(**kwargs)
        if kwargs['phase'] == 'serving' and stop_during_prepare:
            entered.set()
            assert release.wait(5), 'parent_prepare_release_timeout'
        return result
    monkeypatch.setattr(exchange, 'prepare_announcement', prepare)
    real_chain = demo_chain.AppDemoChain(app)
    contexts, errors = [], []
    class Chain:
        def run_step(self, name, ctx):
            if not contexts:
                contexts.append(ctx)
                ctx.state.update(template.state)
            if name == 'serving':
                real_chain._step_serving(ctx)
                serving_done.set()
        def teardown(self, ctx, *, failed):
            wf.stop()
    owner = OwnerRun(Chain(), identities=lambda: ('run', 'deployment'))
    def produce():
        try:
            assert wf.run(approval=SimpleNamespace(operator_plan_digest='digest', physical_host_ids=()), port=45678) == 0
        except BaseException as exc:
            errors.append(type(exc).__name__)
            started.set()
    producer = threading.Thread(target=produce, name='a13-parent-owner-producer')
    producer.start()
    try:
        assert started.wait(5) and not errors
        owner.start()
        if stop_during_prepare:
            assert entered.wait(5), 'parent_publication_prepare_not_reached'
            assert exchange.announcement()['phase'] == 'idle'
        else:
            assert serving_done.wait(5), 'parent_normal_serving_not_reached'
            assert exchange.announcement()['phase'] == 'serving'
        owner.stop()  # Real stop/context state, not a forged owner status.
        release.set()
        owner.join(5)
        assert owner._thread is not None and not owner._thread.is_alive()
        producer.join(5)
        assert not producer.is_alive() and not errors
        status = owner.status()
        assert status['state'] == 'stopped', 'explicit_owner_stop_misclassified_as_failure'
        assert status['error_code'] is None
        if stop_during_prepare:
            assert exchange.announcement()['phase'] != 'serving'
            assert 'registry_origin' not in contexts[0].result
    finally:
        release.set()
        owner.stop()
        wf.stop()
        owner.join(5)
        producer.join(5)
        assert not producer.is_alive()
        assert owner._thread is None or not owner._thread.is_alive()
