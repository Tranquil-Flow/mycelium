# SPDX-License-Identifier: AGPL-3.0-only
"""Real signed TLS artifact delivery; no native/model launch or remote host."""
import hashlib
import pytest
from mycelium_a13_macos.managed_control import ManagedControlError
from test_control_pump import _world
from test_managed_control import RUN_ID, DEPLOYMENT_ID


def test_normal_authority_delivers_bounded_offer_over_existing_tls(tmp_path):
    from mycelium_a13_macos.managed_offers import ManagedOfferClient
    with _world(tmp_path/'world') as w:
        mailbox=w['authority']._control_mailbox
        client=ManagedOfferClient(w['target_client'])
        assert client.pending(w['member']) is None
        data=b'fixture-archive-bytes-'*8000
        offered=mailbox.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=data,ttl_seconds=60)
        assert client.pending(w['member'])==offered
        assert client.fetch(w['member'],offered,max_bytes=len(data))==data
        assert offered['archive_digest']=='sha256:'+hashlib.sha256(data).hexdigest()
        with pytest.raises(ManagedControlError,match='offer_budget_exceeded'):
            client.fetch(w['member'],offered,max_bytes=len(data)-1)
        # Publication/delivery is not execution, qualification, or route readiness.
        assert w['authority'].status()['route_ready'] is False


def test_offer_is_durable_and_member_fenced(tmp_path):
    from mycelium_a13_macos.managed_offers import ManagedOfferClient, ManagedProvisioningMailbox
    with _world(tmp_path/'world') as w:
        box=w['authority']._control_mailbox
        offer=box.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=b'bytes',ttl_seconds=60)
        # Re-open identical private state without changing authority/identity.
        reopened=ManagedProvisioningMailbox(box.database,coordinator=w['authority'].coordinator)
        try:
            assert reopened.pending_offer(w['member'])==offer
        finally:reopened.close()
        client=ManagedOfferClient(w['target_client'])
        altered={**w['member'],'generation':w['member']['generation']+1}
        with pytest.raises(ManagedControlError):client.pending(altered)
        with pytest.raises(ManagedControlError):client.fetch(w['member'],{**offer,'archive_digest':'sha256:'+'0'*64},max_bytes=10)


def test_other_signer_and_expired_offer_rejected(tmp_path):
    from mycelium_a13_macos.managed_offers import ManagedOfferClient
    with _world(tmp_path/'world') as w:
        box=w['authority']._control_mailbox
        offer=box.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=b'bytes',ttl_seconds=1)
        with pytest.raises(ManagedControlError):ManagedOfferClient(w['owner_client']).pending(w['member'])
        old=box._clock
        box._clock=lambda:old()+2
        try:
            assert box.pending_offer(w['member']) is None
        finally:box._clock=old




def test_target_application_fetches_and_stages_signed_offer(tmp_path):
    from mycelium_a13_macos.target_app import TargetApplication
    from test_managed_staging import bundle
    with _world(tmp_path/'world') as w:
        app=TargetApplication(w['target'].state_root)
        app.peer=w['target']
        archive,digest=bundle()
        offered=w['authority']._control_mailbox.publish_offer(target=w['member'],
            run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,archive=archive)
        ca=w['authority'].seed_server.material.ca_certificate
        pending=app.pending_stage(_test_ca_certificate=ca)
        assert pending==offered
        with pytest.raises(ValueError,match='stage_not_authorized'):
            app.stage_offer(offered,max_bytes=len(archive),max_disk_bytes=100_000,
                authorize=lambda intent:False,_test_ca_certificate=ca)
        result=app.stage_offer(offered,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=ca)
        assert result['archive_digest']==digest
        assert result['node_id']==w['member']['node_id']
        assert w['authority']._control_mailbox.stage_receipt(offered)==result
        assert app._runtime is None
        # Idempotent stage retry verifies same bytes and marker, never deletes it.
        assert app.stage_offer(offered,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=ca)==result




def test_transfer_rechecks_target_policy_before_every_chunk(tmp_path):
    from mycelium_a13_macos.managed_offers import ManagedOfferClient,CHUNK
    with _world(tmp_path/'world') as w:
        offer=w['authority']._control_mailbox.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=b'x'*(CHUNK+1))
        calls=[]
        def authorize():
            calls.append('check')
            return len(calls)==1
        with pytest.raises(ManagedControlError,match='offer_transfer_not_authorized'):
            ManagedOfferClient(w['target_client']).fetch(w['member'],offer,
                max_bytes=CHUNK+1,authorize=authorize)
        assert len(calls)==2




def test_target_staged_control_start_requires_consent_and_real_native_hello(native_sidecar_required, tmp_path):
    from mycelium_a13_macos.target_app import TargetApplication
    from test_managed_staging import bundle
    from test_control_session import owner,session
    from test_worker import POLICY,SAFE
    with _world(tmp_path/'world') as w:
        app=TargetApplication(w['target'].state_root);app.peer=w['target']
        data,digest=bundle()
        offer=w['authority']._control_mailbox.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=data)
        ca=w['authority'].seed_server.material.ca_certificate
        sender=owner(tmp_path/'sender',w)
        try:
            with pytest.raises(ValueError,match='control_consent_required'):
                app.start_control(offer,acknowledged=False,resource_policy=POLICY,max_disk_bytes=100_000,
                    _test_observer=lambda:dict(SAFE),_test_ca_certificate=ca)
            result=app.start_control(offer,acknowledged=True,resource_policy=POLICY,max_disk_bytes=100_000,
                _test_observer=lambda:dict(SAFE),_test_ca_certificate=ca)
            assert result['state']=='running' and result['model_load_authorized'] is False
            assert result['work_admitted'] is False  # bootstrap control: no load authorizer
            native=session(sender,w)
            response=native.send(command_id='offer-native-hello',command='hello',payload={})
            assert response['ok'] is True and response['route_ready'] is False
            native.close()
        finally:
            managed=app._runtime
            app.stop();sender.close()
        assert managed is not None and not managed.worker.process_group_present()


def test_offer_storage_cap_precedes_insert(tmp_path,monkeypatch):
    import mycelium_a13_macos.managed_offers as offers
    with _world(tmp_path/'world') as w:
        monkeypatch.setattr(offers,'MAX_TOTAL',4)
        with pytest.raises(ManagedControlError,match='offer_capacity_exhausted'):
            w['authority']._control_mailbox.publish_offer(target=w['member'],run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,archive=b'12345')
        assert offers.ManagedOfferClient(w['target_client']).pending(w['member']) is None


def test_live_offer_cannot_be_replaced_and_total_storage_is_bounded(tmp_path):
    with _world(tmp_path/'world') as w:
        box=w['authority']._control_mailbox
        box.publish_offer(target=w['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            archive=b'one',ttl_seconds=60)
        with pytest.raises(ManagedControlError,match='offer_already_pending'):
            box.publish_offer(target=w['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
                archive=b'two',ttl_seconds=60)
        with pytest.raises(ManagedControlError,match='offer_archive_invalid'):
            box.publish_offer(target=w['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
                archive=b'',ttl_seconds=60)
