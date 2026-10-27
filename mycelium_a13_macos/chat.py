# SPDX-License-Identifier: AGPL-3.0-only
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
import ipaddress
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping

from mycelium_swarm_directory.wire import WireError
from mycelium_http_identity import with_user_agent

from .owner_run import EXECUTION_CEILING_CODE

MAX_PROMPT_CHARS = 2000
MAX_NEW_TOKENS_CAP = 256
DEFAULT_NEW_TOKENS = 128
PER_CLIENT_PER_MINUTE = 4
GLOBAL_PER_MINUTE = 30
MAX_KEPT_REQUESTS = 64
_ID = re.compile(r'[A-Za-z0-9_-]{16,64}\Z')
_CANCEL_PATH = re.compile(r'/api/v1/inference/[A-Za-z0-9_-]{1,128}/cancel\Z')
# Owner-run steps during which the route is being activated/qualified.
_WARMING_STEPS = frozenset({'activate', 'serving'})
# A request that arrives while this Mac is Rejoining (fence released, route not ready yet) waits this long
# for the route instead of being refused at once; kept below the browser and visitor request timeouts.
REJOIN_WAIT_S = 15
REJOIN_POLL_S = 0.25
REJOIN_WAITERS_MAX = 8
_ASSETS = {'/chat': ('chat.html', 'text/html; charset=utf-8'),
           '/chat/chat.js': ('chat.js', 'text/javascript; charset=utf-8'),
           '/chat/chat.css': ('chat.css', 'text/css; charset=utf-8')}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")


class ChatError(RuntimeError):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        self.status, self.code, self.extra = status, code, extra
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
        self._csrf: dict[str, str] = {}
        self._cancel_lock = threading.Lock()
        self._cancel_path: str | None = None  # set while a request is accepted and running
        self._cancel_wanted = False
        self._cancel_sent = False

    def _open(self, path, body=None, *, accept='application/json', method=None):
        """GETs are plain reads; only the POST carries CSRF, Origin and a JSON body
        (the gateway's rules; mycelium_ui_gateway/app.py)."""
        headers = {'Accept': accept}
        data = None
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': self.origin, **self._csrf})
            data = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
        elif method == 'DELETE':
            headers.update({'Origin': self.origin, **self._csrf})
        request = urllib.request.Request(self.origin + path, data=data, headers=with_user_agent(headers),
                                         method=method or ('GET' if body is None else 'POST'))
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            raise ChatError(502, _http_code(request.get_method(), path, error)) from None

    def _json(self, path, body=None):
        with self._open(path, body) as response:
            return json.load(response)

    def stream(self, prompt: str, max_new_tokens: int, on_token: Callable[[str], None]) -> dict[str, Any]:
        # Same request shape as the client that drove this API on hardware
        # (mycelium_a7_physical_measurement.py): request_gateway v2, interactive.
        session = self._json('/api/v1/bootstrap')['session']
        self._csrf = {session['csrf_header']: session['csrf_token']}
        # 503 is the registry's answer while no route is qualified (runs 13/14
        # surfaced it as chat_http_get_qualification_503): name the state.
        try:
            qualification = self._json('/api/v1/qualification/current')
        except ChatError as error:
            if error.code.startswith('chat_http_get_qualification_503'):
                raise ChatError(503, 'chat_route_not_qualified') from None
            raise
        if qualification.get('route_ready') is not True:
            raise ChatError(503, 'chat_route_not_qualified')
        accepted = self._json('/api/v1/inference', {
            'protocol': 'mycelium.request_gateway.v2', 'prompt': prompt,
            'max_new_tokens': max_new_tokens, 'qualification': qualification['binding'],
            'workload_profile_id': 'interactive_chat_v1', 'qos_class': 'interactive'})
        terminal = None
        kind = None
        self._arm_cancel(accepted)
        with self._open(accepted['event_path'], accept='text/event-stream') as response:
            for raw in response:
                line = raw.decode('utf-8', 'strict').rstrip('\r\n')
                if line.startswith('event: '):
                    kind = line[7:]
                elif line.startswith('data: ') and kind is not None:
                    event = json.loads(line[6:])
                    if kind == 'token':
                        on_token(event.get('text') or '')
                    elif kind in {'completed', 'cancelled', 'failed'}:
                        terminal = {'type': kind, 'code': event.get('code')}
                        break
                elif line == '':
                    kind = None
        with self._cancel_lock:
            self._cancel_path = None
        return {'terminal': None if terminal is None else terminal['type'],
                'code': None if terminal is None else terminal.get('code'),
                'deployment_id': qualification['binding'].get('deployment_id'),
                'model_id': qualification['binding'].get('model_id')}

    def read(self, path: str) -> Any:
        """A plain authenticated GET of one registry API document (bootstraps its own session)."""
        session = self._json('/api/v1/bootstrap')['session']
        self._csrf = {session['csrf_header']: session['csrf_token']}
        return self._json(path)

    # -- cancel: the gateway's own DELETE /api/v1/inference/<id>/cancel ---------------------
    def _arm_cancel(self, accepted: dict[str, Any]) -> None:
        path = accepted.get('cancel_path')
        if not isinstance(path, str) or not _CANCEL_PATH.fullmatch(path):
            path = None
        with self._cancel_lock:
            self._cancel_path = path
            wanted = self._cancel_wanted
        if wanted:  # the owner cancelled before the gateway had accepted the request
            self.cancel()

    def request_cancel(self) -> None:
        """Remember a cancel that arrived before the request was accepted; it is sent on acceptance."""
        with self._cancel_lock:
            self._cancel_wanted = True

    def cancel(self) -> bool:
        """Cancel the running request (once).  False: nothing running, already sent, or already terminal."""
        with self._cancel_lock:
            path = self._cancel_path
            if path is None or self._cancel_sent:
                return False
            self._cancel_sent = True
        with self._open(path, method='DELETE') as response:
            document = json.load(response)
        return isinstance(document, dict) and document.get('cancelled') is True


