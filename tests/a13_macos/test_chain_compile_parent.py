"""Compiler orchestration unit seams; not model execution or hardware proof."""
from types import SimpleNamespace as NS
import time,threading
import pytest
from test_serving import _native_snapshot
from mycelium_a13_macos.serving import prepare_local_source
from mycelium_a13_macos import chain_compile as cc


def fixture_app(tmp_path,monkeypatch):
    from scripts import build_qwen_live_route as builder
    prepared=prepare_local_source(_native_snapshot(tmp_path/'source'))
    members=[{'node_id':n,'generation':1,'incarnation':'inc','verification_key_digest':'key',
              'endpoint_id':'endpoint','lease_expires_at':time.time()+300} for n in ('a','b')]
    facts={m['node_id']:{**{k:'fixture' for k in builder._TOPOLOGY_FIELDS},
        'source_root':str(prepared.source_root),'manifest_digest':prepared.manifest_digest,
        'node_id':m['node_id'],'activation_endpoint_id':m['node_id']*64} for m in members}
    channel=object()
    context=NS(retire_bootstrap=lambda:None,run_id='test-compile',deployment_id='test-deployment',members=members,facts=facts,
        factory=NS(owner=channel),observations={'observations':[{'host_id':'one'},{'host_id':'two'}]})
    app=NS(root=tmp_path/'app',guard=threading.RLock(),closed=threading.Event(),installation_role='owner',running=True,_resource_check=lambda:None,
        acquisition=NS(prepared_source=lambda:prepared),_observation_context=context,
        authority=NS(coordinator=NS(member=lambda n:dict(next(m for m in members if m['node_id']==n)),
            signer=NS(public_key_record=lambda:{}))),_chain_target_keys=[])
    monkeypatch.setattr(cc,'fresh_operator_template',lambda *args:{})
    monkeypatch.setattr(builder,'build',lambda args:None)
    def prepare(**kwargs):
        assert kwargs['channel'] is channel
        return NS(launch=NS(operator_plan_digest='unit-digest',physical_host_ids=('one','two')))
    monkeypatch.setattr(cc,'prepare_serving_workflow',prepare)
    monkeypatch.setattr(cc,'owner_channel',lambda *args:(_ for _ in ()).throw(AssertionError('second_owner_channel')))
    return app,prepared


def test_compilation_reuses_existing_owned_channel(tmp_path,monkeypatch):
    app,_=fixture_app(tmp_path,monkeypatch)
    cc.start_compile(app,acknowledged=True)
    app._chain_compile_thread.join(10)
    assert not app._chain_compile_thread.is_alive()
    assert app._chain_compile_state['state']=='compiled'
    import json
    topology=json.loads((app.root/'chain-runs/test-compile/topology.json').read_text())
    assert [n['endpoint_id'] for n in topology['nodes']]==['a'*64,'b'*64]


@pytest.mark.parametrize('change',['manifest','lease','endpoint'])
def test_compile_rechecks_source_and_live_binding(tmp_path,monkeypatch,change):
    app,prepared=fixture_app(tmp_path,monkeypatch)
    if change=='manifest':app._observation_context.facts['a']['manifest_digest']='changed'
    else:
        original=app.authority.coordinator.member
        def member(node):
            result=original(node)
            if change=='lease':result['lease_expires_at']=time.time()-1
            else:result['endpoint_id']='changed'
            return result
        app.authority.coordinator.member=member
    with pytest.raises(ValueError,match='compiler_(model|member)_changed'):
        cc.start_compile(app,acknowledged=True)
    assert not hasattr(app,'_chain_compile_thread')


def test_shutdown_during_build_cannot_create_runtime(tmp_path,monkeypatch):
    import threading
    from scripts import build_qwen_live_route as builder
    app,_=fixture_app(tmp_path,monkeypatch);app.closed=threading.Event()
    monkeypatch.setattr(builder,'build',lambda args:app.closed.set())
    cc.start_compile(app,acknowledged=True);app._chain_compile_thread.join(10)
    assert not hasattr(app,'_chain_workflow')
    assert app._chain_compile_state['state']=='failed'


def test_app_close_drains_serving_before_owned_target(tmp_path,monkeypatch):
    import threading
    from mycelium_a13_macos.app import DemoApp
    app=DemoApp(tmp_path/'close-app').start();events=[];stop=threading.Event()
    def stopped():events.append('workflow-stop');stop.set()
    app._chain_workflow=NS(stop=stopped)
    def serving():
        stop.wait(3);events.append('workflow-exit')
    worker=threading.Thread(target=serving);app._chain_serving_thread=worker;worker.start()
    monkeypatch.setattr(app,'_stop_local_contributor',lambda:events.append('target-stop'))
    try:
        app.close()
        assert events[:3]==['workflow-stop','workflow-exit','target-stop']
        assert not worker.is_alive()
    finally:stop.set();worker.join(5);app.close()


def test_compile_requires_target_confirmed_bootstrap_retirement(tmp_path,monkeypatch):
    app,_=fixture_app(tmp_path,monkeypatch)
    def refuse():raise ValueError('offer_cleanup_unconfirmed')
    app._observation_context.retire_bootstrap=refuse
    with pytest.raises(ValueError,match='offer_cleanup_unconfirmed'):
        cc.start_compile(app,acknowledged=True)
    assert not hasattr(app,'_chain_compile_thread')
