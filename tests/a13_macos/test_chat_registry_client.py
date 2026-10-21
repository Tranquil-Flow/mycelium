# SPDX-License-Identifier: AGPL-3.0-only
"""chat.RegistryClient against a real HTTP server shaped like the product gateway.

The rules mirror mycelium_ui_gateway/app.py (session cookie, CSRF + exact Origin on
POST only, `event:`/`data:` SSE framing) and the request body follows the client
that already drove this API on hardware (mycelium_a7_physical_measurement.py).
Two-Mac run 12 reached serving and chat failed in 0.25 s with a bare
chat_generation_failed; these tests pin (1) the request shape and (2) that a
refusal is recorded as a bounded cause.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mycelium_a13_macos.chat import ChatError, RegistryClient, failure_code

BINDING = {'deployment_id': 'dep-1', 'model_id': 'Qwen/Qwen3-0.6B'}


class Gateway(BaseHTTPRequestHandler):
    refuse_post: tuple[int, str] | None = None
    qualification: tuple[int, dict] = (200, {'route_ready': True, 'binding': BINDING})
    seen: list = []

    def log_message(self, *args):
        pass

    def _json(self, status, document, cookie=None):
        raw = json.dumps(document).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.seen.append(('GET', self.path, dict(self.headers)))
        if self.path == '/api/v1/bootstrap':
            return self._json(200, {'session': {'csrf_header': 'X-Mycelium-CSRF', 'csrf_token': 't0k'}},
                              cookie='mycelium_session=s1; Path=/; HttpOnly')
        if 'mycelium_session=s1' not in (self.headers.get('Cookie') or ''):
            return self._json(401, {'error': {'code': 'session_required'}})
        if self.path == '/api/v1/qualification/current':
            return self._json(*self.qualification)
        if self.path == '/api/v1/inference/r1/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for kind, data in (('accepted', {}), ('token', {'token_index': 0, 'text': 'Hel'}),
                               ('token', {'token_index': 1, 'text': 'lo'}), ('completed', {})):
                # Like the gateway: the kind is the `event:` line.
                self.wfile.write(f'event: {kind}\ndata: {json.dumps(data)}\n\n'.encode())
            return
        return self._json(404, {'error': {'code': 'not_found'}})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        self.seen.append(('POST', self.path, dict(self.headers), body))
        if self.refuse_post is not None:
            status, code = self.refuse_post
            return self._json(status, {'error': {'code': code}})
        if self.headers.get('X-Mycelium-CSRF') != 't0k':
            return self._json(403, {'error': {'code': 'csrf_required'}})
        if self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_address[1]}':
            return self._json(403, {'error': {'code': 'origin_mismatch'}})
        return self._json(202, {'request_id': 'r1', 'event_path': '/api/v1/inference/r1/events'})


@pytest.fixture
def gateway():
    Gateway.refuse_post = None
    Gateway.qualification = (200, {'route_ready': True, 'binding': BINDING})
    Gateway.seen = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_stream_follows_the_gateway_contract_and_returns_tokens(gateway):
    origin = 'http://127.0.0.1:%d' % gateway.server_address[1]
    tokens = []
    result = RegistryClient(origin, timeout=5).stream('hi', 16, tokens.append)
    assert tokens == ['Hel', 'lo']
    assert result['terminal'] == 'completed' and result['model_id'] == 'Qwen/Qwen3-0.6B'
    [post] = [s for s in Gateway.seen if s[0] == 'POST']
    assert post[3] == {'protocol': 'mycelium.request_gateway.v2', 'prompt': 'hi', 'max_new_tokens': 16,
                       'qualification': BINDING, 'workload_profile_id': 'interactive_chat_v1',
                       'qos_class': 'interactive'}
    # GETs carry no CSRF/Origin/Content-Type (only the POST is a state change).
    for method, path, headers, *_ in Gateway.seen:
        if method == 'GET':
            assert 'X-Mycelium-CSRF' not in headers and 'Content-Type' not in headers


def test_a_refused_request_is_recorded_as_a_bounded_cause(gateway):
    Gateway.refuse_post = (403, 'origin_mismatch')
    origin = 'http://127.0.0.1:%d' % gateway.server_address[1]
    with pytest.raises(ChatError) as caught:
        RegistryClient(origin, timeout=5).stream('hi', 16, lambda _t: None)
    assert caught.value.code == 'chat_http_post_inference_403_origin_mismatch'


def test_failure_code_is_bounded_and_never_bare_for_unexpected_errors():
    assert failure_code(ValueError('x' * 500)) == 'chat_generation_failed_valueerror'
    assert failure_code(KeyError('event_path')) == 'chat_generation_failed_keyerror'
    code = failure_code(RuntimeError('boom'))
    assert len(code) <= 96 and code.replace('_', '').isalnum()


@pytest.mark.parametrize('qualification', [
    # Two-Mac runs 13/14: the registry served its API with no qualified route;
    # chat recorded chat_http_get_qualification_503 for minutes.
    (503, {'error': {'code': 'qualification_unavailable'}}),
    (200, {'route_ready': False, 'binding': BINDING}),
])
def test_an_unqualified_route_is_named_not_qualified_not_an_error(gateway, qualification):
    Gateway.qualification = qualification
    origin = 'http://127.0.0.1:%d' % gateway.server_address[1]
    with pytest.raises(ChatError) as caught:
        RegistryClient(origin, timeout=5).stream('hi', 16, lambda _t: None)
    assert (caught.value.status, caught.value.code) == (503, 'chat_route_not_qualified')
    assert not [s for s in Gateway.seen if s[0] == 'POST']
