"""Fixture utilities: tiny tensors, synthetic signers and receipt, NOT an APK."""
import base64
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import numpy as np
import struct


def load(raw):
    length=struct.unpack('<Q',raw[:8])[0]
    header=json.loads(raw[8:8+length]); payload=raw[8+length:]
    return {name:np.frombuffer(payload[item['data_offsets'][0]:item['data_offsets'][1]],dtype='<u2').astype('<u4').__lshift__(16).view('<f4').reshape(item['shape']).copy()
            for name,item in header.items() if name != '__metadata__'}


def save_file(tensors,path):
    header={};payload=bytearray()
    for name,value in sorted(tensors.items()):
        start=len(payload);payload.extend(np.asarray(value,dtype='<f4').tobytes())
        header[name]=dict(dtype='F32',shape=list(value.shape),data_offsets=[start,len(payload)])
    encoded=json.dumps(header,separators=(',',':')).encode();encoded+=b' '*((-len(encoded))%8)
    Path(path).write_bytes(struct.pack('<Q',len(encoded))+encoded+payload)

from tokenizers import Tokenizer, models

from scripts import build_qwen_live_route as builder
from scripts import qwen_phone_builder as phone
from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_membership import ROUTER_ACTIVATION_PROTOCOL
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_seed import SeedCoordinator, SqliteSeedState
from mycelium_seed import route_assignment as ra
from mycelium_seed.phone_preparation import issue_builder_authorization
from mycelium_mobile.qwen_stage_manifest import build_stage_manifest_from_safetensors, sign_stage_manifest
from mycelium_qualification.evidence import canonical_json_bytes
from test_qwen_phone_builder import BINARY, ROOT, topology
from test_qwen_route_builder import _template, _write_topology


