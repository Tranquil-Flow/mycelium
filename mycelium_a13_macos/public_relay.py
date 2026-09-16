"""Bounded demo HTTPS reachability without organizer credentials or TLS bypass.

The selected relay terminates public HTTPS and can see control traffic. The local hop
into this process is loopback HTTP; the canonical seed hop is verified TLS.
Inference still uses Iroh. This relay grants no membership or execution rights.
"""
from __future__ import annotations
from collections import deque
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import threading
from urllib.parse import urlsplit

from .authority import _private_directory
from .tls import LocalTLSSeedServer

_MAX_FRAME=1024*1024


class _RelayServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self):
        self.backend=None
        self.slots=threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1',0),_RelayHandler)
    def process_request(self,request,client_address):
        request.settimeout(5)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()
    def handle_error(self,request,client_address):pass


class _RelayHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def _reply(self,status,raw,content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Connection','close')
        self.end_headers();self.wfile.write(raw);self.close_connection=True
    def _forward(self):
        self.connection.settimeout(5)
        if (not self.path.startswith(('/seed/','/managed-control/'))
                or any(value in self.path for value in ('..','%','?','#','\\'))):
            return self._reply(404,b'{"error":"relay_route_unknown"}')
        if (self.headers.get('Transfer-Encoding') is not None
                or self.headers.get('Cookie') is not None
                or self.headers.get('Authorization') is not None):
            return self._reply(400,b'{"error":"relay_headers_invalid"}')
        connection=None
        try:
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)>1 or (self.command=='POST' and len(lengths)!=1):raise ValueError()
            length=int(lengths[0]) if lengths else 0
            if not 0<=length<=_MAX_FRAME or (self.command=='GET' and length):raise ValueError()
            body=self.rfile.read(length)
            if len(body)!=length:raise ValueError()
            backend=self.server.backend
            if backend is None:return self._reply(503,b'{"error":"relay_seed_not_bound"}')
            port,context=backend
            connection=http.client.HTTPSConnection('127.0.0.1',port,timeout=5,context=context)
            headers={'Content-Type':self.headers.get('Content-Type','application/json')}
            connection.request(self.command,self.path,body=body if self.command=='POST' else None,headers=headers)
            response=connection.getresponse();raw=response.read(_MAX_FRAME+1)
            if len(raw)>_MAX_FRAME:raise ValueError()
            self._reply(response.status,raw,response.getheader('Content-Type','application/json'))
        except ValueError:self._reply(400,b'{"error":"relay_frame_invalid"}')
        except (OSError,http.client.HTTPException):self._reply(502,b'{"error":"relay_backend_unavailable"}')
        finally:
            if connection is not None:connection.close()
    do_GET=_forward
    do_POST=_forward


class SeedRelayProxy:
    def __init__(self):
        self.server=_RelayServer()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=None;self._closed=False
    def start(self):
        if self._closed:raise ValueError('relay_proxy_closed')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True,name='a13-seed-relay')
        self.thread.start();return self
    def bind_seed(self,server):
        if not isinstance(server,LocalTLSSeedServer):raise ValueError('relay_seed_invalid')
        parts=urlsplit(server.base_url)
        if parts.scheme!='https' or parts.hostname!='127.0.0.1':raise ValueError('relay_seed_invalid')
        context=ssl.create_default_context(cafile=str(server.material.ca_certificate))
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        self.server.backend=(parts.port,context)
    def close(self):
        if self._closed:return
        self._closed=True
        if self.thread is not None:
            self.server.shutdown();self.thread.join(5)
        self.server.server_close()


def provider_event(provider,line):
    """Retain only public routing facts, never provider credentials or logs."""
    if provider!='cloudflare':return None
    match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
    return {'event':'opened','origin':match.group(0)} if match else None


def installed_relay_binary():
    bundled=Path(__file__).resolve().parent/'native/cloudflared'
    if bundled.is_file() and os.access(bundled,os.X_OK):return str(bundled)
    return shutil.which('cloudflared')


