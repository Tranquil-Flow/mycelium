# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only owner-status idle writer versus a real OwnerRun serving commit.

Only the non-serving owner steps and native issuer are synthetic. OwnerRun,
AppDemoChain._step_serving, ManagedServingWorkflow, RunExchange, and
ensure_exchange/owner_status are production methods from the bound mirror.
"""
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest
from mycelium_a13_macos import demo_app, demo_chain, run_model, workflow as wm
from mycelium_a13_macos.owner_run import OwnerRun
from mycelium_a13_macos.run_exchange import RunExchange


def test_status_idle_writer_cannot_replace_current_serving(monkeypatch, tmp_path):
    # Intercept every external effect before any application method runs.
    def forbidden(*args, **kwargs):
        raise AssertionError('forbidden_transport_or_process')
    for name in ('connect', 'connect_ex', 'bind', 'listen'):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex',
                 'gethostbyaddr', 'getnameinfo', 'create_connection'):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(wm, 'publish_controller_offers', lambda *a, **k: ({}, object()))
    from mycelium_a13_macos import serving
    monkeypatch.setattr(serving, '_validate_execution_approval', lambda *a: None)
    monkeypatch.setattr(serving, '_plan_digest', lambda *a: 'digest')
    monkeypatch.setattr(serving, '_validate_dynamic_port', lambda *a: None)
    monkeypatch.setattr(serving, '_assert_loopback_port_free', lambda *a: None)

    mailbox = SimpleNamespace()
    exchange = RunExchange(mailbox)
    started, entered, release, serving_done = (threading.Event() for _ in range(4))
    errors = []
    startup = {'startup_qualification_complete': True,
               'unavailable_deployment_count': 0, 'url': 'http://127.0.0.1:45678/'}
    class Launch:
        prepared = None
        operator_plan_digest = 'digest'
        physical_host_ids = ()
        operator_plan = object()
        def run(self, *, approval, port, api_only, stop_event, on_started, on_challenge_event):
            assert api_only and port == 45678
            on_started(startup)
            started.set()
            assert stop_event.wait(5), 'workflow_stop_timeout'
            return 0
    controller = SimpleNamespace(_run_plan={'run_id': 'run', 'deployment_id': 'deployment'},
                                 execute=lambda phase: None)
    wf = wm.ManagedServingWorkflow(authority=SimpleNamespace(), controller=controller, launch=Launch())
    wf.bind_progress_identity(run_id='run', deployment_id='deployment', epoch=2)
    app = SimpleNamespace(installation_role='owner', authority=SimpleNamespace(control_mailbox=mailbox),
                          model_source_server=SimpleNamespace(mailbox=mailbox, hint=lambda: None),
                          run_exchange=exchange, root=tmp_path, guard=threading.RLock(),
                          _chain_workflow=wf, _chain_epoch=2,
                          _chain_compile_state={'state': 'compiled', 'epoch': 2})
    state = {'participants': [], 'compile_state': {'epoch': 2},
             'model': {'model_id': 'synthetic-model', 'resolved_commit': 'a' * 40},
             'slice_mode': False, 'manifest_digest': 'sha256:' + 'b' * 64}
    chain = demo_chain.AppDemoChain(app)
    class Steps:
        def run_step(self, name, ctx):
            if not ctx.state:
                ctx.state.update(state)
            if name == 'serving':
                chain._step_serving(ctx)
                serving_done.set()
        def teardown(self, ctx, *, failed):
            wf.stop()
    owner = OwnerRun(Steps(), identities=lambda: ('run', 'deployment'))
    app.owner_run = owner
    def model_lookup(root):
        entered.set()
        assert release.wait(5), 'idle_lookup_release_timeout'
        return 'synthetic-choice', 'synthetic', SimpleNamespace(model_id='synthetic-model',
                                                                 resolved_commit='a' * 40)
    monkeypatch.setattr(run_model, 'app_run_model', model_lookup)
    def produce():
        try:
            assert wf.run(approval=SimpleNamespace(operator_plan_digest='digest',
                                                      physical_host_ids=()), port=45678) == 0
        except BaseException as error:
            errors.append(('producer', repr(error)))
            started.set()
    def status_reader():
        try:
            demo_app.owner_status(app)  # actual status -> ensure_exchange idle writer
        except BaseException as error:
            errors.append(('reader', repr(error)))
    producer = threading.Thread(target=produce, name='phase-producer')
    reader = threading.Thread(target=status_reader, name='phase-status-reader')
    producer.start()
    try:
        assert started.wait(5) and not errors
        reader.start()
        assert entered.wait(5), 'idle_writer_not_reached'
        assert exchange.announcement()['phase'] == 'idle'
        owner.start()  # actual run lifecycle; no simultaneous owner runs
        assert serving_done.wait(5), 'serving_observer_not_completed'
        assert owner.status()['state'] in {'running', 'serving'}
        assert exchange.announcement()['phase'] == 'serving'
        release.set()
        reader.join(5)
        assert not reader.is_alive(), 'idle_writer_not_reaped'
        assert not errors, errors
        # This expected invariant is the adversarial contract; current bytes RED.
        assert exchange.announcement()['phase'] == 'serving', 'late_idle_overwrote_current_serving'
    finally:
        release.set()
        owner.stop()
        wf.stop()
        for thread in (reader, producer):
            if thread.ident is not None:
                thread.join(5)
        owner.join(5)
        assert all(not t.is_alive() for t in (reader, producer))
        assert owner._thread is None or not owner._thread.is_alive()
        assert not errors, errors
