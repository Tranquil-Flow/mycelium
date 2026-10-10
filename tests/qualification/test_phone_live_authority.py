"""Three-node signed synthetic startup attestation; no hardware/inference claim."""
from dataclasses import replace
import copy

import pytest

from mycelium_qualification import issue_live_route_qualification, LiveRouteQualificationError
from mycelium_qualification.phone import phone_load_proof_digest
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_router.serialization import execution_graph_to_dict
from test_router_inprocess_mesh import three_device_graph

D = 'sha256:' + 'a'*64


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
            d.update(stage_id=s.stage_id,phone_stage_pack_digest=D,load_generation=1)
            configs[n]=dict(assignment_id=p.assignment_id,stage_id=s.stage_id,pack_digest=D,manifest_digest=D,load_generation=1)
        else:
            d.update(placement_id=p.placement_id,stage_pack_digest=D,stage_pack_verification_digest=D)
        o=add(n,'configured',d)
        if i==1:
            stages=list(graph.stages)
            stages[i]=replace(s,placements=(replace(p,load_proof_digest=phone_load_proof_digest(o)),))
            graph=replace(graph,stages=tuple(stages))
    for i,n in enumerate(nodes):
        peer=nodes[(i+1)%3]
        d=(dict(peer_node_id=peer,peer_node_ids=[x for x in nodes if x!=n],local_generation=1) if i==1
           else {'peer':dict(node_id=peer,endpoint_id=endpoints[peer])})
        add(n,'started',d)
        add(n,'snapshot',dict(runtime=dict(active_state_count=0,applied_operation_count=2,mode='complete_context_replay'),
            transport=dict(local_node_id=n,peer_node_id=peer,remote_frames_sent=2,remote_frames_received=2,route_ready=False),transport_fatal_error=None))
    add(nodes[0],'inference_started',dict(request_id='req',status='DECODING',output={'token_ids':[3]}))
    add(nodes[0],'inference_decoded',dict(request_id='req',status='COMPLETED',output={'token_ids':[3,4]}))
    att=dict(protocol='mycelium.live_route_attestation.v1',captured_at_unix_ms=1000,run_id='run',entry_node_id=nodes[0],
             request_id='req',prompt_token_ids=[1,2],max_new_tokens=2,execution_graph=execution_graph_to_dict(graph),
             output_token_ids=[3,4],signed_observations=observations,
             counters=dict(frames_sent=6,frames_received=6,applied_operation_count=6,fatal=None))
    n=nodes[1]
    return att,{n:dict(endpoint_id=endpoints[n],process_id='inc-phone',member_key_digest=signers[n].verification_key_digest,configure=configs[n])},signers


def qualify(att, bindings):
    return issue_live_route_qualification(att,expected_prompt_token_ids=[1,2],expected_output_token_ids=[3,4],phone_bindings=bindings)


def test_live_phone_qualification_with_real_signed_envelopes():
    att, bindings, _ = phone_attestation()
    record=qualify(att,bindings)
    assert record.route_ready
    assert record.stage_bindings[1].process_id == 'inc-phone'


@pytest.mark.parametrize('attack',['missing_authority','wrong_key','wrong_boot','pack','proof','stage','generation','missing_runtime','no_work','fatal','desktop_mode','wrong_peer'])
def test_live_phone_qualification_fails_closed(attack):
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
                if attack=='generation': d['load_generation']=2
                if attack=='desktop_mode': d['runtime_mode']='stage_local_kv'
            if o['event']=='started' and attack=='wrong_peer': d['peer_node_ids']=[]
            if o['event']=='snapshot':
                if attack=='missing_runtime': d.pop('runtime')
                if attack=='no_work': d['runtime']['applied_operation_count']=0
                if attack=='fatal': d['transport_fatal_error']='failed'
            e['signature']=signers[n].sign(o)
    with pytest.raises(LiveRouteQualificationError): qualify(att,bindings)
