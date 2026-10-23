# SPDX-License-Identifier: AGPL-3.0-only
"""Native plan/seed/HF binding composition over tiny files; never loads a model."""
import hashlib
import json
import shutil
from pathlib import Path
import pytest
from test_assignment_planning import enrolled, seed
from test_assignment_provisioning import contribution_consent
from test_serving import _native_snapshot, _operator_plan, private_fixture_identity
from mycelium_a13_macos.serving import prepare_local_source
from mycelium_a13_macos.workflow import owner_channel, prepare_serving_workflow


@pytest.mark.parametrize('activation',['automatic','explicit','stop'])
def test_compiled_plan_binds_real_members_and_inert_serving_composition(tmp_path,enrolled,monkeypatch,activation):
    authority,peer,nodes,_,_=enrolled
    prepared=prepare_local_source(_native_snapshot(tmp_path/'source'))
    plan=_operator_plan(tmp_path/'native',prepared,seed=authority)
    document=json.loads(plan.read_text())
    mapping={'a13-local-0':nodes[0],'a13-local-1':nodes[1],'node-a':nodes[0],'node-b':nodes[1]}
    def rename(value):
        if isinstance(value,str):return mapping.get(value,value)
        if isinstance(value,list):return [rename(x) for x in value]
        if isinstance(value,dict):return {k:rename(v) for k,v in value.items()}
        return value
    document=rename(document);controller=document['controller']
    from tests.seed_coordinator.plan_fixture import _two_member_plan
    offers=rename(_two_member_plan())['controller']['membership_snapshot']['assignment_offers']
    for envelope in offers:
        for endpoint in envelope['message']['peer_endpoint_records']:
            endpoint['endpoint_id']=authority.coordinator.member(endpoint['node_id'])['endpoint_id']
    controller['membership_snapshot']['assignment_offers']=offers
    for row in controller['peers']:
        row.update(process_transport='managed_https',ssh_target=None,ssh_identity_file=None)
    bundle=Path(controller['source_root'])
    for path in (bundle/'control').glob('*.json'):
        path.write_text(json.dumps(rename(json.loads(path.read_text()))))
    (bundle/'physical_inference_node.py').write_text('# inert transfer fixture; installed entrypoint is used\n')
    (bundle/'deployment').mkdir()
    shutil.copyfile(prepared.source_root/'model.safetensors',bundle/'deployment/model.safetensors')
    records=[]
    for path in sorted(p for p in bundle.rglob('*') if p.is_file()):
        raw=path.read_bytes()
        records.append({'path':str(path.relative_to(bundle)),'size_bytes':len(raw),
            'content_digest':'sha256:'+hashlib.sha256(raw).hexdigest()})
    controller['transfer_manifest']={'protocol':'mycelium.controller_transfer_manifest.v1','files':records}
    controller['node_transfer_manifests']={'protocol':'mycelium.controller_node_transfer_manifests.v1',
        'manifests':{node:controller['transfer_manifest'] for node in nodes}}
    plan.write_text(json.dumps(document))
    channel=owner_channel(authority)
    try:
        roots={node:str(prepared.source_root) for node in nodes}  # metadata-only fixture paths
        workflow=prepare_serving_workflow(authority=authority,channel=channel,prepared=prepared,
            operator_plan=plan,target_source_roots=roots)
        assert workflow.status()['state']=='prepared'
        assert workflow.status()['readiness_source']=='registry_api'
        assert 'route_ready' not in workflow.status()
        assert workflow.launch.seed_state_root==authority._seed_root.resolve()
        actual=json.loads(workflow.launch.operator_plan.read_text())
        assert actual['controller']['membership_snapshot']['swarm_id']==authority.coordinator.swarm_id
        assert set(actual['controller']['prepositioned_artifacts']['members'])==set(nodes)
        assert all(authority.control_mailbox.pending_offer(authority.coordinator.member(node)) is None for node in nodes)
        from test_serving import _approval, _free_dynamic_port
        from mycelium_a13_macos.serving import ServingPreparationError
        with pytest.raises(ServingPreparationError,match='approval_required'):
            workflow.run(approval=None,port=_free_dynamic_port())
        assert all(authority.control_mailbox.pending_offer(authority.coordinator.member(node)) is None for node in nodes)
        # Inert execution seams only. Real TLS/native staging is exercised by
        # test_workflow; this pins the complete owner dispatch and grant order.
        from dataclasses import replace
        import socket
        events=[]
        def stage(command):
            assert command=='prepare'
            assert all(authority.control_mailbox.pending_offer(authority.coordinator.member(node)) is not None for node in nodes)
            events.append('stage')
            return {'route_ready':False}
        def runner(**kw):
            assert events==['stage'] and kw['api_only'] is True
            assert kw['governance_root'] is None
            assert kw['session_factory'] is workflow.controller._session_factory
            kw['on_started']({'startup_qualification_complete':False,'deployment_count':0,
                'unavailable_deployment_count':1,'url':'fixture://no-live-listener/'})
            assert workflow.status()['state']=='api_running'
            assert 'route_ready' not in workflow.status()
            events.append('registry')
            return 0
        monkeypatch.setattr(workflow.controller,'execute',stage)
        workflow.launch=replace(workflow.launch,_runner=runner)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        if activation=='automatic':
            assert workflow.run(approval=_approval(workflow.launch),port=port)==0
        else:
            import threading,time
            gate=threading.Event();results=[];errors=[]
            def run():
                try:results.append(workflow.run(approval=_approval(workflow.launch),port=port,activation_event=gate))
                except Exception as error:errors.append(error)
            thread=threading.Thread(target=run,daemon=True);thread.start()
            deadline=time.monotonic()+3
            while workflow.status()['state']!='awaiting_activation' and thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
            try:
                assert workflow.status()['state']=='awaiting_activation'
                assert events==['stage']
                if activation=='stop':workflow.stop()
                else:gate.set()
            finally:
                workflow.stop() if activation=='stop' else gate.set()
                thread.join(3)
            assert not thread.is_alive()
            if activation=='stop':
                assert results==[] and len(errors)==1 and str(errors[0])=='managed_workflow_stopped'
                assert events==['stage']
            else:assert results==[0] and errors==[]
        if activation!='stop':assert events==['stage','registry'] and workflow.status()['state']=='stopped' 
        with pytest.raises(ValueError,match='plan_already_exists'):
            prepare_serving_workflow(authority=authority,channel=channel,prepared=prepared,
                operator_plan=plan,target_source_roots=roots)
    finally:channel.close()
