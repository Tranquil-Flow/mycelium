# SPDX-License-Identifier: AGPL-3.0-only
"""Encrypted join -> target-owned managed client; real TLS, no model work."""
import copy
import pytest
from test_control_pump import _world
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from mycelium_a13_macos.peer import RemoteMacMemberError


def test_joined_target_constructs_client_from_retained_identity_and_seed_pin(tmp_path):
    with _world(tmp_path) as world:
        target=world['target']
        snapshot=target.managed_control_snapshot()
        expected=world['authority'].coordinator.member(snapshot['node_id'])
        for key in ('node_id','endpoint_id','verification_key_digest','generation','incarnation','lease_expires_at'):
            assert snapshot[key]==expected[key]
        client=target.managed_control_client(
            _test_ca_certificate=world['authority'].seed_server.material.ca_certificate)
        assert client.base_url==world['authority'].seed_server.base_url
        assert client.signer.verification_key_digest==expected['verification_key_digest']
        result=client.poll(target=snapshot,run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            sequence=1,nonce='managed-peer-poll',request_id='managed-peer-poll')
        assert result['delivery_kind']=='empty'
        assert 'signer' not in snapshot and 'seed_key_records' not in snapshot
        snapshot['generation']+=1
        assert target.managed_control_snapshot()['generation']==expected['generation']


@pytest.mark.parametrize('state',['stopped','expired','revoked'])
def test_target_control_binding_fails_closed_after_lifecycle_change(tmp_path,monkeypatch,state):
    with _world(tmp_path) as world:
        target=world['target']
        original=target.managed_control_snapshot()
        if state=='stopped': target.stop()
        elif state=='expired': monkeypatch.setattr(target,'_clock_source',lambda:original['lease_expires_at']+1)
        else: target._record_disposition('revoked')
        with pytest.raises(RemoteMacMemberError): target.managed_control_snapshot()
        with pytest.raises(RemoteMacMemberError): target.managed_control_client()


def test_target_client_does_not_mutate_global_tls_trust(tmp_path):
    with _world(tmp_path) as world:
        client=world['target'].managed_control_client()
        # The production constructor retains Web PKI and cannot use the fixture
        # CA merely because membership joined through an explicit test client.
        from mycelium_a13_macos.managed_control import ManagedControlError
        with pytest.raises(ManagedControlError):
            client.poll(target=world['target'].managed_control_snapshot(),run_id=RUN_ID,
                deployment_id=DEPLOYMENT_ID,sequence=1,nonce='untrusted-ca',request_id='untrusted-ca')
