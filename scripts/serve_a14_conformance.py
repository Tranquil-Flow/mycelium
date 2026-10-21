#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Finite loopback-only conformance host for the BUILT production app.

Synthetic inputs -> ProductProjector -> ProductEvidenceApplication -> ordinary
ProductGateway -> browser decoder -> App/Network. No fleet, model or route work.
The unrelated Observatory feed is explicitly empty/unqualified conformance input.
"""
import asyncio
import json
import mimetypes
import os
from pathlib import Path
import runpy
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mycelium_product_spine import ProductEvidenceApplication
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application

conformance = runpy.run_path(str(ROOT / 'tests/a14_acceptance/conformance.py'))
now = int(time.time()*1000)
members, route, internet = conformance['inputs'](now, int(os.environ.get('A14_CONFORMANCE_MEMBERS', '2')))
product = ProductEvidenceApplication(projector=conformance['ConformanceProjector'](pseudonym_salt=secrets.token_bytes(32)),
    membership_source=lambda: members, route_source=lambda: route, qualification_source=lambda: None,
    internet_native_source=lambda: internet)

async def upstream(scope, receive, send):
    # Not a model/Router substitute: no requests or execution are accepted.
    path = scope['path']
    cursor = 1
    bundle = {'snapshot': {'protocol':'mycelium.observatory.request_projection.v1','source_cursor':cursor,
        'observed_at_unix_ms':now, 'qualification':None,'sessions':[]},'incidents':[],
        'provisioning':{'protocol':'mycelium.observatory.event_adapter_status.v1','route_ready':False,
        'source_cursor':cursor,'buffered_sessions':0,'quarantine_capacity':4,'dropped_quarantine_count':0}}
    if path.endswith('/snapshot'):
        body=json.dumps({'protocol':'mycelium.observatory_stream.v1','generation':cursor,'bundle':bundle}).encode()
        status, media = 200, b'application/json'
    elif path.endswith('/events'):
        body=b''; status, media=200,b'text/event-stream'
    else:
        body=b'{"error":"conformance_execution_unavailable"}';status,media=404,b'application/json'
    await send({'type':'http.response.start','status':status,'headers':[(b'content-type',media)]})
    await send({'type':'http.response.body','body':body})

class NoActions:
    def __getattr__(self, name):
        raise RuntimeError('conformance_mutation_forbidden')

gateway=create_product_gateway_application(config=GatewayConfig(), observatory_app=upstream,
    request_gateway_app=upstream, product_app=product, swarm_coordinator=NoActions(),
    request_gateway_bearer_token=secrets.token_urlsafe(32))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No headers, cookies, URLs or credentials in logs.
    def do_POST(self):
        self.send_error(405)
    def do_GET(self):
        if self.path.startswith('/api/'):
            messages=[]
            async def receive():
                await asyncio.sleep(0)
                return {'type':'http.request','body':b'','more_body':False}
            async def send(message): messages.append(message)
            scope={'type':'http','http_version':'1.1','method':'GET','scheme':'http','path':self.path,
                   'raw_path':self.path.encode(),'query_string':b'',
                   'headers':[(k.lower().encode(),v.encode()) for k,v in self.headers.items()],
                   'client':self.client_address,'server':self.server.server_address}
            asyncio.run(gateway(scope, receive, send))
            start=next(m for m in messages if m['type']=='http.response.start')
            body=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
            self.send_response(start['status'])
            for k,v in start.get('headers',[]):
                if k.lower() not in (b'content-length', b'cache-control'):
                    self.send_header(k.decode(),v.decode())
        else:
            dist=(ROOT/'ui/web/dist').resolve()
            relative=self.path.split('?',1)[0].lstrip('/') or 'index.html'
            path=(dist/relative).resolve()
            if not path.is_relative_to(dist) or not path.is_file():
                self.send_error(404);return
            body=path.read_bytes();self.send_response(200)
            self.send_header('Content-Type',mimetypes.guess_type(path)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'none'")
        self.send_header('Permissions-Policy','geolocation=(), camera=(), microphone=()')
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass

if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    timer=threading.Timer(600,server.shutdown);timer.daemon=True;timer.start()
    print(json.dumps({'origin':f'http://127.0.0.1:{server.server_port}','evidence_class':'local_conformance','physical_devices':0}),flush=True)
    try:server.serve_forever(poll_interval=.2)
    finally:timer.cancel();server.server_close()
