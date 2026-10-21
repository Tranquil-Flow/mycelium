# SPDX-License-Identifier: AGPL-3.0-only
"""Full Python startup callback wiring, explicitly NO native qualification.

Real workflow/launch/registry-server/qualified-runtime/qualify/observer functions;
model route, qualification issuer, HTTP stack and persistent registry are fenced
in-memory dependencies. Token IDs below are synthetic fixtures, never model output.
"""
from pathlib import Path
from types import SimpleNamespace
import socket,subprocess,threading
import pytest
from mycelium_a13_macos import demo_chain,serving,workflow as wm
from mycelium_a13_macos.owner_run import OwnerRunError
from mycelium_a13_macos.run_exchange import RunExchange
from mycelium_live import supervisor as sup


@pytest.mark.parametrize('qualified',[True,False])
def test_real_launch_to_registry_qualifier_workflow_and_observer(tmp_path,monkeypatch,qualified):
 # Effect fences precede the first production dispatch. bind-only port checks
 # remain allowed, but no socket connect/process/model/native startup can run.
 def forbidden(*args,**kwargs):raise AssertionError('forbidden_transport_or_process')
 monkeypatch.setattr(socket.socket,'connect',forbidden)
 monkeypatch.setattr(socket.socket,'connect_ex',forbidden)
 monkeypatch.setattr(subprocess,'Popen',forbidden)
 class Clock:
  now=0
  def monotonic(self):return self.now
  def time(self):return 1000+self.now
 clock=Clock()
 monkeypatch.setattr(demo_chain.time,'monotonic',clock.monotonic)
 monkeypatch.setattr(demo_chain.time,'time',clock.time)
 entered=threading.Event();permits=[threading.Event(),threading.Event()]
 emitted=[threading.Event(),threading.Event()];finish=threading.Event();started=threading.Event()
 effects=[];failures=[]
 class Route:
  execution_graph=SimpleNamespace(deployment_id='deployment')
  startup_challenge=((11,),(21,22))
  def open(self):
   effects.append('route_open')
   return SimpleNamespace(deployment_id='deployment',model_id='synthetic-model',resolved_commit='synthetic-revision')
  def infer_startup_challenge(self,*,request_id,sink):
   entered.set()
   for index,token in enumerate((21,22)):
    assert permits[index].wait(5),'fixture_token_barrier_timeout'
    sink.emit(index,token);emitted[index].set()
   assert finish.wait(5),'fixture_finish_barrier_timeout'
   return SimpleNamespace(token_ids=(21,22) if qualified else (21,23))
  def counters(self):return SimpleNamespace(fatal=None)
  def live_attestation(self,*,request_id):return {'synthetic_fixture':True}
  def release_request(self,request_id):effects.append('request_released')
  def set_deployment_qualification(self,qualification,*,quantization):effects.append('fixture_qualification_bound')
  def set_stop_token_ids(self,ids):assert ids==frozenset()
  def close(self):effects.append('route_closed')
  def cleanup(self):effects.append('route_cleanup')
 route=Route()
 monkeypatch.setattr(sup.PhysicalLiveRoute,'from_operator_plan',lambda *a,**k:route)
 projection=SimpleNamespace(deployment_id='deployment',model_id='synthetic-model',model_revision='synthetic-revision',quantization='none')
 monkeypatch.setattr(sup,'native_deployment_projection',lambda p:projection)
 monkeypatch.setattr(sup,'_validate_route_identity',lambda identity,graph:None)
 monkeypatch.setattr(sup,'issue_live_route_qualification',lambda *a,**k:SimpleNamespace(issued_at_unix_ms=1))
 monkeypatch.setattr(sup,'_deployment_from_plan',lambda p:tmp_path)
 monkeypatch.setattr(sup,'prompt_codec_from_deployment',lambda p:SimpleNamespace(stop_token_ids=frozenset()))
 for name in ['_placement_projection','_topology_projection','_workload_comparison','_m16_performance_budget','_m17_model_operation','_m18_replica_plan']:
  monkeypatch.setattr(sup,name,lambda *a,**k:None)
 monkeypatch.setattr(sup,'_historical_evidence',lambda *a,**k:())
 def unavailable(deployment,exc,*,unavailable):
  assert str(exc)=='startup_challenge_failed'
  unavailable.append(SimpleNamespace(reason_code='startup_challenge_failed'))
 monkeypatch.setattr(sup,'_record_unavailable_deployment',unavailable)
 class Registry:
  def __init__(self,runtimes,*,unavailable_deployments,state_path,qualification_refresher):self.runtimes=runtimes
  def public_status(self):return {}
  def close(self):
   effects.append('registry_closed')
   for runtime in self.runtimes:runtime.route.close()
 monkeypatch.setattr(sup,'LiveDeploymentRegistry',Registry)
 monkeypatch.setattr(sup,'ArtifactAcquisitionStore',lambda p:SimpleNamespace())
 def stack(*,registry,bearer_token,product_state_root,seed_url,public_origin,trusted_https_proxy,trusted_proxy_capability):
  return SimpleNamespace(app=object())
 monkeypatch.setattr(sup,'build_registry_stack',stack)
 class Server:
  server_address=('127.0.0.1',45678)
  def handle_request(self):wf.stop_event.wait(.01)
  def server_close(self):effects.append('server_closed')
 monkeypatch.setattr(sup,'create_server',lambda **kwargs:Server())
 # Source/approval fixtures isolate model loading; real wrapper signatures and
 # actual run_registry_server remain in the producer->consumer call path.
 monkeypatch.setattr(serving,'_validate_execution_approval',lambda approval,prepared:approval)
 monkeypatch.setattr(serving,'_plan_digest',lambda path:'digest')
 monkeypatch.setattr(serving,'_validate_operator_plan',lambda *args:None)
 monkeypatch.setattr(serving,'_canonical_host_ids',lambda *args:())
 monkeypatch.setattr(serving,'_bind_existing_seed',lambda *args:(None,('fixture-seed','fixture-binding',1)))
 monkeypatch.setattr(serving,'_validate_dynamic_port',lambda port:None)
 monkeypatch.setattr(serving,'_assert_loopback_port_free',lambda port:None)
 monkeypatch.setattr(wm,'publish_controller_offers',lambda *args,**kwargs:({},object()))
 launch=serving.RegistryLaunch(prepared=None,operator_plan=tmp_path/'plan',
  private_state_root=tmp_path/'private',registry_state=tmp_path/'registry.json',
  seed_state_root=tmp_path/'seed',product_state_root=tmp_path/'product',
  operator_plan_digest='digest',physical_host_ids=(),seed_binding=('fixture-seed','fixture-binding',1),
  _runner=sup.run_registry_server)
 controller=SimpleNamespace(_run_plan={'run_id':'run','deployment_id':'deployment'},execute=lambda phase:None)
 wf=wm.ManagedServingWorkflow(authority=SimpleNamespace(),controller=controller,launch=launch)
 wf.bind_progress_identity(run_id='run',deployment_id='deployment',epoch=2)
 approval=SimpleNamespace(operator_plan_digest='digest',physical_host_ids=())
 def producer():
  try:assert wf.run(approval=approval,port=45678,on_started=lambda doc:started.set())==0
  except BaseException as exc:failures.append(type(exc).__name__);started.set()
 thread=threading.Thread(target=producer,name='mycelium-a13-inmemory-composition')
 exchange=RunExchange(SimpleNamespace(),clock=clock.time)
 app=SimpleNamespace(_chain_workflow=wf,_chain_epoch=2,_chain_compile_state={'state':'compiled','epoch':2},run_exchange=exchange)
 chain=demo_chain.AppDemoChain(app,budgets={'serving':3},interval=1)
 class Ctx:
  run_id='run';deployment_id='deployment'
  def __init__(self):
   self.state={'participants':[],'compile_state':{'epoch':2},'model':{'model_id':'synthetic-model','resolved_commit':'a'*40},'slice_mode':False,'manifest_digest':'sha256:'+'b'*64}
   self.result={}
  def progress(self,message):pass
  def wait_until(self,predicate,*,budget,code,interval):
   for _ in range(10):
    if clock.now==2:
     permits[0].set();assert emitted[0].wait(5),'first_fixture_event_missing'
     assert wf.status()['challenge_progress']=={'verified_token_count':1}
    if clock.now==4:
     permits[1].set();assert emitted[1].wait(5),'second_fixture_event_missing'
     assert wf.status()['challenge_progress']=={'verified_token_count':2}
    if clock.now==5:
     finish.set();assert started.wait(5),'fixture_registry_terminal_missing';assert not failures
    if predicate():return True
    clock.now+=interval
   pytest.fail('fixture observer never completed')
 ctx=Ctx();thread.start()
 try:
  assert entered.wait(5),'fixture route not reached'
  if qualified:
   chain._step_serving(ctx)
   assert clock.now==5 and ctx.result['registry_origin']=='http://127.0.0.1:45678'
   assert exchange.announcement()['phase']=='serving'
  else:
   with pytest.raises(OwnerRunError) as caught:chain._step_serving(ctx)
   assert caught.value.code=='route_qualification_failed' and clock.now==5
   assert exchange.announcement()['phase']=='idle' and ctx.result=={}
  assert effects.count('request_released')==1
  assert not failures
 finally:
  for permit in permits:permit.set()
  finish.set();wf.stop();thread.join(5)
  assert not thread.is_alive(),'owned_fixture_thread_not_reaped'
 assert wf.status()['challenge_progress'] is None
 assert effects.count('server_closed')==1 and effects.count('registry_closed')==1
 assert effects.count('route_closed')==1
