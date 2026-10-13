# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared chat page for the coworking demo: no accounts, rate-limited, capped.

The owner app serves ``/chat`` locally and (through the public relay) as a
shareable HTTPS link.  A prompt is forwarded server-side to the run's
loopback registry API exactly like the operator stream script did
(bootstrap -> current qualification -> ``POST /api/v1/inference`` -> SSE), and
the generated tokens are buffered so browsers poll them (POST
``/chat/api/events``) every ~250 ms: robust through the quick tunnel and the
relay's short socket timeouts.  Each answer shows which Macs served it (the
run's participants and their layer ranges).

Limits (all server-side): prompt <= 2,000 characters; ``max_new_tokens`` is
clamped to ``MAX_NEW_TOKENS_CAP``; one request in flight for the whole swarm;
per-client 4 requests / 60 s and swarm-wide 30 / 60 s.  The client key is the
Cloudflare ``Cf-Connecting-Ip`` header behind the relay (the relay is reached
only through cloudflared on loopback), else ``local``.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import secrets
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_PROMPT_CHARS = 2000
MAX_NEW_TOKENS_CAP = 256
DEFAULT_NEW_TOKENS = 128
PER_CLIENT_PER_MINUTE = 4
GLOBAL_PER_MINUTE = 30
MAX_KEPT_REQUESTS = 64
_ID = re.compile(r'[A-Za-z0-9_-]{16,64}\Z')
_ASSETS = {'/chat': ('chat.html', 'text/html; charset=utf-8'),
           '/chat/chat.js': ('chat.js', 'text/javascript; charset=utf-8'),
           '/chat/chat.css': ('chat.css', 'text/css; charset=utf-8')}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")


class ChatError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code
        super().__init__(code)


class RegistryClient:
    """Browser-equivalent client of the run's loopback product API."""

    def __init__(self, origin: str, *, timeout: float = 300.0) -> None:
        if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{2,5}', origin or ''):
            raise ChatError(503, 'chat_registry_origin_invalid')
        self.origin = origin
        self.timeout = timeout
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._headers = {'Origin': origin, 'Accept': 'application/json', 'Content-Type': 'application/json'}

    def _open(self, path, body=None, extra=None):
        request = urllib.request.Request(self.origin + path,
            data=None if body is None else json.dumps(body).encode(), headers={**self._headers, **(extra or {})})
        return self._opener.open(request, timeout=self.timeout)

    def _json(self, path, body=None):
        with self._open(path, body) as response:
            return json.load(response)

    def stream(self, prompt: str, max_new_tokens: int, on_token: Callable[[str], None]) -> dict[str, Any]:
        from mycelium_request_gateway.contracts import InferenceSubmission, QualificationBinding
        session = self._json('/api/v1/bootstrap')['session']
        self._headers[session['csrf_header']] = session['csrf_token']
        qualification = self._json('/api/v1/qualification/current')
        if qualification.get('route_ready') is not True:
            raise ChatError(503, 'chat_route_not_ready')
        submission = InferenceSubmission(prompt=prompt, max_new_tokens=max_new_tokens,
            qualification=QualificationBinding.from_dict(qualification['binding']))
        accepted = self._json('/api/v1/inference', submission.to_dict())
        terminal = None
        with self._open(accepted['event_path'], extra={'Accept': 'text/event-stream'}) as response:
            for raw in response:
                if not raw.startswith(b'data: '):
                    continue
                event = json.loads(raw[6:])
                if event.get('type') == 'token':
                    on_token(event['text'])
                elif event.get('type') in {'completed', 'cancelled', 'failed'}:
                    terminal = event
                    break
        return {'terminal': None if terminal is None else terminal.get('type'),
                'code': None if terminal is None else terminal.get('code'),
                'deployment_id': qualification['binding'].get('deployment_id'),
                'model_id': qualification['binding'].get('model_id')}


