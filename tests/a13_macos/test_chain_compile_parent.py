# SPDX-License-Identifier: AGPL-3.0-only
"""Compiler orchestration unit seams; not model execution or hardware proof."""
from types import SimpleNamespace as NS
import json,time,threading
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
        def bind_progress_identity(**identity):
            assert identity == {'run_id':context.run_id,'deployment_id':context.deployment_id,
                                'epoch':getattr(app,'_chain_epoch',0)}
        return NS(launch=NS(operator_plan_digest='unit-digest',physical_host_ids=('one','two')),
                  bind_progress_identity=bind_progress_identity)
    monkeypatch.setattr(cc,'prepare_serving_workflow',prepare)
    monkeypatch.setattr(cc,'owner_channel',lambda *args:(_ for _ in ()).throw(AssertionError('second_owner_channel')))
    return app,prepared


def test_completion_is_private_and_only_published_after_real_producer_guard_exits(tmp_path,monkeypatch):
    app,_=fixture_app(tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event();exited=threading.Event();lock=app.guard
    class Guard:
        def __enter__(self):
            lock.acquire();entered.set();return self
        def __exit__(self,*args):lock.release();exited.set()
    app.guard=Guard()
    atomic=cc._atomic_private_json
    def ordered(path,document):
        if path.name=='compile-complete.private.json':
            assert exited.is_set(), 'actual compiler guard must exit before evidence publication'
        return atomic(path,document)
    monkeypatch.setattr(cc,'_atomic_private_json',ordered)
    original=cc.prepare_serving_workflow
    def held(**kwargs):
        assert release.wait(10), 'test did not release actual compiler guard'
        return original(**kwargs)
    monkeypatch.setattr(cc,'prepare_serving_workflow',held)
    root=app.root/'chain-runs/test-compile'
    try:
        cc.start_compile(app,acknowledged=True)
        assert entered.wait(10)
        assert app._chain_compile_state['state']=='compiling'
        assert not (root/'compile-complete.private.json').exists()
    finally:
        release.set();app._chain_compile_thread.join(10)
    assert not app._chain_compile_thread.is_alive()
    assert app._chain_compile_state['state']=='compiled'
    marker=root/'compile-complete.private.json'
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_text()) == {
        'protocol':'mycelium.chain_compile_complete.v1','run_id':'test-compile',
        'deployment_id':'test-deployment','epoch':0,'state':'compiled'}


def test_completion_publication_failure_is_not_compiled(tmp_path,monkeypatch):
    app,_=fixture_app(tmp_path,monkeypatch)
    original=cc._atomic_private_json
    def fail(path,document):
        if path.name=='compile-complete.private.json':raise OSError('injected publication failure')
        return original(path,document)
    monkeypatch.setattr(cc,'_atomic_private_json',fail)
    cc.start_compile(app,acknowledged=True);app._chain_compile_thread.join(10)
    assert app._chain_compile_state['state']=='failed'
    root=app.root/'chain-runs/test-compile'
    assert not (root/'compile-complete.private.json').exists()
    assert (root/'compile-error.private.log').exists()


