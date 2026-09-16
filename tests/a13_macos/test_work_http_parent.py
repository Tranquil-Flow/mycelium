"""Real authenticated HTTP + pinned TLS + native hello, no model tensors."""
import json
from dataclasses import asdict
from mycelium_a13_macos.app import DemoApp
from test_app import login,request
from test_app_authority import CONSENT
from test_work_acceptance_parent import arrange
from test_worker import SAFE


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
        code,_,raw=request(app,'/api/work/accept','POST',accepted,headers)
        result=json.loads(raw)
        assert code==200,result
        assert result['worker_running'] is True
        assert result['model_load_authorized'] is False
        code,_,raw=request(app,'/api/control/stop','POST',{},headers)
        result=json.loads(raw)
        assert code==200 and result['state']=='stopped'
        assert actor._runtime is None
    finally:app.close()
