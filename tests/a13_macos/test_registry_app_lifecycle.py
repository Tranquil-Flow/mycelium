# SPDX-License-Identifier: AGPL-3.0-only
"""App-owned maintained server lifecycle; no models or physical qualification."""
from types import SimpleNamespace
import json
import threading
from urllib.request import urlopen
import pytest
import mycelium_live.supervisor as supervisor


def test_registry_server_can_be_owned_by_app_thread_and_reports_bound_port(tmp_path,monkeypatch):
    static=tmp_path/'static';static.mkdir();(static/'index.html').write_text('<html>lifecycle fixture, not product build</html>')
    (tmp_path/'product').mkdir(mode=0o700)
    monkeypatch.setattr(supervisor,'native_deployment_projection',lambda plan:SimpleNamespace(
        deployment_id='fixture',model_id='fixture-model',model_revision='fixture-revision',quantization='int8-weight-only',topology_size=2))
    def unavailable(*args,**kwargs):raise ValueError('fixture_model_execution_denied')
    monkeypatch.setattr(supervisor,'_qualified_runtime',unavailable)
    def unrelated_audit(*args, **kwargs):
        raise AssertionError('app lifecycle fixture must not run repository audits')
    monkeypatch.setattr(supervisor, 'governance_readiness', unrelated_audit)
    monkeypatch.setattr(supervisor.signal,'signal',lambda *args:pytest.fail('application worker must not install global signal handlers'))
    stop=threading.Event();ready=threading.Event();started=[];outcomes=[]
    def observed(document):started.append(document);ready.set()
    def run():
        try:
            outcomes.append(supervisor.run_registry_server(operator_plans=[tmp_path/'fixture-plan'],
                host='127.0.0.1',port=0,static_root=static,seed_state_root=tmp_path/'seed',
                product_state_root=tmp_path/'product',stop_event=stop,on_started=observed,
                governance_root=None))
        except BaseException as exc:outcomes.append(exc);ready.set()
    worker=threading.Thread(target=run,daemon=True);worker.start()
    try:
        assert ready.wait(10),'maintained server did not reach terminal startup outcome'
        assert started,repr(outcomes)
        document=started[0]
        assert document['startup_qualification_complete'] is False
        assert document['deployment_count']==0
        assert document['unavailable_deployment_count']==1
        assert ':0/' not in document['url']
        with urlopen(document['url']+'api/v1/bootstrap',timeout=3) as response:
            bootstrap=json.load(response)
        assert bootstrap['protocol']=='mycelium.product_ui.bootstrap.v1'
    finally:
        stop.set();worker.join(5)
    assert not worker.is_alive()
    assert outcomes==[0]
