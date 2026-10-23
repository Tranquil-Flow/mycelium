# SPDX-License-Identifier: AGPL-3.0-only
"""Real loopback HTTP/SSE + shipped chat page over the native desk route.

Only owner run metadata/qualification and compute are fixtures. No installed app,
public relay, model, or device is used. The normal auth/CSRF checks stay enabled.
"""
import asyncio
import json
import re
from pathlib import Path
import secrets
import subprocess
from types import SimpleNamespace
import urllib.request
import time

from mycelium_a13_macos.chat import CSP, ChatService, handle
from mycelium_demo.gateway_control_runtime import LoopbackASGIServer
from mycelium_request_gateway import RequestGatewayASGIApplication, StaticBearerAuthenticator
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


def complete_http_chat(*, service, graph, failure, browser=False):
    credential=secrets.token_urlsafe(32)
    async def unused(*args):raise AssertionError('unrelated upstream must not be used')
    gateway=create_product_gateway_application(config=GatewayConfig(),observatory_app=unused,
        request_gateway_app=RequestGatewayASGIApplication(service,authenticator=StaticBearerAuthenticator(credential)),
        swarm_coordinator=SimpleNamespace(),request_gateway_bearer_token=credential)
    participants=[dict(label='Desk stage '+str(i),node_id=p.node_id,
        layers=[s.layer_range.start_layer,s.layer_range.end_layer_exclusive])
        for i,s in enumerate(graph.stages) for p in s.placements]
    app=SimpleNamespace(installation_role='owner',chat=None,
        run_model_contract=lambda:SimpleNamespace(model_id=graph.model_id))
    status=dict(state='serving',step=None,registry_origin=None,run_id='native-desk',participants=participants)
    app.owner_run=SimpleNamespace(status=lambda:dict(status))
    app.chat=ChatService(app)
    async def application(scope,receive,send):
        if not scope['path'].startswith('/chat'):
            return await gateway(scope,receive,send)
        body=b''
        while True:
            message=await receive()
            body+=message.get('body',b'')
            if not message.get('more_body',False):break
        headers={k.decode().title():v.decode() for k,v in scope['headers']}
        code,kind,raw=await asyncio.to_thread(handle,app,scope['method'],scope['path'],headers,body,via_relay=False)
        await send(dict(type='http.response.start',status=code,headers=[
            (b'content-type',kind.encode()),(b'content-length',str(len(raw)).encode()),
            (b'content-security-policy',CSP.encode())]))
        await send(dict(type='http.response.body',body=raw))
    server=LoopbackASGIServer(('127.0.0.1',0),application).start()
    thread=server.serve_forever_thread()
    origin=f'http://127.0.0.1:{server.port}'
    status['registry_origin']=origin
    expected=[dict(label=p['label'],node=p['node_id'][:12],layers=p['layers']) for p in participants]
    def post(path,body):
        request=urllib.request.Request(origin+path,data=json.dumps(body).encode(),headers={
            'Content-Type':'application/json','X-Mycelium-Chat':'1'})
        with urllib.request.urlopen(request,timeout=5) as response:
            assert response.status==200
            return json.load(response)
    try:
        state=post('/chat/api/state',{})
        assert state['available'] is True and state['served_by']==expected
        if browser:
            from playwright.sync_api import sync_playwright,expect
            seen={};errors=[]
            with sync_playwright() as p:
                # Check cached test browser and its parent directories before launch.
                executable=Path(p.chromium.executable_path)
                from test_token_two_process_join import _safe
                _safe(executable)  # Read-only libc check includes every app ancestor.
                instance=p.chromium.launch(headless=True,executable_path=str(executable),args=['--disable-gpu'])
                try:
                    page=instance.new_page()
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def response(response):
                        path=response.url.removeprefix(origin)
                        if path in ('/chat/api/send','/chat/api/events'):
                            assert response.status==200
                            seen[path]=response.json()
                    page.on('response',response)
                    page.goto(origin+'/chat')
                    expect(page.locator('#served')).to_contain_text('Served by:')
                    # V1 presents a permanent privacy warning, not V2's notice dialog.
                    expect(page.get_by_text(re.compile('Do not type secrets'))).to_be_visible()
                    page.locator('#prompt').fill('live recovery prompt')
                    page.locator('#tokens').select_option('128')
                    page.locator('#send').click()
                    expect(page.locator('#log .meta')).to_contain_text(re.compile(r' · [0-9.]+ s$'),timeout=35000)
                    accepted=seen['/chat/api/send'];result=seen['/chat/api/events']
                    for row in expected:
                        expect(page.locator('#log .meta')).to_contain_text(row['node'])
                        expect(page.locator('#log .meta')).to_contain_text(f"layers {row['layers'][0]}–{row['layers'][1]-1}")
                    assert not errors
                finally:instance.close()
        else:
            accepted=post('/chat/api/send',dict(prompt='live recovery prompt',max_new_tokens=6))
            deadline=time.monotonic()+35
            while time.monotonic()<deadline:
                result=post('/chat/api/events',dict(request_id=accepted['request_id'],after=0))
                if result['done']:break
                time.sleep(.02)
        assert accepted['served_by']==result['served_by']==expected
        assert result['done'] is True,result
        assert (result['terminal'],result['error'])==(
            ('failed','decode_completion_timeout') if failure else ('completed',None)),result
        # Browser polls may consume earlier chunks; the retained API record must
        # return the entire identical ordered answer when replayed from zero.
        result=post('/chat/api/events',dict(request_id=accepted['request_id'],after=0))
        # The browser arm's sixth generated token is EOS, correctly suppressed.
        assert result['tokens']==[f'<{100+i}>' for i in range(1 if failure else (5 if browser else 6))],result
        assert result['served_by']==expected
        return [int(token[1:-1]) for token in result['tokens']]
    finally:
        app.chat.cancel_all()
        server.stop(thread)
        assert not thread.is_alive()
