# SPDX-License-Identifier: AGPL-3.0-only
"""Real tiny-source hashing, canonical archives and signed local staging; no models loaded."""
import copy
import hashlib
import shutil
from pathlib import Path
import pytest
from test_control_pump import _world
from test_controller_managed_staging import setup_controller
from test_serving import _native_snapshot
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from mycelium_a13_macos.serving import prepare_local_source
from mycelium_a13_macos.target_app import TargetApplication


def inputs(tmp_path, world):
    controller, peer = setup_controller(tmp_path, world)
    prepared = prepare_local_source(_native_snapshot(world['target'].state_root/'serving'))
    destination = controller.source_root/'deployment/model.safetensors'
    destination.parent.mkdir()
    shutil.copyfile(prepared.source_root/'model.safetensors', destination)
    raw = destination.read_bytes()
    records = sorted([*controller._transfer_manifest['files'], {
        'path':'deployment/model.safetensors', 'size_bytes':len(raw),
        'content_digest':'sha256:'+hashlib.sha256(raw).hexdigest(),
    }], key=lambda item:item['path'])
    document = {'peers':[{'node_id':peer.node_id,'process_transport':'managed_https'}],
        'transfer_manifest':{'protocol':'mycelium.controller_transfer_manifest.v1','files':records},
        'node_transfer_manifests':{'protocol':'mycelium.controller_node_transfer_manifests.v1',
            'manifests':{peer.node_id:{'protocol':'mycelium.controller_transfer_manifest.v1','files':copy.deepcopy(records)}}}}
    return controller, peer, prepared, document, {peer.node_id:str(prepared.source_root)}


