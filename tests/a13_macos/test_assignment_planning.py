# SPDX-License-Identifier: AGPL-3.0-only
"""Real local TLS membership + canonical metadata compiler; no model execution."""
from dataclasses import replace
import copy
import json
from pathlib import Path

import pytest

import model_manifest as mm
from layer_assignment import validate_assignment_identity
from mycelium_a13_macos.authority import DemoAuthorityError
from mycelium_a13_macos.serving import CANDIDATE_MODEL, PreparedSource
from tests.a13_macos.test_peer import seed, _member, _enroll, CONSENT, QUOTA


@pytest.fixture
def enrolled(tmp_path, seed):
    owner, factory = seed
    request=owner.pair_request()
    owner.accept_pairing(owner.approve_pairing(request,ttl_seconds=120,quota=QUOTA))
    peer=_member(tmp_path/'peer',factory)
    peer.start()
    try:
        _enroll(owner,peer)
        nodes=(owner.member.node_id, next(m['node_id'] for m in owner.coordinator.members()
            if m['node_id'] != owner.member.node_id))
        # Synthetic Qwen metadata through the real compiler; no weights loaded.
        weights={name:'toy.safetensors' for name in (
            'model.embed_tokens.weight','model.norm.weight','lm_head.weight',
            *(f'model.layers.{i}.self_attn.q_proj.weight' for i in range(3)))}
        model=CANDIDATE_MODEL
        manifest=mm.compile_model_manifest(
            model_id=model.model_id,requested_revision=model.resolved_commit,
            resolved_commit=model.resolved_commit,
            config={'model_type':'qwen2','architectures':['Qwen2ForCausalLM'],
                'num_hidden_layers':3,'hidden_size':4,'num_attention_heads':1,
                'num_key_value_heads':1,'head_dim':4,'intermediate_size':8,
                'vocab_size':8,'max_position_embeddings':16,'rms_norm_eps':1e-6,
                'rope_theta':10000.0,'hidden_act':'silu','tie_word_embeddings':False},
            checkpoint_index={'metadata':{'total_size':128},'weight_map':weights},
            file_metadata={'toy.safetensors':{'size_bytes':128,'sha256':'1'*64}},
        )
        prepared=PreparedSource(model=model,source_root=tmp_path/'unused-source',source_bytes=0,
            manifest=manifest,manifest_digest=mm.manifest_digest_ref(manifest))
        yield owner,peer,nodes,prepared,{node:str(tmp_path/f'cache-{index}') for index,node in enumerate(nodes)}
    finally:
        peer.stop()


def test_owner_compiles_fresh_assignments_for_enrolled_identities(enrolled):
    owner,peer,nodes,prepared,roots=enrolled
    result=owner.plan_assignments(prepared=prepared,node_ids=nodes,split_layer=2,cache_roots=roots)
    path=Path(result['plan_path'])
    assert path.is_relative_to(owner.state_root/'preparations')
    assert path.stat().st_mode & 0o777 == 0o600
    plan=json.loads(path.read_text())
    assert plan['protocol']=='mycelium.a13_assignment_preparation.v1'
    assert plan['state']=='assignments_compiled_not_provisioned'
    assert plan['route_ready'] is False
    assert plan['swarm_id']==owner.coordinator.swarm_id
    assert plan['seed_key_digest']==owner.coordinator.signer.verification_key_digest
    assert [a['node_id'] for a in plan['assignments']]==list(nodes)
    assert [(a['range']['start_layer'],a['range']['end_layer_exclusive']) for a in plan['assignments']]==[(0,2),(2,3)]
    assert plan['assignments'][0]['components']==['input_embedding','decoder']
    assert plan['assignments'][1]['components']==['decoder','final_norm','lm_head']
    current={m['node_id']:m for m in owner.coordinator.members()}
    for binding in plan['members']:
        assert binding['generation']==current[binding['node_id']]['generation']
        assert binding['endpoint_id']==current[binding['node_id']]['endpoint_id']
    for assignment in plan['assignments']:
        validate_assignment_identity(assignment)
        assert assignment['runtime']['dtype']==prepared.model.runtime_dtype
        assert assignment['runtime']['quantization']==prepared.model.runtime_quantization
        assert assignment['manifest_digest']==prepared.manifest_digest
    assert not any(Path(root).exists() for root in roots.values())
    assert owner.status()['route_ready'] is False
    again=owner.plan_assignments(prepared=prepared,node_ids=nodes,split_layer=2,cache_roots=roots)
    other=json.loads(Path(again['plan_path']).read_text())
    assert plan['deployment_id'] != other['deployment_id']
    assert plan['assignments'][0]['assignment_id'] != other['assignments'][0]['assignment_id']
    assert json.loads(path.read_text())==plan


@pytest.mark.parametrize('mutation',['duplicate','unknown','missing_cache','split_zero','split_end','split_bool','manifest_tamper','manifest_pin','revoked','expired','stopped'])
def test_invalid_planning_creates_no_preparation(enrolled,mutation,monkeypatch):
    owner,peer,nodes,prepared,roots=enrolled
    split=2
    if mutation=='duplicate': nodes=(nodes[0],nodes[0])
    elif mutation=='unknown': nodes=(nodes[0],'not-enrolled')
    elif mutation=='missing_cache': roots={nodes[0]:roots[nodes[0]]}
    elif mutation=='split_zero': split=0
    elif mutation=='split_end': split=3
    elif mutation=='split_bool': split=True
    elif mutation=='manifest_tamper':
        manifest=copy.deepcopy(prepared.manifest);manifest['num_layers']=4
        prepared=replace(prepared,manifest=manifest)
    elif mutation=='manifest_pin': prepared=replace(prepared,manifest_digest='sha256:'+'0'*64)
    elif mutation=='revoked':
        member=next(m for m in owner.coordinator.members() if m['node_id']==nodes[1])
        owner.revoke(member['node_id'],member['generation'])
    elif mutation=='expired':
        expired=max(m['lease_expires_at'] for m in owner.coordinator.members())+1
        monkeypatch.setattr(owner,'_clock',lambda:expired)
    elif mutation=='stopped': owner.stop()
    with pytest.raises(DemoAuthorityError):
        owner.plan_assignments(prepared=prepared,node_ids=nodes,split_layer=split,cache_roots=roots)
    assert not (owner.state_root/'preparations').exists()
