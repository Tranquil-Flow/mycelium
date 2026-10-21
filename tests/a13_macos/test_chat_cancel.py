# SPDX-License-Identifier: AGPL-3.0-only
"""Chat cancel path: the owner can stop a generation that is already running on the route.

FAKE (labelled): ``Gateway`` is a stand-in HTTP server shaped like mycelium_ui_gateway/app.py (session cookie, CSRF +
exact Origin on every state change incl. DELETE, `cancel_path` in the accepted document, SSE `cancelled` terminal);
``Client`` is a duck-typed registry client.  ChatService and RegistryClient are the shipped code.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mycelium_a13_macos.chat import ChatError, ChatService, RegistryClient

BINDING = {'deployment_id': 'dep-1', 'model_id': 'Qwen/Qwen3-0.6B'}


class Gateway(BaseHTTPRequestHandler):
    cancelled = threading.Event()
    accepted = threading.Event()
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
        if self.path == '/api/v1/bootstrap':
            return self._json(200, {'session': {'csrf_header': 'X-Mycelium-CSRF', 'csrf_token': 't0k'}},
                              cookie='mycelium_session=s1; Path=/; HttpOnly')
        if self.path == '/api/v1/qualification/current':
            return self._json(200, {'route_ready': True, 'binding': BINDING})
        if self.path == '/api/v1/inference/r1/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'event: accepted\ndata: {}\n\n')
            self.wfile.write(b'event: token\ndata: {"token_index": 0, "text": "Ber"}\n\n')
            self.wfile.flush()
            Gateway.accepted.set()
            if not Gateway.cancelled.wait(5):  # a generation that never ends by itself
                return
            self.wfile.write(b'event: cancelled\ndata: {"code": "client_cancelled"}\n\n')
            return
        return self._json(404, {'error': {'code': 'not_found'}})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        return self._json(202, {'request_id': 'r1', 'accepted': True, 'event_path': '/api/v1/inference/r1/events',
                                'cancel_path': '/api/v1/inference/r1/cancel'})

    def do_DELETE(self):
        self.seen.append(('DELETE', self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.headers.get('X-Mycelium-CSRF') != 't0k':
            return self._json(403, {'error': {'code': 'csrf_required'}})
        if self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_address[1]}':
            return self._json(403, {'error': {'code': 'origin_mismatch'}})
        if self.path != '/api/v1/inference/r1/cancel':
            return self._json(404, {'error': {'code': 'request_not_found'}})
        Gateway.cancelled.set()
        return self._json(202, {'protocol': 'mycelium.request_gateway.v1', 'request_id': 'r1', 'cancelled': True})


@pytest.fixture
def gateway():
    Gateway.cancelled, Gateway.accepted, Gateway.seen = threading.Event(), threading.Event(), []
    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    Gateway.cancelled.set()
    server.shutdown()
    server.server_close()


def origin_of(server):
    return 'http://127.0.0.1:%d' % server.server_address[1]


def test_registry_client_cancel_sends_an_authorised_delete_to_the_accepted_cancel_path(gateway):
    client = RegistryClient(origin_of(gateway), timeout=5)
    tokens, result = [], {}
    worker = threading.Thread(target=lambda: result.update(client.stream('hi', 64, tokens.append)))
    worker.start()
    assert Gateway.accepted.wait(5)
    assert client.cancel() is True
    worker.join(5)
    assert not worker.is_alive()
    assert result['terminal'] == 'cancelled' and tokens == ['Ber']
    [(method, path, headers)] = Gateway.seen
    assert (method, path) == ('DELETE', '/api/v1/inference/r1/cancel')
    assert headers['x-mycelium-csrf'] == 't0k' and headers['origin'] == origin_of(gateway)


def test_registry_client_cancel_with_nothing_running_is_a_noop(gateway):
    client = RegistryClient(origin_of(gateway), timeout=5)
    assert client.cancel() is False and Gateway.seen == []


def test_a_cancel_that_arrives_before_the_gateway_accepted_the_request_still_cancels(gateway):
    client = RegistryClient(origin_of(gateway), timeout=5)
    assert client.cancel() is False  # nothing to cancel yet ...
    client.request_cancel()  # ... but the intent is remembered
    result = client.stream('hi', 64, lambda _t: None)
    assert result['terminal'] == 'cancelled'
    assert [seen[1] for seen in Gateway.seen] == ['/api/v1/inference/r1/cancel']


# -- ChatService ---------------------------------------------------------------------------------

class Run:
    def status(self):
        return {'state': 'serving', 'step': None, 'registry_origin': 'http://127.0.0.1:55555', 'run_id': 'r1',
                'participants': [{'node_id': 'a' * 20, 'label': 'This Mac (owner)', 'layers': [0, 20]}]}


class FakeApp:
    installation_role = 'owner'

    def __init__(self):
        self.owner_run, self.chat, self.server = Run(), None, None

    def run_model_contract(self):
        return type('M', (), {'model_id': 'Qwen/Qwen3-14B'})()


class Client:
    """FAKE registry client: streams one token, then runs until cancel() (or the test releases it)."""
    instances: list = []
    release = None

    def __init__(self, origin):
        self.cancelled = threading.Event()
        self.cancel_calls = 0
        Client.instances.append(self)

    def stream(self, prompt, max_new_tokens, on_token):
        on_token('Ber')
        if self.cancelled.wait(5):
            return {'terminal': 'cancelled', 'code': None}
        return {'terminal': 'completed', 'code': None}

    def cancel(self):
        self.cancel_calls += 1
        self.cancelled.set()
        return True


@pytest.fixture
def svc():
    Client.instances = []
    return ChatService(FakeApp(), client_factory=Client)


def wait_for(predicate, what):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(what)


def page_of(svc, request_id):
    return svc.events({'request_id': request_id, 'after': 0})


def test_cancel_stops_the_running_generation_and_ends_it_with_the_given_reason(svc):
    accepted = svc.send('c1', {'prompt': 'hello', 'max_new_tokens': 16})
    wait_for(lambda: page_of(svc, accepted['request_id'])['tokens'], 'first token')
    assert svc.cancel(accepted['request_id'], 'entry_killed_by_owner') is True
    wait_for(lambda: page_of(svc, accepted['request_id'])['done'], 'terminal')
    page = page_of(svc, accepted['request_id'])
    assert page['error'] == 'entry_killed_by_owner' and page['terminal'] == 'cancelled'
    assert Client.instances[0].cancel_calls == 1
    assert svc.state()['busy'] is False  # the swarm is free again, no waiting for max_new_tokens


def test_a_cancelled_generation_is_never_reported_as_a_clean_completion(svc):
    accepted = svc.send('c1', {'prompt': 'hello', 'max_new_tokens': 16})
    wait_for(lambda: page_of(svc, accepted['request_id'])['tokens'], 'first token')
    svc.cancel(accepted['request_id'])
    wait_for(lambda: page_of(svc, accepted['request_id'])['done'], 'terminal')
    page = page_of(svc, accepted['request_id'])
    assert page['error'] == 'chat_cancelled' and page['terminal'] == 'cancelled'


def test_a_completed_generation_reports_the_completed_terminal():
    class Finishes(Client):
        def stream(self, prompt, max_new_tokens, on_token):
            on_token('Ber')
            return {'terminal': 'completed', 'code': None}

    service = ChatService(FakeApp(), client_factory=Finishes)
    accepted = service.send('c1', {'prompt': 'hello', 'max_new_tokens': 16})
    wait_for(lambda: page_of(service, accepted['request_id'])['done'], 'terminal')
    page = page_of(service, accepted['request_id'])
    assert page['terminal'] == 'completed' and page['error'] is None


def test_cancel_is_idempotent_and_a_finished_request_is_left_alone(svc):
    accepted = svc.send('c1', {'prompt': 'hello', 'max_new_tokens': 16})
    wait_for(lambda: page_of(svc, accepted['request_id'])['tokens'], 'first token')
    assert svc.cancel(accepted['request_id'], 'entry_killed_by_owner') is True
    assert svc.cancel(accepted['request_id'], 'entry_killed_by_owner') is False
    wait_for(lambda: page_of(svc, accepted['request_id'])['done'], 'terminal')
    assert svc.cancel(accepted['request_id'], 'entry_killed_by_owner') is False
    assert Client.instances[0].cancel_calls == 1


def test_cancel_of_an_unknown_request_is_a_closed_404(svc):
    with pytest.raises(ChatError) as caught:
        svc.cancel('r' * 24, 'x')
    assert (caught.value.status, caught.value.code) == (404, 'chat_request_unknown')


def test_cancel_all_stops_whatever_is_in_flight_and_is_a_noop_when_idle(svc):
    assert svc.cancel_all('entry_killed_by_owner') == 0
    accepted = svc.send('c1', {'prompt': 'hello', 'max_new_tokens': 16})
    wait_for(lambda: page_of(svc, accepted['request_id'])['tokens'], 'first token')
    assert svc.cancel_all('entry_killed_by_owner') == 1
    wait_for(lambda: page_of(svc, accepted['request_id'])['done'], 'terminal')
    assert page_of(svc, accepted['request_id'])['error'] == 'entry_killed_by_owner'


def test_a_backend_that_cannot_cancel_does_not_break_the_service(svc):
    class Stubborn(Client):
        def cancel(self):
            raise OSError('gateway unreachable')

    svc._client_factory = Stubborn
    accepted = svc.send('c1', {'prompt': 'hello', 'max_new_tokens': 16})
    wait_for(lambda: page_of(svc, accepted['request_id'])['tokens'], 'first token')
    assert svc.cancel(accepted['request_id'], 'entry_killed_by_owner') is False  # failure is reported, not raised
    Client.instances[-1].cancelled.set()
    wait_for(lambda: page_of(svc, accepted['request_id'])['done'], 'terminal')