def fixture_build(tmp_path, *, native=None, observe=None, staged_local=False):
    assert BINARY.is_file(), 'Required previously-built real native fixture executor missing'
    fixture=json.loads((ROOT/'native/mobile_core/tests/fixtures/qwen2_synthetic.json').read_text())
    weights=load(base64.b64decode(fixture['safetensors_b64']))
    tensors={}
    for name,value in weights.items():
        for offset in (0,2,4):
            parts=name.split('.')
            parts[2]=str(int(parts[2])+offset)
            tensors['.'.join(parts)]=value.copy()
    config=dict(fixture['config'],model_type='qwen2',num_hidden_layers=6,vocab_size=32,tie_word_embeddings=False,
                hidden_act='silu',max_position_embeddings=1024,architectures=['Qwen2ForCausalLM'])
    hidden=config['hidden_size']
    rng=np.random.default_rng(42)
    tensors.update({'model.embed_tokens.weight':rng.normal(size=(32,hidden)).astype('float32'),
                    'model.norm.weight':np.ones(hidden,dtype='float32'),
                    'lm_head.weight':rng.normal(size=(32,hidden)).astype('float32')})
    model=tmp_path/'model';model.mkdir()
    (model/'config.json').write_text(json.dumps(config))
    save_file(tensors,str(model/'model.safetensors'))
    tokenizer=Tokenizer(models.WordLevel(vocab={'fixture':0,'[UNK]':1},unk_token='[UNK]'))
    tokenizer.save(str(model/'tokenizer.json'))
    # A minimal clean source tree exercises receipt validation, but is expressly
    # NOT source provenance for the executor and these bytes are NOT an APK.
    source=Path(native['source']) if native else tmp_path/'synthetic-source'
    if not native:
        (source/'native/mobile_core').mkdir(parents=True)
        (source/'native/mobile_core/Cargo.toml').write_text('[package]\nname="synthetic-fixture"\nversion="0.1.0"\n')
        for args in [('init',),('add','native/mobile_core/Cargo.toml'),
                     ('-c','user.name=Tranquil-Flow','-c','user.email=tranquil_flow@protonmail.com',
                      '-c','core.hooksPath=/dev/null','commit','-m','test: synthetic source receipt fixture')]:
            subprocess.run(['git','-C',str(source),*args],check=True,capture_output=True)
    commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    apk=tmp_path/'NOT-AN-APK.fixture';apk.write_bytes(b'SYNTHETIC RECEIPT FIXTURE; NOT AN APK')
    receipt=dict(protocol=phone.BUILD_PROTOCOL,source_commit=commit,mobile_core_version='0.1.0',
        runtime_build_digest=phone.digest(canonical_json_bytes(dict(protocol=phone.BUILD_ID_PROTOCOL,
                                  source_commit=commit,mobile_core_version='0.1.0'))),
        executor_digest=phone.file_digest(BINARY),apk_digest=phone.file_digest(apk))
    now=time.time()
    seed_root=tmp_path/'seed';seed_root.mkdir(mode=0o700)
    database=seed_root/'state.sqlite3'
    seed=SeedCoordinator(swarm_id='synthetic-swarm',seed_node_id='seed',seed_url='http://127.0.0.1:8788',
        signer=load_or_create_node_signer(seed_root/'identity/seed.key'),invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),incarnation='seed-fixture',lease_seconds=300)
    nodes=topology(); sessions={}
    if staged_local: nodes[0]['staging_root']=str(tmp_path/'full-build/transfer-bundle')
    for i,node in enumerate(nodes):
        is_phone=i==1
        node['process_transport']='external_iroh' if is_phone else 'local' if i==0 else 'ssh'
        node['ssh_target']=None if is_phone else 'operator@synthetic-no-network';node['ssh_identity_file']=None
        from mycelium_qualification.signing import generate_ed25519_signer
        signer=generate_ed25519_signer(endpoint_id=('%02x' % (i+1))*32)
        if is_phone and native:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from mycelium_qualification.signing import Ed25519EvidenceSigner
            private=Ed25519PrivateKey.from_private_bytes(bytes([114])*32)
            signer=Ed25519EvidenceSigner(endpoint_id=native['endpoint_id'],_private_key=private,
                _public_key_bytes=private.public_key().public_bytes_raw())
        session=NodeMembershipSession(node_id=node['node_id'],swarm_id='synthetic-swarm',seed_node_id='seed',
            signer=signer,incarnation=f'inc-fixture-{i}',software_version='fixture',
            peer_class='android_native_qwen2_iroh' if is_phone else 'linux_native_iroh',
            runtime_capability=dict(runtime_backend=phone.BACKEND if is_phone else 'numpy',transport='iroh',
                                    activation_protocol=ROUTER_ACTIVATION_PROTOCOL))
        bundle=seed.mint_invite(nonce=f'fixture-{i}',ttl_seconds=120)
        verified=verify_invite_bundle(bundle,now=now)
        request=session.join_request(invite_nonce=verified['payload']['nonce'],endpoint_addrs=['https://127.0.0.1:9443/control'])
        accepted=seed.accept_join(invite_token=bundle['token'],join_envelope=request)
        session.accept_join(accepted,seed_key_digest=verified['seed_key_digest'])
        member=seed.member(node['node_id']);sessions[node['node_id']]=session
        node.update(endpoint_id=member['endpoint_id'],membership_generation=member['generation'])
    bootstrap=tmp_path/'bootstrap';bootstrap.write_bytes(b'x'*32);bootstrap.chmod(0o600)
    descriptor=dict(socket_path=str(tmp_path/'control.sock'),bootstrap_secret_file=str(bootstrap),controller_endpoint_id=native['controller'] if native else 'cd'*32)
    template=_template();template['controller']['external_control']=descriptor
    template['protocol']='mycelium.physical_runner_operator_plan.v1'
    template['paths']={k:str(tmp_path/'operator'/k) for k in ('evidence_output_dir','lock_path','state_path','log_path')}
    (tmp_path/'operator').mkdir(mode=0o700)
    template['verification_keys']={'gossip':[seed.signer.public_key_record()], 'load_proof':[sessions['node-1'].signer.public_key_record()]}
    template['controller']['mode']='physical'
    keyfile=tmp_path/'ssh-fixture.key';keyfile.write_bytes(b'NOT A REAL SSH KEY');keyfile.chmod(0o600)
    nodes[2]['ssh_identity_file']=str(keyfile)
    template['controller']['transfer_manifest']={'files':[]}
    template_path=tmp_path/'template.json';template_path.write_text(json.dumps(template))
    args=SimpleNamespace(template_plan=template_path,topology=_write_topology(tmp_path/'topology.json',nodes),
        output_root=tmp_path/'preparation',model_root=model,route_label='fixture',model_id='Qwen/fixture',
        resolved_commit='a'*40,phone_prepare_only=True)
    prepared=builder.build(args)
    assignment=prepared['assignments'][1]
    golden=json.loads((ROOT/'native/mobile_core/tests/fixtures/qwen2_stage_manifest_v2.json').read_text())['manifest']['golden']
    manifest=build_stage_manifest_from_safetensors(model/'model.safetensors',config,stage_id='stage-001',model_id='Qwen/fixture',
        resolved_commit='a'*40,start_layer=2,end_layer_exclusive=4,golden=golden)
    signed_manifest=sign_stage_manifest(manifest,seed.signer)
    record=ra.issue_route_assignment(database,node_id='node-1',stage_pack=signed_manifest,peer_endpoint_id=nodes[2]['endpoint_id'],
        preparation_assignment=assignment,route_generation=1,controller_endpoint_ids=[descriptor['controller_endpoint_id']],seed_key_record=seed.signer.public_key_record(),
        artifact_sources=[dict(endpoint_id=nodes[0]['endpoint_id'],verification_key_digest=sessions['node-0'].signer.verification_key_digest)])
    member=seed.member('node-1');signer=sessions['node-1'].signer
    msg=dict(protocol=ra.ROUTE_ASSIGNMENT_REQUEST_PROTOCOL,message_id='applied-fixture',swarm_id=seed.swarm_id,
        sender_node_id='node-1',sender_endpoint_id=member['endpoint_id'],recipient_node_id='seed',incarnation=member['incarnation'],
        generation=member['generation'],issued_at=now,expires_at=now+60,applied_assignment_id=record['assignment_id'])
    ra.service_for(seed).answer(dict(protocol=ra.SIGNED_MESSAGE_PROTOCOL,message=msg,signature=signer.sign(msg),verification_key=signer.public_key_record()))
    profile=dict(assignment_id=assignment['assignment_id'],stage_id='stage-001',manifest_digest=assignment['manifest_digest'],
                 pack_digest=manifest['pack_digest'],load_generation=1)
    def response(event,details,mono):
        obs=dict(protocol='mycelium.physical_node_observation.v1',event=event,monotonic_ns=mono,run_id='synthetic-run',
            deployment_id=prepared['deployment_id'],node_id='node-1',host_id='node-1',process_id=member['incarnation'],
            endpoint_id=member['endpoint_id'],peer_generation=1,state='CONFIGURED',route_ready=False,details=details)
        return dict(ok=True,result=dict(observation=obs,signature=signer.sign(obs),verification_key=signer.public_key_record()))
    details={k:v for k,v in profile.items() if k!='pack_digest'}
    details.update(phone_stage_pack_digest=profile['pack_digest'],endpoint_addr={'id':member['endpoint_id']},
        runtime_mode='complete_context_replay',runtime_backend=phone.BACKEND,runtime_build_digest=receipt['runtime_build_digest'],
        golden_output_digest=golden['output_digest'],load_identity_provenance=phone.LOAD_IDENTITY_PROVENANCE)
    doc=dict(manifest=signed_manifest,build_receipt=receipt)
    if observe is None:
        doc.update(configured=response('configured',details,1),
            snapshot=response('snapshot',{'host_resources':{'supported_decode_modes':['complete_context_replay']}},2))
    if observe is not None:
        # The callback returns untouched signed native responses. No observation
        # field, signature, generation or ID is rewritten by this fixture.
        doc['configured'],doc['snapshot']=observe(dict(assignment=assignment,manifest=signed_manifest,
            model=str(model),member=member,record=record,profile=profile,deployment_id=prepared['deployment_id']))
    request=dict(document=doc,node=nodes[1],run_id='synthetic-run',deployment_id=prepared['deployment_id'],controller_endpoint_id=descriptor['controller_endpoint_id'])
    approved=phone.digest(canonical_json_bytes(receipt))
    doc['authorization']=issue_builder_authorization(seed_root,seed_key_digest=seed.signer.verification_key_digest,
                    approved_build_receipt_digest=approved,**request)
    evidence_path=tmp_path/'evidence.json'
    evidence_path.write_text(json.dumps(dict(protocol='mycelium.phone_builder_evidence.v1',run_id='synthetic-run',nodes={'node-1':doc})))
    args.phone_prepare_only=False;args.output_root=tmp_path/'full-build'
    args.phone_evidence=evidence_path;args.phone_seed_key_digest=seed.signer.verification_key_digest
    args.phone_executor=BINARY;args.phone_apk=apk;args.phone_source_root=source;args.phone_seed_state_root=seed_root
    return SimpleNamespace(args=args,seed=seed,database=database,request=request,approved=approved,document=doc,
                           nodes=nodes,sessions=sessions,preparation=prepared,descriptor=descriptor)