class ChatService:
    def __init__(self, app: Any, *, client_factory: Callable[[str], Any] = RegistryClient,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self._client_factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._requests: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque()
        self._per_client: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._busy: str | None = None

    # -- state --------------------------------------------------------------
    def _serving(self) -> dict[str, Any] | None:
        run = getattr(self.app, 'owner_run', None)
        status = None if run is None else run.status()
        if not status or status.get('state') != 'serving' or not status.get('registry_origin'):
            return None
        return status

    def available(self) -> bool:
        return self._serving() is not None

    def _rate(self, key: str) -> None:
        now = self._clock()
        bucket = self._per_client.setdefault(key, deque())
        for queue in (bucket, self._global):
            while queue and now - queue[0] >= 60:
                queue.popleft()
        if len(bucket) >= PER_CLIENT_PER_MINUTE:
            raise ChatError(429, 'chat_rate_limited')
        if len(self._global) >= GLOBAL_PER_MINUTE:
            raise ChatError(429, 'chat_swarm_rate_limited')
        bucket.append(now); self._global.append(now)
        if len(self._per_client) > 4096:  # bounded memory under many clients
            self._per_client = {k: v for k, v in self._per_client.items() if v}

    # -- API ----------------------------------------------------------------
    def send(self, client: str, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not set(body) <= {'prompt', 'max_new_tokens'} or 'prompt' not in body:
            raise ChatError(400, 'chat_request_invalid')
        prompt = body['prompt']
        if not isinstance(prompt, str) or not prompt.strip():
            raise ChatError(400, 'chat_prompt_required')
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ChatError(413, 'chat_prompt_too_long')
        wanted = body.get('max_new_tokens', DEFAULT_NEW_TOKENS)
        if type(wanted) is not int or wanted < 1:
            raise ChatError(400, 'chat_max_new_tokens_invalid')
        max_new_tokens = min(wanted, MAX_NEW_TOKENS_CAP)
        serving = self._serving()
        if serving is None:
            raise ChatError(503, 'chat_not_ready')
        with self._lock:
            if self._busy is not None:
                raise ChatError(429, 'chat_busy')
            self._rate(client)
            request_id = secrets.token_urlsafe(18)
            record = {'id': request_id, 'tokens': [], 'done': False, 'error': None,
                      'max_new_tokens': max_new_tokens, 'clamped': wanted != max_new_tokens,
                      'served_by': [{'label': p.get('label'), 'node': (p.get('node_id') or '')[:12],
                                     'layers': p.get('layers')} for p in (serving.get('participants') or [])],
                      'run_id': serving.get('run_id'), 'started': self._clock(), 'finished': None}
            self._requests[request_id] = record
            self._order.append(request_id)
            while len(self._order) > MAX_KEPT_REQUESTS:
                self._requests.pop(self._order.popleft(), None)
            self._busy = request_id
        thread = threading.Thread(target=self._work, args=(record, serving['registry_origin'], prompt),
                                  name='a13-chat', daemon=True)
        thread.start()
        return {'request_id': request_id, 'max_new_tokens': max_new_tokens, 'clamped': record['clamped'],
                'served_by': record['served_by']}

    def _work(self, record, origin, prompt):
        try:
            client = self._client_factory(origin)
            def token(text):
                with self._lock:
                    record['tokens'].append(text)
            result = client.stream(prompt, record['max_new_tokens'], token)
            with self._lock:
                if result.get('terminal') == 'failed':
                    record['error'] = result.get('code') or 'chat_generation_failed'
        except ChatError as error:
            with self._lock: record['error'] = error.code
        except Exception:
            with self._lock: record['error'] = 'chat_generation_failed'
        finally:
            with self._lock:
                record['done'] = True
                record['finished'] = self._clock()
                if self._busy == record['id']:
                    self._busy = None

    def events(self, body: Any) -> dict[str, Any]:
        if (not isinstance(body, dict) or set(body) != {'request_id', 'after'}
                or not isinstance(body['request_id'], str) or not _ID.fullmatch(body['request_id'])
                or type(body['after']) is not int or body['after'] < 0):
            raise ChatError(400, 'chat_request_invalid')
        with self._lock:
            record = self._requests.get(body['request_id'])
            if record is None:
                raise ChatError(404, 'chat_request_unknown')
            tokens = record['tokens'][body['after']:]
            seconds = None
            if record['finished'] is not None:
                seconds = round(record['finished'] - record['started'], 2)
            return {'tokens': tokens, 'next': body['after'] + len(tokens), 'done': record['done'],
                    'error': record['error'], 'served_by': record['served_by'], 'seconds': seconds}

    def state(self) -> dict[str, Any]:
        serving = self._serving()
        return {'available': serving is not None, 'busy': self._busy is not None,
                'model_id': None if serving is None else _model_id(self.app),
                'served_by': [] if serving is None else [
                    {'label': p.get('label'), 'node': (p.get('node_id') or '')[:12], 'layers': p.get('layers')}
                    for p in (serving.get('participants') or [])],
                'limits': {'max_prompt_chars': MAX_PROMPT_CHARS, 'max_new_tokens': MAX_NEW_TOKENS_CAP,
                           'per_client_per_minute': PER_CLIENT_PER_MINUTE,
                           'swarm_per_minute': GLOBAL_PER_MINUTE}}


def _model_id(app):
    try:
        return app.run_model_contract().model_id
    except Exception:
        return None


def service(app: Any) -> ChatService:
    chat = getattr(app, 'chat', None)
    if chat is None:
        chat = ChatService(app)
        app.chat = chat
    return chat


def chat_status(app: Any) -> dict[str, Any] | None:
    if app.installation_role != 'owner':
        return None
    relay = getattr(app, 'public_relay', None)
    origin = getattr(relay, 'public_origin', None) if relay is not None else None
    state = service(app).state()
    return {'available': state['available'], 'busy': state['busy'],
            'local_url': app.origin + '/chat' if app.server is not None else None,
            'share_url': (origin.rstrip('/') + '/chat') if isinstance(origin, str) and origin.startswith('https://') else None}


def client_key(headers: Mapping[str, str], *, via_relay: bool) -> str:
    if via_relay:
        value = headers.get('Cf-Connecting-Ip') or headers.get('cf-connecting-ip') or ''
        if re.fullmatch(r'[0-9A-Fa-f:.]{2,45}', value):
            return 'relay:' + value
        return 'relay:unknown'
    return 'local'


def handle(app: Any, method: str, path: str, headers: Mapping[str, str], body: bytes, *,
           via_relay: bool) -> tuple[int, str, bytes]:
    """Chat routes for both the loopback app server and the public relay."""
    if app.installation_role != 'owner':
        return _json(404, {'error': 'not_found'})
    if method == 'GET' and path in _ASSETS:
        name, kind = _ASSETS[path]
        raw = (Path(__file__).parent / 'web' / name).read_bytes()
        return 200, kind, raw
    if method != 'POST' or path not in {'/chat/api/send', '/chat/api/events', '/chat/api/state'}:
        return _json(404, {'error': 'not_found'})
    # A JSON body plus a custom header: a cross-site page cannot send this
    # without a CORS preflight, which is never answered.
    if headers.get('X-Mycelium-Chat') != '1' or headers.get('Content-Type') != 'application/json':
        return _json(403, {'error': 'chat_header_required'})
    if len(body) > 16384:
        return _json(413, {'error': 'chat_body_too_large'})
    try:
        document = json.loads(body or b'{}')
    except (ValueError, UnicodeError):
        return _json(400, {'error': 'chat_request_invalid'})
    chat = service(app)
    try:
        if path == '/chat/api/state':
            return _json(200, chat.state())
        if path == '/chat/api/send':
            return _json(200, chat.send(client_key(headers, via_relay=via_relay), document))
        return _json(200, chat.events(document))
    except ChatError as error:
        return _json(error.status, {'error': error.code})


def _json(status: int, value: Any) -> tuple[int, str, bytes]:
    return status, 'application/json', json.dumps(value, separators=(',', ':')).encode()
