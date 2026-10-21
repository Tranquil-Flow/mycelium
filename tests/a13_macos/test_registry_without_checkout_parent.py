# SPDX-License-Identifier: AGPL-3.0-only
"""Actual registry startup without a source checkout; no model execution."""
import threading,json,urllib.request,urllib.error
from types import SimpleNamespace as NS
from mycelium_live import supervisor as s


def test_registry_can_explicitly_omit_source_governance(tmp_path,monkeypatch):
    def no_model(*args,**kwargs):raise RuntimeError('test_route_unavailable')
    monkeypatch.setattr(s,'_qualified_runtime',no_model)
    monkeypatch.setattr(s,'native_deployment_projection',lambda plan:NS(
        deployment_id='a13-no-model',model_id='Qwen/Qwen2.5-7B-Instruct',
        model_revision='a09a35458c702b33eeacc393d103063234e8bc28',quantization='int8-weight-only',topology_size=2))
    def forbidden(*args,**kwargs):raise AssertionError('source_checkout_must_not_be_read')
    monkeypatch.setattr(s,'governance_readiness',forbidden)
    (tmp_path/'private-state').mkdir(mode=0o700)
    stop=threading.Event();started=[];responses=[];threads=[]
    def observe(document):
        started.append(document)
        def request():
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(document['url']+'__mycelium/governance-readiness',timeout=3) as response:
                    responses.append(response.status)
            except urllib.error.HTTPError as error:
                responses.append((error.code,json.load(error)))
            finally:stop.set()
        thread=threading.Thread(target=request,daemon=True);threads.append(thread);thread.start()
    assert s.run_registry_server(operator_plans=(tmp_path/'inert-plan',),host='127.0.0.1',port=0,
        seed_state_root=tmp_path/'private-state',governance_root=None,api_only=True,
        stop_event=stop,on_started=observe)==0
    assert len(started)==1
    assert started[0]['startup_qualification_complete'] is False
    assert started[0]['deployment_count']==0

    for thread in threads:thread.join(4)
    assert responses==[(404,{'error':'governance_readiness_unavailable'})]
