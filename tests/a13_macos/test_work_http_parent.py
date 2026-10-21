# SPDX-License-Identifier: AGPL-3.0-only
"""Real authenticated HTTP + pinned TLS + native hello, no model tensors."""
import http.client
import json
import time
from dataclasses import asdict
from mycelium_a13_macos.app import DemoApp
from test_app import login,request
from test_app_authority import CONSENT
from test_work_acceptance_parent import arrange
from test_worker import SAFE


# The accept request is the native start (admitted staging, managed control origin,
# sidecar hello). test_app.request pins a 3 s socket timeout, which is right for the
# consent-rejection probes below but was the recorded L2 tail timeout under -n8
# self-load. This is an explicit, bounded deadline for the native start only; no
# consent-rejection assertion is relaxed.
NATIVE_START_DEADLINE_S = 120.0


def request_with_deadline(app,path,method='POST',body=None,headers=None,*,deadline_s):
    """Same authenticated POST as test_app.request, with an explicit deadline."""
    h=dict(headers or {})
    if body is not None:
        body=json.dumps(body)
        h.setdefault('Content-Type','application/json')
    connection=http.client.HTTPConnection('127.0.0.1',app.port,timeout=deadline_s)
    connection.request(method,path,body=body,headers=h)
    response=connection.getresponse()
    result=(response.status,dict(response.getheaders()),response.read())
    connection.close()
    return result


def test_work_http_exact_consent_rejections_and_native_start(tmp_path):
    app=DemoApp(tmp_path/'owner',check_platform=False).start()
    try:
        app.action('/api/create',{'consent':CONSENT})
        app.action('/api/local-contributor/start',{})
        actor=app.local_contributor
        assert actor is not None
        _,offer,options=arrange({'target':actor.peer,'authority':app.authority,
            'member':app.authority.coordinator.member(actor.member.node_id)})
        app._policy_observer=lambda:dict(SAFE)
        limits=asdict(options['limits'])
        body={'limits':limits,'stage_acknowledged':True}
        # Arrange the staged bytes through the same product path the HTTP
        # preview uses, before any timed request. This test's contract is the
        # exact consent/native-start semantics over authenticated HTTP -- not
        # host performance: the per-request staging child
        # (`python -B -m mycelium_a13_macos.managed_staging prepare`, 60 s
        # subprocess budget) was measured at ~93% of the first preview
        # request's wall cost and is the recorded L2 batch-13 tail timeout.
        # The request under test still re-verifies the staged bytes, marker
        # and offer binding (TargetStaging.prepare verify path).
        from mycelium_a13_macos.work_acceptance import preview_work
        preview_work(actor,offer,stage_acknowledged=True,**options)
        assert request(app,'/api/work/preview','POST',body)[0]==401
        headers=login(app)
        code,_,raw=request(app,'/api/work/preview','POST',body,headers)
        preview=json.loads(raw)
        assert code==200,preview
        assert actor._runtime is None
        accepted={'limits':limits,'preview':preview,'work_acknowledged':True}
        assert request(app,'/api/work/accept','POST',accepted)[0]==401
        bad={**accepted,'preview':{**preview,'consent_digest':'sha256:'+'0'*64}}
        assert request(app,'/api/work/accept','POST',bad,headers)[0]!=200
        assert actor._runtime is None
        code,_,raw=request_with_deadline(app,'/api/work/accept','POST',accepted,headers,
            deadline_s=NATIVE_START_DEADLINE_S)
        result=json.loads(raw)
        assert code==200,result
        # Event-driven readiness: poll the attached runtime (the native-start signal)
        # up to the same explicit deadline -- never a fixed short sleep.
        deadline=time.monotonic()+NATIVE_START_DEADLINE_S
        while actor._runtime is None and time.monotonic()<deadline:time.sleep(0.02)
        assert actor._runtime is not None
        assert result['worker_running'] is True
        assert result['model_load_authorized'] is False
        code,_,raw=request(app,'/api/control/stop','POST',{},headers)
        result=json.loads(raw)
        assert code==200 and result['state']=='stopped'
        assert actor._runtime is None
    finally:app.close()
