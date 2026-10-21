# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic seed state and signed replies; no device/build-provenance claim."""
import copy
import json
import time

import pytest

from mycelium_seed import route_assignment as ra
from mycelium_seed.phone_preparation import issue_builder_authorization, main
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_invite import verify_invite_bundle
from mycelium_membership import ROUTER_ACTIVATION_PROTOCOL
from mycelium_mobile.qwen_stage_manifest import sign_stage_manifest
from mycelium_qualification.evidence import canonical_json_bytes
from scripts import qwen_phone_builder as phone
from test_route_assignment import Swarm
from test_qwen_phone_builder import evidence, synthetic_snapshot


@pytest.fixture
def preparation(tmp_path,evidence):
    swarm=Swarm(tmp_path);swarm.clock[0]=time.time()
    node=NodeMembershipSession(node_id='node-1',swarm_id='swarm-a',seed_node_id='seed-node',
        signer=load_or_create_node_signer(tmp_path/'member.key'),incarnation='inc-phone',software_version='fixture',
        peer_class='android_native_qwen2_iroh',runtime_capability=dict(runtime_backend=phone.BACKEND,
            transport='iroh',activation_protocol=ROUTER_ACTIVATION_PROTOCOL),clock=lambda:swarm.clock[0])
    bundle=swarm.coordinator.mint_invite(nonce='q2',ttl_seconds=120)
    verified=verify_invite_bundle(bundle,now=swarm.clock[0])
    acceptance=swarm.coordinator.accept_join(invite_token=bundle['token'],join_envelope=node.join_request(
        invite_nonce=verified['payload']['nonce'],endpoint_addrs=['https://127.0.0.1:9443/control']))
    node.accept_join(acceptance,seed_key_digest=verified['seed_key_digest']);swarm.nodes['node-1']=node
    member=swarm.coordinator.member('node-1');doc=copy.deepcopy(evidence[0])
    doc.pop('authorization')
    doc['manifest']=sign_stage_manifest(doc['manifest']['message'],swarm.coordinator.signer)
    for field in ('configured','snapshot'):
        result=doc[field]['result'];obs=result['observation']
        obs.update(process_id='inc-phone',endpoint_id=member['endpoint_id'])
        if field=='configured': obs['details']['endpoint_addr']={'id':member['endpoint_id']}
        result.update(signature=node.signer.sign(obs),verification_key=node.signer.public_key_record())
    record=ra.issue_route_assignment(swarm.database,node_id='node-1',peer_endpoint_id='ef'*32,route_generation=17,
        now=lambda:swarm.clock[0],preparation_assignment=evidence[0]['authorization']['message']['preparation_binding']['assignment'],
        stage_pack=doc['manifest'],controller_endpoint_ids=['cd'*32],
        seed_key_record=swarm.coordinator.signer.public_key_record(),artifact_sources=[dict(endpoint_id='ef'*32,
            verification_key_digest=node.signer.verification_key_digest)])
    swarm.service().answer(swarm.request('node-1',applied=record['assignment_id']))
    request=dict(document=doc,node=dict(node_id='node-1',endpoint_id=member['endpoint_id'],membership_generation=member['generation'],
        staging_root='/tmp/mycelium/phone'),run_id='synthetic-run',deployment_id='11111111-1111-4111-8111-111111111111',controller_endpoint_id='cd'*32)
    kwargs=dict(seed_key_digest=swarm.coordinator.signer.verification_key_digest,
        approved_build_receipt_digest=phone.digest(canonical_json_bytes(doc['build_receipt'])),**request)
    return swarm,kwargs


def test_seed_authorizes_only_current_applied_q2(preparation,tmp_path):
    swarm,kw=preparation
    envelope=issue_builder_authorization(swarm.database.parent,clock=lambda:swarm.clock[0],**kw)
    e=phone.PhoneEvidence(dict(kw['document'],authorization=envelope),seed_key_digest=kw['seed_key_digest'],
                         node=kw['node'],run_id=kw['run_id'],deployment_id=kw['deployment_id'],now=swarm.clock[0])
    assert e.statement['controller_endpoint_id']=='cd'*32
    assert ra.newest_android_member(swarm.database,now=lambda:swarm.clock[0])=='node-1'
    request=tmp_path/'request.json';request.write_text(json.dumps({k:v for k,v in kw.items() if k not in
        {'seed_key_digest','approved_build_receipt_digest'}}))
    output=tmp_path/'authorization.json'
    assert main(['--state-root',str(swarm.database.parent),'--seed-key-digest',kw['seed_key_digest'],
        '--approved-build-receipt-digest',kw['approved_build_receipt_digest'],'--request',str(request),'--output',str(output)])==0
    assert json.loads(output.read_text())['message']['configure']==e.profile
    untrusted=json.loads(request.read_text());untrusted['now']=swarm.clock[0]-1000
    request.write_text(json.dumps(untrusted))
    with pytest.raises(Exception,match='phone_preparation_request_invalid'):
        main(['--state-root',str(swarm.database.parent),'--seed-key-digest',kw['seed_key_digest'],
            '--approved-build-receipt-digest',kw['approved_build_receipt_digest'],'--request',str(request),'--output',str(output)])


@pytest.mark.parametrize('attack,code',[
    ('revoked','seed_member_revoked'),('generation','phone_authority_identity_mismatch'),
    ('lease','seed_member_lease_expired'),('heartbeat','seed_member_liveness_stale'),
    ('unapplied','phone_applied_assignment_required'),('stale_apply','phone_applied_assignment_required'),
    ('withdrawn','phone_applied_assignment_required'),('controller','phone_controller_not_authorized'),
    ('receipt','phone_build_receipt_not_approved'),('signature','observation_signature_invalid'),
    ('manifest','phone_applied_manifest_mismatch'),('seed','phone_authority_key_mismatch')])
def test_seed_preparation_refuses_substitution(preparation,attack,code):
    swarm,kw=preparation
    sql={
        'revoked':"UPDATE seed_members SET lifecycle_state='STOPPED'",
        'generation':'UPDATE seed_members SET generation=generation+1',
        'lease':'UPDATE seed_members SET lease_expires_at=0',
        'heartbeat':'UPDATE seed_members SET next_heartbeat_due_at=0',
        'unapplied':'UPDATE seed_route_assignments SET applied_at=NULL',
        'stale_apply':'UPDATE seed_route_assignments SET applied_at=0',
        'withdrawn':'UPDATE seed_route_assignments SET withdrawn=1',
        'controller':"UPDATE seed_route_assignments SET controller_endpoint_ids_json='[]'"}
    if attack in sql:
        with ra._connect(swarm.database) as c:c.execute(sql[attack])
    elif attack=='receipt':kw['approved_build_receipt_digest']='sha256:'+'0'*64
    elif attack=='signature':kw['document']['configured']['result']['observation']['monotonic_ns']+=1
    elif attack=='manifest':kw['document']['manifest']['message']['stage_id']='wrong'
    elif attack=='seed':kw['seed_key_digest']='sha256:'+'0'*64
    with pytest.raises(Exception,match=code):
        issue_builder_authorization(swarm.database.parent,clock=lambda:swarm.clock[0],**kw)
