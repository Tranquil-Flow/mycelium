# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical provisioner/pack consumer over tiny real Safetensors, no tensor execution."""
import copy
import hashlib
import json
import struct
from dataclasses import replace
import pytest
from test_assignment_planning import enrolled, seed
from test_serving import _native_snapshot
from mycelium_a13_macos.serving import prepare_local_source
from mycelium_qualification.evidence import canonical_json_bytes


@pytest.fixture(autouse=True)
def contribution_consent(monkeypatch):
    # This fixture explicitly enrolls a willing model-free contributor; the
    # inherited pairing-only fixture intentionally defaults contribution off.
    from tests.a13_macos.test_peer import CONSENT
    monkeypatch.setitem(CONSENT,'contribution_enabled',True)


def selected(enrolled,tmp_path):
    owner,peer,nodes,_,_=enrolled
    root=_native_snapshot(tmp_path/'source')
    config=json.loads((root/'config.json').read_text());config['num_hidden_layers']=2
    (root/'config.json').write_text(json.dumps(config))
    file=root/'model.safetensors'; raw=file.read_bytes();size=struct.unpack('<Q',raw[:8])[0]
    header=json.loads(raw[8:8+size]);payload=raw[8+size:]
    header['model.layers.1.self_attn.q_proj.weight']={'dtype':'F32','shape':[1],'data_offsets':[len(payload),len(payload)+4]}
    encoded=json.dumps(header,separators=(',',':')).encode()
    file.write_bytes(struct.pack('<Q',len(encoded))+encoded+payload+struct.pack('<f',1))
    prepared=prepare_local_source(root)
    proposal=owner.plan_assignments(prepared=prepared,node_ids=nodes,split_layer=1,
        cache_roots={node:str(root) for node in nodes})
    from pathlib import Path
    assignment=json.loads(Path(proposal['plan_path']).read_text())['assignments'][1]
    binding=peer.managed_control_snapshot()
    pin='sha256:'+hashlib.sha256(canonical_json_bytes(assignment)).hexdigest()
    return peer,prepared,assignment,binding,pin


def test_acquired_source_to_native_pack_and_loader_report(enrolled,tmp_path):
    from mycelium_a13_macos.assignment_provisioning import provision_selected_assignment
    peer,prepared,assignment,binding,pin=selected(enrolled,tmp_path)
    intents=[]
    def allow(intent): intents.append(intent);return True
    result=provision_selected_assignment(prepared=prepared,assignment=assignment,
        expected_assignment_digest=pin,member_binding=binding,
        current_member=peer.managed_control_snapshot,authorize=allow,max_artifact_bytes=100_000)
    assert result['state']=='artifacts_verified_not_loaded' and result['route_ready'] is False
    assert result['artifact_report']['network_download_bytes']==0
    assert result['artifact_report']['ready_for_load'] is True
    assert result['stage_pack']['assignment_id']==assignment['assignment_id']
    assert len(intents)>=2
    assert intents[0]['assignment_digest']==pin
    assert result['loader_report']['route_ready'] is False


def test_target_application_consumes_provisioner_and_fences_late_revoke(enrolled,tmp_path):
    from mycelium_a13_macos.target_app import TargetApplication
    from mycelium_a13_macos.assignment_provisioning import AssignmentProvisioningError
    peer,prepared,assignment,binding,pin=selected(enrolled,tmp_path)
    app=TargetApplication(tmp_path/'app',_test_peer_factory=lambda root:peer)
    result=app.provision_assignment(prepared=prepared,assignment=assignment,
        expected_assignment_digest=pin,authorize=lambda intent:True,max_artifact_bytes=100_000)
    assert result['stage_pack']['assignment_id']==assignment['assignment_id']
    calls=[]
    def revoke_during_verification(intent):
        calls.append(intent)
        if len(calls)==2:peer._record_disposition('revoked')
        return True
    with pytest.raises(AssignmentProvisioningError,match='not_authorized'):
        app.provision_assignment(prepared=prepared,assignment=assignment,
            expected_assignment_digest=pin,authorize=revoke_during_verification,max_artifact_bytes=100_000)
    assert len(calls)==2


@pytest.mark.parametrize('mutation',['pin','cache','member','expired','budget','denied','model'])
def test_assignment_admission_precedes_native_provisioning(enrolled,tmp_path,monkeypatch,mutation):
    from mycelium_a13_macos.assignment_provisioning import provision_selected_assignment,AssignmentProvisioningError
    peer,prepared,assignment,binding,pin=selected(enrolled,tmp_path)
    allow=lambda intent:True;maximum=100_000
    if mutation=='pin':pin='sha256:'+'0'*64
    elif mutation=='cache':
        from layer_assignment import assignment_id_for
        assignment['artifact_cache_root']=str(tmp_path/'unowned-cache')
        assignment['assignment_id']=assignment_id_for(assignment)
        pin='sha256:'+hashlib.sha256(canonical_json_bytes(assignment)).hexdigest()
    elif mutation=='member':binding['generation']+=1
    elif mutation=='expired':peer._record_disposition('expired')
    elif mutation=='budget':maximum=1
    elif mutation=='denied':allow=lambda intent:False
    else:prepared=replace(prepared,model=replace(prepared.model,runtime_quantization='none'))
    import weight_provisioning
    calls=[]
    monkeypatch.setattr(weight_provisioning,'provision_assignment',lambda *a,**kw:calls.append(kw))
    with pytest.raises(AssignmentProvisioningError):
        provision_selected_assignment(prepared=prepared,assignment=assignment,
            expected_assignment_digest=pin,member_binding=binding,
            current_member=peer.managed_control_snapshot,authorize=allow,max_artifact_bytes=maximum)
    assert calls==[] and not (tmp_path/'unowned-cache').exists()
