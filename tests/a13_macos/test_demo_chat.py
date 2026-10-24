# SPDX-License-Identifier: AGPL-3.0-only
"""Shared chat page: no accounts, rate limits, caps, streaming via polling, relay route."""
import http.client
import json
import threading
import time

import pytest

from mycelium_a13_macos import chat as chat_module
from mycelium_a13_macos.chat import (MAX_NEW_TOKENS_CAP, PER_CLIENT_PER_MINUTE, ChatError, ChatService,
                                     handle)


class Run:
    def __init__(self, state='serving', step=None):
        self.state = state
        self.step = step
    def status(self):
        return {'state': self.state, 'step': self.step, 'registry_origin': 'http://127.0.0.1:55555', 'run_id': 'r1',
                'participants': [{'node_id': 'a' * 20, 'label': 'This Mac (owner)', 'layers': [0, 20]},
                                 {'node_id': 'b' * 20, 'label': 'Joined Mac', 'layers': [20, 40]}]}


class FakeApp:
    installation_role = 'owner'
    def __init__(self, state='serving', step=None):
        self.owner_run = Run(state, step)
        self.chat = None
        self.server = None
    def run_model_contract(self):
        return type('M', (), {'model_id': 'Qwen/Qwen3-14B'})()


class Client:
    gate = None
    seen = []
    def __init__(self, origin):
        assert origin == 'http://127.0.0.1:55555'
    def stream(self, prompt, max_new_tokens, on_token):
        Client.seen.append((prompt, max_new_tokens))
        for text in ['Ber', 'lin', '.']:
            on_token(text)
        if Client.gate is not None:
            Client.gate.wait(5)
        return {'terminal': 'completed', 'code': None}


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


@pytest.fixture
def svc():
    Client.gate = None; Client.seen = []
    clock = Clock()
    return ChatService(FakeApp(), client_factory=Client, clock=clock), clock