class TemporaryHttpsRelay:
    """One explicitly approved, bounded quick tunnel; no account/config reuse."""
    def __init__(self,state_root,provider='cloudflare'):
        if provider!='cloudflare':raise ValueError('relay_provider_invalid')
        self.provider=provider
        self.error_code=None
        self.root=Path(state_root)
        self.process=None;self.proxy=None;self.public_origin=None
        self._ready=threading.Event();self._lines=deque(maxlen=32)
        self._reader=None;self._timer=None;self._lock=threading.RLock();self._closed=False
    def start(self,*,acknowledged,max_seconds=1800):
        if acknowledged is not True:raise ValueError('relay_approval_required')
        if type(max_seconds) is not int or not 30<=max_seconds<=3600:raise ValueError('relay_budget_invalid')
        if self.process is not None or self._closed:raise ValueError('relay_not_startable')
        binary=installed_relay_binary()
        if binary is None:raise ValueError('relay_client_installation_required')
        self.root=_private_directory(self.root,create=True)
        home=_private_directory(self.root/'isolated-home',create=True)
        env={k:v for k,v in os.environ.items() if not k.startswith(('TUNNEL_','CLOUDFLARE_','CF_')) and k!='NO_TLS_VERIFY'}
        env['HOME']=str(home)
        self.proxy=SeedRelayProxy().start()
        try:
            command=[binary,'--no-autoupdate','tunnel','--url',self.proxy.origin,
                '--protocol','http2','--metrics','127.0.0.1:0','--grace-period','5s','--management-diagnostics=false']
            self.process=subprocess.Popen(command,
                cwd=self.root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,start_new_session=True)
            def read():
                while True:
                    raw=self.process.stdout.readline(65537)
                    if not raw:break
                    if len(raw)>65536:self.process.terminate();break
                    event=provider_event(self.provider,raw.decode('utf-8','replace'))
                    if event is not None:
                        self._lines.append(json.dumps(event,sort_keys=True)+'\n')
                        if not self.observe_origin(event['origin']):
                            threading.Thread(target=self.close,daemon=True,name='a13-relay-withdraw').start()
                            break
            self._reader=threading.Thread(target=read,daemon=True,name='a13-relay-log');self._reader.start()
            if not self._ready.wait(45) or self.error_code is not None or self.process.poll() is not None:
                raise ValueError('relay_start_failed')
            self._timer=threading.Timer(max_seconds,self.close);self._timer.daemon=True;self._timer.start()
            return self
        except BaseException:self.close();raise
    def observe_origin(self,origin):
        if self.public_origin is None:
            self.public_origin=origin;self._ready.set();return True
        if origin==self.public_origin:return True
        self.error_code='relay_origin_changed'
        if self.proxy is not None:self.proxy.server.backend=None
        if self.process is not None and self.process.poll() is None:self.process.terminate()
        return False

    def status(self):
        running=self.process is not None and self.process.poll() is None and not self._closed and self.error_code is None
        return {'state':'relay_process_running' if running else 'stopped',
            'provider':self.provider,'public_origin':self.public_origin,'error_code':self.error_code,
            'seed_bound':self.proxy is not None and self.proxy.server.backend is not None,
            'privacy':'The selected relay terminates HTTPS and can observe control traffic; inference uses Iroh. If system DNS fails, relay names use Cloudflare HTTPS DNS with unchanged certificate verification.',
            'relay_host_trust':'public_webpki'}

    def close(self):
        with self._lock:
            if self._closed:return
            self._closed=True
            if self._timer is not None:self._timer.cancel()
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:self.process.wait(8)
                    except subprocess.TimeoutExpired:self.process.kill();self.process.wait(5)
                if self._reader is not None:self._reader.join(2)
                self.process.stdout.close()
            if self.proxy is not None:self.proxy.close()
            if self.root.exists():
                path=self.root/'relay-tail.log'
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'w') as stream:stream.write(''.join(self._lines))
