"""Application composition of canonical compiler and managed registry runtime."""
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path
import json,socket,threading,time,traceback
from .authority import _private_directory,_atomic_private_json
from .compiler_inputs import fresh_operator_template
from .serving import ExecutionApproval
from .workflow import owner_channel,prepare_serving_workflow


def start_compile(app,*,acknowledged):
    if app.installation_role!='owner' or not app.running or acknowledged is not True:
        raise ValueError('model_preparation_consent_required')
    context=getattr(app,'_observation_context',None)
    doc=None if context is None else context.observations
    if doc is None or len(doc['observations'])!=2 or len({v['host_id'] for v in doc['observations']})!=2:
        raise ValueError('two_distinct_hosts_required')
    if getattr(app,'_chain_compile_state',None) is not None:raise ValueError('compiler_already_started')
    # Current local source, consent and current members must still bind.
    if app._resource_check() is not None:raise ValueError('compiler_resource_policy_denied')
    prepared=app.acquisition.prepared_source()
    if any(f['manifest_digest']!=prepared.manifest_digest for f in context.facts.values()):
        raise ValueError('compiler_model_changed')
    for m in context.members:
        current=app.authority.coordinator.member(m['node_id'])
        if (time.time()>=current['lease_expires_at'] or
                any(current[k]!=m[k] for k in ('generation','incarnation','verification_key_digest','endpoint_id'))):
            raise ValueError('compiler_member_changed')
    context.retire_bootstrap()
    root=_private_directory(app.root/'chain-runs'/context.run_id,create=True)
    app._chain_compile_state={'state':'compiling','route_ready':False}
    def work():
        try:
            from scripts.build_qwen_live_route import build,_TOPOLOGY_FIELDS
            keys={'gossip':[app.authority.coordinator.signer.public_key_record()],
                  'load_proof':app._chain_target_keys}
            template=fresh_operator_template(root/'compiled',keys)
            _atomic_private_json(root/'template.json',template)
            topology={'protocol':'mycelium.qwen_live_topology.v1','nodes':[
                {**{k:f[k] for k in _TOPOLOGY_FIELDS},'endpoint_id':f['activation_endpoint_id']}
                for f in context.facts.values()]}
            _atomic_private_json(root/'topology.json',topology)
            _atomic_private_json(root/'observations.json',doc)
            args=Namespace(template_plan=root/'template.json',model_root=prepared.source_root,
                model_id=prepared.model.model_id,resolved_commit=prepared.model.resolved_commit,
                deployment_id=context.deployment_id,output_root=root/'compiled',
                route_label='a13',topology=root/'topology.json',managed_transport=True,
                managed_hello_observations=root/'observations.json',stage_sharded=False,
                reuse_stage_sharded_root=None,temporary_root=root/'temporary',
                m13_control_plane=None,m14_control_plane=None,m18_control_plane=None,
                model_preparation_authorization=None)
            build(args)
            with app.guard:
                if app.closed.is_set() or not app.running:raise ValueError('compiler_stopped')
                workflow=prepare_serving_workflow(authority=app.authority,
                    channel=context.factory.owner,prepared=prepared,
                    operator_plan=root/'compiled/operator-plan.json',
                    target_source_roots={node:f['source_root'] for node,f in context.facts.items()})
                model=prepared.model;p=prepared.resource_proposal
                approval=ExecutionApproval(model_id=model.model_id,resolved_commit=model.resolved_commit,
                    source_representation=model.source_representation,runtime_dtype=model.runtime_dtype,
                    runtime_quantization=model.runtime_quantization,max_execution_download_bytes=p.execution_download_bytes,
                    max_additional_disk_bytes=p.additional_disk_bytes,max_combined_rss_bytes=p.max_combined_rss_bytes,
                    max_wall_seconds=p.max_wall_seconds,run_count=p.run_count,
                    operator_plan_digest=workflow.launch.operator_plan_digest,
                    physical_host_ids=workflow.launch.physical_host_ids)
                app._chain_workflow=workflow;app._chain_approval=approval
                app._chain_compile_state={'state':'compiled','route_ready':False,'execution_proposal':json.loads(json.dumps(asdict(approval)))}
        except Exception:
            (root/'compile-error.private.log').write_text(traceback.format_exc())
            (root/'compile-error.private.log').chmod(0o600)
            app._chain_compile_state={'state':'failed','error_code':'chain_compile_failed','route_ready':False}
    app._chain_compile_thread=threading.Thread(target=work,name='a13-chain-compile',daemon=True)
    app._chain_compile_thread.start()
    return dict(app._chain_compile_state)


def start_serving(app,*,proposal,acknowledged):
    if app.installation_role!='owner' or not app.running or acknowledged is not True:
        raise ValueError('model_execution_consent_required')
    state=getattr(app,'_chain_compile_state',None)
    if state is None or state.get('state')!='compiled' or proposal!=state['execution_proposal']:
        raise ValueError('model_execution_proposal_changed')
    if getattr(app,'_chain_serving_thread',None) is not None:raise ValueError('chain_already_started')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    # The canonical runner independently enforces the dynamic/protected-port guard.
    app._chain_activation_event=threading.Event()
    def serve():
        try:app._chain_workflow.run(approval=app._chain_approval,port=port,
            activation_event=app._chain_activation_event)
        except Exception as error:
            # Keep the original cause private; public workflow codes stay bounded.
            root=_private_directory(app.root/'chain-runs'/app._observation_context.run_id,create=True)
            _atomic_private_json(root/'serve-error.private.json',{
                'exception_type':type(error).__name__,'traceback':traceback.format_exc()})
    app._chain_serving_thread=threading.Thread(target=serve,name='a13-chain-serving',daemon=True)
    app._chain_serving_thread.start()
    return {'state':'starting','registry_origin':f'http://127.0.0.1:{port}','route_ready':False}


def activate_serving(app,*,proposal,acknowledged):
    """Owner starts qualification after the contributors have accepted work.

    This is scheduling consent, not a worker/readiness attestation. Native
    startup still authenticates and qualifies every required participant.
    """
    if app.installation_role!='owner' or not app.running or acknowledged is not True:
        raise ValueError('model_execution_consent_required')
    state=getattr(app,'_chain_compile_state',None)
    workflow=getattr(app,'_chain_workflow',None)
    gate=getattr(app,'_chain_activation_event',None)
    if state is None or proposal!=state.get('execution_proposal'):
        raise ValueError('model_execution_proposal_changed')
    if workflow is None or gate is None or workflow.status()['state']!='awaiting_activation':
        raise ValueError('managed_workflow_not_awaiting_activation')
    gate.set()
    return {'state':'activation_requested','route_ready':False}
