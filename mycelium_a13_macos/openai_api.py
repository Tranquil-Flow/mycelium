# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenAI-compatible API for a PRIVATE swarm, served by the owner app (PV22).

    POST /v1/chat/completions   (stream and non-stream)        GET /v1/models, GET /v1/models/{id}
    Authorization: Bearer <owner-issued key>                   (openai_keys.KeyStore: hashed, per-key quotas)

Served on its own loopback port (127.0.0.1 only, default 8793, off until the owner turns it on).  A second, explicit
owner setting ``expose_via_relay`` also answers ``/v1/*`` on the swarm's own relay origin; it is never routed through
the directory (mycelium.now), and the relay answers 404 for ``/v1/*`` unless that setting is on.

The route behind it is the one the shared chat page uses (``chat.RegistryClient`` against the run's loopback product
API): one request in flight for the swarm, greedy decoding, the model's own chat template applied to ONE user turn.
So ``temperature``/``top_p`` are accepted only at the values that mean the same as greedy (0 / 1) and every other
sampling or tool parameter is a clear 400; a multi-turn ``messages`` list is sent as a transcript inside that turn.
Chat content, keys and token text are never logged.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from .chat import ChatError, RegistryClient, service as chat_service
from .openai_keys import KeyStore, KeyStoreError, QuotaExceeded

DEFAULT_PORT = 8793
HOST = '127.0.0.1'
MAX_BODY = 262144
DRAIN_LIMIT = 4 * 1024 * 1024
MAX_PROMPT_BYTES = 131072  # the request gateway's own prompt bound
DEFAULT_MAX_TOKENS = 256
MAX_TOKENS_CAP = 4096  # the request gateway's own max_new_tokens bound
MAX_MESSAGES = 256
SETTINGS_FILE = 'settings.json'
SETTINGS_PROTOCOL = 'mycelium.openai_api_settings.v1'
ROUTES = frozenset({'/api/openai/status', '/api/openai/key/create', '/api/openai/key/revoke', '/api/openai/settings'})
_CODE = re.compile(r'[a-z0-9_]{1,96}\Z')
_CHAT_HTTP = re.compile(r'chat_http_(?:get|post|delete)_[a-z0-9]+_([0-9]{3})(?:_([a-z0-9_]*))?\Z')


class ApiSettingsError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ApiError(Exception):
    def __init__(self, status: int, etype: str, code: str, message: str, param: str | None = None,
                 retry_after: int | None = None) -> None:
        self.status, self.etype, self.code, self.message = status, etype, code, message
        self.param, self.retry_after = param, retry_after
        super().__init__(code)

    def body(self) -> bytes:
        return _dump({'error': {'message': self.message, 'type': self.etype, 'param': self.param, 'code': self.code}})

    def headers(self) -> tuple[tuple[str, str], ...]:
        extra: list[tuple[str, str]] = []
        if self.retry_after is not None:
            extra.append(('Retry-After', str(self.retry_after)))
        if self.status == 401:
            extra.append(('WWW-Authenticate', 'Bearer'))
        return tuple(extra)


class ClientGone(Exception):
    """The caller closed the connection mid-stream."""


class _Abort(Exception):
    pass


def _dump(value: Any) -> bytes:
    return json.dumps(value, separators=(',', ':'), allow_nan=False).encode()


# ---------------------------------------------------------------- error mapping ------------------------------------
#: gateway / route code -> (status, OpenAI error type, stable code, message, Retry-After seconds)
_UNAVAILABLE = (503, 'server_error', 'route_unavailable',
                'The swarm route changed or was dropped while the request was being served. Try again.', 5)
_BUSY = (429, 'rate_limit_error', 'swarm_busy', 'The swarm is serving another request. Try again shortly.', 2)
_FAILED = (502, 'server_error', 'route_failed', 'The swarm route failed while serving the request.', None)
_INVALID = (400, 'invalid_request_error', 'invalid_request', 'The swarm route refused the request as invalid.', None)
_CANCELLED = (503, 'server_error', 'request_cancelled', 'The request was cancelled before it finished.', 1)
_NOT_READY = (503, 'server_error', 'swarm_not_ready', 'The swarm is not serving yet. Try again shortly.', 5)
_GENERIC = (500, 'server_error', 'generation_failed', 'The request failed.', None)
_BY_CODE: dict[str, tuple] = {
    **{code: _UNAVAILABLE for code in (
        'stale_qualification', 'qualification_mismatch', 'deployment_epoch_changed', 'path_changed', 'route_dropped',
        'readiness_revoked', 'qualification_unavailable')},
    'gateway_capacity_exhausted': _BUSY, 'event_backpressure': _BUSY,
    'slow_consumer': (408, 'invalid_request_error', 'slow_consumer',
                      'The stream was closed because the client read too slowly.', None),
    'request_cancelled': _CANCELLED, 'client_disconnected': _CANCELLED,
    'invalid_submission': _INVALID, 'invalid_request_body': _INVALID, 'invalid_encoded_prompt': _INVALID,
    'encoded_prompt_mismatch': _INVALID,
    'request_body_too_large': (413, 'invalid_request_error', 'request_too_large',
                               'The swarm route refused the request as too large.', None),
    'backend_failed': _FAILED, 'router_decode_stalled': _FAILED, 'token_order_violation': _FAILED,
    'stream_event_timeout': _FAILED, 'stream_closed': _FAILED,
    'chat_route_not_qualified': _NOT_READY, 'chat_not_ready': _NOT_READY,
    'chat_warming_up': _NOT_READY, 'route_rejoining': _NOT_READY,
    'route_rejoining_busy': _BUSY, 'chat_busy': _BUSY,
}


def map_code(code: Any, *, retry_after: int | None = None) -> ApiError:
    """A bounded router/gateway/chat code -> the OpenAI-shaped error that carries it."""
    text = code if isinstance(code, str) and _CODE.fullmatch(code) else None
    upstream = text
    entry = _BY_CODE.get(text or '')
    if entry is None and text:
        found = _CHAT_HTTP.fullmatch(text)
        if found:
            upstream = found.group(2) or text
            entry = _BY_CODE.get(found.group(2) or '', _FAILED)
        elif text.startswith('chat_generation_failed_'):
            entry = _GENERIC
    if entry is None:
        entry = _GENERIC
    status, etype, stable, message, retry = entry
    if upstream:
        message += f' (upstream: {upstream})'
    return ApiError(status, etype, stable, message, retry_after=retry_after if retry_after is not None else retry)


def _error_for_exception(error: BaseException) -> ApiError:
    if isinstance(error, ChatError):
        return map_code(error.code, retry_after=error.extra.get('retry_after_s'))
    if isinstance(error, OSError):
        return ApiError(*_FAILED[:4])
    return ApiError(*_GENERIC[:4])


# ---------------------------------------------------------------- request validation ------------------------------
_ROLES = {'system': 'system', 'developer': 'system', 'user': 'user', 'assistant': 'assistant'}
#: parameter -> (value that equals this route's behaviour, why nothing else is accepted)
_ROUTE_FIXED = {
    'temperature': (0, 'This private swarm decodes greedily: only temperature 0 (or omitting it) is supported.'),
    'top_p': (1, 'This private swarm decodes greedily: only top_p 1 (or omitting it) is supported.'),
    'n': (1, 'This private swarm returns one choice: only n 1 (or omitting it) is supported.'),
    'presence_penalty': (0, 'Penalties are not supported by this private swarm: only 0 (or omit it).'),
    'frequency_penalty': (0, 'Penalties are not supported by this private swarm: only 0 (or omit it).'),
}
_ROUTE_ABSENT = {
    'stop': 'Stop sequences are not supported by this private swarm.',
    'seed': 'Seeds are not supported: this private swarm decodes greedily.',
    'logprobs': 'Log probabilities are not supported by this private swarm.',
    'top_logprobs': 'Log probabilities are not supported by this private swarm.',
    'tools': 'Tool calling is not supported by this private swarm.',
    'tool_choice': 'Tool calling is not supported by this private swarm.',
    'parallel_tool_calls': 'Tool calling is not supported by this private swarm.',
    'functions': 'Function calling is not supported by this private swarm.',
    'function_call': 'Function calling is not supported by this private swarm.',
    'logit_bias': 'logit_bias is not supported by this private swarm.',
    'metadata': 'metadata is not supported by this private swarm.',
    'modalities': 'Only text output is supported by this private swarm.',
    'audio': 'Audio is not supported by this private swarm.',
    'prediction': 'Predicted outputs are not supported by this private swarm.',
    'reasoning_effort': 'reasoning_effort is not supported by this private swarm.',
    'service_tier': 'service_tier is not supported by this private swarm.',
}
_KNOWN = ({'model', 'messages', 'max_tokens', 'max_completion_tokens', 'stream', 'stream_options', 'user',
           'response_format', 'store'} | set(_ROUTE_FIXED) | set(_ROUTE_ABSENT))


def _bad(param: str, code: str, message: str, status: int = 400) -> ApiError:
    return ApiError(status, 'invalid_request_error', code, message, param)


class ParsedRequest:
    __slots__ = ('model', 'prompt', 'max_tokens', 'stream', 'include_usage')

    def __init__(self, model: str, prompt: str, max_tokens: int, stream: bool, include_usage: bool) -> None:
        self.model, self.prompt, self.max_tokens, self.stream, self.include_usage = (
            model, prompt, max_tokens, stream, include_usage)


def _content_text(content: Any, param: str) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        parts = []
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'text' or not isinstance(part.get('text'), str):
                raise _bad(param, 'unsupported_parameter', 'Only text content parts are supported by this private swarm.')
            parts.append(part['text'])
        return ''.join(parts)
    raise _bad(param, 'invalid_value', 'content must be a string or a list of text parts.')


def render_prompt(messages: Any) -> str:
    if not isinstance(messages, list):
        raise _bad('messages', 'invalid_type', 'messages must be a list.')
    if not messages or len(messages) > MAX_MESSAGES:
        raise _bad('messages', 'invalid_value', f'messages must hold 1 to {MAX_MESSAGES} entries.')
    system: list[str] = []
    turns: list[tuple[str, str]] = []
    for index, message in enumerate(messages):
        where = f'messages[{index}]'
        if not isinstance(message, dict):
            raise _bad(where, 'invalid_type', 'Each message must be an object.')
        role = _ROLES.get(message.get('role')) if isinstance(message.get('role'), str) else None
        if role is None:
            raise _bad(where + '.role', 'invalid_value', "role must be 'system', 'user' or 'assistant'.")
        if message.get('content') is None:
            raise _bad(where + '.content', 'invalid_value', 'content is required.')
        text = _content_text(message['content'], where + '.content')
        if role == 'system':
            system.append(text)
        else:
            turns.append((role, text))
    if not turns or turns[-1][0] != 'user':
        raise _bad('messages', 'invalid_value', 'The last message must be from the user.')
    head = '\n\n'.join(system)
    if len(turns) == 1:
        prompt = (head + '\n\n' if head else '') + turns[0][1]
    else:
        lines = [('User: ' if role == 'user' else 'Assistant: ') + text for role, text in turns]
        prompt = (head + '\n\n' if head else '') + '\n'.join(lines) + '\nAssistant:'
    if not prompt.strip():
        raise _bad('messages', 'invalid_value', 'The conversation is empty.')
    if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise _bad('messages', 'context_length_exceeded',
                   f'The conversation is larger than the swarm route accepts ({MAX_PROMPT_BYTES} bytes).')
    return prompt


def _number(value: Any) -> bool:
    return type(value) in (int, float) and not isinstance(value, bool) and value == value


def parse_chat_request(document: Any) -> ParsedRequest:
    if not isinstance(document, dict):
        raise ApiError(400, 'invalid_request_error', 'invalid_json', 'The request body must be a JSON object.')
    for name in document:
        if name not in _KNOWN:
            raise _bad(str(name)[:64], 'unknown_parameter', f'Unrecognized request argument: {str(name)[:64]}.')
    model = document.get('model')
    if not isinstance(model, str) or not model:
        raise _bad('model', 'invalid_value' if model is not None else 'missing_required_parameter',
                   'model is required: use the id from GET /v1/models.')
    for name, (wanted, message) in _ROUTE_FIXED.items():
        value = document.get(name)
        if value is None:
            continue
        if not _number(value):
            raise _bad(name, 'invalid_type', f'{name} must be a number.')
        if value != wanted:
            raise _bad(name, 'unsupported_parameter', message)
    for name, message in _ROUTE_ABSENT.items():
        value = document.get(name)
        if value is None or (name == 'logit_bias' and value == {}) or (name == 'logprobs' and value is False):
            continue
        raise _bad(name, 'unsupported_parameter', message)
    fmt = document.get('response_format')
    if fmt is not None and fmt != {'type': 'text'}:
        raise _bad('response_format', 'unsupported_parameter', 'Only response_format {"type":"text"} is supported.')
    if document.get('store') not in (None, False):
        raise _bad('store', 'unsupported_parameter', 'store is not supported by this private swarm.')
    user = document.get('user')
    if user is not None and (not isinstance(user, str) or len(user) > 256):
        raise _bad('user', 'invalid_type', 'user must be a string of at most 256 characters.')
    stream = document.get('stream', False)
    if stream is None:
        stream = False
    if type(stream) is not bool:
        raise _bad('stream', 'invalid_type', 'stream must be a boolean.')
    options = document.get('stream_options')
    include_usage = False
    if options is not None:
        if (not stream or not isinstance(options, dict) or set(options) - {'include_usage'}
                or type(options.get('include_usage', False)) is not bool):
            raise _bad('stream_options', 'invalid_value',
                       'stream_options is only valid with stream true and only include_usage is supported.')
        include_usage = options.get('include_usage', False)
    limits = {}
    for name in ('max_tokens', 'max_completion_tokens'):
        value = document.get(name)
        if value is None:
            continue
        if type(value) is not int:
            raise _bad(name, 'invalid_type', f'{name} must be an integer.')
        if not 1 <= value <= MAX_TOKENS_CAP:
            raise _bad(name, 'invalid_value', f'{name} must be between 1 and {MAX_TOKENS_CAP}.')
        limits[name] = value
    if len(set(limits.values())) > 1:
        raise _bad('max_completion_tokens', 'invalid_value', 'max_tokens and max_completion_tokens disagree.')
    max_tokens = next(iter(limits.values()), DEFAULT_MAX_TOKENS)
    prompt = render_prompt(document.get('messages'))
    return ParsedRequest(model, prompt, max_tokens, stream, include_usage)


# ---------------------------------------------------------------- responders ---------------------------------------
class _BufferResponder:
    """Collects a whole answer (the relay hands back one buffer; streams are emitted whole)."""

    def __init__(self) -> None:
        self.status, self.ctype, self.extra, self.chunks, self.streaming = 500, 'application/json', (), [], False

    def send(self, status: int, body: bytes, ctype: str = 'application/json', headers: tuple = ()) -> None:
        self.status, self.ctype, self.extra, self.chunks = status, ctype, tuple(headers), [body]

    def begin_stream(self) -> None:
        self.status, self.ctype, self.streaming = 200, 'text/event-stream', True

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def result(self) -> tuple[int, str, bytes, tuple]:
        return self.status, self.ctype, b''.join(self.chunks), self.extra


class _HttpResponder:
    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self.handler = handler

    def _head(self, status: int, ctype: str, headers: tuple, length: int | None) -> None:
        h = self.handler
        h.send_response(status)
        h.send_header('Content-Type', ctype)
        if length is not None:
            h.send_header('Content-Length', str(length))
        h.send_header('Cache-Control', 'no-store' if length is not None else 'no-cache')
        h.send_header('Connection', 'close')
        h.send_header('X-Content-Type-Options', 'nosniff')
        for name, value in headers:
            h.send_header(name, value)
        h.end_headers()
        h.close_connection = True

    def send(self, status: int, body: bytes, ctype: str = 'application/json', headers: tuple = ()) -> None:
        try:
            self._head(status, ctype, headers, len(body))
            self.handler.wfile.write(body)
        except OSError:
            pass

    def begin_stream(self) -> None:
        try:
            self._head(200, 'text/event-stream', (), None)
            self.handler.wfile.flush()
        except OSError as error:
            raise ClientGone() from error

    def write(self, data: bytes) -> None:
        try:
            self.handler.wfile.write(data)
            self.handler.wfile.flush()
        except OSError as error:
            raise ClientGone() from error


def _frame(value: Any) -> bytes:
    return b'data: ' + _dump(value) + b'\n\n'


# ---------------------------------------------------------------- the API --------------------------------------------
class OpenAIApi:
    def __init__(self, app: Any, *, client_factory: Callable[[str], Any] = RegistryClient,
                 clock: Callable[[], float] = time.time) -> None:
        self.app = app
        self._client_factory = client_factory
        self._clock = clock
        self.keys = KeyStore(Path(app.root) / 'openai-api', clock=clock)
        self._settings = self._load_settings()
        self._lock = threading.RLock()
        self.server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._created = int(clock())

    # -- settings -----------------------------------------------------------
    def _load_settings(self) -> dict[str, Any]:
        document = self.keys.read_json(SETTINGS_FILE)
        defaults = {'enabled': False, 'port': DEFAULT_PORT, 'expose_via_relay': False}
        if document is None:
            return defaults
        value = document.get('settings')
        if (document.get('protocol') != SETTINGS_PROTOCOL or not isinstance(value, dict) or set(value) != set(defaults)
                or type(value['enabled']) is not bool or type(value['expose_via_relay']) is not bool
                or not self._port_ok(value['port'])):
            raise ApiSettingsError('openai_settings_corrupt')
        return dict(value)

    @staticmethod
    def _port_ok(port: Any) -> bool:
        return type(port) is int and (port == 0 or 1024 <= port <= 65535)

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)

    @property
    def running(self) -> bool:
        return self.server is not None

    @property
    def port(self) -> int | None:
        return None if self.server is None else self.server.server_address[1]

    def apply_settings(self, *, enabled: bool | None = None, port: int | None = None,
                       expose_via_relay: bool | None = None, relay_acknowledged: bool = False) -> dict[str, Any]:
        with self._lock:
            new = dict(self._settings)
            if port is not None:
                if not self._port_ok(port):
                    raise ApiSettingsError('openai_port_invalid')
                new['port'] = port
            if enabled is not None:
                new['enabled'] = bool(enabled)
            if expose_via_relay is not None:
                if expose_via_relay and not self._settings['expose_via_relay'] and relay_acknowledged is not True:
                    raise ApiSettingsError('openai_relay_consent_required')
                new['expose_via_relay'] = bool(expose_via_relay)
            restart = (new['enabled'] != self.running or (new['enabled'] and port is not None
                                                           and port != self._settings['port']))
            if restart:
                previous = self.server
                if previous is not None:
                    self._shutdown()
                if new['enabled']:
                    try:
                        self._listen(new['port'])
                    except ApiSettingsError:
                        if previous is not None:  # keep serving where it was
                            try:
                                self._listen(self._settings['port'])
                            except ApiSettingsError:
                                pass
                        raise
            self._settings = new
            self.keys.write_json(SETTINGS_FILE, {'protocol': SETTINGS_PROTOCOL, 'settings': new})
            self.bind_relay()
            return self.settings()

    # -- lifecycle ------------------------------------------------------------
    def _listen(self, port: int) -> None:
        handler = type('_Bound', (_Handler,), {'api': self})
        try:
            server = _Server((HOST, port), handler)
        except OSError as error:
            raise ApiSettingsError('openai_port_in_use' if error.errno in (48, 98, 10048)
                                   else 'openai_port_unavailable') from None
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), name='a13-openai-api', daemon=True)
        thread.start()
        self.server, self._thread = server, thread

    def _shutdown(self) -> None:
        server, thread = self.server, self._thread
        self.server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(5)

    def start(self) -> None:
        with self._lock:
            if self._settings['enabled'] and self.server is None:
                self._listen(self._settings['port'])
            self.bind_relay()

    def stop(self) -> None:
        with self._lock:
            self._shutdown()

    def close(self) -> None:
        with self._lock:
            self._shutdown()
            self._set_relay_handler(None)

    # -- relay ----------------------------------------------------------------
    def _relay(self) -> Any:
        return getattr(getattr(self.app, 'public_relay', None), 'proxy', None)

    def _set_relay_handler(self, handler: Any) -> bool:
        bind = getattr(self._relay(), 'bind_openai', None)
        if bind is None:
            return False
        bind(handler)
        return True

    def bind_relay(self) -> bool:
        """(Un)bind ``/v1/*`` on the swarm's relay origin to match the owner's setting."""
        return self._set_relay_handler(self.handle_relay if self._settings['expose_via_relay'] else None)

    def relay_url(self) -> str | None:
        origin = getattr(getattr(self.app, 'public_relay', None), 'public_origin', None)
        if self._settings['expose_via_relay'] and isinstance(origin, str) and origin.startswith('https://'):
            return origin.rstrip('/') + '/v1'
        return None

    def status(self) -> dict[str, Any]:
        return {'settings': self.settings(), 'running': self.running,
                'url': None if self.port is None else f'http://{HOST}:{self.port}/v1',
                'relay_url': self.relay_url(), 'model_id': self._model_id(), 'keys': self.keys.list()}

    # -- request handling -------------------------------------------------------
    def handle_relay(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple:
        if not self._settings['expose_via_relay']:
            return 404, 'application/json', b'{"error":"relay_route_unknown"}', ()
        responder = _BufferResponder()
        self.handle(method, path, headers, body, responder)
        return responder.result()

    def _model_id(self) -> str | None:
        try:
            value = self.app.run_model_contract().model_id
        except Exception:  # noqa: BLE001
            return None
        return value if isinstance(value, str) and value else None

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes, responder: Any) -> None:
        try:
            self._route(method, path.split('?', 1)[0], headers, body, responder)
        except ApiError as error:
            responder.send(error.status, error.body(), headers=error.headers())
        except ClientGone:
            pass
        except Exception:  # noqa: BLE001 - never leak internals or content
            responder.send(500, ApiError(500, 'server_error', 'internal_error', 'Internal error.').body())

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if key.lower() == name:
                return value
        return None

    def _authenticate(self, headers: Mapping[str, str]) -> dict[str, Any]:
        value = self._header(headers, 'authorization')
        if value is None:
            raise ApiError(401, 'authentication_error', 'missing_api_key',
                           'No API key provided. Send "Authorization: Bearer <key>" with a key from the Mycelium app.')
        scheme, _, secret = value.partition(' ')
        record = self.keys.authenticate(secret.strip()) if scheme.lower() == 'bearer' else None
        if record is None:
            raise ApiError(401, 'authentication_error', 'invalid_api_key',
                           'The API key is not valid or has been revoked.')
        return record

    def _route(self, method: str, path: str, headers: Mapping[str, str], body: bytes, responder: Any) -> None:
        models = path == '/v1/models'
        one_model = path.startswith('/v1/models/') and len(path) > len('/v1/models/')
        if path != '/v1/chat/completions' and not models and not one_model:
            raise ApiError(404, 'not_found_error', 'not_found', f'Invalid URL ({method} {path[:64]}).')
        key = self._authenticate(headers)
        wanted = 'POST' if path == '/v1/chat/completions' else 'GET'
        if method != wanted:
            raise ApiError(405, 'invalid_request_error', 'method_not_allowed',
                           f'{method} is not allowed on {path[:64]}; use {wanted}.')
        if path == '/v1/chat/completions':
            self._chat(key, body, responder)
            return
        model = self._model_id()
        if model is None:
            raise ApiError(503, 'server_error', 'swarm_not_ready', 'The swarm has no pinned model yet.', retry_after=5)
        entry = {'id': model, 'object': 'model', 'created': self._created, 'owned_by': 'mycelium-private-swarm'}
        if one_model:
            if path[len('/v1/models/'):] != model:
                raise ApiError(404, 'not_found_error', 'model_not_found', "The model does not exist on this swarm.",
                               param='model')
            responder.send(200, _dump(entry))
        else:
            responder.send(200, _dump({'object': 'list', 'data': [entry]}))

    def _serving(self) -> dict[str, Any]:
        chat = chat_service(self.app)
        serving = chat._serving()
        if serving is None:
            try:
                serving = chat._await_rejoin()
            except ChatError as error:
                raise map_code(error.code, retry_after=error.extra.get('retry_after_s')) from None
        if serving is None:
            raise map_code('chat_warming_up' if chat._warming_up() else 'chat_not_ready')
        return serving

    def _chat(self, key: dict[str, Any], body: bytes, responder: Any) -> None:
        try:
            document = json.loads(body)
        except (ValueError, UnicodeError):
            raise ApiError(400, 'invalid_request_error', 'invalid_json', 'The request body is not valid JSON.') from None
        parsed = parse_chat_request(document)
        model = self._model_id()
        if parsed.model != model:
            raise ApiError(404, 'not_found_error', 'model_not_found',
                           'The model does not exist on this swarm. List it with GET /v1/models.', param='model')
        serving = self._serving()
        chat, marker = chat_service(self.app), 'openai-' + secrets.token_hex(8)
        with chat._lock:  # the route serves one request for the swarm: take the chat page's own busy flag
            if chat._busy is not None:
                raise map_code('chat_busy')
            chat._busy = marker
        try:
            try:
                reservation = self.keys.admit(key['id'], parsed.max_tokens)
            except QuotaExceeded as error:
                raise self._quota_error(key, error, parsed) from None
            except KeyStoreError:  # revoked between authenticate and admit
                raise ApiError(401, 'authentication_error', 'invalid_api_key',
                               'The API key is not valid or has been revoked.') from None
            self._run(parsed, serving, reservation, responder)
        finally:
            with chat._lock:
                if chat._busy == marker:
                    chat._busy = None

    @staticmethod
    def _quota_error(key: dict[str, Any], error: QuotaExceeded, parsed: ParsedRequest) -> ApiError:
        quota = key['quota']
        if error.retry_after_s is None:
            return ApiError(400, 'invalid_request_error', 'max_tokens_exceeds_quota',
                            f"max_tokens {parsed.max_tokens} is larger than this key's whole token quota "
                            f"({quota['tokens_per_window']} per {quota['window_seconds']} s).", param='max_tokens')
        limit = quota['requests_per_window'] if error.kind == 'requests' else quota['tokens_per_window']
        return ApiError(429, 'rate_limit_error', 'rate_limit_exceeded',
                        f'Rate limit reached for {error.kind} on this key ({limit} per {quota["window_seconds"]} s). '
                        f'Try again in {error.retry_after_s} s.', retry_after=error.retry_after_s)

    def _run(self, parsed: ParsedRequest, serving: Mapping[str, Any], reservation: Any, responder: Any) -> None:
        completion_id = 'chatcmpl-' + secrets.token_hex(12)
        created = int(self._clock())
        state = {'count': 0, 'begun': False}
        pieces: list[str] = []
        client = self._client_factory(serving['registry_origin'])

        def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
            return _frame({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created,
                           'model': parsed.model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})

        def begin() -> None:
            responder.begin_stream()
            state['begun'] = True
            responder.write(chunk({'role': 'assistant', 'content': ''}))

        def on_token(text: str) -> None:
            state['count'] += 1
            if not parsed.stream:
                pieces.append(text or '')
                return
            try:
                if not state['begun']:
                    begin()
                if text:
                    responder.write(chunk({'content': text}))
            except ClientGone:
                try:
                    client.cancel()
                except Exception:  # noqa: BLE001
                    pass
                raise _Abort() from None

        failure: ApiError | None = None
        try:
            result = client.stream(parsed.prompt, parsed.max_tokens, on_token)
        except _Abort:
            self.keys.settle(reservation, state['count'])
            return
        except Exception as error:  # noqa: BLE001
            failure = _error_for_exception(error)
        else:
            terminal = result.get('terminal') if isinstance(result, dict) else None
            if terminal == 'failed':
                failure = map_code(result.get('code'))
            elif terminal == 'cancelled':
                failure = map_code('request_cancelled')
            elif terminal != 'completed':
                failure = ApiError(*_FAILED[:4])
        count = state['count']
        if failure is not None and count == 0:
            self.keys.release(reservation)  # the swarm produced nothing: the request does not count
        else:
            self.keys.settle(reservation, count)
        prompt_tokens = max(1, math.ceil(len(parsed.prompt.encode('utf-8')) / 4))
        usage = {'prompt_tokens': prompt_tokens, 'completion_tokens': count, 'total_tokens': prompt_tokens + count}
        if failure is not None:
            if state['begun']:
                try:
                    responder.write(_frame({'error': {'message': failure.message, 'type': failure.etype,
                                                       'param': None, 'code': failure.code}}))
                except ClientGone:
                    pass
            else:
                responder.send(failure.status, failure.body(), headers=failure.headers())
            return
        finish = 'length' if count >= parsed.max_tokens else 'stop'
        if not parsed.stream:
            responder.send(200, _dump({
                'id': completion_id, 'object': 'chat.completion', 'created': created, 'model': parsed.model,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''.join(pieces)},
                             'finish_reason': finish}], 'usage': usage}))
            return
        try:
            if not state['begun']:
                begin()
            responder.write(chunk({}, finish))
            if parsed.include_usage:
                responder.write(_frame({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created,
                                        'model': parsed.model, 'choices': [], 'usage': usage}))
            responder.write(b'data: [DONE]\n\n')
        except ClientGone:
            pass


# ---------------------------------------------------------------- HTTP server --------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    api: OpenAIApi

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args: Any) -> None:
        return

    def _allowed_host(self) -> bool:
        hosts = self.headers.get_all('Host') or []
        port = self.server.server_address[1]
        return len(hosts) == 1 and hosts[0].lower() in {f'127.0.0.1:{port}', f'localhost:{port}'}

    def _serve(self) -> None:
        responder = _HttpResponder(self)
        self.close_connection = True
        if not self._allowed_host():
            responder.send(403, ApiError(403, 'invalid_request_error', 'host_rejected',
                                         'Use http://127.0.0.1 or http://localhost with this port.').body())
            return
        body = b''
        if self.command == 'POST':
            if self.headers.get('Transfer-Encoding') is not None:
                responder.send(400, ApiError(400, 'invalid_request_error', 'chunked_unsupported',
                                             'Send the body with a Content-Length.').body())
                return
            lengths = self.headers.get_all('Content-Length') or []
            try:
                length = int(lengths[0]) if len(lengths) == 1 else -1
            except ValueError:
                length = -1
            if length < 0:
                responder.send(400, ApiError(400, 'invalid_request_error', 'invalid_json',
                                             'A Content-Length is required.').body())
                return
            if length > MAX_BODY:
                if length <= DRAIN_LIMIT:
                    try:
                        remaining = length
                        while remaining > 0:
                            piece = self.rfile.read(min(65536, remaining))
                            if not piece:
                                break
                            remaining -= len(piece)
                    except OSError:
                        pass
                responder.send(413, ApiError(413, 'invalid_request_error', 'request_too_large',
                                             f'The request body is larger than {MAX_BODY} bytes.').body())
                return
            try:
                body = self.rfile.read(length)
            except OSError:
                return
            if len(body) != length:
                return
        self.connection.settimeout(60)
        self.api.handle(self.command, self.path, {k: v for k, v in self.headers.items()}, body, responder)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_HEAD = _serve


# ---------------------------------------------------------------- app glue ------------------------------------------
def service(app: Any) -> OpenAIApi:
    api = getattr(app, 'openai_api', None)
    if api is None:
        api = OpenAIApi(app)
        app.openai_api = api
    return api


def start(app: Any) -> None:
    if getattr(app, 'installation_role', None) == 'owner':
        service(app).start()


def close(app: Any) -> None:
    api = getattr(app, 'openai_api', None)
    if api is not None:
        api.close()


def bind_relay(app: Any) -> None:
    """Called whenever a relay comes up; never lets this API's state break the relay."""
    try:
        if getattr(app, 'installation_role', None) == 'owner':
            service(app).bind_relay()
    except Exception:  # noqa: BLE001
        pass


def action(app: Any, path: str, body: Any) -> dict[str, Any]:
    from .app import DemoError
    if app.installation_role != 'owner':
        raise DemoError('owner_role_required')
    if path not in ROUTES or not isinstance(body, dict):
        raise DemoError('invalid_request')
    try:
        api = service(app)
        if path == '/api/openai/status':
            if body:
                raise DemoError('invalid_request')
            return api.status()
        if path == '/api/openai/key/create':
            if not set(body) <= {'label', 'requests_per_window', 'tokens_per_window', 'window_seconds'}:
                raise DemoError('invalid_request')
            options = {k: v for k, v in body.items() if k != 'label'}
            record, secret = api.keys.create(label=body.get('label'), **options)
            return {'key': record, 'secret': secret}
        if path == '/api/openai/key/revoke':
            if set(body) != {'id'}:
                raise DemoError('invalid_request')
            return {'key': api.keys.revoke(body['id'])}
        if not set(body) <= {'enabled', 'port', 'expose_via_relay', 'relay_acknowledged'}:
            raise DemoError('invalid_request')
        for name in ('enabled', 'expose_via_relay', 'relay_acknowledged'):
            if name in body and type(body[name]) is not bool:
                raise DemoError('invalid_request')
        api.apply_settings(enabled=body.get('enabled'), port=body.get('port'),
                           expose_via_relay=body.get('expose_via_relay'),
                           relay_acknowledged=body.get('relay_acknowledged', False))
        return api.status()
    except (KeyStoreError, ApiSettingsError) as error:
        raise DemoError(error.code) from None
