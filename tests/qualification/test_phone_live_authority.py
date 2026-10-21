# SPDX-License-Identifier: AGPL-3.0-only
"""Three-node signed synthetic startup attestation; no hardware/inference claim.

The phone records here are signed by a test key in the native v2 shape. They
exercise the qualifier's own comparisons only. The fixture authority below is a
test stand-in for the seed validation (``current_load_authority``), which the
composition tests exercise for real; nothing here is evidence of a phone.
"""
from dataclasses import replace
import copy

import pytest

from mycelium_qualification import issue_live_route_qualification, LiveRouteQualificationError
from mycelium_qualification.evidence import sha256_document
from mycelium_qualification import phone as load


def legacy_phone_digest(o):
    """Test-only retired v1 identity; NEVER qualification authority."""
    return sha256_document({"protocol": "mycelium.phone_load_identity.v1",
        **{k: o[k] for k in ("node_id", "host_id", "process_id", "endpoint_id")},
        **{k: o["details"][k] for k in ("assignment_id", "stage_id", "manifest_digest",
            "phone_stage_pack_digest", "load_generation", "runtime_mode")}})
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.serialization import execution_graph_to_dict
from test_router_inprocess_mesh import three_device_graph

D = 'sha256:' + 'a'*64
BUILD = 'sha256:' + 'b'*64
GOLDEN = 'sha256:' + 'c'*64
LOAD = 4


def fixture_authority(observation, *, member_key_digest, **overrides):
    """Stand-in for the seed's validated result, for qualifier unit tests ONLY."""
    d = observation['details']
    fields = dict(node_id=observation['node_id'], endpoint_id=observation['endpoint_id'],
        process_id=observation['process_id'], member_key_digest=member_key_digest, membership_generation=1,
        seed_key_digest=D, assignment_id=d['assignment_id'], assignment_digest=D, stage_id=d['stage_id'],
        pack_digest=d['phone_stage_pack_digest'], manifest_digest=d['manifest_digest'],
        load_generation=d['load_generation'], runtime_backend=d['runtime_backend'],
        runtime_build_digest=d['runtime_build_digest'], golden_output_digest=d['golden_output_digest'],
        load_proof_digest=load.phone_load_proof_digest(observation), checked_at=0.0)
    fields.update(overrides)
    return load._seal_phone_load_authority(**fields)


def phone_attestation():
    graph = three_device_graph()
    stages = tuple(replace(s, placements=(replace(s.placements[0], runtime_backend=(
        'mobile-rust-qwen2' if i == 1 else 'numpy'), stage_signature=D, load_proof_digest=D),))
        for i,s in enumerate(graph.stages))
    pids = {s.placements[0].placement_id for s in stages}
    graph = replace(graph, stages=stages, manifest_digest=D,
                    edges=tuple(e for e in graph.edges if e.from_placement_id in pids and e.to_placement_id in pids),
                    loopback_edges=tuple(e for e in graph.loopback_edges if e.from_placement_id in pids and e.to_placement_id in pids))
    nodes = [s.placements[0].node_id for s in stages]
    endpoints = {n: ('%02x' % (i+1))*32 for i,n in enumerate(nodes)}
    signers = {n:generate_ed25519_signer(endpoint_id=endpoints[n]) for n in nodes}
    observations = []
    configs = {}
    phone_configured = None
    def obs(n,event,details):
        return dict(protocol='mycelium.physical_node_observation.v1', event=event, monotonic_ns=len(observations)+1,
                    run_id='run', deployment_id=graph.deployment_id, node_id=n, host_id=n,
                    process_id='inc-phone' if n==nodes[1] else 100+nodes.index(n), endpoint_id=endpoints[n],
                    peer_generation=1, state='RUNNING', route_ready=False, details=details)
    def add(n,event,details):
        o=obs(n,event,details)
        observations.append(dict(observation=o, signature=signers[n].sign(o), verification_key=signers[n].public_key_record()))
        return o
    for i,s in enumerate(stages):
        p=s.placements[0]; n=p.node_id
        d=dict(assignment_id=p.assignment_id, manifest_digest=D, endpoint_addr={'id':endpoints[n]},runtime_mode='complete_context_replay')
        if i==1:
            d.update(stage_id=s.stage_id,phone_stage_pack_digest=D,load_generation=LOAD,runtime_backend='mobile-rust-qwen2',
                runtime_build_digest=BUILD,golden_output_digest=GOLDEN,
                load_identity_provenance=copy.deepcopy(load.LOAD_IDENTITY_PROVENANCE_V2))
            configs[n]=dict(assignment_id=p.assignment_id,stage_id=s.stage_id,pack_digest=D,manifest_digest=D,load_generation=LOAD)
        else:
            d.update(placement_id=p.placement_id,stage_pack_digest=D,stage_pack_verification_digest=D)
        o=add(n,'configured',d)
        if i==1:
            phone_configured = copy.deepcopy(o)
            stages=list(graph.stages)
            stages[i]=replace(s,placements=(replace(p,load_proof_digest=load.phone_load_proof_digest(o)),))
            graph=replace(graph,stages=tuple(stages))
    for i,n in enumerate(nodes):
        peer=nodes[(i+1)%3]
        d=(dict(peer_node_id=peer,peer_node_ids=[x for x in nodes if x!=n],local_generation=1) if i==1
           else {'peer':dict(node_id=peer,endpoint_id=endpoints[peer])})
        add(n,'started',d)
        snapshot=dict(runtime=dict(active_state_count=0,applied_operation_count=2,mode='complete_context_replay'),
            transport=dict(local_node_id=n,peer_node_id=peer,remote_frames_sent=2,remote_frames_received=2,route_ready=False),transport_fatal_error=None)
        if i==1:
            c=configs[n]
            snapshot.update(load_identity=dict(assignment_id=c['assignment_id'],stage_id=c['stage_id'],
                phone_stage_pack_digest=c['pack_digest'],load_generation=LOAD),configured_load_generation=LOAD)
        add(n,'snapshot',snapshot)
    add(nodes[0],'inference_started',dict(request_id='req',status='DECODING',output={'token_ids':[3]}))
    add(nodes[0],'inference_decoded',dict(request_id='req',status='COMPLETED',output={'token_ids':[3,4]}))
    att=dict(protocol='mycelium.live_route_attestation.v1',captured_at_unix_ms=1000,run_id='run',entry_node_id=nodes[0],
             request_id='req',prompt_token_ids=[1,2],max_new_tokens=2,execution_graph=execution_graph_to_dict(graph),
             output_token_ids=[3,4],signed_observations=observations,
             counters=dict(frames_sent=6,frames_received=6,applied_operation_count=6,fatal=None))
    n=nodes[1]
    key=signers[n].verification_key_digest
    return att,{n:dict(endpoint_id=endpoints[n],process_id='inc-phone',member_key_digest=key,configure=configs[n],
                       load_authority=fixture_authority(phone_configured,member_key_digest=key))},signers


