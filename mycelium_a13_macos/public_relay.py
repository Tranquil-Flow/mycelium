# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded demo HTTPS reachability without organizer credentials or TLS bypass.

The selected relay terminates public HTTPS and can see control traffic. The local hop
into this process is loopback HTTP; the canonical seed hop is verified TLS.
Inference still uses Iroh. This relay grants no membership or execution rights.
"""
from __future__ import annotations
from mycelium_http_identity import with_user_agent
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
import sys
import threading
import time
from urllib.parse import urlsplit

from .authority import _private_directory
from .tls import LocalTLSSeedServer

_MAX_FRAME=1024*1024
#: Join-flow routes answered in-process by the owner app (pairing window, link
#: probe, member capability report) instead of being forwarded to the seed.
#: Interface for the join lane: ``SeedRelayProxy.bind_local(handler)`` where
#: ``handler(method, path, body) -> (status, raw_json_bytes)``.
_LOCAL_PREFIXES=('/pairing-window/','/link-probe/','/member-report/')
#: The owner's shared chat page (chat.py), answered in-process like the join
#: routes: ``SeedRelayProxy.bind_chat(handler)`` with
#: ``handler(method, path, headers, body) -> (status, content_type, raw)``.
_CHAT_CSP=("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
           "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
_CHUNK_LINE_MAX=10
_CHUNK_SIZE_RE=re.compile(rb'[0-9A-Fa-f]{1,8}\r\n')
_LOCAL_TYPES=frozenset({'application/json','text/html; charset=utf-8','image/svg+xml'})
#: Public swarm entry (swarm_entry.py): ``SeedRelayProxy.bind_swarm_entry(handler)``
#: with ``handler(method, path, headers, body) -> (status, 'application/json', raw)``.
#: Cross-origin (the directory page is another origin): CORS ``*``, no credentials.
_SWARM_PREFIX='/swarm-entry/v1/'
_SWARM_CORS=(('Access-Control-Allow-Origin','*'),('Access-Control-Allow-Methods','POST'),
             ('Access-Control-Allow-Headers','Content-Type'),('Access-Control-Max-Age','600'))


def _chat_path(path):
    return path=='/chat' or path.startswith('/chat/')


class _RelayServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,port=0,trust_client_ip=True):
        self.backend=None
        self.local=None
        self.chat=None
        self.swarm=None
        #: Whether ``Cf-Connecting-Ip`` was set by a Cloudflare edge we trust (quick tunnel, or an owner
        #: front declared to be a Cloudflare tunnel).  Otherwise a visitor could pick its own rate-limit key.
        self.trust_client_ip=trust_client_ip
        self.slots=threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1',port),_RelayHandler)
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
    def _reply(self,status,raw,content_type='application/json',csp=None,extra=()):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        for name,value in extra:self.send_header(name,value)
        if csp is not None:
            self.send_header('Content-Security-Policy',csp)
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Connection','close')
        self.end_headers();self.wfile.write(raw);self.close_connection=True
    def _forward(self):
        self.connection.settimeout(5)
        if ((not self.path.startswith(('/seed/','/managed-control/',_SWARM_PREFIX)+_LOCAL_PREFIXES)
                and not _chat_path(self.path))
                or any(value in self.path for value in ('..','%','?','#','\\'))):
            return self._reply(404,b'{"error":{"code":"relay_route_unknown"}}')
        if _chat_path(self.path):return self._local(chat=True)
        if self.path.startswith(_SWARM_PREFIX):return self._local(swarm=True)
        if self.path.startswith(_LOCAL_PREFIXES):return self._local()
        if (self.headers.get('Transfer-Encoding') is not None
                or self.headers.get('Cookie') is not None
                or self.headers.get('Authorization') is not None):
            return self._reply(400,b'{"error":{"code":"relay_headers_invalid"}}')
        connection=None
        try:
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)>1 or (self.command=='POST' and len(lengths)!=1):raise ValueError()
            length=int(lengths[0]) if lengths else 0
            if not 0<=length<=_MAX_FRAME or (self.command=='GET' and length):raise ValueError()
            body=self.rfile.read(length)
            if len(body)!=length:raise ValueError()
            backend=self.server.backend
            if backend is None:return self._reply(503,b'{"error":{"code":"relay_seed_not_bound"}}')
            port,context=backend
            connection=http.client.HTTPSConnection('127.0.0.1',port,timeout=5,context=context)
            headers={'Content-Type':self.headers.get('Content-Type','application/json')}
            connection.request(self.command,self.path,body=body if self.command=='POST' else None,headers=with_user_agent(headers))
            response=connection.getresponse();raw=response.read(_MAX_FRAME+1)
            if len(raw)>_MAX_FRAME:raise ValueError()
            self._reply(response.status,raw,response.getheader('Content-Type','application/json'))
        except ValueError:self._reply(400,b'{"error":{"code":"relay_frame_invalid"}}')
        except (OSError,http.client.HTTPException):self._reply(502,b'{"error":{"code":"relay_backend_unavailable"}}')
        finally:
            if connection is not None:connection.close()
    def _public_headers(self):
        headers={k:v for k,v in self.headers.items()}
        if not self.server.trust_client_ip:
            headers={k:v for k,v in headers.items() if k.lower()!='cf-connecting-ip'}
        return headers
    def _read_chunked(self):
        """Strict, bounded HTTP/1.1 chunked body (no extensions, no trailers)."""
        body=bytearray()
        while True:
            line=self.rfile.readline(_CHUNK_LINE_MAX+1)
            if not _CHUNK_SIZE_RE.fullmatch(line):raise ValueError()
            size=int(line[:-2],16)
            if size==0:break
            if len(body)+size>_MAX_FRAME:raise ValueError()
            data=self.rfile.read(size)
            if len(data)!=size or self.rfile.read(2)!=b'\r\n':raise ValueError()
            body+=data
        if self.rfile.read(2)!=b'\r\n':raise ValueError()
        return bytes(body)
    def _local(self,chat=False,swarm=False):
        # Cloudflare's tunnel re-frames a body whose length its edge did not
        # forward (observed: every empty-body POST) as ``Transfer-Encoding:
        # chunked`` without Content-Length.  Only the in-process routes accept
        # exactly that one shape; TE+CL, repeated/listed codings, GET bodies and
        # credentials stay refused.  Forwarded /seed/ traffic is unchanged.
        codings=self.headers.get_all('Transfer-Encoding',[])
        chunked=len(codings)==1 and codings[0].strip().lower()=='chunked'
        if ((codings and (not chunked or self.command!='POST'
                          or self.headers.get_all('Content-Length',[])))
                or self.headers.get('Cookie') is not None
                or self.headers.get('Authorization') is not None):
            return self._reply(400,b'{"error":{"code":"relay_headers_invalid"}}')
        try:
            if chunked:body=self._read_chunked()
            else:
                lengths=self.headers.get_all('Content-Length',[])
                if len(lengths)>1 or (self.command=='POST' and len(lengths)!=1):raise ValueError()
                length=int(lengths[0]) if lengths else 0
                if not 0<=length<=_MAX_FRAME or (self.command=='GET' and length):raise ValueError()
                body=self.rfile.read(length)
                if len(body)!=length:raise ValueError()
        except (ValueError,OSError):return self._reply(400,b'{"error":{"code":"relay_frame_invalid"}}')
        if swarm:
            handler=self.server.swarm
            if handler is None:return self._reply(503,b'{"error":{"code":"relay_swarm_entry_not_bound"}}',extra=_SWARM_CORS)
            try:
                status,kind,raw=handler(self.command,self.path,self._public_headers(),body)
                if (type(status) is not int or not 200<=status<=599 or not isinstance(raw,bytes)
                        or len(raw)>_MAX_FRAME or kind!='application/json'):
                    raise ValueError()
            except Exception:return self._reply(500,b'{"error":{"code":"relay_swarm_entry_failed"}}',extra=_SWARM_CORS)
            return self._reply(status,raw,kind,extra=_SWARM_CORS)
        if chat:
            handler=self.server.chat
            if handler is None:return self._reply(503,b'{"error":{"code":"relay_chat_not_bound"}}')
            try:
                status,kind,raw=handler(self.command,self.path,self._public_headers(),body)
                if (type(status) is not int or not 200<=status<=599 or not isinstance(raw,bytes)
                        or len(raw)>_MAX_FRAME or not isinstance(kind,str)):
                    raise ValueError()
            except Exception:return self._reply(500,b'{"error":{"code":"relay_chat_failed"}}')
            return self._reply(status,raw,kind,csp=_CHAT_CSP)
        handler=self.server.local
        if handler is None:return self._reply(503,b'{"error":{"code":"relay_join_not_bound"}}')
        try:
            answer=handler(self.command,self.path,body)
            # (status, raw) is JSON; (status, raw, content_type) admits the
            # QR join page / its SVG (closed set of types).
            status,raw=answer[0],answer[1]
            kind=answer[2] if len(answer)==3 else 'application/json'
            if (type(status) is not int or not 200<=status<=599 or not isinstance(raw,bytes)
                    or len(raw)>_MAX_FRAME or kind not in _LOCAL_TYPES):
                raise ValueError()
        except Exception:return self._reply(500,b'{"error":{"code":"relay_join_failed"}}')
        self._reply(status,raw,kind,csp=None if kind=='application/json' else _CHAT_CSP)
    def do_OPTIONS(self):
        # CORS preflight for the swarm entry only; nothing else answers OPTIONS.
        if (not self.path.startswith(_SWARM_PREFIX) or any(v in self.path for v in ('..','%','?','#','\\'))
                or self.headers.get('Content-Length') not in (None,'0') or self.headers.get('Transfer-Encoding')):
            return self._reply(404,b'{"error":{"code":"relay_route_unknown"}}')
        return self._reply(204,b'',extra=_SWARM_CORS)
    do_GET=_forward
    do_POST=_forward


class SeedRelayProxy:
    def __init__(self,port=0,trust_client_ip=True):
        self.server=_RelayServer(port,trust_client_ip)
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
    def bind_local(self,handler):
        if handler is not None and not callable(handler):raise ValueError('relay_local_invalid')
        self.server.local=handler
    def bind_chat(self,handler):
        if handler is not None and not callable(handler):raise ValueError('relay_chat_invalid')
        self.server.chat=handler
    def bind_swarm_entry(self,handler):
        if handler is not None and not callable(handler):raise ValueError('relay_swarm_entry_invalid')
        self.server.swarm=handler
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
    name='cloudflared.exe' if sys.platform=='win32' else 'cloudflared'
    bundled=Path(__file__).resolve().parent/'native'/name
    if bundled.is_file() and os.access(bundled,os.X_OK):return str(bundled)
    return shutil.which('cloudflared')


#: Same shape the swarm directory accepts for an entry origin (so a listing can carry it): lowercase
#: DNS name with at least two labels and an alphabetic TLD, https, default port, nothing else.
_EXTERNAL_ORIGIN=re.compile(r'https://(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z')
_NON_PUBLIC_SUFFIXES=frozenset({'local','localhost','internal','lan','home','corp','invalid','test','onion','arpa'})


def validate_external_origin(value):
    """The owner-chosen public origin: ``https://<dns-name>`` only (no credentials, port, path or IP literal)."""
    if not isinstance(value,str) or value!=value.strip():raise ValueError('relay_origin_invalid')
    candidate=value.lower()
    try:
        from mycelium_seed.http import canonical_https_origin
        canonical=canonical_https_origin(candidate)
    except (TypeError,ValueError):raise ValueError('relay_origin_invalid') from None
    if (canonical!=candidate or _EXTERNAL_ORIGIN.fullmatch(candidate) is None
            or candidate.rsplit('.',1)[1] in _NON_PUBLIC_SUFFIXES):
        raise ValueError('relay_origin_invalid')
    return candidate