def build_packaging_fixture(args, monkeypatch):
    """B2-only harness: construct a legacy bundle despite blocked admission.

    Never use this for a positive phone qualification claim. The unmodified
    production build is separately required to refuse; all source/identity/
    runtime extraction assertions still run on the emitted desktop artifact.
    """
    from mycelium_qualification import phone as provenance
    with monkeypatch.context() as m:
        m.setattr(provenance, "require_phone_load_provenance", lambda observation: None)
        m.setattr(phone, "require_phone_load_provenance", lambda observation: None)
        return builder.build(args)


def qualify_fixture_plan(fixture, plan, checkpoint):
    """Exercise real qualifier on emitted graph and SYNTHETIC signed events.

    Output tokens are the measured local challenge. Transport/start/snapshot
    events below are explicitly synthetic, NOT native/device execution evidence.
    """
    from mycelium_qualification import issue_live_route_qualification
    from physical_inference_node import execution_graph_from_document
    from mycelium_router.serialization import execution_graph_to_dict
    run=plan['controller']['run_plan']
    graph=execution_graph_to_dict(execution_graph_from_document(run['nodes'][0]['configure']['graph']))
    endpoints={n['node_id']:n['endpoint_id'] for n in fixture.nodes}
    nodes=list(endpoints); observations=[]
    def add(node,event,details):
        signer=fixture.sessions[node].signer
        obs=dict(protocol='mycelium.physical_node_observation.v1',event=event,monotonic_ns=len(observations)+10,
            run_id=run['run_id'],deployment_id=run['deployment_id'],node_id=node,host_id=node,
            process_id=fixture.document['authorization']['message']['incarnation'] if node=='node-1' else 100+nodes.index(node),
            endpoint_id=endpoints[node],peer_generation=1,state='RUNNING',route_ready=False,details=details)
        observations.append(dict(observation=obs,signature=signer.sign(obs),verification_key=signer.public_key_record()))
    for i,node in enumerate(nodes):
        p=graph['stages'][i]['placements'][0]
        if node=='node-1':
            observations.append(fixture.document['configured']['result'])
        else:
            pack=json.loads((fixture.args.output_root/f'transfer-bundle/control/node-{i}-stage-pack.json').read_text())
            proof=json.loads((fixture.args.output_root/f'transfer-bundle/control/node-{i}-load-proof.json').read_text())
            add(node,'configured',dict(assignment_id=p['assignment_id'],placement_id=p['placement_id'],
                manifest_digest=graph['manifest_digest'],endpoint_addr={'id':endpoints[node]},runtime_mode='complete_context_replay',
                stage_pack_digest=pack['stage_pack_digest'],stage_pack_verification_digest=proof['stage_pack_verification_digest']))
        peer=nodes[(i+1)%len(nodes)]
        add(node,'started',dict(peer_node_id=peer,peer_node_ids=[n for n in nodes if n!=node],local_generation=1)
            if node=='node-1' else dict(peer=dict(node_id=peer,endpoint_id=endpoints[peer])))
        add(node,'snapshot',dict(runtime=dict(active_state_count=0,applied_operation_count=4,mode='complete_context_replay'),
            transport=dict(local_node_id=node,peer_node_id=peer,remote_frames_sent=4,remote_frames_received=4,route_ready=False),
            transport_fatal_error=None))
    output=checkpoint['challenge_output_token_ids'];prompt=checkpoint['challenge_prompt_token_ids']
    add(nodes[0],'inference_started',dict(request_id='fixture-request',status='DECODING',output={'token_ids':output[:1]}))
    add(nodes[0],'inference_decoded',dict(request_id='fixture-request',status='COMPLETED',output={'token_ids':output}))
    att=dict(protocol='mycelium.live_route_attestation.v1',captured_at_unix_ms=int(time.time()*1000),run_id=run['run_id'],
        entry_node_id=nodes[0],request_id='fixture-request',prompt_token_ids=prompt,max_new_tokens=len(output),execution_graph=graph,
        output_token_ids=output,signed_observations=observations,counters=dict(frames_sent=12,frames_received=12,
        applied_operation_count=12,fatal=None))
    statement=fixture.document['authorization']['message']
    binding=dict(endpoint_id=endpoints['node-1'],process_id=statement['incarnation'],
        member_key_digest=statement['member_verification_key']['verification_key_digest'],configure=statement['configure'])
    return issue_live_route_qualification(att,expected_prompt_token_ids=prompt,expected_output_token_ids=output,
                                          phone_bindings={'node-1':binding})
