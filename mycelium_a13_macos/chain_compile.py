"""Application composition of canonical compiler and managed registry runtime.

Stable library surface for the owner UI (lane a13-demo-ui calls these; it owns
the routes/buttons):

* ``plan_member_split(capability_reports, *, model_root, model_contract=None,
  members=None)`` -> memory-proportional split document (2..8 hosts).
* ``start_compile(app, *, acknowledged)`` -> N-host compile in a thread; uses
  ``app._member_capability_reports`` and, when ``app._chain_slice_fetch`` is
  True, emits a digest-pinned per-member slice plan.
* ``compile_member_slices(*, operator_plan, shard_root, prepared)`` -> slice plan.
* ``stop_serving(app, *, timeout)`` and ``rebuild_with_current_members(app, *,
  acknowledged, run_id, deployment_id, descriptors)`` -> idempotent rebuild.
"""
from argparse import Namespace
from dataclasses import asdict
from typing import Any, Mapping, Sequence
import hashlib,json,math,socket,threading,time,traceback
from .authority import _private_directory,_atomic_private_json
from .chain_observation import prepare_observation
from .compiler_inputs import fresh_operator_template
from .installation_descriptor import verify_installation_descriptor
from .layer_split import (MAX_HOSTS,MIN_HOSTS,SplitRefused,memory_proportional_split,
                          model_layer_profile,policy_for_runtime)
from .serving import ExecutionApproval,slice_fetch_permitted
from .workflow import owner_channel,prepare_serving_workflow


def _capability_rows(reports:Mapping[str,Any],node_ids:Sequence[str],*,members=None,now=None):
    """Owner-verified capability records -> split member rows (fail closed).

    Each record: ``memory_bytes`` (int), optional ``decode_tokens_per_second``;
    when ``members`` is given it must also carry the member's current
    ``generation`` and an unexpired ``expires_at``. Signature verification of
    the member's report envelope is the join lane's job before it lands here.
    """
    now=time.time() if now is None else now
    rows=[]
    for node in node_ids:
        report=reports.get(node) if isinstance(reports,Mapping) else None
        if not isinstance(report,Mapping):raise ValueError('member_capability_report_required')
        if members is not None:
            expires=report.get('expires_at')
            if (report.get('generation')!=members[node]['generation'] or type(expires) not in (int,float)
                    or not math.isfinite(expires) or now>=expires):
                raise ValueError('member_capability_report_stale')
        row={'node_id':node,'memory_bytes':report.get('memory_bytes')}
        if 'decode_tokens_per_second' in report:row['decode_tokens_per_second']=report['decode_tokens_per_second']
        rows.append(row)
    return rows


def plan_member_split(capability_reports,*,model_root,model_contract=None,members=None,now=None):
    """Stable API: capability reports + model -> deterministic split document.

    ``capability_reports`` is a mapping node_id -> record or a sequence of
    records with ``node_id``; member order is node-id order (the builder's
    topology order). Raises ``SplitRefused`` (a ValueError with ``code`` and
    ``node_id``) for a member that cannot hold its share.
    """
    if not isinstance(capability_reports,Mapping):
        capability_reports={r.get('node_id'):r for r in capability_reports}
    node_ids=sorted(capability_reports)
    if not MIN_HOSTS<=len(node_ids)<=MAX_HOSTS:raise SplitRefused('host_count_invalid')
    rows=_capability_rows(capability_reports,node_ids,members=members,now=now)
    policy=policy_for_runtime(getattr(model_contract,'runtime_quantization',None))
    return memory_proportional_split(rows,model_layer_profile(model_root),policy)


def compile_member_slices(*,operator_plan,shard_root,prepared):
    """Stable API: compiled plan + emitted stage shards -> per-member slice plan."""
    from .slice_acquisition import compile_slice_plan,node_weight_files
    return compile_slice_plan(model_id=prepared.model.model_id,
        resolved_commit=prepared.model.resolved_commit,source_root=prepared.source_root,
        source_manifest_files=list(prepared.manifest['files']),
        node_files=node_weight_files(operator_plan),shard_root=shard_root)


