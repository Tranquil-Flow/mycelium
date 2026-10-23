# SPDX-License-Identifier: AGPL-3.0-only
"""Installed supervisor admission order, with fake qualification/model metadata.

No native or device qualification claim: real signed native installation is
covered separately by test_token_two_process_join. This pins the startup caller.
"""
from types import MethodType, SimpleNamespace
import pytest
from test_token_workflow_owner import configured, world
from mycelium_live import supervisor as sup
from mycelium_live.route import PhysicalLiveRoute, RouteIdentity


@pytest.mark.parametrize('refused',[False,True])
def test_installed_startup_requires_policy_install_before_ready(tmp_path,world,monkeypatch,refused):
    owner,route,members,installs=configured(world,tmp_path,monkeypatch)
    graph=route._graph
    order=[]
    route.execution_graph=graph
    route.open=lambda:RouteIdentity(deployment_id=graph.deployment_id,model_id=graph.model_id,
        resolved_commit=graph.resolved_commit,endpoint_ids=tuple(m['endpoint_id'] for m in route._endpoints.values()))
    route.close=lambda:order.append('closed')
    route._token_path_owner=owner
    route._open=True
    def qualify(q,**kwargs):
        route._deployment_qualification=q
        order.append('qualified')
    def stops(ids):
        route._stop_token_ids=frozenset(ids)
        order.append(('stops',tuple(ids)))
    route.set_deployment_qualification=qualify
    route.set_stop_token_ids=stops
    original_install=owner.install
    def install(**kwargs):
        assert order==['qualified',('stops',(99,))]
        original_install(**kwargs)
        order.append('installed')
    owner.install=install
    route.qualify_token_path=MethodType(PhysicalLiveRoute.qualify_token_path,route)
    if refused:
        def fail(**kwargs):raise RuntimeError('native_install_refused')
        monkeypatch.setattr(route._sessions['tail'],'send',fail)
    monkeypatch.setattr(sup.PhysicalLiveRoute,'from_operator_plan',lambda *a,**k:route)
    monkeypatch.setattr(sup,'native_deployment_projection',lambda p:SimpleNamespace(
        deployment_id=graph.deployment_id,model_id=graph.model_id,model_revision=graph.resolved_commit,quantization='none'))
    monkeypatch.setattr(sup,'_qualify_open_route',lambda r:SimpleNamespace(issued_at_unix_ms=1))
    monkeypatch.setattr(sup,'_deployment_from_plan',lambda p:tmp_path)
    monkeypatch.setattr(sup,'prompt_codec_from_deployment',lambda p:SimpleNamespace(stop_token_ids=frozenset({99})))
    for name in ('_placement_projection','_topology_projection','_workload_comparison','_m16_performance_budget','_m17_model_operation','_m18_replica_plan'):
        monkeypatch.setattr(sup,name,lambda *a,**k:None)
    monkeypatch.setattr(sup,'_historical_evidence',lambda *a,**k:())
    if refused:
        with pytest.raises(RuntimeError,match='native_install_refused'):
            sup._qualified_runtime(tmp_path/'plan',seed_state_root=tmp_path/'seed')
        assert order[-1]=='closed' and [node for node,_ in installs]==['entry']
    else:
        runtime=sup._qualified_runtime(tmp_path/'plan',seed_state_root=tmp_path/'seed')
        assert runtime.route is route and order[-1]=='installed'
        assert [node for node,_ in installs]==['entry','tail']
    assert owner.mailbox.opens==0
