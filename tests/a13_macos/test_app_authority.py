# SPDX-License-Identifier: AGPL-3.0-only
"""Real app → canonical seed → pinned HTTPS enrollment composition; no model."""
import json
import pytest
from mycelium_a13_macos.app import DemoApp
from test_app import login, request

CONSENT={'peer_visibility':True,'power':'ac_only','storage_bytes':2_000_000_000,
         'network':'unmetered_only','background':False,'diagnostics':False,'thermal':'stop_on_serious'}


def test_real_http_create_enroll_restart_and_revoke(tmp_path):
    root=tmp_path/'app'
    app=DemoApp(root,check_platform=False).start()
    try:
        headers=login(app)
        failures=[]
        original_action=app.action
        def observe_action(path,body):
            try: return original_action(path,body)
            except Exception as error:
                import traceback
                failures.append((type(error).__name__,getattr(error,'code','no_public_code'),
                    [(frame.name,frame.lineno) for frame in traceback.extract_tb(error.__traceback__)]))
                raise
        app.action=observe_action
        assert request(app,'/api/capability/report','POST',{},headers)[0]==409
        result=request(app,'/api/create','POST',{'consent':CONSENT},headers)
        assert result[0]==200, failures
        status=json.loads(request(app,'/api/status',headers=headers)[2])
        assert status['authority']['membership_state']=='current'
        assert status['role']=='not_yet_serving' and not status['serving']['route_ready']
        member=app.authority.member
        identity=member.signer.verification_key_digest
        generation=member.generation
        assert request(app,'/api/create','POST',{'consent':CONSENT},headers)[0]==200
        assert app.authority.member.generation==generation
        changed={**CONSENT,'storage_bytes':4_000_000_000}
        assert request(app,'/api/create','POST',{'consent':changed},headers)[0]==400
        report=request(app,'/api/capability/report','POST',{},headers)
        assert report[0]==200, failures
        capability=json.loads(report[2])
        assert capability['state']=='self_report_accepted'
        assert capability['generation']==generation
        assert capability['backends']==['mlx']
        assert capability['independently_qualified'] is False
        status=json.loads(request(app,'/api/status',headers=headers)[2])
        assert status['authority']['capability_state']=='self_report_accepted'
        assert not status['serving']['route_ready']
        reports=list((root/'authority'/'target'/'capability-reports').glob('*.json'))
        assert len(reports)==1
        evidence=json.loads(reports[0].read_text())
        assert evidence['receipt']['accepted_message_id']==evidence['report']['message']['message_id']
        assert reports[0].stat().st_mode & 0o777 == 0o600
        assert request(app,'/api/capability/report','POST',{'backends':['invented']},headers)[0]==400
        app.authority.renew()
        assert app.authority.member.generation==generation
        assert request(app,'/api/stop','POST',{},headers)[0]==200
        assert request(app,'/api/start','POST',{},headers)[0]==200
        assert app.authority.member.signer.verification_key_digest==identity
        assert app.authority.status()['capability_state']=='not_reported'
        member=app.authority.member
        assert request(app,'/api/revoke-self','POST',{'confirm':False},headers)[0]==400
        assert app.authority.status()['membership_state']=='current'
        assert request(app,'/api/revoke-self','POST',{'confirm':True},headers)[0]==200
        assert app.authority.status()['membership_state']=='revoked'
        assert request(app,'/api/capability/report','POST',{},headers)[0]==409
        with pytest.raises(Exception): app.authority.renew()
    finally: app.close()