def test_compiled_source_becomes_small_archive_and_real_target_local_copy(tmp_path):
    from mycelium_a13_macos.managed_transfers import bind_hf_prepositions
    with _world(tmp_path/'world') as world:
        controller,peer,prepared,document,roots=inputs(tmp_path,world)
        before=copy.deepcopy(document)
        result=bind_hf_prepositions(document,prepared=prepared,target_source_roots=roots)
        assert document==before
        controller._node_transfer_manifests=result['node_transfer_manifests']
        controller._prepositioned_artifacts=result['prepositioned_artifacts']
        controller._transfer_manifest=result['transfer_manifest']
        controller._validate_transfers()
        archive,_=controller._archive_identity_for_peer(peer)
        import io,tarfile,json
        with tarfile.open(fileobj=io.BytesIO(archive),mode='r:') as tar:
            assert tar.getnames()==['physical_inference_node.py']
        preposition,_,_=controller._preposition_identity_for_peer(peer)
        box=world['authority']._control_mailbox
        offer=box.publish_offer(target=world['member'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
            archive=archive,preposition=json.loads(preposition))
        app=TargetApplication(world['target'].state_root);app.peer=world['target']
        ack=app.stage_offer(offer,max_bytes=len(archive),max_disk_bytes=100_000,
            authorize=lambda intent:True,_test_ca_certificate=world['authority'].seed_server.material.ca_certificate)
        assert box.stage_receipt(offer)==ack
        assert (Path(peer.staging_root)/'deployment/model.safetensors').read_bytes()==(prepared.source_root/'model.safetensors').read_bytes()
        assert app._runtime is None


@pytest.mark.parametrize('mutation',['digest','size','unknown_weight','missing_node','extra_node','relative','traversal','revision','unmanaged','duplicate','already_bound'])
def test_invalid_source_or_target_binding_fails_without_mutation(tmp_path,mutation):
    from mycelium_a13_macos.managed_transfers import bind_hf_prepositions
    with _world(tmp_path/'world') as world:
        _,peer,prepared,document,roots=inputs(tmp_path,world)
        record=document['transfer_manifest']['files'][0]
        if mutation=='digest':record['content_digest']='sha256:'+'0'*64
        elif mutation=='size':record['size_bytes']+=1
        elif mutation=='unknown_weight':record['path']='deployment/foreign.safetensors';record['content_digest']='sha256:'+'1'*64
        elif mutation=='missing_node':roots={}
        elif mutation=='extra_node':roots['extra']=str(prepared.source_root)
        elif mutation=='relative':roots[peer.node_id]='relative/path'
        elif mutation=='traversal':roots[peer.node_id]=str(prepared.source_root)+'/../'+prepared.source_root.name
        elif mutation=='revision':roots[peer.node_id]=str(prepared.source_root.parent/'wrong')
        elif mutation=='unmanaged':document['peers'][0]['process_transport']='ssh'
        elif mutation=='duplicate':document['peers'].append(copy.deepcopy(document['peers'][0]))
        else:document['prepositioned_artifacts']={'protocol':'untrusted'}
        before=copy.deepcopy(document)
        with pytest.raises(ValueError,match='managed_hf_transfer'):
            bind_hf_prepositions(document,prepared=prepared,target_source_roots=roots)
        assert document==before


def _qwen3_snapshot(root):
    """The tiny native snapshot re-homed as the pinned Qwen3-0.6B revision."""
    import json
    from mycelium_a13_macos.run_model import QWEN3_0_6B_MODEL
    tiny = _native_snapshot(root/'tiny')
    snapshot = (root/('models--'+QWEN3_0_6B_MODEL.model_id.replace('/','--'))
                /'snapshots'/QWEN3_0_6B_MODEL.resolved_commit)
    snapshot.parent.mkdir(parents=True)
    shutil.copytree(tiny, snapshot)
    config = json.loads((snapshot/'config.json').read_text())
    config.update(architectures=['Qwen3ForCausalLM'], model_type='qwen3')
    (snapshot/'config.json').write_text(json.dumps(config))
    return snapshot, QWEN3_0_6B_MODEL


def test_whole_mode_binds_a_registered_qwen3_run_model(tmp_path):
    # Two-Mac run 8: whole-snapshot mode for Qwen3-0.6B failed
    # managed_hf_transfer_binding_invalid because binding accepted only the
    # Qwen2.5-7B candidate's identity and snapshot path.
    from mycelium_a13_macos.managed_transfers import bind_hf_prepositions
    with _world(tmp_path/'world') as world:
        controller, peer, _, document, _ = inputs(tmp_path, world)
        snapshot, model = _qwen3_snapshot(world['target'].state_root/'serving'/'model-source-c1899de289a0')
        prepared = prepare_local_source(snapshot, model=model)
        raw = (snapshot/'model.safetensors').read_bytes()
        for manifest in (document['transfer_manifest'], *document['node_transfer_manifests']['manifests'].values()):
            for row in manifest['files']:
                if row['path'] == 'deployment/model.safetensors':
                    row['size_bytes'] = len(raw)
                    row['content_digest'] = 'sha256:'+hashlib.sha256(raw).hexdigest()
        result = bind_hf_prepositions(document, prepared=prepared,
                                      target_source_roots={peer.node_id: str(snapshot)})
        [local] = result['prepositioned_artifacts']['members'][peer.node_id]
        assert local['source_path'] == str(snapshot/'model.safetensors')
        # A Qwen3 source cannot be bound to a member root of another model or revision.
        for wrong in (str(snapshot.parent/('0'*40)), str(prepared.source_root).replace('Qwen3-0.6B', 'Qwen3-8B')):
            with pytest.raises(ValueError, match='managed_hf_transfer'):
                bind_hf_prepositions(document, prepared=prepared, target_source_roots={peer.node_id: wrong})


def test_whole_mode_refuses_an_unregistered_model(tmp_path):
    import dataclasses
    from mycelium_a13_macos.managed_transfers import bind_hf_prepositions
    with _world(tmp_path/'world') as world:
        _, peer, prepared, document, roots = inputs(tmp_path, world)
        rogue = dataclasses.replace(prepared, model=dataclasses.replace(prepared.model, model_id='Evil/Model'))
        with pytest.raises(ValueError, match='managed_hf_transfer'):
            bind_hf_prepositions(document, prepared=rogue, target_source_roots=roots)