def drain(svc, request_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page = svc.events({'request_id': request_id, 'after': 0})
        if page['done']: return page
        time.sleep(0.01)
    raise AssertionError('not done')


def test_tokens_stream_and_answer_names_the_macs_that_served_it(svc):
    s, _ = svc
    accepted = s.send('local', {'prompt': 'Capital of Germany?', 'max_new_tokens': 16})
    assert accepted['max_new_tokens'] == 16 and accepted['clamped'] is False
    assert [p['layers'] for p in accepted['served_by']] == [[0, 20], [20, 40]]
    page = drain(s, accepted['request_id'])
    assert ''.join(page['tokens']) == 'Berlin.' and page['error'] is None and page['next'] == 3
    assert s.events({'request_id': accepted['request_id'], 'after': 2})['tokens'] == ['.']


def test_max_new_tokens_is_clamped_to_the_cap(svc):
    s, _ = svc
    accepted = s.send('local', {'prompt': 'hi', 'max_new_tokens': 4096})
    assert accepted['max_new_tokens'] == MAX_NEW_TOKENS_CAP and accepted['clamped'] is True
    drain(s, accepted['request_id'])
    assert Client.seen[-1] == ('hi', MAX_NEW_TOKENS_CAP)


@pytest.mark.parametrize('body,code', [
    ({'prompt': 'x' * 2001}, 'chat_prompt_too_long'),
    ({'prompt': '   '}, 'chat_prompt_required'),
    ({'prompt': 'x', 'max_new_tokens': 0}, 'chat_max_new_tokens_invalid'),
    ({'prompt': 'x', 'max_new_tokens': True}, 'chat_max_new_tokens_invalid'),
    ({'prompt': 'x', 'account': 'root'}, 'chat_request_invalid'),
])
def test_invalid_requests_are_refused(svc, body, code):
    s, _ = svc
    with pytest.raises(ChatError, match=code):
        s.send('local', body)


def test_one_request_in_flight_for_the_whole_swarm(svc):
    s, _ = svc
    Client.gate = threading.Event()
    first = s.send('relay:1.1.1.1', {'prompt': 'one'})
    with pytest.raises(ChatError, match='chat_busy'):
        s.send('relay:2.2.2.2', {'prompt': 'two'})
    Client.gate.set(); drain(s, first['request_id'])
    s.send('relay:2.2.2.2', {'prompt': 'two'})


def test_per_client_rate_limit_resets_after_a_minute(svc):
    s, clock = svc
    for _ in range(PER_CLIENT_PER_MINUTE):
        drain(s, s.send('relay:1.1.1.1', {'prompt': 'p'})['request_id'])
    with pytest.raises(ChatError, match='chat_rate_limited'):
        s.send('relay:1.1.1.1', {'prompt': 'p'})
    drain(s, s.send('relay:9.9.9.9', {'prompt': 'other client'})['request_id'])
    clock.t += 61
    drain(s, s.send('relay:1.1.1.1', {'prompt': 'p'})['request_id'])


def test_swarm_wide_rate_limit(svc, monkeypatch):
    s, _ = svc
    monkeypatch.setattr(chat_module, 'GLOBAL_PER_MINUTE', 3)
    for i in range(3):
        drain(s, s.send('relay:%d.0.0.1' % i, {'prompt': 'p'})['request_id'])
    with pytest.raises(ChatError, match='chat_swarm_rate_limited'):
        s.send('relay:7.0.0.1', {'prompt': 'p'})


def test_not_serving_refuses_with_not_ready():
    s = ChatService(FakeApp(state='running'), client_factory=Client)
    with pytest.raises(ChatError, match='chat_not_ready'):
        s.send('local', {'prompt': 'hi'})
    assert s.state()['available'] is False


@pytest.mark.parametrize('step', ['activate', 'serving'])
def test_route_still_qualifying_is_warming_up_not_an_error(step):
    s = ChatService(FakeApp(state='running', step=step), client_factory=Client)
    with pytest.raises(ChatError) as caught:
        s.send('local', {'prompt': 'hi'})
    assert (caught.value.status, caught.value.code) == (503, 'chat_warming_up')
    state = s.state()
    assert state['available'] is False and state['warming_up'] is True


def test_a_failed_run_is_not_warming_up():
    s = ChatService(FakeApp(state='failed', step='serving'), client_factory=Client)
    with pytest.raises(ChatError, match='chat_not_ready'):
        s.send('local', {'prompt': 'hi'})
    assert s.state()['warming_up'] is False


def test_generation_failure_is_a_closed_code(svc):
    s, _ = svc
    class Broken(Client):
        def stream(self, *a): raise RuntimeError('/secret/path')
    s._client_factory = Broken
    page = drain(s, s.send('local', {'prompt': 'hi'})['request_id'])
    # Closed code naming only the exception type; never the message.
    assert page['error'] == 'chat_generation_failed_runtimeerror' and 'secret' not in json.dumps(page)


def test_handler_requires_header_and_serves_page_without_session():
    app = FakeApp(); app.chat = ChatService(app, client_factory=Client)
    status, kind, raw = handle(app, 'GET', '/chat', {}, b'', via_relay=True)
    assert status == 200 and kind.startswith('text/html') and b'chat.js' in raw
    status, _, raw = handle(app, 'POST', '/chat/api/send', {'Content-Type': 'application/json'},
                            b'{"prompt":"x"}', via_relay=True)
    assert status == 403 and json.loads(raw)['error'] == 'chat_header_required'
    headers = {'Content-Type': 'application/json', 'X-Mycelium-Chat': '1', 'Cf-Connecting-Ip': '203.0.113.9'}
    status, _, raw = handle(app, 'POST', '/chat/api/send', headers, b'{"prompt":"x"}', via_relay=True)
    assert status == 200 and 'request_id' in json.loads(raw)
    assert 'relay:203.0.113.9' in app.chat._per_client
    target = FakeApp(); target.installation_role = 'target'
    assert handle(target, 'GET', '/chat', {}, b'', via_relay=True)[0] == 404


def test_relay_routes_chat_with_csp_and_rejects_unbound():
    from mycelium_a13_macos.public_relay import SeedRelayProxy
    proxy = SeedRelayProxy().start()
    try:
        port = int(proxy.origin.rsplit(':', 1)[1])
        def get(path):
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            c.request('GET', path); r = c.getresponse(); value = (r.status, dict(r.getheaders()), r.read()); c.close()
            return value
        assert get('/chat')[0] == 503
        app = FakeApp(); app.chat = ChatService(app, client_factory=Client)
        proxy.bind_chat(lambda m, p, h, b: handle(app, m, p, h, b, via_relay=True))
        status, headers, raw = get('/chat')
        assert status == 200 and b'chat.js' in raw
        assert "script-src 'self'" in headers['Content-Security-Policy']
        assert get('/chatty')[0] == 404 and get('/chat/../seed/x')[0] == 404
    finally:
        proxy.close()


_CHAT_JS = __import__('pathlib').Path(chat_module.__file__).with_name('web') / 'chat.js'
_NODE_HARNESS = r"""
const vm = require('vm'); const fs = require('fs');
const el = () => ({addEventListener(){}, append(){}, scrollIntoView(){}, textContent:'', value:'', disabled:false});
const ctx = {document:{getElementById:el, createElement:el}, setInterval(){}, setTimeout, console};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
process.stdout.write(JSON.stringify(JSON.parse(process.argv[2]).map(([t, e]) => ctx.finishedText(t, e))));
"""


def _finished(cases):
    import shutil, subprocess
    node = shutil.which('node')
    assert node, 'node is required to exercise the shipped chat page'
    out = subprocess.run([node, '-e', _NODE_HARNESS, str(_CHAT_JS), json.dumps(cases)],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_unproven_cleanup_after_text_is_a_note_not_a_stop():
    """The answer was produced; slow node-cleanup confirmation must not read as a cryptic stop."""
    note, empty, other = _finished([['Hello world', 'request_cleanup_unproven'],
                                    ['', 'request_cleanup_unproven'],
                                    ['Partial', 'chat_busy']])
    assert note.startswith('Hello world\n') and '[stopped:' not in note
    assert 'request_cleanup_unproven' not in note and 'cleanup' in note.lower()
    assert 'everything the swarm produced' in note  # honest for complete and partial text alike
    assert empty.startswith('[stopped:') and 'request_cleanup_unproven' not in empty  # nothing shown: a real stop
    assert other == 'Partial\n[stopped: ' + "Someone else's prompt is running. Try again in a few seconds." + ']'
    assert _finished([['Done', None]]) == ['Done']