def validate_proxy_port(value):
    if type(value) is not int or not 1024<=value<=65535:raise ValueError('relay_proxy_port_invalid')
    return value


#: Relay lifetime (V1.1 e): while serving, extend shortly before expiry up to a total
#: granted-window budget of 24 h. Only an external relay may raise that cap to a week
#: or explicitly choose ``long_lived``; quick tunnels always keep the 24 h cap.
RENEWAL_CAP_SECONDS=86400
MAX_RENEWAL_CAP_SECONDS=7*86400
RENEWAL_MARGIN_SECONDS=60.0


class TemporaryHttpsRelay:
    """One explicitly approved, bounded relay.

    ``cloudflare``: a quick tunnel; no account/config reuse.  ``external``: the owner's own stable https
    origin, served by a front the owner runs (for example a named Cloudflare tunnel) that forwards to
    ``127.0.0.1:<proxy_port>``; this process starts only the in-process relay proxy and spawns nothing.
    """
    def __init__(self,state_root,provider='cloudflare',*,public_origin=None,proxy_port=None,cloudflare_front=False,
                 auto_renew=True,long_lived=False,max_renewed_seconds=RENEWAL_CAP_SECONDS,
                 _clock=time.monotonic,_lifetime_poll=1.0):
        if provider not in ('cloudflare','external'):raise ValueError('relay_provider_invalid')
        if provider=='cloudflare' and (public_origin is not None or proxy_port is not None or cloudflare_front
                                       or long_lived is not False or auto_renew is not True
                                       or max_renewed_seconds!=RENEWAL_CAP_SECONDS):
            raise ValueError('relay_provider_invalid')
        if (type(auto_renew) is not bool or type(long_lived) is not bool or type(max_renewed_seconds) not in (int,float)
                or not 0<max_renewed_seconds<=MAX_RENEWAL_CAP_SECONDS):
            raise ValueError('relay_renewal_invalid')
        self._auto_renew=auto_renew;self._long_lived=long_lived;self._renewal_cap=float(max_renewed_seconds)
        self._clock=_clock;self._lifetime_poll=_lifetime_poll
        self._serving_probe=lambda:False
        self._window=None;self._deadline=None;self._granted=0.0;self._renewals=0;self._renewal_stopped=None
        self._lifetime=None
        self.provider=provider
        self._chosen_origin=validate_external_origin(public_origin) if provider=='external' else None
        self._proxy_port=validate_proxy_port(proxy_port) if provider=='external' else None
        self._cloudflare_front=cloudflare_front is True
        self.error_code=None
        self.root=Path(state_root)
        self.process=None;self.proxy=None;self.public_origin=None
        self._ready=threading.Event();self._lines=deque(maxlen=32)
        self._reader=None;self._lock=threading.RLock();self._closed=False
    def start(self,*,acknowledged,max_seconds=1800):
        if acknowledged is not True:raise ValueError('relay_approval_required')
        if type(max_seconds) is not int or not 30<=max_seconds<=3600:raise ValueError('relay_budget_invalid')
        if self.process is not None or self.proxy is not None or self._closed:raise ValueError('relay_not_startable')
        if self.provider=='external':return self._start_external(max_seconds)
        binary=installed_relay_binary()
        if binary is None:raise ValueError('relay_client_installation_required')
        self.root=_private_directory(self.root,create=True)
        home=_private_directory(self.root/'isolated-home',create=True)
        env={k:v for k,v in os.environ.items() if not k.startswith(('TUNNEL_','CLOUDFLARE_','CF_')) and k!='NO_TLS_VERIFY'}
        env['HOME']=str(home)
        if sys.platform=='win32':
            # cloudflared resolves ~/.cloudflared from USERPROFILE on Windows: isolate it too.
            env['USERPROFILE']=str(home)
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
            self._start_lifetime(max_seconds)
            return self
        except BaseException:self.close();raise
    def _start_external(self,max_seconds):
        self.root=_private_directory(self.root,create=True)
        try:self.proxy=SeedRelayProxy(self._proxy_port,self._cloudflare_front).start()
        except OSError:raise ValueError('relay_proxy_port_unavailable') from None
        self.public_origin=self._chosen_origin
        self._start_lifetime(max_seconds)
        return self

    def _start_lifetime(self,max_seconds):
        """Both providers use the same bounded renew-while-serving policy."""
        self._window=float(max_seconds);self._granted=self._window
        self._deadline=None if self._long_lived else self._clock()+self._window
        if not self._long_lived and self._lifetime_poll is not None:
            self._lifetime=threading.Thread(target=self._lifetime_loop,daemon=True,name='a13-relay-lifetime')
            self._lifetime.start()

    def set_serving_probe(self,probe):
        """``probe() -> bool``: is a run serving through this relay right now (the owner app supplies it)."""
        if not callable(probe):raise ValueError('relay_renewal_invalid')
        self._serving_probe=probe

    def _lifetime_loop(self):
        while not self._closed:
            time.sleep(self._lifetime_poll)
            try:self._lifetime_step()
            except Exception:pass

    def _lifetime_step(self):
        """Renew shortly before expiry while serving (bounded), otherwise close at the deadline."""
        with self._lock:
            if self._closed or self._deadline is None:return
            now=self._clock()
            left=self._deadline-now
            if left<=0:
                pass
            elif self._auto_renew and self._renewal_stopped is None and left<=min(RENEWAL_MARGIN_SECONDS,self._window/3):
                try:serving=bool(self._serving_probe())
                except Exception:serving=False
                if serving:
                    if self._granted+self._window>self._renewal_cap:
                        self._renewal_stopped='relay_renewal_cap_reached'
                    else:
                        self._granted+=self._window;self._renewals+=1
                        self._deadline=now+self._window
                    return
                return
            else:return
        self.close()

    def observe_origin(self,origin):
        if self.public_origin is None:
            self.public_origin=origin;self._ready.set();return True
        if origin==self.public_origin:return True
        self.error_code='relay_origin_changed'
        if self.proxy is not None:self.proxy.server.backend=None
        if self.process is not None and self.process.poll() is None:self.process.terminate()
        return False

    def status(self):
        if self.provider=='external':running=self.proxy is not None and not self._closed and self.error_code is None
        else:running=self.process is not None and self.process.poll() is None and not self._closed and self.error_code is None
        value={'state':'relay_process_running' if running else 'stopped',
            'provider':self.provider,'public_origin':self.public_origin,'error_code':self.error_code,
            'seed_bound':self.proxy is not None and self.proxy.server.backend is not None,
            'privacy':'The selected relay terminates HTTPS and can observe control traffic, visitor IP addresses and timing, and web chat prompts and replies in plaintext (they cross this relay); only inference between the Macs uses Iroh. If system DNS fails, relay names use Cloudflare HTTPS DNS with unchanged certificate verification.',
            'relay_host_trust':'public_webpki'}
        left=None
        if self._deadline is not None and not self._closed:left=max(0,int(round(self._deadline-self._clock())))
        value.update({'auto_renew':self._auto_renew,'long_lived':self._long_lived,'renewals':self._renewals,
            'expires_in_seconds':left,'renewal_cap_seconds':int(self._renewal_cap),'renewal_stopped':self._renewal_stopped})
        if self.provider=='external':
            host=(self._chosen_origin or '').removeprefix('https://')
            value['local_forward_target']='http://127.0.0.1:'+str(self._proxy_port)
            value['privacy']=('Your own address ('+host+') is served by the front you pointed at this Mac (for example a named '
                'Cloudflare tunnel or a reverse proxy). That front terminates HTTPS and can observe control traffic, visitor IP '
                'addresses and timing, and web chat prompts and replies in plaintext (they cross it); only inference between the '
                'Macs uses Iroh. Mycelium starts no tunnel for it: you must point this hostname at '+value['local_forward_target']+' yourself.')
        return value

    def close(self):
        with self._lock:
            if self._closed:return
            self._closed=True
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
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
                with os.fdopen(fd,'w') as stream:stream.write(''.join(self._lines))