@pytest.mark.parametrize('publication_fails',[False,True])
def test_serving_cannot_start_before_completion_publication(tmp_path,monkeypatch,publication_fails):
    """A published workflow must not grant serving before the durable write succeeds."""
    app,_=fixture_app(tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event();launched=threading.Event()
    original=cc._atomic_private_json
    def held(path,document):
        if path.name=='compile-complete.private.json':
            entered.set()
            assert release.wait(10), 'publication seam not released'
            if publication_fails:raise OSError('injected completion publication failure')
        return original(path,document)
    monkeypatch.setattr(cc,'_atomic_private_json',held)
    try:
        cc.start_compile(app,acknowledged=True)
        assert entered.wait(10)
        app._chain_workflow.run=lambda **kwargs:launched.set()
        proposal=json.loads(json.dumps(cc.asdict(app._chain_approval)))
        with app.guard:
            with pytest.raises(ValueError,match='model_execution_proposal_changed'):
                cc.start_serving(app,proposal=proposal,acknowledged=True)
        assert not launched.is_set()
        assert getattr(app,'_chain_serving_thread',None) is None
    finally:
        release.set();app._chain_compile_thread.join(10)
        serving=getattr(app,'_chain_serving_thread',None)
        if serving is not None:serving.join(10)
    assert not app._chain_compile_thread.is_alive()
    if publication_fails:
        assert app._chain_compile_state['state']=='failed'
        assert not launched.is_set()
    else:
        assert app._chain_compile_state['state']=='compiled'
        cc.start_serving(app,proposal=proposal,acknowledged=True)
        app._chain_serving_thread.join(10)
        assert launched.is_set(), 'successful publication still admits serving'


def test_superseded_compile_never_publishes_completion(tmp_path,monkeypatch):
    app,_=fixture_app(tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event()
    original=cc.prepare_serving_workflow
    def held(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)
    monkeypatch.setattr(cc,'prepare_serving_workflow',held)
    cc.start_compile(app,acknowledged=True)
    try:
        assert entered.wait(10)
        app._chain_epoch+=1  # stop_serving/rebuild's supersession convention
    finally:
        release.set();app._chain_compile_thread.join(10)
    assert not app._chain_compile_thread.is_alive()
    root=app.root/'chain-runs/test-compile'
    assert not (root/'compile-complete.private.json').exists()
    assert (root/'compile-error.private.log').exists()


def test_stop_invalidates_old_completion(tmp_path,monkeypatch):
    app,_=fixture_app(tmp_path,monkeypatch)
    cc.start_compile(app,acknowledged=True);app._chain_compile_thread.join(10)
    root=app.root/'chain-runs/test-compile'
    assert (root/'compile-complete.private.json').exists()
    app._chain_workflow.stop=lambda:None
    cc.stop_serving(app)
    assert not (root/'compile-complete.private.json').exists()


def test_real_compiler_guard_and_observer_never_handshake_on_early_plan(tmp_path,monkeypatch):
    """A real start_compile worker holds its guard; the operator's own wait must stay observational."""
    import importlib,sys
    app,_=fixture_app(tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event();observed=threading.Event()
    again=threading.Event();ack=threading.Event();blocked=threading.Event()
    early=app.root/'authority/serving-runs/test-compile/operator-plan.json'
    original=cc.prepare_serving_workflow
    def held(**kwargs):
        early.parent.mkdir(parents=True)
        early.write_text('{}')   # the actual workflow's early publication point, under app.guard
        entered.set()
        assert release.wait(15)
        return original(**kwargs)
    monkeypatch.setattr(cc,'prepare_serving_workflow',held)
    from pathlib import Path
    operator=str(Path(__file__).resolve().parents[2]/'scripts/a13_operator')
    monkeypatch.syspath_prepend(operator)
    monkeypatch.setenv('A13_OWNER_ROOT',str(app.root))
    monkeypatch.setenv('A13_REMOTE_ROOT',str(tmp_path/'remote'))
    monkeypatch.setenv('A13_TARGET_PREVIEW','probe')
    monkeypatch.setenv('A13_RPC_ROUND','probe')
    sys.modules.pop('round14_rpc',None)
    rpc=importlib.import_module('round14_rpc')
    scope=rpc.PRIVATE/'current-scope.json'
    scope.write_text(json.dumps({'run_id':'test-compile','deployment_id':'test-deployment'}))
    monkeypatch.setattr(rpc,'require_local',lambda *a,**k:None)
    monkeypatch.setattr(rpc,'local_state',lambda *a,**k:{'present':True})
    monkeypatch.setattr(rpc,'_readiness_retry_s',lambda:0.001)
    class Client:
        def __init__(self,root):
            if not release.is_set():blocked.set();again.set()
        def call(self,path,body=None):return {'ok':path}
    monkeypatch.setattr(rpc,'AppClient',Client)
    first=rpc._compile_evidence
    count=[0]
    def watched(run_id):
        result=first(run_id)
        if early.exists() and not release.is_set():
            count[0]+=1
            if count[0]==1:
                observed.set()
                assert ack.wait(10)
            elif count[0]==2:again.set()
        return result
    monkeypatch.setattr(rpc,'_compile_evidence',watched)
    result=[]
    thread=None
    try:
        cc.start_compile(app,acknowledged=True)
        assert entered.wait(10)
        thread=threading.Thread(target=lambda:result.append(rpc.owner('/api/serving/status')))
        thread.start()
        assert observed.wait(10), 'observer reached early plan while actual compiler owns guard'
        assert not blocked.is_set()
        ack.set()
        assert again.wait(10) or blocked.is_set(), 'observer did not continue polling'
        assert not blocked.is_set(), 'no handshake while actual producer holds app.guard'
    finally:
        ack.set();release.set();app._chain_compile_thread.join(10)
        if thread is not None:thread.join(10)
        sys.modules.pop('round14_rpc',None)
    assert result==[{'ok':'/api/serving/status'}]
    assert app._chain_compile_state['state']=='compiled'


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
