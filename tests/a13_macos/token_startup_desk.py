# SPDX-License-Identifier: AGPL-3.0-only
"""Owner startup qualification over real native children with fake model load.

Plan/codec/projection loading and host/model metadata are synthetic fixtures;
the shipped route.open(), exact
startup challenge, native signatures, live qualification issuer and policy
installation are not replaced. No artifact here is physical release evidence.
"""
from types import SimpleNamespace
from mycelium_live import supervisor as sup


def qualify_startup(*,route,owner,sessions,graph,tmp_path,monkeypatch,inference_commands,enabled):
    route._open=False
    route._deployment_qualification=None
    route._sessions={}
    route._identities={}
    route._verification_keys={}
    for node in sessions:
        route._plans_by_node[node]['configure'].update(assignment_file='fixture-assignment.json',
            artifact_report_file='fixture-artifact-report.json',device_states=[],load_generation=1)
    monkeypatch.setattr(route,'_node_command',lambda _:())
    route._session_factory=lambda **kwargs:sessions[kwargs['node_id']]
    installed=[]
    for node,session in sessions.items():
        original=session.send
        def send(*,command_id,command,payload,node=node,original=original):
            if command=='install_token_path':
                assert inference_commands==['infer_start','infer_decode']
                assert owner is not None and not owner._owners
            result=original(command_id=command_id,command=command,payload=payload)
            if command=='install_token_path' and result.get('ok') is True:installed.append(node)
            return result
        monkeypatch.setattr(session,'send',send)
    monkeypatch.setattr(sup.PhysicalLiveRoute,'from_operator_plan',lambda *a,**k:route)
    monkeypatch.setattr(sup,'native_deployment_projection',lambda p:SimpleNamespace(
        deployment_id=graph.deployment_id,model_id=graph.model_id,model_revision=graph.resolved_commit,quantization='none'))
    monkeypatch.setattr(sup,'_deployment_from_plan',lambda p:tmp_path)
    monkeypatch.setattr(sup,'prompt_codec_from_deployment',lambda p:SimpleNamespace(stop_token_ids=frozenset()))
    for name in ('_placement_projection','_topology_projection','_workload_comparison','_m16_performance_budget','_m17_model_operation','_m18_replica_plan'):
        monkeypatch.setattr(sup,name,lambda *a,**k:None)
    monkeypatch.setattr(sup,'_historical_evidence',lambda *a,**k:())
    original_qualify=sup._qualify_open_route
    def qualify(r):
        try:return original_qualify(r)
        except Exception as error:
            diagnostic={node:r.diagnostic_stderr(node) for node in sessions}
            raise AssertionError((str(error),diagnostic)) from error
    monkeypatch.setattr(sup,'_qualify_open_route',qualify)
    runtime=sup._qualified_runtime(tmp_path/'fixture-plan',seed_state_root=tmp_path/'fixture-seed')
    assert runtime.route is route and runtime.qualification.route_ready is True
    assert set(installed)==(set(sessions) if enabled else set())
    if enabled:assert owner.current() and not owner._owners
    assert inference_commands==['infer_start','infer_decode']
    inference_commands.clear()  # Product-request counts exclude startup work.
    return runtime.qualification