def _compile_hosts(context,doc):
    observations=doc['observations']
    hosts=[v['host_id'] for v in observations]
    if len(observations)>MAX_HOSTS or len(context.facts)>MAX_HOSTS:
        raise ValueError('host_count_exceeds_limit')
    if (len(observations)<MIN_HOSTS or len(set(hosts))!=len(hosts)
            or len(observations)!=len(context.facts)):
        raise ValueError('two_distinct_hosts_required')
    return sorted(context.facts)


def start_compile(app,*,acknowledged):
    if app.installation_role!='owner' or not app.running or acknowledged is not True:
        raise ValueError('model_preparation_consent_required')
    context=getattr(app,'_observation_context',None)
    doc=None if context is None else context.observations
    if doc is None:raise ValueError('two_distinct_hosts_required')
    node_ids=_compile_hosts(context,doc)
    if getattr(app,'_chain_compile_state',None) is not None:raise ValueError('compiler_already_started')
    # Current local source, consent and current members must still bind.
    if app._resource_check() is not None:raise ValueError('compiler_resource_policy_denied')
    prepared=app.acquisition.prepared_source()
    if any(f['manifest_digest']!=prepared.manifest_digest for f in context.facts.values()):
        raise ValueError('compiler_model_changed')
    current={}
    for m in context.members:
        record=app.authority.coordinator.member(m['node_id'])
        if (time.time()>=record['lease_expires_at'] or
                any(record[k]!=m[k] for k in ('generation','incarnation','verification_key_digest','endpoint_id'))):
            raise ValueError('compiler_member_changed')
        current[m['node_id']]=record
    reports=getattr(app,'_member_capability_reports',None)
    split=None
    if reports is not None:
        if not isinstance(reports,Mapping):raise ValueError('member_capability_report_required')
        split=plan_member_split({n:reports.get(n) for n in node_ids},
            model_root=prepared.source_root,model_contract=prepared.model,members=current)
        if [s['node_id'] for s in split['stages']]!=node_ids:raise ValueError('member_capability_report_required')
    elif len(node_ids)!=2:
        raise ValueError('member_capability_report_required')
    slice_fetch=getattr(app,'_chain_slice_fetch',False) is True
    if slice_fetch:
        architecture=model_layer_profile(prepared.source_root).model_type
        if split is None or not slice_fetch_permitted(getattr(prepared,'source_bytes',None),architecture=architecture):
            raise ValueError('slice_fetch_model_not_permitted')
    context.retire_bootstrap()
    root=_private_directory(app.root/'chain-runs'/context.run_id,create=True)
    epoch=getattr(app,'_chain_epoch',0);app._chain_epoch=epoch
    app._chain_compile_state={'state':'compiling','route_ready':False}
    def work():
        try:
            from scripts.build_qwen_live_route import build,_TOPOLOGY_FIELDS
            keys={'gossip':[app.authority.coordinator.signer.public_key_record()],
                  'load_proof':app._chain_target_keys}
            template=fresh_operator_template(root/'compiled',keys)
            _atomic_private_json(root/'template.json',template)
            topology={'protocol':'mycelium.qwen_live_topology.v1','nodes':[
                {**{k:context.facts[n][k] for k in _TOPOLOGY_FIELDS},'endpoint_id':context.facts[n]['activation_endpoint_id']}
                for n in node_ids]}
            _atomic_private_json(root/'topology.json',topology)
            _atomic_private_json(root/'observations.json',doc)
            if split is not None:_atomic_private_json(root/'layer-split.json',split)
            args=Namespace(template_plan=root/'template.json',model_root=prepared.source_root,
                model_id=prepared.model.model_id,resolved_commit=prepared.model.resolved_commit,
                deployment_id=context.deployment_id,output_root=root/'compiled',
                route_label='a13',topology=root/'topology.json',managed_transport=True,
                managed_hello_observations=root/'observations.json',stage_sharded=slice_fetch,
                emit_stage_shards=root/'stage-shards' if slice_fetch else None,
                reuse_stage_sharded_root=None,temporary_root=root/'temporary',
                m13_control_plane=None,m14_control_plane=None,m18_control_plane=None,
                model_preparation_authorization=None,
                managed_layer_ranges=None if split is None else [[s['start'],s['end']] for s in split['stages']])
            build(args)
            slice_plan=None
            if slice_fetch:
                from .slice_acquisition import node_slice_plan
                operator_plan=json.loads((root/'compiled/operator-plan.json').read_text('utf-8'))
                slice_plan=compile_member_slices(operator_plan=operator_plan,
                    shard_root=root/'stage-shards',prepared=prepared)
                _atomic_private_json(root/'slice-plan.json',slice_plan)
            with app.guard:
                if app.closed.is_set() or not app.running:raise ValueError('compiler_stopped')
                if getattr(app,'_chain_epoch',0)!=epoch:raise ValueError('compiler_superseded')
                # Slice mode: the workflow must bind slice_acquisition.bind_slice_prepositions
                # (lane a13-demo owns workflow.py); without that support this fails closed.
                workflow=prepare_serving_workflow(authority=app.authority,
                    channel=context.factory.owner,prepared=prepared,
                    operator_plan=root/'compiled/operator-plan.json',
                    target_source_roots={node:f['source_root'] for node,f in context.facts.items()},
                    **({} if slice_plan is None else {'slice_plan':slice_plan}))
                model=prepared.model;p=prepared.resource_proposal
                approval=ExecutionApproval(model_id=model.model_id,resolved_commit=model.resolved_commit,
                    source_representation=model.source_representation,runtime_dtype=model.runtime_dtype,
                    runtime_quantization=model.runtime_quantization,max_execution_download_bytes=p.execution_download_bytes,
                    max_additional_disk_bytes=p.additional_disk_bytes,max_combined_rss_bytes=p.max_combined_rss_bytes,
                    max_wall_seconds=p.max_wall_seconds,run_count=p.run_count,
                    operator_plan_digest=workflow.launch.operator_plan_digest,
                    physical_host_ids=workflow.launch.physical_host_ids)
                app._chain_workflow=workflow;app._chain_approval=approval
                state={'state':'compiled','route_ready':False,'execution_proposal':json.loads(json.dumps(asdict(approval)))}
                if split is not None:
                    state['layer_split']={'split_digest':split['split_digest'],'weighting':split['weighting'],
                        'stages':[{k:s[k] for k in ('node_id','start','end','stage_bytes','cost_bytes','budget_bytes')}
                                  for s in split['stages']]}
                if slice_plan is not None:
                    state['slice_plan_digest']=slice_plan['plan_digest']
                    state['node_slice_plan_digests']={n:node_slice_plan(slice_plan,n)['node_plan_digest']
                                                      for n in sorted(slice_plan['nodes'])}
                app._chain_compile_state=state
        except Exception:
            (root/'compile-error.private.log').write_text(traceback.format_exc())
            (root/'compile-error.private.log').chmod(0o600)
            if getattr(app,'_chain_epoch',0)==epoch:
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


