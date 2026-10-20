# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV22 wire proofs: the real ``openai`` Python client -> the owner app's API -> the real ``chat.RegistryClient`` ->
a stand-in product gateway.

FAKE (labelled): ``Gateway`` is a stand-in HTTP server shaped like mycelium_ui_gateway/app.py (session cookie, CSRF +
exact Origin on POST, ``event:``/``data:`` SSE).  Everything between it and the ``openai`` package is shipped code.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mycelium_a13_macos.app import private_dir
from mycelium_a13_macos.chat import ChatService
from mycelium_a13_macos.openai_api import OpenAIApi

BINDING = {'deployment_id': 'dep-1', 'model_id': 'Qwen/Qwen3-0.6B'}


class Gateway(BaseHTTPRequestHandler):
    events: list = []
    refuse: tuple | None = None
    posted: list = []

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
        if self.path == '/api/v1/bootstrap':
            return self._json(200, {'session': {'csrf_header': 'X-Mycelium-CSRF', 'csrf_token': 't0k'}},
                              cookie='mycelium_session=s1; Path=/; HttpOnly')
        if self.path == '/api/v1/qualification/current':
            return self._json(200, {'route_ready': True, 'binding': BINDING})
        if self.path == '/api/v1/inference/r1/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for kind, data in Gateway.events:
                self.wfile.write(f'event: {kind}\ndata: {json.dumps(data)}\n\n'.encode())
            return
        return self._json(404, {'error': {'code': 'not_found'}})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        if Gateway.refuse is not None:
            return self._json(*Gateway.refuse)
        Gateway.posted.append(body)
        return self._json(202, {'request_id': 'r1', 'event_path': '/api/v1/inference/r1/events'})


class Run:
    def __init__(self, origin):
        self.origin = origin

    def status(self):
        return {'state': 'serving', 'step': None, 'registry_origin': self.origin, 'run_id': 'r1', 'participants': []}


class App:
    installation_role = 'owner'

    def __init__(self, root, origin):
        self.root, self.owner_run, self.chat, self.server, self.public_relay = root, Run(origin), None, None, None
        self.chat = ChatService(self)

    def run_model_contract(self):
        return type('M', (), {'model_id': BINDING['model_id']})()


@pytest.fixture
def stack(tmp_path):
    Gateway.events = [('accepted', {}), ('token', {'token_index': 0, 'text': 'Hel'}),
                      ('token', {'token_index': 1, 'text': 'lo'}), ('completed', {})]
    Gateway.refuse, Gateway.posted = None, []
    gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    app = App(private_dir(tmp_path / 'app'), 'http://127.0.0.1:%d' % gateway.server_address[1])
    api = OpenAIApi(app)  # the shipped chat.RegistryClient
    api.apply_settings(enabled=True, port=0)
    _, secret = api.keys.create(label='wire')
    yield api, secret
    api.stop()
    gateway.shutdown()
    gateway.server_close()


def client_for(api, secret):
    openai = pytest.importorskip('openai')
    return openai.OpenAI(base_url=f'http://127.0.0.1:{api.port}/v1', api_key=secret, max_retries=0, timeout=10)


def test_openai_python_client_lists_the_pinned_model_and_completes(stack):
    api, secret = stack
    client = client_for(api, secret)
    assert [m.id for m in client.models.list().data] == ['Qwen/Qwen3-0.6B']
    reply = client.chat.completions.create(model='Qwen/Qwen3-0.6B', max_tokens=32,
                                           messages=[{'role': 'user', 'content': 'say hello'}])
    assert reply.choices[0].message.content == 'Hello' and reply.choices[0].finish_reason == 'stop'
    assert reply.usage.completion_tokens == 2
    assert Gateway.posted[0]['prompt'] == 'say hello' and Gateway.posted[0]['max_new_tokens'] == 32
    assert Gateway.posted[0]['protocol'] == 'mycelium.request_gateway.v2'


def test_openai_python_client_streams(stack):
    api, secret = stack
    client = client_for(api, secret)
    stream = client.chat.completions.create(model='Qwen/Qwen3-0.6B', stream=True,
                                            messages=[{'role': 'user', 'content': 'say hello'}])
    pieces = [c.choices[0].delta.content for c in stream if c.choices]
    assert ''.join(p for p in pieces if p) == 'Hello'


def test_openai_python_client_gets_typed_errors(stack):
    openai = pytest.importorskip('openai')
    api, secret = stack
    client = client_for(api, secret)
    with pytest.raises(openai.BadRequestError) as caught:
        client.chat.completions.create(model='Qwen/Qwen3-0.6B', temperature=0.7,
                                       messages=[{'role': 'user', 'content': 'x'}])
    assert caught.value.code == 'unsupported_parameter' and caught.value.param == 'temperature'
    with pytest.raises(openai.NotFoundError):
        client.chat.completions.create(model='gpt-4o', messages=[{'role': 'user', 'content': 'x'}])
    with pytest.raises(openai.AuthenticationError):
        client_for(api, 'myc-sk-nope').models.list()
    Gateway.refuse = (409, {'error': {'code': 'stale_qualification'}})
    with pytest.raises(openai.InternalServerError) as caught:
        client.chat.completions.create(model='Qwen/Qwen3-0.6B', messages=[{'role': 'user', 'content': 'x'}])
    assert caught.value.status_code == 503 and caught.value.code == 'route_unavailable'
    Gateway.refuse = None
    limited = client_for(api, api.keys.create(label='one', requests_per_window=1)[1])
    Gateway.refuse = None
    limited.chat.completions.create(model='Qwen/Qwen3-0.6B', messages=[{'role': 'user', 'content': 'x'}])
    with pytest.raises(openai.RateLimitError) as caught:
        limited.chat.completions.create(model='Qwen/Qwen3-0.6B', messages=[{'role': 'user', 'content': 'x'}])
    assert caught.value.code == 'rate_limit_exceeded'


def test_a_gateway_failed_terminal_with_a_code_reaches_the_client_as_its_openai_error(stack):
    openai = pytest.importorskip('openai')
    api, secret = stack
    Gateway.events = [('accepted', {}), ('failed', {'code': 'route_dropped'})]
    client = client_for(api, secret)
    with pytest.raises(openai.InternalServerError) as caught:
        client.chat.completions.create(model='Qwen/Qwen3-0.6B', messages=[{'role': 'user', 'content': 'x'}])
    assert caught.value.status_code == 503 and caught.value.code == 'route_unavailable'
