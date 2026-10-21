# SPDX-License-Identifier: AGPL-3.0-only
"""Signed concurrent-claim transport only; no model/native execution in this file."""
import pytest
from test_managed_control import _world,_enqueue,_command,_native_response,RUN_ID,DEPLOYMENT_ID
from mycelium_a13_macos.managed_control import ManagedControlError,validate_native_command


def poll(world,sequence,active):
    return world['target_client'].poll(target=world['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
        sequence=sequence,nonce=f'concurrent-nonce-{sequence}',request_id=f'concurrent-poll-{sequence}',
        active_claims=active)


def test_control_is_delivered_while_data_command_is_claimed(tmp_path):
    with _world(tmp_path) as world:
        _enqueue(world,'data-one',operation='infer_decode',payload={'request_id':'request-one','count':1})
        first=poll(world,1,{})
        assert first['command_id']=='data-one'
        _enqueue(world,'data-two',sequence=2,operation='infer_decode',payload={'request_id':'request-two','count':1})
        _enqueue(world,'health',sequence=3,operation='health')
        second=poll(world,2,{'data-one':first['claim_token']})
        assert second['command_id']=='health'
        assert second['delivery_kind']=='command'
        assert world['mailbox'].command_receipt('data-one')['state']=='claimed'
        active={'data-one':first['claim_token'],'health':second['claim_token']}
        assert poll(world,3,active)['delivery_kind']=='empty' # work slot still occupied
        # Schema-valid simulated ack: this test does NOT claim native execution.
        world['target_client'].ack(target=world['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            command_id='data-one',claim_token=first['claim_token'],disposition='completed',
            native_response=_native_response('data-one',world['member']['node_id']),sequence=4,
            nonce='finish-data-nonce',request_id='finish-data-request')
        assert poll(world,5,{'health':second['claim_token']})['command_id']=='data-two'


def test_missing_active_claim_is_reconciliation_not_redelivery(tmp_path):
    with _world(tmp_path) as world:
        _enqueue(world,'first');first=poll(world,1,{})
        _enqueue(world,'next',sequence=2)
        result=poll(world,2,{}) # target did not retain its first executing claim
        assert result['delivery_kind']=='reconciliation_required'
        assert result['command_id']==first['command_id']
        assert result['command_envelope'] is None
        assert world['mailbox'].command_receipt('next')['state']=='pending'


def test_forged_active_claim_is_rejected_without_changing_real_claim(tmp_path):
    with _world(tmp_path) as world:
        _enqueue(world,'first');first=poll(world,1,{})
        with pytest.raises(ManagedControlError,match='active_claim_invalid'):
            poll(world,2,{'first':'wrong-claim'})
        assert world['mailbox'].command_receipt('first')['state']=='claimed'
        with pytest.raises(ManagedControlError,match='active_claim_invalid'):
            poll(world,3,{'foreign-command':first['claim_token']})


@pytest.mark.parametrize('operation,payload',[
    ('bind_request_control',{'request_id':'r','control':{}}),
    ('update_request_control',{'request_id':'r','control':{}}),
    ('infer_start',{'request':{}}),
    ('infer_decode',{'request_id':'r','count':1}),
    ('infer_cancel',{'request_id':'r'}),
    ('infer_cancel_wait',{k:1 for k in ('request_id','deployment_id','deployment_epoch',
        'qualification_digest','command_id','publisher_generation','absolute_deadline_ms',
        'request_attempt','path_id','path_attempt','path_digest','topology_generation',
        'cancellation_generation','deadline_budget_ms')}),
    ('rotate',{'peer':{}}),
])
def test_native_live_payload_shapes_are_closed_without_claiming_semantic_validity(operation,payload):
    frame=_command('shape',operation,payload)
    assert validate_native_command(frame)==frame
    with pytest.raises(ManagedControlError):
        validate_native_command({**frame,'payload':{**payload,'argv':['forbidden']}})