_CHAIN_FIELDS=('_chain_compile_state','_chain_compile_thread','_chain_workflow','_chain_approval',
               '_chain_serving_thread','_chain_activation_event')


def stop_serving(app,*,timeout=30.0):
    """Idempotent owner stop: drain the workflow, then forget the old run.

    Never sets the activation gate. An in-flight compile is superseded by the
    epoch bump and can no longer publish a workflow for the old member set.
    Refuses (leaving the old state) if a thread does not exit within ``timeout``.
    """
    workflow=getattr(app,'_chain_workflow',None)
    if workflow is not None:workflow.stop()
    app._chain_epoch=getattr(app,'_chain_epoch',0)+1
    for name in ('_chain_serving_thread','_chain_compile_thread'):
        thread=getattr(app,name,None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():raise ValueError('rebuild_stop_timeout')
    for name in _CHAIN_FIELDS:setattr(app,name,None)
    return {'state':'stopped','route_ready':False}


def _rebuild_key(run_id,deployment_id,members):
    value=json.dumps({'run_id':run_id,'deployment_id':deployment_id,'members':[
        [m[k] for k in ('node_id','generation','incarnation','verification_key_digest','endpoint_id')]
        for m in sorted(members,key=lambda m:m['node_id'])]},sort_keys=True)
    return 'sha256:'+hashlib.sha256(value.encode()).hexdigest()


def rebuild_with_current_members(app,*,acknowledged,run_id,deployment_id,descriptors,stop_timeout=30.0):
    """Owner 'Rebuild with current members': stop -> republish -> (compile, start).

    Idempotent per (run_id, deployment_id, current member bindings): repeating
    the call returns the same result without a second stop or publication.
    Descriptors of members that vanished (lease expired, revoked, re-joined
    under a new generation) are dropped, never fatal; fewer than two current
    members refuses before anything is stopped. A current member whose previous
    stage offer awaits target-confirmed cleanup is asked to clean up and the
    call returns ``awaiting_member_cleanup``; call again to continue. The
    returned state is followed by the ordinary observe -> compile -> start ->
    activate sequence (brief outage; no live expansion).
    """
    if app.installation_role!='owner' or not app.running or acknowledged is not True:
        raise ValueError('rebuild_owner_consent_required')
    if not isinstance(descriptors,list) or len(descriptors)>4*MAX_HOSTS:
        raise ValueError('rebuild_descriptors_invalid')
    coordinator=app.authority.coordinator
    kept,members,dropped=[],[],[]
    for card in descriptors:
        try:node=card['statement']['target_node_id']
        except (KeyError,TypeError):continue
        if not isinstance(node,str) or node in {m['node_id'] for m in members}:continue
        try:
            member=coordinator.member(node)
            verify_installation_descriptor(card,member=member,swarm_id=coordinator.swarm_id,
                run_id=run_id,deployment_id=deployment_id)
        except Exception:
            dropped.append(node);continue
        kept.append(card);members.append(member)
    if len(kept)>MAX_HOSTS:raise ValueError('host_count_exceeds_limit')
    if len(kept)<MIN_HOSTS:raise ValueError('rebuild_requires_two_current_members')
    key=_rebuild_key(run_id,deployment_id,members)
    previous=getattr(app,'_chain_rebuild',None)
    context=getattr(app,'_observation_context',None)
    if previous is not None and previous['run_id']==run_id:
        if previous['rebuild_key']!=key:raise ValueError('rebuild_run_id_reused')
        if previous['state']=='awaiting_bootstrap_workers':return dict(previous)
    elif context is not None and getattr(context,'run_id',None)==run_id:
        raise ValueError('rebuild_run_id_reused')
    stop_serving(app,timeout=stop_timeout)
    mailbox=app.authority.control_mailbox
    waiting=[]
    for member in members:
        offer=mailbox.pending_offer(member)
        if offer is None:continue
        try:mailbox.retire_offer(offer)
        except Exception:
            mailbox.request_stage_cleanup(offer);waiting.append(member['node_id'])
    base={'route_ready':False,'run_id':run_id,'deployment_id':deployment_id,'rebuild_key':key,
          'node_ids':sorted(m['node_id'] for m in members),'dropped_node_ids':sorted(set(dropped))}
    if waiting:
        app._chain_rebuild={**base,'state':'awaiting_member_cleanup','waiting_node_ids':sorted(waiting)}
        return dict(app._chain_rebuild)
    app._observation_context=None
    order=sorted(range(len(kept)),key=lambda i:members[i]['node_id'])
    kept=[kept[i] for i in order]
    app._observation_context=prepare_observation(app,run_id=run_id,deployment_id=deployment_id,
        descriptors=kept,acknowledged=True)
    app._chain_target_keys=[json.loads(json.dumps(c['verification_key'])) for c in kept]
    app._chain_rebuild={**base,'state':'awaiting_bootstrap_workers'}
    return dict(app._chain_rebuild)
