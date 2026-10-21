# SPDX-License-Identifier: AGPL-3.0-only
"""CPU observer and deployment-identity regressions; no physical qualification.

The prior characterization is frozen in the parent verification lane. This
separate proposal requires mismatched/missing deployment reports to be ignored.
"""
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import pytest
from mycelium_a13_macos.run_exchange import RunExchange, MEMBER_REPORT

spec=importlib.util.spec_from_file_location('leaf_observer_controls',Path(__file__).with_name('test_serving_progress_offline_f.py'))
leaf=importlib.util.module_from_spec(spec);spec.loader.exec_module(leaf)


def prepared(monkeypatch,*,participants=()):
 clock,workflow,chain,ctx=leaf.setup(monkeypatch,participants=participants)
 exchange=RunExchange(SimpleNamespace(),clock=clock.time)
 chain.app.run_exchange=exchange
 ctx.state.update(model={'model_id':'synthetic-model','resolved_commit':'a'*40},
                  slice_mode=False,manifest_digest='sha256:'+'b'*64)
 return clock,workflow,chain,ctx,exchange


def report(clock,*,run_id='current-run',deployment_id='current-deployment',detail=None):
 return {'protocol':MEMBER_REPORT,'run_id':run_id,'deployment_id':deployment_id,
         'state':'work_running','detail':detail,'model':None,'progress':None,
         'descriptor':None,'issued_at':clock.time()}


def qualified(workflow):
 workflow._state='api_running'
 workflow._startup={'startup_qualification_complete':True,
                    'unavailable_deployment_count':0,'url':'http://127.0.0.1:55555/'}


def test_qualified_terminal_announces_with_real_exchange(monkeypatch):
 clock,workflow,chain,ctx,exchange=prepared(monkeypatch)
 qualified(workflow)
 chain._step_serving(ctx)
 assert ctx.polls==1 and clock.now==0
 assert ctx.result['registry_origin']=='http://127.0.0.1:55555'
 assert exchange.announcement()['phase']=='serving'
 assert exchange.announcement()['run_id']==ctx.run_id
 assert exchange.announcement()['deployment_id']==ctx.deployment_id


def test_distinct_workflow_phases_renew_before_qualified_completion(monkeypatch):
 clock,workflow,chain,ctx,exchange=prepared(monkeypatch)
 original=workflow.status
 def status():
  if clock.now>=5:qualified(workflow)
  else:workflow._state='publishing' if clock.now<2 else 'staging' if clock.now<4 else 'starting'
  return original()
 monkeypatch.setattr(workflow,'status',status)
 chain._step_serving(ctx)
 assert clock.now==5 and ctx.result['registry_origin']=='http://127.0.0.1:55555'
 assert exchange.announcement()['phase']=='serving'


def test_repeated_workflow_flapping_eventually_expires(monkeypatch):
 clock,workflow,chain,ctx,_=prepared(monkeypatch)
 original=workflow.status
 def status():
  workflow._state='starting' if int(clock.now)%2==0 else 'staging'
  return original()
 monkeypatch.setattr(workflow,'status',status)
 with pytest.raises(leaf.OwnerRunError) as caught:chain._step_serving(ctx)
 assert caught.value.code=='route_qualification_timeout' and clock.now==4


def test_validated_wrong_run_reports_do_not_renew(monkeypatch):
 clock,_,chain,ctx,exchange=prepared(monkeypatch,participants=('owner-node',))
 original=exchange.reports
 def reports():
  exchange.submit_local('owner-node',report(clock,run_id='old-run',detail='phase_'+str(int(clock.now))))
  return original()
 monkeypatch.setattr(exchange,'reports',reports)
 leaf.assert_timeout(chain,ctx)


@pytest.mark.parametrize('state',['failed','stopped'])
def test_terminal_workflow_error_preserved_without_waiting(monkeypatch,state):
 clock,workflow,chain,ctx,_=prepared(monkeypatch)
 workflow._state=state;workflow._error='synthetic_workflow_error'
 with pytest.raises(leaf.OwnerRunError) as caught:chain._step_serving(ctx)
 assert caught.value.code=='synthetic_workflow_error' and clock.now==0 and ctx.result=={}


@pytest.mark.parametrize('deployment',['other-deployment',None])
def test_current_run_other_or_missing_deployment_is_rejected(monkeypatch,deployment):
 clock,_,chain,ctx,exchange=prepared(monkeypatch,participants=('owner-node',))
 exchange.submit_local('owner-node',report(clock,deployment_id=deployment))
 assert exchange.reports()['owner-node']['deployment_id']==deployment
 assert 'owner-node' not in chain._reports(ctx)


@pytest.mark.parametrize('deployment',['other-deployment',None])
def test_other_or_missing_deployment_cannot_satisfy_member_readiness(monkeypatch,deployment):
 clock,_,chain,ctx,exchange=prepared(monkeypatch,participants=('owner-node',))
 value=report(clock,deployment_id=deployment);value['state']='serving'
 exchange.submit_local('owner-node',value)
 assert chain._all_in(ctx,{'serving'}) is False


def test_matching_run_and_deployment_remain_visible_and_ready(monkeypatch):
 clock,_,chain,ctx,exchange=prepared(monkeypatch,participants=('owner-node',))
 exchange.submit_local('owner-node',report(clock))
 assert chain._reports(ctx)['owner-node']['deployment_id']==ctx.deployment_id
 assert chain._all_in(ctx,{'work_running'}) is True
 chain._publish_participants(ctx)
 assert ctx.result['participants'][0]['state']=='work_running'


@pytest.mark.parametrize('run_id,deployment_id',[('current-run','other-deployment'),('old-run','current-deployment')])
def test_participant_snapshot_does_not_misattribute_other_identity(monkeypatch,run_id,deployment_id):
 clock,_,chain,ctx,exchange=prepared(monkeypatch,participants=('owner-node',))
 exchange.submit_local('owner-node',report(clock,run_id=run_id,deployment_id=deployment_id,detail='synthetic_detail'))
 chain._publish_participants(ctx)
 value=ctx.result['participants'][0]
 assert value['state'] is None and value['detail'] is None and value['progress'] is None