_CODE_PART = re.compile(r'[^a-z0-9]+')


def _bounded(text: str, limit: int = 96) -> str:
    return _CODE_PART.sub('_', text.lower()).strip('_')[:limit].rstrip('_')


def _http_code(method: str, path: str, error: Exception) -> str:
    """e.g. chat_http_post_inference_403_origin_mismatch (bounded, no IDs)."""
    route = path.rstrip('/').split('/')[3] if path.startswith('/api/v1/') else 'events'
    code = ''
    try:
        document = json.loads(error.read(4096) or b'{}')
        inner = document.get('error')
        code = inner.get('code', '') if isinstance(inner, dict) else (inner if isinstance(inner, str) else '')
    except Exception:
        pass
    return _bounded('chat_http_%s_%s_%s_%s' % (method, route, getattr(error, 'code', 0), code))


def failure_code(error: BaseException) -> str:
    """Bounded public code for an unexpected chat failure: never a bare code."""
    if isinstance(error, ChatError):
        return error.code
    return _bounded('chat_generation_failed_' + type(error).__name__)


class ChatService:
    def __init__(self, app: Any, *, client_factory: Callable[[str], Any] = RegistryClient,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.app = app
        self._sleep = sleep
        self._rejoin_waiters = 0
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

    def _switching_runs(self) -> bool:
        run = getattr(self.app, 'owner_run', None)
        status = {} if run is None else run.status()
        return (status.get('rollover') or {}).get('state') in {'switching', 'backoff'}

    def _warming_up(self) -> bool:
        """The owner run is activating or qualifying its route (not failed)."""
        run = getattr(self.app, 'owner_run', None)
        status = None if run is None else run.status()
        return bool(status) and status.get('state') == 'running' and status.get('step') in _WARMING_STEPS

    def _rejoining(self) -> bool:
        from .sharing_state import sharing_state
        try:
            return sharing_state(self.app)['state'] == 'rejoining'
        except Exception:  # noqa: BLE001
            return False

    def _await_rejoin(self) -> dict[str, Any] | None:
        """The only wait on the chat path: bounded, capped, and only while the state is ``rejoining``.

        Paused (drain/withdraw/manual fence), idle, warming-from-cold and failed runs never wait; a real
        failure is never masked.  The waiter slot is released on every exit, including a client that went away.
        """
        if not self._rejoining():
            return None
        with self._lock:
            if self._rejoin_waiters >= REJOIN_WAITERS_MAX:
                raise ChatError(503, 'route_rejoining_busy', retry_after_s=REJOIN_WAIT_S)
            self._rejoin_waiters += 1
        try:
            deadline = self._clock() + REJOIN_WAIT_S
            while True:
                serving = self._serving()
                if serving is not None:
                    return serving
                if not self._rejoining() or self._clock() >= deadline:
                    break
                self._sleep(min(REJOIN_POLL_S, max(0.0, deadline - self._clock())) or REJOIN_POLL_S)
            if self._rejoining():
                raise ChatError(503, 'route_rejoining', retry_after_s=5)
            return None
        finally:
            with self._lock:
                self._rejoin_waiters -= 1

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
            if self._switching_runs():
                raise ChatError(503, 'chat_switching_runs', retry_after_s=5)
            serving = self._await_rejoin()
        if serving is None:
            raise ChatError(503, 'chat_warming_up' if self._warming_up() else 'chat_not_ready')
        with self._lock:
            if self._busy is not None:
                raise ChatError(429, 'chat_busy')
            self._rate(client)
            request_id = secrets.token_urlsafe(18)
            record = {'id': request_id, 'tokens': [], 'done': False, 'error': None, 'terminal': None,
                      'client': None, 'cancel_reason': None,
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
            ended = self._run_ended_by_ceiling(record.get('run_id'))
            if ended:
                raise ChatError(503, ended, retry_after_s=5)
            client = self._client_factory(origin)
            with self._lock:
                record['client'] = client
                early = record['cancel_reason'] is not None
            if early:  # cancelled before the backend client existed
                request = getattr(client, 'request_cancel', None)
                if request is not None:
                    request()
            def token(text):
                with self._lock:
                    record['tokens'].append(text)
            result = client.stream(prompt, record['max_new_tokens'], token)
            ended = self._run_ended_by_ceiling(record.get('run_id')) if result.get('terminal') == 'failed' else None
            with self._lock:
                terminal = result.get('terminal')
                record['terminal'] = terminal
                if terminal == 'failed':
                    record['error'] = record['cancel_reason'] or ended or result.get('code') or 'chat_generation_failed'
                elif terminal == 'cancelled':
                    record['error'] = record['cancel_reason'] or 'chat_cancelled'
        except ChatError as error:
            ended = self._run_ended_by_ceiling(record.get('run_id'))
            with self._lock:
                reason = record['cancel_reason'] or ended
                record['terminal'] = 'cancelled' if reason else 'failed'
                record['error'] = reason or error.code
        except Exception as error:
            ended = self._run_ended_by_ceiling(record.get('run_id'))
            with self._lock:
                reason = record['cancel_reason'] or ended
                record['terminal'] = 'cancelled' if reason else 'failed'
                record['error'] = reason or failure_code(error)
        finally:
            with self._lock:
                record['done'] = True
                record['client'] = None
                record['finished'] = self._clock()
                if self._busy == record['id']:
                    self._busy = None

    def _run_ended_by_ceiling(self, run_id=None) -> str | None:
        """Explain a deadline or rollover, including the admission/enqueue race.

        A request whose stream dies with the route must say why, not surface the route's ``request_cleanup_unproven``.
        """
        try:
            run = getattr(self.app, 'owner_run', None)
            status = None if run is None else run.status()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(status, dict):
            if ((status.get('rollover') or {}).get('state') in {'switching', 'backoff'}
                    or (run_id is not None and status.get('run_id') != run_id
                        and status.get('reason') == 'rollover')):
                return 'chat_switching_runs'
            if status.get('error_code') == EXECUTION_CEILING_CODE:
                return EXECUTION_CEILING_CODE
        return None

    def cancel(self, request_id: Any, code: str = 'chat_cancelled') -> bool:
        """Stop a running generation on the route.  The request ends with ``terminal: cancelled`` and ``code``.

        True: a cancel was dispatched (or will be, the moment the backend request exists).  False: already
        finished/cancelled, or the backend could not be told (the generation then runs on; never raised).
        """
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            raise ChatError(400, 'chat_request_invalid')
        with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                raise ChatError(404, 'chat_request_unknown')
            if record['done'] or record['cancel_reason'] is not None:
                return False
            record['cancel_reason'] = code
            client = record['client']
        if client is None:
            return True
        try:
            sent = bool(client.cancel())
            if not sent:
                request = getattr(client, 'request_cancel', None)
                if request is None:
                    raise RuntimeError('nothing_to_cancel')
                request()
            return True
        except Exception:
            with self._lock:
                record['cancel_reason'] = None
            return False

    def cancel_all(self, code: str = 'chat_cancelled') -> int:
        with self._lock:
            running = [rid for rid, record in self._requests.items() if not record['done']]
        return sum(1 for rid in running if self._cancel_quietly(rid, code))

    def _cancel_quietly(self, request_id: str, code: str) -> bool:
        try:
            return self.cancel(request_id, code)
        except ChatError:
            return False

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
                    'error': record['error'], 'terminal': record['terminal'], 'served_by': record['served_by'],
                    'seconds': seconds}

    def state(self) -> dict[str, Any]:
        # Display reads must not call _serving/_warming_up: those retain the
        # live monitor for prompt admission and can drive network work.
        publisher = getattr(self.app, 'status_publisher', None)
        if publisher is not None and publisher._published is not None:
            view = publisher.read()
            run = view.get('owner_run') or {}
            model_id = (view.get('model_contract') or {}).get('model_id')
            rejoining = (view.get('sharing') or {}).get('state') == 'rejoining'
        else:
            owner = getattr(self.app, 'owner_run', None)
            snapshot = getattr(owner, 'status_snapshot', None)
            run = (snapshot() if snapshot is not None else owner.status()) if owner is not None else {}
            model_id = _model_id(self.app)
            rejoining = self._rejoining() if publisher is None else False
        serving = run if run.get('state') == 'serving' and run.get('registry_origin') else None
        return {'available': serving is not None, 'busy': self._busy is not None,
                'switching_runs': serving is None and (run.get('rollover') or {}).get('state') in {'switching', 'backoff'},
                'warming_up': serving is None and run.get('state') == 'running' and run.get('step') in _WARMING_STEPS,
                'rejoining': serving is None and rejoining,
                'model_id': None if serving is None else model_id,
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
    """The rate-limit key: the relay's ``Cf-Connecting-Ip`` normalised (IPv6 by its /64, v4-mapped as v4), else ``local``.

    A household or a rotating-address client holds a whole /64, so one address must not be a fresh budget per request.
    """
    if via_relay:
        value = headers.get('Cf-Connecting-Ip') or headers.get('cf-connecting-ip') or ''
        if re.fullmatch(r'[0-9A-Fa-f:.]{2,45}', value):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                return 'relay:unknown'
            if isinstance(address, ipaddress.IPv6Address):
                address = address.ipv4_mapped or address
            if isinstance(address, ipaddress.IPv6Address):
                return 'relay:' + str(ipaddress.ip_network((address, 64), strict=False).network_address) + '/64'
            return 'relay:' + str(address)
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
            return _json(200, _send(app, chat, client_key(headers, via_relay=via_relay), document, via_relay))
        return _json(200, chat.events(document))
    except ChatError as error:
        return _json(error.status, {'error': error.code, **error.extra})
    except WireError as error:  # the swarm entry's checks (kill switch, access mode, daily cap)
        return _json(error.status, {'error': error.code})


def _entry_guard(app: Any) -> Any:
    gateway = getattr(getattr(app, 'swarm_publisher', None), 'gateway', None)
    return getattr(gateway, 'guard', None)


def _send(app: Any, chat: ChatService, client: str, document: Any, via_relay: bool) -> dict[str, Any]:
    """A prompt from the relay's shared chat page goes through the published swarm's entry checks first.

    Only the public relay is gated (the loopback page is the owner's own).  Bodies the chat service would refuse
    anyway are handed straight to it, so nothing is charged for a request that does no work.
    """
    guard = _entry_guard(app) if via_relay else None
    wanted = document.get('max_new_tokens', DEFAULT_NEW_TOKENS) if isinstance(document, dict) else None
    if guard is None or type(wanted) is not int or wanted < 1:
        return chat.send(client, document)
    admission = guard.admit_relay_chat(client, min(wanted, MAX_NEW_TOKENS_CAP))
    if admission is None:
        return chat.send(client, document)
    try:
        return chat.send(client, {**document, 'max_new_tokens': min(wanted, admission.max_new_tokens)})
    except BaseException:
        guard.refund(admission)
        raise


def _json(status: int, value: Any) -> tuple[int, str, bytes]:
    return status, 'application/json', json.dumps(value, separators=(',', ':')).encode()
