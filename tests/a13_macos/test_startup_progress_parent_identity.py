# SPDX-License-Identifier: AGPL-3.0-only
"""Parent regressions for missing/replaced/retired authority at serving observer."""
from types import SimpleNamespace
import pytest
from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_a13_macos.owner_run import OwnerRunError
from mycelium_a13_macos.run_exchange import RunExchange
from mycelium_a13_macos.workflow import ManagedServingWorkflow


@pytest.mark.parametrize('case',['replacement_unbound','missing_context_epoch','compiled_boolean_epoch','app_boolean_epoch','stopped_workflow'])
def test_new_compile_authority_cannot_downgrade_to_unbound_or_retired_workflow(case):
 wf=ManagedServingWorkflow(authority=None,controller=None,launch=None)
 wf.bind_progress_identity(run_id='run',deployment_id='deployment',epoch=1)
 wf._state='api_running';wf._startup={'startup_qualification_complete':True,'unavailable_deployment_count':0,'url':'http://127.0.0.1:45678/'}
 exchange=RunExchange(SimpleNamespace())
 app=SimpleNamespace(_chain_workflow=wf,_chain_epoch=1,_chain_compile_state={'state':'compiled','epoch':1},run_exchange=exchange)
 state={'participants':[],'compile_state':{'epoch':1},'model':{'model_id':'synthetic-model','resolved_commit':'a'*40},'slice_mode':False,'manifest_digest':'sha256:'+'b'*64}
 ctx=SimpleNamespace(run_id='run',deployment_id='deployment',state=state,result={},progress=lambda value:None,wait_until=lambda predicate,**kwargs:predicate())
 if case in {'replacement_unbound','missing_context_epoch'}:
  replacement=ManagedServingWorkflow(authority=None,controller=None,launch=None)
  replacement._state='api_running';replacement._startup=dict(wf._startup)
  app._chain_workflow=replacement
  if case=='missing_context_epoch':state['compile_state']={}
 elif case=='compiled_boolean_epoch':app._chain_compile_state['epoch']=True
 elif case=='app_boolean_epoch':app._chain_epoch=True
 elif case=='stopped_workflow':wf.stop()
 with pytest.raises(OwnerRunError) as caught:AppDemoChain(app)._step_serving(ctx)
 assert caught.value.code=='serving_workflow_identity_changed'
 assert ctx.result=={} and exchange.announcement()['phase']=='idle'
