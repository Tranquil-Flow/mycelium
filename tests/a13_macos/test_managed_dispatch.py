# SPDX-License-Identifier: AGPL-3.0-only
"""Public controller dispatch must reach its authenticated target-owned adapter."""
import pytest
from test_control_pump import _world
from test_controller_managed_staging import setup_controller
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.controller_staging import ManagedControllerStaging
from physical_inference_qualification import ControllerError


def test_dispatch_reaches_real_signed_stage_and_cleanup_receipts(tmp_path,monkeypatch):
    with _world(tmp_path/'world') as w:
        box=w['authority'].control_mailbox
        adapter=ManagedControllerStaging(mailbox=box,members=[w['member']],
            run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        controller,peer=setup_controller(tmp_path,w,staging_adapter=adapter)
        # This fixture has one real host. Membership/distinctness policy is
        # covered independently; this test proves dispatch, not qualification.
        checks=[]
        monkeypatch.setattr(controller,'_validate_membership',lambda:checks.append('membership') or {})
        monkeypatch.setattr(controller,'_validate_physical_distinctness',lambda:checks.append('distinctness'))
        archive,digest=controller._archive_identity_for_peer(peer)
        offer=box.publish_offer(target=w['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,archive=archive)
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        ca=w['authority'].seed_server.material.ca_certificate
        ack=app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=ca)
        result=controller.execute('prepare')
        assert checks==['membership','distinctness']
        assert result['actions'][0]['acknowledgement']==ack and result['route_ready'] is False
        box.request_stage_cleanup(offer)
        app.cleanup_offer(offer,acknowledged=True,_test_ca_certificate=ca)
        cleanup=controller.execute('cleanup')
        assert checks==['membership','distinctness','membership','distinctness']
        assert cleanup['route_ready'] is False


def test_dispatch_without_target_owner_adapter_still_refuses(tmp_path):
    with _world(tmp_path/'world') as w:
        controller,_=setup_controller(tmp_path,w)
        with pytest.raises(ControllerError,match='managed_artifact_lifecycle_requires_target_owner'):
            controller.execute('prepare')