def qualify(att, bindings):
    return issue_live_route_qualification(att,expected_prompt_token_ids=[1,2],expected_output_token_ids=[3,4],phone_bindings=bindings)


def phone_records(att, node, event):
    return [e for e in att['signed_observations'] if e['observation']['node_id']==node and e['observation']['event']==event]


def test_live_phone_qualification_binds_the_v2_load_identity():
    att, bindings, _ = phone_attestation()
    n = next(iter(bindings))
    original = copy.deepcopy(att)
    record = qualify(att, bindings)
    assert record.route_ready is True
    configured = phone_records(att, n, 'configured')[0]['observation']
    digest = load.phone_load_proof_digest(configured)
    assert digest != legacy_phone_digest(configured)
    assert att['execution_graph']['stages'][1]['placements'][0]['load_proof_digest'] == digest
    assert att == original, 'qualification never rewrites a signed envelope'


@pytest.mark.parametrize('attack,code',[
    ('missing_authority','live_phone_authority_missing'),('wrong_key','live_identity_invalid'),
    ('wrong_boot','live_identity_invalid'),('pack','live_phone_load_authority_mismatch'),
    ('proof','live_phone_load_authority_mismatch'),('stage','live_phone_load_authority_mismatch'),
    ('generation','live_phone_load_authority_mismatch'),('build','live_phone_load_authority_mismatch'),
    ('golden','live_phone_load_authority_mismatch'),('manifest','live_phone_load_authority_mismatch'),
    ('missing_runtime','live_snapshot_invalid'),('no_work','live_snapshot_invalid'),('fatal','live_snapshot_invalid'),
    ('desktop_mode','live_phone_replay_required'),('wrong_peer','live_peer_binding_invalid')])
def test_live_phone_qualification_fails_closed(attack, code):
    # Every attack re-signs with the member's own key: the refusal is the
    # qualifier's comparison, not a broken signature.
    att,bindings,signers=phone_attestation()
    n=next(iter(bindings))
    if attack=='missing_authority': bindings={}
    elif attack=='wrong_key': bindings[n]['member_key_digest']='sha256:'+'0'*64
    elif attack=='wrong_boot': bindings[n]['process_id']='inc-stale'
    elif attack=='proof': att['execution_graph']['stages'][1]['placements'][0]['load_proof_digest']='sha256:'+'0'*64
    else:
        for e in att['signed_observations']:
            o=e['observation']; d=o['details']
            if o['node_id']!=n: continue
            if o['event']=='configured':
                if attack=='pack': d['phone_stage_pack_digest']='sha256:'+'0'*64
                if attack=='stage': d['stage_id']='wrong'
                if attack=='generation': d['load_generation']=LOAD+1
                if attack=='build': d['runtime_build_digest']='sha256:'+'0'*64
                if attack=='golden': d['golden_output_digest']='sha256:'+'0'*64
                if attack=='manifest': d['manifest_digest']='sha256:'+'0'*64
                if attack=='desktop_mode': d['runtime_mode']='stage_local_kv'
            if o['event']=='started' and attack=='wrong_peer': d['peer_node_ids']=[]
            if o['event']=='snapshot':
                if attack=='missing_runtime': d.pop('runtime')
                if attack=='no_work': d['runtime']['applied_operation_count']=0
                if attack=='fatal': d['transport_fatal_error']='failed'
            e['signature']=signers[n].sign(o)
    with pytest.raises(LiveRouteQualificationError) as refused: qualify(att,bindings)
    assert str(refused.value) == code
