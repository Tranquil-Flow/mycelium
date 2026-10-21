# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded CPU-only publication interleavings; no native/cryptographic qualification.

The paused seam is the *entry* to workflow.status() in _step_serving's
post-wait startup read (demo_chain.py:898), after its final current() check.
The status implementation itself remains the actual ManagedServingWorkflow method.
"""
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest
from mycelium_a13_macos import demo_chain, serving, workflow as wm
from mycelium_a13_macos.run_exchange import RunExchange


def _fence(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('forbidden_transport_or_process')
    for name in ('connect', 'connect_ex', 'bind', 'listen'):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex',
                 'gethostbyaddr', 'getnameinfo', 'create_connection'):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def _fixture(monkeypatch):
    _fence(monkeypatch)  # Before invoking any producer/observer logic.
    monkeypatch.setattr(serving, '_validate_execution_approval', lambda *a: None)
    monkeypatch.setattr(serving, '_plan_digest', lambda *a: 'digest')
    monkeypatch.setattr(serving, '_validate_dynamic_port', lambda *a: None)
    monkeypatch.setattr(serving, '_assert_loopback_port_free', lambda *a: None)
    monkeypatch.setattr(wm, 'publish_controller_offers', lambda *a, **k: ({}, object()))
    started = threading.Event()
    startup = {'startup_qualification_complete': True,
               'unavailable_deployment_count': 0, 'url': 'http://127.0.0.1:45678/'}
    class Launch:
        prepared = None
        operator_plan_digest = 'digest'
        physical_host_ids = ()
        operator_plan = object()
        def run(self, *, approval, port, api_only, stop_event, on_started,
                on_challenge_event):
            assert api_only and port == 45678
            on_started(startup)  # synthetic issuer, not native qualification
            started.set()
            assert stop_event.wait(5), 'producer_stop_timeout'
            return 0
    controller = SimpleNamespace(_run_plan={'run_id': 'run', 'deployment_id': 'deployment'},
                                 execute=lambda phase: None)
    wf = wm.ManagedServingWorkflow(authority=SimpleNamespace(),
                                    controller=controller, launch=Launch())
    wf.bind_progress_identity(run_id='run', deployment_id='deployment', epoch=2)
    exchange = RunExchange(SimpleNamespace())
    app = SimpleNamespace(_chain_workflow=wf, _chain_epoch=2,
                          _chain_compile_state={'state': 'compiled', 'epoch': 2},
                          run_exchange=exchange)
    state = {'participants': [], 'compile_state': {'epoch': 2},
             'model': {'model_id': 'synthetic-model', 'resolved_commit': 'a' * 40},
             'slice_mode': False, 'manifest_digest': 'sha256:' + 'b' * 64}
    ctx = SimpleNamespace(run_id='run', deployment_id='deployment', state=state,
                          result={}, progress=lambda message: None,
                          wait_until=lambda predicate, **kwargs: predicate())
    return wf, app, exchange, ctx, started


@pytest.mark.parametrize('interleaving', ['normal', 'completed_stop', 'replacement'])
def test_serving_publication_after_final_identity_check(monkeypatch, interleaving):
    wf, app, exchange, ctx, started = _fixture(monkeypatch)
    errors = []
    def producer():
        try:
            assert wf.run(approval=SimpleNamespace(operator_plan_digest='digest',
                                                     physical_host_ids=()), port=45678) == 0
        except BaseException as exc:
            errors.append(('producer', repr(exc)))
            started.set()
    producer_thread = threading.Thread(target=producer, name='a13-review-producer')
    entered = threading.Event()
    release = threading.Event()
    real_status = wf.status
    calls = []
    if interleaving != 'normal':
        def gated_status():
            calls.append('status')
            if len(calls) == 2:
                entered.set()
                assert release.wait(5), 'observer_release_timeout'
            return real_status()
        monkeypatch.setattr(wf, 'status', gated_status)
    chain = demo_chain.AppDemoChain(app)
    def observer():
        try:
            chain._step_serving(ctx)  # real progress predicate, status and announcement
        except BaseException as exc:
            errors.append(('observer', repr(exc)))
    observer_thread = threading.Thread(target=observer, name='a13-review-observer')
    producer_thread.start()
    try:
        assert started.wait(5), 'producer_start_timeout'
        assert not errors, errors
        observer_thread.start()
        if interleaving != 'normal':
            assert entered.wait(5), 'post_identity_status_not_entered'
            assert len(calls) == 2, 'unexpected_status_seam'
            # At this point the final identity check passed. No patched state
            # or forged status: retire the actual workflow and await run exit.
            if interleaving == 'replacement':
                replacement = wm.ManagedServingWorkflow(authority=None,
                                                         controller=None, launch=None)
                app._chain_workflow = replacement
            wf.stop()
            producer_thread.join(5)
            assert not producer_thread.is_alive(), 'producer_not_reaped_before_publication'
            assert wf.status()['state'] == 'stopped'
            assert exchange.announcement()['phase'] == 'idle'
            release.set()
        observer_thread.join(5)
        assert not observer_thread.is_alive(), 'observer_not_reaped'
        if interleaving == 'normal':
            assert not errors, errors
            assert exchange.announcement()['phase'] == 'serving'
            assert ctx.result['registry_origin'] == 'http://127.0.0.1:45678'
        else:
            # A true fence refuses the retired commit. The original observer
            # previously returned normally; requiring that old instrumentation
            # expectation would reward the stale publication defect.
            assert errors == [('observer', "OwnerRunError('serving_workflow_identity_changed')")], errors
            # Intentionally retained RED contract: a completed stop/replacement
            # must not publish the retired run as serving-ready.
            assert exchange.announcement()['phase'] != 'serving', (
                'stale_serving_publication_after_' + interleaving)
    finally:
        release.set()
        wf.stop()
        for thread in (observer_thread, producer_thread):
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive(), 'owned_thread_leaked:' + thread.name
