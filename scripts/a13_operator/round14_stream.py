# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the existing product API and SSE over the live physical route."""
from pathlib import Path
import json,sys,time,http.cookiejar,urllib.request
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from round14_rpc import owner,PRIVATE
from mycelium_request_gateway.contracts import InferenceSubmission,QualificationBinding
from mycelium_ui_gateway.validation import validate_qualification,validate_stream_event

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def main():
    state=owner('/api/serving/status',{})
    serving=state.get('serving') or {}
    assert serving.get('state')=='api_running', 'serving_not_running'
    startup=serving['startup'];base=startup['url'].rstrip('/')
    assert base.startswith('http://127.0.0.1:'), 'registry_origin_invalid'
    scope=json.loads((PRIVATE/'current-scope.json').read_text())
    folder=PRIVATE/('stream-'+scope['run_id']);folder.mkdir(mode=0o700,exist_ok=False)
    def save(name,value):
        p=folder/name;p.write_text(json.dumps(value,sort_keys=True,indent=2));p.chmod(0o600)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    headers={'Origin':base,'Accept':'application/json','Content-Type':'application/json'}
    def request(path,body=None,extra=None):
        assert path.startswith('/') and not path.startswith('//')
        req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={**headers,**(extra or {})})
        return opener.open(req,timeout=300)
    def read(path,body=None):
        with request(path,body) as response:return json.load(response)
    save('startup.json',startup)
    save('deployments-before.json',read('/__mycelium/deployments'))
    assert startup['startup_qualification_complete'] is True,'qualification_not_complete'
    session=read('/api/v1/bootstrap')['session']
    headers[session['csrf_header']]=session['csrf_token']
    qualification=read('/api/v1/qualification/current');validate_qualification(qualification)
    save('qualification.json',qualification)
    assert qualification['route_ready'] is True and qualification['evidence_class']=='physical_qualification','physical_route_not_ready'
    save('runtime-before.json',read('/__mycelium/evidence/runtime'))
    submission=InferenceSubmission(prompt='The capital of Germany is',max_new_tokens=8,qualification=QualificationBinding.from_dict(qualification['binding']))
    accepted=read('/api/v1/inference',submission.to_dict());save('accepted.json',accepted)
    events=[];terminals=[];started=time.monotonic()
    try:
        with request(accepted['event_path'],extra={'Accept':'text/event-stream'}) as response:
            assert response.headers.get_content_type()=='text/event-stream'
            with (folder/'events.jsonl').open('x') as log:
                (folder/'events.jsonl').chmod(0o600)
                for raw in response:
                    if raw.startswith(b'data: '):
                        event=json.loads(raw[6:]);validate_stream_event(event,accepted['request_id'])
                        if events:assert event['sequence']==events[-1]['sequence']+1,'stream_sequence_gap'
                        events.append(event);log.write(json.dumps(event)+'\n');log.flush()
                        if event['type'] in ('completed','cancelled','failed'):terminals.append(event)
    except BaseException:
        # CAPTURE THE POST-FAILURE STATE WHILE THE APP IS STILL UP. soak04 (2026-09-19) hung here for
        # the client's own 300 s read timeout and left NO runtime-after.json, so the route's incident
        # ledger -- the one artifact that names WHY no terminal event was published -- was unreadable
        # afterwards: incidents live in the app's memory, and the round quits the apps at teardown
        # (measured: no persisted evidence store under the owner root, native-exit written 11:41 while
        # the failure was 11:28). The step still fails exactly as it did; it now fails with evidence.
        for name,path in (('runtime-after.json','/__mycelium/evidence/runtime'),
                          ('deployments-after.json','/__mycelium/deployments')):
            try:
                save(name,read(path))
            except Exception as capture_error:
                print('POST_FAILURE_CAPTURE_FAILED %s %r' % (name,capture_error),flush=True)
        try:
            print('POST_FAILURE_SERVING %s' % json.dumps(owner('/api/serving/status',{}),sort_keys=True),flush=True)
        except Exception as capture_error:
            print('POST_FAILURE_SERVING_FAILED %r' % (capture_error,),flush=True)
        raise
    save('runtime-after.json',read('/__mycelium/evidence/runtime'))
    save('deployments-after.json',read('/__mycelium/deployments'))
    summary={'run_id':scope['run_id'],'request_id':accepted['request_id'],'event_count':len(events),'token_events':sum(e['type']=='token' for e in events),'terminals':terminals,'seconds':time.monotonic()-started,'generated_text':''.join(e['text'] for e in events if e['type']=='token')}
    save('summary.json',summary);print(json.dumps(summary),flush=True)
    assert len(terminals)==1 and terminals[0]['type']=='completed','request_not_completed'
    assert summary['token_events']>0,'no_streamed_tokens'

if __name__=='__main__':main()
