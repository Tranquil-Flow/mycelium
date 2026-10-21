# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical controller consumes target-owned signed staging, no model work."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import pytest
from physical_inference_qualification import QualificationController, PeerIdentity, ControllerError, CommandCapture
from test_control_pump import _world
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from mycelium_a13_macos.target_app import TargetApplication


def setup_controller(tmp_path,w,**kwargs):
    source=tmp_path/'source';source.mkdir()
    data=b'# inert transfer fixture, not a model or host observation\n'
    (source/'physical_inference_node.py').write_bytes(data)
    target=w['target']
    peer=PeerIdentity(node_id=w['member']['node_id'],ssh_target=None,host_id='fixture-host',boot_id='fixture-boot',
        staging_root=str(target.state_root/'managed'/RUN_ID/'mycelium-stage'),process_transport='managed_https')
    return QualificationController(mode='physical',peers=[peer],source_root=source,
        transfer_manifest={'protocol':'mycelium.controller_transfer_manifest.v1','files':[
            {'path':'physical_inference_node.py','size_bytes':len(data),'content_digest':'sha256:'+hashlib.sha256(data).hexdigest()}]},
        membership_snapshot={},now=0,**kwargs),peer


def test_managed_prepare_never_calls_empty_subprocess_argv(tmp_path):
    class Runner:
        def run(self,*args,**kwargs):pytest.fail('managed staging reached subprocess runner')
    with _world(tmp_path/'world') as w:
        controller,peer=setup_controller(tmp_path,w,runner=Runner())
        with pytest.raises(ControllerError,match='managed_staging_provider_required'):
            controller._prepare_physical((),{})


def test_controller_admits_real_target_stage_ack_and_rejects_wrong_root(tmp_path):
    from mycelium_a13_macos.controller_staging import ManagedControllerStaging
    with _world(tmp_path/'world') as w:
        box=w['authority']._control_mailbox
        adapter=ManagedControllerStaging(mailbox=box,members=[w['member']],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        controller,peer=setup_controller(tmp_path,w,staging_adapter=adapter)
        archive,digest=controller._archive_identity_for_peer(peer)
        offer=box.publish_offer(target=w['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,archive=archive)
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        ca=w['authority'].seed_server.material.ca_certificate
        ack=app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=ca)
        result=controller._prepare_physical((),{})
        assert result['actions'][0]['acknowledgement']==ack
        assert result['route_ready'] is False
        assert app._runtime is None
        with pytest.raises(ControllerError,match='managed_staging_root_mismatch'):
            adapter.prepare(peer=replace(peer,staging_root='/other/run/mycelium-stage'),archive=archive,
                archive_digest=digest,preposition_bytes=b'',preposition_digest=None,timeout_seconds=1)


def test_target_cleanup_is_signed_explicit_and_retains_identity(tmp_path):
    from mycelium_a13_macos.controller_staging import ManagedControllerStaging
    with _world(tmp_path/'world') as w:
        box=w['authority']._control_mailbox
        adapter=ManagedControllerStaging(mailbox=box,members=[w['member']],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        controller,peer=setup_controller(tmp_path,w,staging_adapter=adapter)
        archive,digest=controller._archive_identity_for_peer(peer)
        offer=box.publish_offer(target=w['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,archive=archive)
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        ca=w['authority'].seed_server.material.ca_certificate
        app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=ca)
        identity={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in app.peer.state_root.rglob('*.key')}
        assert identity
        box.request_stage_cleanup(offer)
        from mycelium_a13_macos.managed_control import ManagedControlError
        with pytest.raises(ManagedControlError,match='offer_cleanup_requested'):
            box.stage_receipt(offer)
        with pytest.raises(ManagedControlError,match='offer_cleanup_requested'):
            app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
                authorize=lambda intent:True,_test_ca_certificate=ca)
        assert app.pending_cleanup(_test_ca_certificate=ca)==offer
        with pytest.raises(ValueError,match='stage_not_authorized'):
            app.cleanup_offer(offer,acknowledged=False,_test_ca_certificate=ca)
        assert Path(peer.staging_root).exists()
        ack=app.cleanup_offer(offer,acknowledged=True,_test_ca_certificate=ca)
        result=controller._cleanup_peer(peer,archive_digest=digest)
        assert result==ack and ack['removed'] is True
        assert not Path(peer.staging_root).exists()
        assert all(p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in identity.items())
        assert app.pending_cleanup(_test_ca_certificate=ca) is None
