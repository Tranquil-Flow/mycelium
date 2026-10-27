# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal integrated managed flow on ONE local host; flow evidence only.

Real _world (signed TLS, current members) -> publish inert bootstrap offer ->
TargetApplication.start_control -> real owner ManagedSessionFactory native
hello (host+boot identity, no model) -> stop -> owner request_stage_cleanup ->
target cleanup_offer -> owner retire_offer -> publish a new offer ->
TargetApplication.start_work with an explicit deny load authorizer -> real
native hello -> denied configure before any native load -> stop and signed
cleanup. Not physical qualification; no fake native hello, no fabricated ready.
"""
from pathlib import Path
import pytest

from test_control_pump import _world
from test_control_session import owner
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_managed_staging import bundle
from test_worker import POLICY, SAFE
from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.session_factory import ManagedSessionFactory
from mycelium_a13_macos.worker import WorkerLimits, WorkerError
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity

WORK_RUN = RUN_ID + '-work'


def _app(w):
    # Application adapter over the already-joined signed-TLS peer.
    return TargetApplication(w['target'].state_root,
                             _test_peer_factory=lambda root: w['target'])


def _options(w, **changes):
    options = dict(acknowledged=True, resource_policy=POLICY,
        limits=WorkerLimits(max_rss_bytes=512*1024*1024, max_disk_bytes=100_000,
            max_wall_seconds=30, max_commands=20),
        authorize_load=lambda envelope: False,
        _test_observer=lambda: dict(SAFE),
        _test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
    options.update(changes)
    return options


def _factory(w, sender, run_id):
    member = w['authority'].coordinator.member(w['member']['node_id'])
    return ManagedSessionFactory(owner=sender, run_id=run_id,
                                  deployment_id=DEPLOYMENT_ID, members=[member])


def _assert_real_native_hello(reply, w, run_id):
    assert reply['ok'] is True and reply['node_id'] == w['member']['node_id']
    assert reply['route_ready'] is False
    identity = reply['result']
    assert identity['protocol'] == 'mycelium.physical_node_control.v1'
    assert identity['run_id'] == run_id
    assert identity['deployment_id'] == DEPLOYMENT_ID
    assert identity['node_id'] == w['member']['node_id']
    assert identity['state'] == 'NEW' and identity['route_ready'] is False
    assert isinstance(identity['process_id'], int) and identity['process_id'] > 0
    # Host+boot identity is measured on this machine and run-scoped, not invented.
    host_id, boot_id = derive_local_run_scoped_identity(run_id)
    assert identity['host_id'] == host_id and identity['boot_id'] == boot_id


def test_minimal_managed_control_work_and_signed_cleanup_flow(native_sidecar_required, tmp_path):
    archive, _ = bundle()
    with _world(tmp_path/'world') as w:
        box = w['authority']._control_mailbox
        ca = w['authority'].seed_server.material.ca_certificate
        app = _app(w)
        sender = owner(tmp_path/'sender', w)
        control_session = None
        work_session = None
        runtime = None
        try:
            # --- bootstrap: publish an inert offer, control-only activation ---
            offer = box.publish_offer(target=w['member'], run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID, archive=archive)
            status = app.start_control(offer, acknowledged=True,
                resource_policy=POLICY, max_disk_bytes=100_000,
                _test_observer=lambda: dict(SAFE), _test_ca_certificate=ca)
            assert status['state'] == 'running' and status['route_ready'] is False
            assert status['model_load_authorized'] is False

            # --- real owner factory session over the running control worker ---
            control_session = _factory(w, sender, RUN_ID)(
                argv=(), node_id=w['member']['node_id'], run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID, timeout_seconds=10)
            assert control_session.returncode is None and control_session.stderr == b''
            reply = control_session.send(command_id='flow-control-hello',
                command='hello', payload={})
            _assert_real_native_hello(reply, w, RUN_ID)
            control_session.close()
            assert control_session.returncode == 0
            assert control_session.stop_response['ok'] is True
            control_session = None

            # --- stop the control worker ---
            app.stop_control()
            runtime = None
            assert app.status()['native_control']['state'] == 'stopped'

            # --- owner-requested cleanup, target-confirmed, then retirement ---
            box.request_stage_cleanup(offer)
            cleanup = app.cleanup_offer(offer, acknowledged=True, _test_ca_certificate=ca)
            assert cleanup['protocol'] == 'mycelium.controller_remote_cleanup_ack.v1'
            assert cleanup['node_id'] == w['member']['node_id']
            assert cleanup['removed'] is True
            receipt = box.retire_offer(offer)
            assert receipt['offer'] == offer
            assert receipt['stage_ack'] is not None
            assert receipt['cleanup_ack'] == cleanup
            assert box.pending_offer(w['member']) is None

            # --- new work offer after the slot was released by retirement ---
            work_offer = box.publish_offer(target=w['member'], run_id=WORK_RUN,
                deployment_id=DEPLOYMENT_ID, archive=archive)
            assert work_offer['offer_id'] != offer['offer_id']

            # --- work activation under an explicit deny load authorizer ---
            seen = []
            def deny(envelope):
                seen.append(dict(envelope))
                return False
            options = _options(w, authorize_load=deny)
            status = app.start_work(work_offer, **options)
            assert status['state'] == 'running' and status['route_ready'] is False
            assert status['model_load_authorized'] is False
            runtime = app._runtime

            # --- real native hello through a run-scoped factory session ---
            work_session = _factory(w, sender, WORK_RUN)(
                argv=(), node_id=w['member']['node_id'], run_id=WORK_RUN,
                deployment_id=DEPLOYMENT_ID, timeout_seconds=10)
            try:
                reply = work_session.send(command_id='flow-work-hello',
                    command='hello', payload={})
            except Exception:
                print('WORK_RUNTIME_DIAGNOSTIC', runtime.status(), runtime.worker.status())
                raise
            _assert_real_native_hello(reply, w, WORK_RUN)
            commands_before = runtime.worker.status()['command_count']

            # --- denied configure: load gate refuses before any native call ---
            with pytest.raises(WorkerError, match='model_execution_not_authorized'):
                runtime.worker.send(command_id='flow-denied-configure',
                    command='configure', payload={})
            assert seen, 'deny authorizer was never consulted'
            assert seen[0]['run_id'] == WORK_RUN
            assert seen[0]['deployment_id'] == DEPLOYMENT_ID
            assert seen[0]['command'] == 'configure'
            assert runtime.worker.status()['command_count'] == commands_before
            assert runtime.worker.status()['model_load_authorized'] is False

            # --- stop and signed cleanup of the work run ---
            work_session.close()
            assert work_session.stop_response['ok'] is True
            work_session = None
            app.stop_control()
            assert not runtime.worker.process_group_present()
            runtime = None
            box.request_stage_cleanup(work_offer)
            work_cleanup = app.cleanup_offer(work_offer, acknowledged=True,
                _test_ca_certificate=ca)
            assert work_cleanup['removed'] is True
            assert box.retire_offer(work_offer)['cleanup_ack'] == work_cleanup
            assert box.pending_offer(w['member']) is None

            # Membership/identity survive the whole flow on one local host.
            assert app.peer.status()['membership_state'] == 'current'
            assert app.peer.status()['identity_state'] == 'present'
        finally:
            for handle in (work_session, control_session):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
            try:
                app.stop_control()
            except Exception:
                pass
            sender.close()
        if runtime is not None:
            assert not runtime.worker.process_group_present()
