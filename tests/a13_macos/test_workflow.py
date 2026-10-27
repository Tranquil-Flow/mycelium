# SPDX-License-Identifier: AGPL-3.0-only
"""Real owner TLS + native worker workflow; not two-host/model qualification."""
import threading
from pathlib import Path
import pytest
from test_control_pump import _world
from test_controller_managed_staging import setup_controller
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_worker import POLICY, SAFE
from mycelium_a13_macos.target_app import TargetApplication
from physical_inference_qualification import ControllerError
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_a13_macos.workflow import owner_channel, publish_controller_offers, run_target_offer


def _compose_publish_run_cleanup(tmp_path):
    with _world(tmp_path/'world') as w:
        authority=w['authority']
        controller,peer=setup_controller(tmp_path,w)
        owner_stop=threading.Event()
        cleanup_tick=lambda:None
        offers,adapter=publish_controller_offers(controller,authority=authority,
            run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,stop_event=owner_stop,cleanup_progress=cleanup_tick)
        assert adapter.stop_event is owner_stop and adapter.cleanup_progress is cleanup_tick
        offer=offers[peer.node_id]
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        sender=owner_channel(authority)
        stop=threading.Event();outcomes=[];errors=[]
        def contribute():
            try:
                outcomes.append(run_target_offer(app,offer,acknowledged=True,
                    resource_policy=POLICY,limits=WorkerLimits(max_rss_bytes=512*1024*1024,
                        max_disk_bytes=100_000,max_wall_seconds=30,max_commands=20),
                    authorize_load=lambda intent:False,stop_event=stop,cleanup_on_owner_request=True,
                    _test_observer=lambda:dict(SAFE),
                    _test_ca_certificate=authority.seed_server.material.ca_certificate))
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=contribute,daemon=True);thread.start()
        try:
            archive,digest=controller._archive_identity_for_peer(peer)
            capture=adapter.prepare(peer=peer,archive=archive,archive_digest=digest,
                preposition_bytes=b'',preposition_digest=None,timeout_seconds=10)
            assert capture.returncode==0
            session=sender.session(node_id=peer.node_id,run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,timeout_seconds=10)
            try:reply=session.send(command_id='workflow-hello',command='hello',payload={})
            except ControllerError:
                # A target worker that died before serving leaves the owner waiting
                # until the response deadline; report the worker's cause, not the wait.
                thread.join(1)
                if errors:raise AssertionError(f'target worker failed: {errors[0]!r}') from errors[0]
                raise
            assert reply['ok'] is True and reply['result']['state']=='NEW'
            assert reply['route_ready'] is False
            session.close()
            cleanup=adapter.cleanup(peer=peer,archive_digest=digest,preposition_digest=None,timeout_seconds=10)
            thread.join(5)
            assert not thread.is_alive() and not errors
            assert outcomes[0]['state']=='cleaned' and outcomes[0]['route_ready'] is False
            assert cleanup.returncode==0 and not Path(peer.staging_root).exists()
            assert app.peer.status()['identity_state']=='present'
            authority.control_mailbox.retire_offer(offer)
            assert authority.control_mailbox.pending_offer(w['member']) is None
        finally:
            stop.set();thread.join(5)
            app.stop_control();sender.close()
        assert not thread.is_alive()


def test_owner_publish_target_run_and_requested_cleanup_compose(native_sidecar_required, tmp_path):
    _compose_publish_run_cleanup(tmp_path)


def test_compose_reports_a_dead_target_worker_cause_not_the_owner_wait(tmp_path,monkeypatch):
    def broken(self):raise ValueError('native_sidecar_installation_invalid')
    monkeypatch.setattr(TargetApplication,'_installed_sidecar',broken)
    with pytest.raises(AssertionError,match='native_sidecar_installation_invalid'):
        _compose_publish_run_cleanup(tmp_path)


def test_archive_budget_rejection_precedes_any_offer_publication(tmp_path):
    with _world(tmp_path/'world') as w:
        controller,_=setup_controller(tmp_path,w)
        with pytest.raises(ValueError,match='archive_budget_exceeded'):
            publish_controller_offers(controller,authority=w['authority'],
                run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,max_archive_bytes=1)
        assert w['authority'].control_mailbox.pending_offer(w['member']) is None


def test_pre_cancelled_target_never_creates_identity_or_worker(tmp_path):
    app=TargetApplication(tmp_path/'never-started')
    stop=threading.Event();stop.set()
    with pytest.raises(ValueError,match='managed_workflow_stopped'):
        run_target_offer(app,{},acknowledged=True,resource_policy=POLICY,limits=None,
            authorize_load=None,stop_event=stop,cleanup_on_owner_request=True)
    assert not app.peer.state_root.exists()
