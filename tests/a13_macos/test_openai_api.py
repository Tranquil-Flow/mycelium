# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV22: the OpenAI-compatible API for a private swarm, served by the owner app.

Real code: ``mycelium_a13_macos.openai_api`` (HTTP server, validation, error mapping, quotas), ``openai_keys.KeyStore``,
``chat.ChatService`` (route state), ``public_relay.SeedRelayProxy`` and ``DemoApp`` (actions, lifecycle).
FAKES (labelled): ``Client`` stands in for ``chat.RegistryClient`` (the swarm route), ``FakeApp`` for the owner app.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from mycelium_a13_macos.app import DemoApp, DemoError
from mycelium_a13_macos.chat import ChatError, ChatService
from mycelium_a13_macos.openai_api import OpenAIApi, ApiSettingsError

ORIGIN = 'http://127.0.0.1:55555'
MODEL = 'Qwen/Qwen3-0.6B'


class Run:
    def __init__(self, state='serving'):
        self.state = state

    def status(self):
        return {'state': self.state, 'step': None, 'registry_origin': ORIGIN, 'run_id': 'r1', 'participants': []}


class FakeApp:
    installation_role = 'owner'

    def __init__(self, root, state='serving'):
        self.root = root
        self.owner_run = Run(state)
        self.chat = None
        self.server = None
        self.public_relay = None

    def run_model_contract(self):
        return type('M', (), {'model_id': MODEL})()


class Client:
    """FAKE swarm route.  Class attributes script one run."""
    tokens = ['Ber', 'lin', '.']
    terminal = 'completed'
    code = None
    raises = None
    pause = 0.0
    after_first = None
    seen: list = []
    cancelled = 0

    def __init__(self, origin):
        assert origin == ORIGIN

    def stream(self, prompt, max_new_tokens, on_token):
        Client.seen.append((prompt, max_new_tokens))
        if Client.raises is not None:
            raise Client.raises
        for index, text in enumerate(Client.tokens[:max_new_tokens]):
            on_token(text)
            if index == 0 and Client.after_first is not None:
                Client.after_first()
            if Client.pause:
                time.sleep(Client.pause)
        return {'terminal': Client.terminal, 'code': Client.code, 'deployment_id': 'd1', 'model_id': MODEL}

    def cancel(self):
        Client.cancelled += 1
        return True


@pytest.fixture(autouse=True)
def reset_client():
    Client.tokens = ['Ber', 'lin', '.']
    Client.terminal, Client.code, Client.raises = 'completed', None, None
    Client.pause, Client.after_first, Client.seen, Client.cancelled = 0.0, None, [], 0
    yield


@pytest.fixture
def app(tmp_path):
    from mycelium_a13_macos.app import private_dir
    root = private_dir(tmp_path / 'app')
    fake = FakeApp(root)
    fake.chat = ChatService(fake, client_factory=Client)
    return fake


@pytest.fixture
def api(app):
    server = OpenAIApi(app, client_factory=Client)
    server.apply_settings(enabled=True, port=0)
    yield server
    server.stop()


@pytest.fixture
def key(api):
    record, secret = api.keys.create(label='test')
    return secret


def call(api, method, path, body=None, *, secret=None, headers=None, raw=None):
    connection = http.client.HTTPConnection('127.0.0.1', api.port, timeout=5)
    sent = dict(headers or {})
    if secret is not None:
        sent['Authorization'] = 'Bearer ' + secret
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        sent.setdefault('Content-Type', 'application/json')
    connection.request(method, path, body=data, headers=sent)
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, payload)
    connection.close()
    return result


def chat(api, secret, **fields):
    body = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'Capital of Germany?'}], **fields}
    status, headers, payload = call(api, 'POST', '/v1/chat/completions', body, secret=secret)
    return status, headers, json.loads(payload)


def sse(payload):
    frames = []
    for block in payload.decode().split('\n\n'):
        if block.startswith('data: '):
            data = block[6:]
            frames.append(data if data == '[DONE]' else json.loads(data))
    return frames


def error_of(document):
    assert set(document) == {'error'}
    assert set(document['error']) == {'message', 'type', 'param', 'code'}
    return document['error']


# -- binding and lifecycle -------------------------------------------------------------------------------------
def test_default_settings_are_closed_and_nothing_listens(app):
    api = OpenAIApi(app, client_factory=Client)
    assert api.settings() == {'enabled': False, 'port': 8793, 'expose_via_relay': False}
    assert api.running is False and api.port is None
    api.start()  # disabled: still nothing
    assert api.running is False


def test_server_binds_loopback_only_on_its_own_port(api, app):
    assert api.server.server_address[0] == '127.0.0.1'
    assert api.port != 0
    assert app.server is None  # the app's own UI server is untouched
    assert api.status()['url'] == f'http://127.0.0.1:{api.port}/v1'


def test_settings_persist_in_an_owner_only_file_and_survive_reopen(api, app):
    import os, stat
    path = app.root / 'openai-api' / 'settings.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.getuid()
    port = api.port
    api.stop()
    again = OpenAIApi(app, client_factory=Client)
    assert again.settings()['enabled'] is True
    again.start()
    try:
        assert again.running is True
    finally:
        again.stop()
    assert port  # a port was assigned


@pytest.mark.parametrize('port', [-1, 80, 1023, 70000, True, '8793', 1.5])
def test_port_is_validated(app, port):
    api = OpenAIApi(app, client_factory=Client)
    with pytest.raises(ApiSettingsError) as caught:
        api.apply_settings(enabled=True, port=port)
    assert caught.value.code == 'openai_port_invalid'
    assert api.running is False


def test_a_busy_port_is_refused_and_leaves_the_api_off(app):
    squatter = socket.socket()
    squatter.bind(('127.0.0.1', 0))
    squatter.listen(1)
    try:
        api = OpenAIApi(app, client_factory=Client)
        with pytest.raises(ApiSettingsError) as caught:
            api.apply_settings(enabled=True, port=squatter.getsockname()[1])
        assert caught.value.code == 'openai_port_in_use'
        assert api.running is False and api.settings()['enabled'] is False
    finally:
        squatter.close()


def test_disable_stops_listening(api):
    port = api.port
    api.apply_settings(enabled=False)
    assert api.running is False
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', port), timeout=1).close()


def test_foreign_host_header_is_refused(api, key):
    status, _, payload = call(api, 'GET', '/v1/models', secret=key, headers={'Host': 'evil.example'})
    assert status == 403 and error_of(json.loads(payload))['code'] == 'host_rejected'
    status, _, _ = call(api, 'GET', '/v1/models', secret=key, headers={'Host': f'localhost:{api.port}'})
    assert status == 200


def test_no_cors_is_ever_granted(api, key):
    status, headers, _ = call(api, 'OPTIONS', '/v1/chat/completions', headers={'Origin': 'https://evil.example',
                                                                              'Access-Control-Request-Method': 'POST'})
    assert status >= 400  # a preflight is never answered with success
    assert not any(name.startswith('access-control-') for name in headers)
    _, headers, _ = call(api, 'GET', '/v1/models', secret=key, headers={'Origin': 'https://evil.example'})
    assert not any(name.startswith('access-control-') for name in headers)


# -- authentication ----------------------------------------------------------------------------------------------
def test_missing_wrong_and_revoked_keys_get_openai_401s(api, key):
    status, headers, payload = call(api, 'GET', '/v1/models')
    assert status == 401 and error_of(json.loads(payload))['type'] == 'authentication_error'
    assert error_of(json.loads(payload))['code'] == 'missing_api_key'
    assert headers['www-authenticate'].lower().startswith('bearer')
    status, _, payload = call(api, 'GET', '/v1/models', secret='myc-sk-wrong')
    assert status == 401 and error_of(json.loads(payload))['code'] == 'invalid_api_key'
    status, _, payload = call(api, 'GET', '/v1/models', headers={'Authorization': key})  # no Bearer scheme
    assert status == 401
    assert call(api, 'GET', '/v1/models', secret=key)[0] == 200
    record = api.keys.list()[0]
    api.keys.revoke(record['id'])
    assert call(api, 'GET', '/v1/models', secret=key)[0] == 401
    assert call(api, 'POST', '/v1/chat/completions', {'model': MODEL, 'messages': []}, secret=key)[0] == 401


def test_the_secret_never_appears_in_any_response(api, key):
    for path, method, body in (('/v1/models', 'GET', None), ('/v1/chat/completions', 'POST', {'model': 'x'})):
        _, headers, payload = call(api, method, path, body, secret=key)
        assert key.encode() not in payload and key not in json.dumps(headers)


def test_unknown_path_and_wrong_method(api, key):
    status, _, payload = call(api, 'GET', '/v1/nope', secret=key)
    assert status == 404 and error_of(json.loads(payload))['type'] == 'not_found_error'
    status, _, payload = call(api, 'GET', '/v1/chat/completions', secret=key)
    assert status == 405 and error_of(json.loads(payload))['code'] == 'method_not_allowed'
    status, _, payload = call(api, 'POST', '/v1/models', {}, secret=key)
    assert status == 405


# -- /v1/models ----------------------------------------------------------------------------------------------------
def test_models_lists_only_the_swarms_pinned_model(api, key):
    status, _, payload = call(api, 'GET', '/v1/models', secret=key)
    document = json.loads(payload)
    assert status == 200 and document['object'] == 'list'
    assert [m['id'] for m in document['data']] == [MODEL]
    assert document['data'][0]['object'] == 'model' and isinstance(document['data'][0]['created'], int)
    status, _, payload = call(api, 'GET', '/v1/models/' + MODEL, secret=key)
    assert status == 200 and json.loads(payload)['id'] == MODEL
    status, _, payload = call(api, 'GET', '/v1/models/gpt-4', secret=key)
    assert status == 404 and error_of(json.loads(payload))['code'] == 'model_not_found'


# -- chat completions: success -------------------------------------------------------------------------------------
def test_non_stream_completion_has_the_openai_shape(api, key):
    status, headers, document = chat(api, key, max_tokens=16)
    assert status == 200 and headers['content-type'].startswith('application/json')
    assert document['object'] == 'chat.completion' and document['id'].startswith('chatcmpl-')
    assert document['model'] == MODEL and isinstance(document['created'], int)
    choice = document['choices'][0]
    assert choice == {'index': 0, 'message': {'role': 'assistant', 'content': 'Berlin.'}, 'finish_reason': 'stop'}
    assert document['usage']['completion_tokens'] == 3
    assert document['usage']['total_tokens'] == document['usage']['prompt_tokens'] + 3
    assert Client.seen == [('Capital of Germany?', 16)]


def test_finish_reason_is_length_when_the_budget_is_used(api, key):
    status, _, document = chat(api, key, max_tokens=2)
    assert status == 200 and document['choices'][0]['finish_reason'] == 'length'
    assert document['choices'][0]['message']['content'] == 'Berlin'[:6] or document['usage']['completion_tokens'] == 2


def test_max_completion_tokens_is_accepted_and_default_is_bounded(api, key):
    chat(api, key, max_completion_tokens=7)
    chat(api, key)
    assert [n for _, n in Client.seen] == [7, 256]


def test_messages_are_rendered_into_one_prompt_for_the_route(api, key):
    chat(api, key, messages=[{'role': 'system', 'content': 'Be brief.'},
                             {'role': 'user', 'content': 'Hi'},
                             {'role': 'assistant', 'content': 'Hello!'},
                             {'role': 'user', 'content': [{'type': 'text', 'text': 'Capital of'},
                                                           {'type': 'text', 'text': ' Germany?'}]}])
    prompt = Client.seen[0][0]
    assert prompt.startswith('Be brief.') and prompt.rstrip().endswith('Assistant:')
    assert 'User: Hi' in prompt and 'Assistant: Hello!' in prompt and 'User: Capital of Germany?' in prompt


def test_greedy_noop_parameters_are_accepted(api, key):
    status, _, _ = chat(api, key, temperature=0, top_p=1, n=1, user='u1', presence_penalty=0,
                        frequency_penalty=0, stop=None, tools=None)
    assert status == 200


# -- chat completions: streaming ---------------------------------------------------------------------------------
def test_stream_frames_are_openai_chunks_ending_in_done(api, key):
    status, headers, payload = call(api, 'POST', '/v1/chat/completions',
                                    {'model': MODEL, 'stream': True, 'max_tokens': 16,
                                     'messages': [{'role': 'user', 'content': 'hi'}]}, secret=key)
    assert status == 200 and headers['content-type'].startswith('text/event-stream')
    assert headers['cache-control'] == 'no-cache'
    frames = sse(payload)
    assert frames[-1] == '[DONE]'
    chunks = frames[:-1]
    assert {c['object'] for c in chunks} == {'chat.completion.chunk'} and len({c['id'] for c in chunks}) == 1
    assert chunks[0]['choices'][0]['delta'] == {'role': 'assistant', 'content': ''}
    assert [c['choices'][0]['delta'].get('content') for c in chunks[1:-1]] == ['Ber', 'lin', '.']
    assert chunks[-1]['choices'][0] == {'index': 0, 'delta': {}, 'finish_reason': 'stop'}
    assert all(c['choices'][0]['finish_reason'] is None for c in chunks[:-1])


def test_stream_include_usage_adds_a_final_usage_chunk(api, key):
    _, _, payload = call(api, 'POST', '/v1/chat/completions',
                         {'model': MODEL, 'stream': True, 'stream_options': {'include_usage': True},
                          'messages': [{'role': 'user', 'content': 'hi'}]}, secret=key)
    chunks = sse(payload)[:-1]
    assert chunks[-1]['choices'] == [] and chunks[-1]['usage']['completion_tokens'] == 3


def test_tokens_are_flushed_while_generation_continues(api, key):
    first_seen, release = threading.Event(), threading.Event()
    Client.after_first = lambda: release.wait(5)
    connection = http.client.HTTPConnection('127.0.0.1', api.port, timeout=5)
    connection.request('POST', '/v1/chat/completions', json.dumps(
        {'model': MODEL, 'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}),
        {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    response = connection.getresponse()
    assert response.status == 200
    lines = []
    while len(lines) < 2:  # role chunk, then the first content chunk, while the route is still "generating"
        line = response.fp.readline().decode()
        if line.startswith('data: '):
            lines.append(json.loads(line[6:]))
    assert lines[1]['choices'][0]['delta']['content'] == 'Ber'
    release.set()
    rest = response.read().decode()
    assert rest.rstrip().endswith('data: [DONE]')
    connection.close()


def test_client_disconnect_cancels_the_route_and_frees_the_slot(api, key):
    Client.tokens = ['t'] * 400
    Client.pause = 0.005
    record = api.keys.list()[0]
    raw = socket.create_connection(('127.0.0.1', api.port), timeout=5)
    body = json.dumps({'model': MODEL, 'stream': True, 'max_tokens': 400,
                       'messages': [{'role': 'user', 'content': 'hi'}]}).encode()
    raw.sendall(b'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n'
                b'Content-Type: application/json\r\nContent-Length: %d\r\n\r\n' % (api.port, key.encode(), len(body)) + body)
    raw.recv(4096)
    raw.close()
    deadline = time.monotonic() + 5
    while Client.cancelled == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert Client.cancelled >= 1
    Client.tokens, Client.pause = ['ok'], 0.0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, _, document = chat(api, key, max_tokens=4)
        if status == 200:
            break
        time.sleep(0.05)
    assert status == 200
    assert api.keys.list()[0]['usage']['tokens'] < 400 + 4


# -- validation: clear OpenAI-style 400s ---------------------------------------------------------------------------
@pytest.mark.parametrize('fields,param,code', [
    ({'temperature': 0.7}, 'temperature', 'unsupported_parameter'),
    ({'temperature': 1}, 'temperature', 'unsupported_parameter'),
    ({'top_p': 0.9}, 'top_p', 'unsupported_parameter'),
    ({'n': 2}, 'n', 'unsupported_parameter'),
    ({'stop': ['x']}, 'stop', 'unsupported_parameter'),
    ({'seed': 3}, 'seed', 'unsupported_parameter'),
    ({'presence_penalty': 0.5}, 'presence_penalty', 'unsupported_parameter'),
    ({'logprobs': True}, 'logprobs', 'unsupported_parameter'),
    ({'tools': [{'type': 'function'}]}, 'tools', 'unsupported_parameter'),
    ({'response_format': {'type': 'json_object'}}, 'response_format', 'unsupported_parameter'),
    ({'logit_bias': {'1': 5}}, 'logit_bias', 'unsupported_parameter'),
    ({'totally_new_field': 1}, 'totally_new_field', 'unknown_parameter'),
    ({'max_tokens': 0}, 'max_tokens', 'invalid_value'),
    ({'max_tokens': -3}, 'max_tokens', 'invalid_value'),
    ({'max_tokens': True}, 'max_tokens', 'invalid_type'),
    ({'max_tokens': '8'}, 'max_tokens', 'invalid_type'),
    ({'max_tokens': 4097}, 'max_tokens', 'invalid_value'),
    ({'max_tokens': 5, 'max_completion_tokens': 6}, 'max_completion_tokens', 'invalid_value'),
    ({'stream': 'yes'}, 'stream', 'invalid_type'),
    ({'stream_options': {'include_usage': True}}, 'stream_options', 'invalid_value'),
    ({'messages': []}, 'messages', 'invalid_value'),
    ({'messages': 'hi'}, 'messages', 'invalid_type'),
    ({'messages': [{'role': 'tool', 'content': 'x'}]}, 'messages[0].role', 'invalid_value'),
    ({'messages': [{'role': 'user'}]}, 'messages[0].content', 'invalid_value'),
    ({'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}]}]},
     'messages[0].content', 'unsupported_parameter'),
    ({'messages': [{'role': 'user', 'content': 'x'}, {'role': 'assistant', 'content': 'y'}]}, 'messages',
     'invalid_value'),
])
def test_unsupported_or_malformed_requests_are_clear_400s(api, key, fields, param, code):
    status, _, document = chat(api, key, **fields)
    error = error_of(document)
    assert status == 400, document
    assert error['type'] == 'invalid_request_error' and error['param'] == param and error['code'] == code
    assert len(error['message']) > 10
    assert Client.seen == []  # nothing reached the route
    assert api.keys.list()[0]['usage']['requests'] == 0  # and nothing was charged


def test_unsupported_temperature_message_says_what_to_do(api, key):
    _, _, document = chat(api, key, temperature=0.7)
    assert 'temperature' in document['error']['message'] and '0' in document['error']['message']


def test_model_must_be_present_and_pinned(api, key):
    status, _, payload = call(api, 'POST', '/v1/chat/completions',
                              {'messages': [{'role': 'user', 'content': 'x'}]}, secret=key)
    assert status == 400 and error_of(json.loads(payload))['param'] == 'model'
    status, _, document = chat(api, key, model='gpt-4o')
    assert status == 404 and error_of(document)['code'] == 'model_not_found' and error_of(document)['param'] == 'model'


def test_bad_bodies(api, key):
    status, _, payload = call(api, 'POST', '/v1/chat/completions', raw=b'{nope', secret=key,
                              headers={'Content-Type': 'application/json'})
    assert status == 400 and error_of(json.loads(payload))['code'] == 'invalid_json'
    status, _, payload = call(api, 'POST', '/v1/chat/completions', raw=b'[]', secret=key,
                              headers={'Content-Type': 'application/json'})
    assert status == 400
    status, _, payload = call(api, 'POST', '/v1/chat/completions', raw=b'', secret=key)
    assert status == 400
    status, _, payload = call(api, 'POST', '/v1/chat/completions', raw=b'x' * 300000, secret=key,
                              headers={'Content-Type': 'application/json'})
    assert status == 413 and error_of(json.loads(payload))['code'] == 'request_too_large'
    status, _, document = chat(api, key, messages=[{'role': 'user', 'content': 'x' * 140000}])
    assert status == 400 and error_of(document)['code'] == 'context_length_exceeded'


# -- quotas --------------------------------------------------------------------------------------------------------
def test_request_quota_returns_429_with_retry_after(api):
    record, secret = api.keys.create(label='small', requests_per_window=2, window_seconds=3600)
    assert chat(api, secret)[0] == 200 and chat(api, secret)[0] == 200
    status, headers, document = chat(api, secret)
    error = error_of(document)
    assert status == 429 and error['type'] == 'rate_limit_error' and error['code'] == 'rate_limit_exceeded'
    assert 'requests' in error['message']
    assert 1 <= int(headers['retry-after']) <= 3600
    assert len(Client.seen) == 2


def test_token_quota_reserves_max_tokens_and_charges_actual(api):
    record, secret = api.keys.create(label='tok', tokens_per_window=10, window_seconds=3600)
    assert chat(api, secret, max_tokens=6)[0] == 200   # 3 produced
    assert chat(api, secret, max_tokens=6)[0] == 200   # 3 + 6 <= 10
    status, headers, document = chat(api, secret, max_tokens=6)  # 6 + 6 > 10
    assert status == 429 and error_of(document)['code'] == 'rate_limit_exceeded' and 'tokens' in document['error']['message']
    assert 'retry-after' in headers
    status, _, document = chat(api, secret, max_tokens=11)  # can never fit
    assert status == 400 and error_of(document)['code'] == 'max_tokens_exceeds_quota'


def test_quota_is_per_key_and_does_not_affect_other_keys(api, key):
    _, small = api.keys.create(label='small', requests_per_window=1)
    assert chat(api, small)[0] == 200 and chat(api, small)[0] == 429
    assert chat(api, key)[0] == 200


def test_streamed_tokens_are_counted_for_the_quota(api):
    record, secret = api.keys.create(label='s', tokens_per_window=100)
    call(api, 'POST', '/v1/chat/completions', {'model': MODEL, 'stream': True, 'max_tokens': 50,
                                               'messages': [{'role': 'user', 'content': 'x'}]}, secret=secret)
    usage = [k for k in api.keys.list() if k['id'] == record['id']][0]['usage']
    assert usage['requests'] == 1 and usage['tokens'] == 3


# -- swarm state and concurrency ---------------------------------------------------------------------------------
def test_swarm_not_serving_is_503_and_not_charged(api, key, app):
    app.owner_run.state = 'idle'
    status, _, document = chat(api, key)
    assert status == 503 and error_of(document)['type'] == 'server_error'
    assert error_of(document)['code'] == 'swarm_not_ready'
    assert api.keys.list()[0]['usage']['requests'] == 0
    status, _, _ = call(api, 'GET', '/v1/models', secret=key)
    assert status == 200  # listing the pinned model does not need a live route


def test_one_request_in_flight_the_next_gets_429_busy_and_is_not_charged(api, key):
    release = threading.Event()
    started = threading.Event()
    Client.after_first = lambda: (started.set(), release.wait(5))
    results = []
    worker = threading.Thread(target=lambda: results.append(chat(api, key)[0]))
    worker.start()
    assert started.wait(5)
    status, headers, document = chat(api, key)
    assert status == 429 and error_of(document)['code'] == 'swarm_busy' and error_of(document)['type'] == 'rate_limit_error'
    assert headers['retry-after']
    release.set()
    worker.join(5)
    assert results == [200]
    assert api.keys.list()[0]['usage']['requests'] == 1


# -- error mapping -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('upstream,status,etype,code', [
    ('stale_qualification', 503, 'server_error', 'route_unavailable'),
    ('qualification_mismatch', 503, 'server_error', 'route_unavailable'),
    ('deployment_epoch_changed', 503, 'server_error', 'route_unavailable'),
    ('path_changed', 503, 'server_error', 'route_unavailable'),
    ('route_dropped', 503, 'server_error', 'route_unavailable'),
    ('readiness_revoked', 503, 'server_error', 'route_unavailable'),
    ('qualification_unavailable', 503, 'server_error', 'route_unavailable'),
    ('gateway_capacity_exhausted', 429, 'rate_limit_error', 'swarm_busy'),
    ('event_backpressure', 429, 'rate_limit_error', 'swarm_busy'),
    ('slow_consumer', 408, 'invalid_request_error', 'slow_consumer'),
    ('request_cancelled', 503, 'server_error', 'request_cancelled'),
    ('client_disconnected', 503, 'server_error', 'request_cancelled'),
    ('invalid_submission', 400, 'invalid_request_error', 'invalid_request'),
    ('invalid_request_body', 400, 'invalid_request_error', 'invalid_request'),
    ('request_body_too_large', 413, 'invalid_request_error', 'request_too_large'),
    ('backend_failed', 502, 'server_error', 'route_failed'),
    ('router_decode_stalled', 502, 'server_error', 'route_failed'),
    ('token_order_violation', 502, 'server_error', 'route_failed'),
    ('something_unforeseen', 500, 'server_error', 'generation_failed'),
    (None, 500, 'server_error', 'generation_failed'),
])
def test_gateway_failure_codes_map_to_openai_errors(api, key, upstream, status, etype, code):
    Client.tokens, Client.terminal, Client.code = [], 'failed', upstream
    got, headers, document = chat(api, key)
    error = error_of(document)
    assert (got, error['type'], error['code']) == (status, etype, code), document
    if upstream:
        assert upstream in error['message']  # the bounded upstream code stays visible for support
    if got in (429, 503):
        assert 'retry-after' in headers
    assert api.keys.list()[0]['usage']['tokens'] == 0  # failed with nothing produced: nothing charged


@pytest.mark.parametrize('raised,status,code', [
    (ChatError(503, 'chat_route_not_qualified'), 503, 'swarm_not_ready'),
    (ChatError(502, 'chat_http_post_inference_409_stale_qualification'), 503, 'route_unavailable'),
    (ChatError(502, 'chat_http_post_inference_429_gateway_capacity_exhausted'), 429, 'swarm_busy'),
    (ChatError(502, 'chat_http_get_bootstrap_500_'), 502, 'route_failed'),
    (ChatError(502, 'chat_http_post_inference_400_invalid_submission'), 400, 'invalid_request'),
    (RuntimeError('boom with /secret/path'), 500, 'generation_failed'),
    (OSError('connection refused'), 502, 'route_failed'),
])
def test_exceptions_from_the_route_client_map_to_openai_errors(api, key, raised, status, code):
    Client.raises = raised
    got, _, document = chat(api, key)
    assert (got, error_of(document)['code']) == (status, code), document
    assert 'secret' not in json.dumps(document) and 'boom' not in json.dumps(document)


def test_cancelled_terminal_is_a_503_request_cancelled(api, key):
    Client.tokens, Client.terminal = [], 'cancelled'
    got, _, document = chat(api, key)
    assert got == 503 and error_of(document)['code'] == 'request_cancelled'


def test_failure_after_tokens_were_streamed_is_an_error_frame_not_a_new_status(api, key):
    Client.terminal, Client.code = 'failed', 'route_dropped'
    status, _, payload = call(api, 'POST', '/v1/chat/completions',
                              {'model': MODEL, 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}, secret=key)
    assert status == 200
    frames = sse(payload)
    assert frames[-1]['error']['code'] == 'route_unavailable' and frames[-1]['error']['type'] == 'server_error'
    assert '[DONE]' not in frames
    assert api.keys.list()[0]['usage']['tokens'] == 3  # what was streamed is charged


def test_failure_before_any_token_on_a_stream_request_is_a_real_http_error(api, key):
    Client.tokens, Client.terminal, Client.code = [], 'failed', 'route_dropped'
    status, _, payload = call(api, 'POST', '/v1/chat/completions',
                              {'model': MODEL, 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}, secret=key)
    assert status == 503 and error_of(json.loads(payload))['code'] == 'route_unavailable'


def test_empty_completion_is_a_valid_stream(api, key):
    Client.tokens = []
    _, _, payload = call(api, 'POST', '/v1/chat/completions',
                         {'model': MODEL, 'stream': True, 'messages': [{'role': 'user', 'content': 'x'}]}, secret=key)
    frames = sse(payload)
    assert frames[-1] == '[DONE]' and frames[-2]['choices'][0]['finish_reason'] == 'stop'


# -- relay exposure --------------------------------------------------------------------------------------------------
def relay_get(proxy, method, path, body=None, secret=None):
    port = int(proxy.origin.rsplit(':', 1)[1])
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {}
    if secret:
        headers['Authorization'] = 'Bearer ' + secret
    if body is not None:
        body = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    value = (response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
    connection.close()
    return value


@pytest.fixture
def relay(app):
    from mycelium_a13_macos.public_relay import SeedRelayProxy
    proxy = SeedRelayProxy().start()
    app.public_relay = type('R', (), {'proxy': proxy, 'public_origin': 'https://relay.example'})()
    yield proxy
    proxy.close()


def test_relay_does_not_serve_the_api_by_default(api, key, relay, app):
    api.bind_relay()  # the app calls this whenever a relay comes up; the setting is off
    for method, path in (('GET', '/v1/models'), ('POST', '/v1/chat/completions')):
        status, _, payload = relay_get(relay, method, path, {} if method == 'POST' else None, secret=key)
        assert status == 404 and json.loads(payload) == {'error': 'relay_route_unknown'}
    assert api.status()['relay_url'] is None


def test_relay_exposure_needs_an_explicit_acknowledged_setting(app, relay):
    local = OpenAIApi(app, client_factory=Client)
    with pytest.raises(ApiSettingsError) as caught:
        local.apply_settings(expose_via_relay=True)
    assert caught.value.code == 'openai_relay_consent_required'
    assert local.settings()['expose_via_relay'] is False


def test_relay_serves_the_api_with_bearer_auth_once_exposed(api, key, relay):
    api.apply_settings(expose_via_relay=True, relay_acknowledged=True)
    assert api.status()['relay_url'] == 'https://relay.example/v1'
    status, _, payload = relay_get(relay, 'GET', '/v1/models')
    assert status == 401 and error_of(json.loads(payload))['code'] == 'missing_api_key'
    status, _, payload = relay_get(relay, 'GET', '/v1/models', secret=key)
    assert status == 200 and json.loads(payload)['data'][0]['id'] == MODEL
    status, _, payload = relay_get(relay, 'POST', '/v1/chat/completions',
                                   {'model': MODEL, 'messages': [{'role': 'user', 'content': 'x'}]}, secret=key)
    assert status == 200 and json.loads(payload)['choices'][0]['message']['content'] == 'Berlin.'


def test_relay_streams_are_buffered_whole_sse_bodies(api, key, relay):
    api.apply_settings(expose_via_relay=True, relay_acknowledged=True)
    status, headers, payload = relay_get(relay, 'POST', '/v1/chat/completions',
                                         {'model': MODEL, 'stream': True,
                                          'messages': [{'role': 'user', 'content': 'x'}]}, secret=key)
    assert status == 200 and headers['content-type'].startswith('text/event-stream')
    assert sse(payload)[-1] == '[DONE]'


def test_relay_only_exposes_v1_and_the_unbound_state_returns_after_unexpose(api, key, relay):
    api.apply_settings(expose_via_relay=True, relay_acknowledged=True)
    for path in ('/v1/other', '/v1/../seed/x', '/healthz', '/api/v1/models', '/v1'):
        assert relay_get(relay, 'GET', path, secret=key)[0] == 404
    api.apply_settings(expose_via_relay=False)
    assert relay_get(relay, 'GET', '/v1/models', secret=key)[0] == 404


def test_relay_exposure_works_without_the_local_port_being_enabled(app, relay):
    local = OpenAIApi(app, client_factory=Client)
    _, secret = local.keys.create(label='r')
    local.apply_settings(expose_via_relay=True, relay_acknowledged=True)
    assert local.running is False
    assert relay_get(relay, 'GET', '/v1/models', secret=secret)[0] == 200


def test_relay_requests_use_the_same_quotas(app, relay):
    local = OpenAIApi(app, client_factory=Client)
    record, secret = local.keys.create(label='r', requests_per_window=1)
    local.apply_settings(expose_via_relay=True, relay_acknowledged=True)
    body = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'x'}]}
    assert relay_get(relay, 'POST', '/v1/chat/completions', body, secret=secret)[0] == 200
    assert relay_get(relay, 'POST', '/v1/chat/completions', body, secret=secret)[0] == 429


# -- DemoApp integration -------------------------------------------------------------------------------------------
class Authority:
    def __init__(self, root):
        pass

    def start(self):
        return self

    def status(self):
        return {'membership': 'not_joined', 'route_ready': False}

    def stop(self):
        pass

    def renew(self):
        return self.status()


@pytest.fixture
def demo(tmp_path):
    instance = DemoApp(tmp_path / 'private', authority_factory=Authority, check_platform=False).start()
    yield instance
    instance.close()


def test_app_actions_create_list_revoke_and_show_the_secret_once(demo):
    created = demo.action('/api/openai/key/create', {'label': 'laptop', 'requests_per_window': 5,
                                                      'tokens_per_window': 1000, 'window_seconds': 600})
    assert created['secret'].startswith('myc-sk-') and created['key']['label'] == 'laptop'
    listing = demo.action('/api/openai/status', {})
    assert [k['label'] for k in listing['keys']] == ['laptop']
    assert created['secret'] not in json.dumps(listing)
    assert listing['keys'][0]['quota'] == {'requests_per_window': 5, 'tokens_per_window': 1000, 'window_seconds': 600}
    assert listing['settings'] == {'enabled': False, 'port': 8793, 'expose_via_relay': False}
    revoked = demo.action('/api/openai/key/revoke', {'id': created['key']['id']})
    assert revoked['key']['revoked_at'] is not None
    with pytest.raises(DemoError) as caught:
        demo.action('/api/openai/key/revoke', {'id': 'key_nope'})
    assert caught.value.args[0] == 'key_unknown'


def test_app_action_input_is_strict(demo):
    for body, code in (({}, 'key_label_invalid'), ({'label': 'x', 'extra': 1}, 'invalid_request'),
                       ({'label': 'x', 'tokens_per_window': 0}, 'key_quota_invalid')):
        with pytest.raises(DemoError) as caught:
            demo.action('/api/openai/key/create', body)
        assert caught.value.args[0] == code
    with pytest.raises(DemoError):
        demo.action('/api/openai/settings', {'enabled': 'yes'})
    with pytest.raises(DemoError):
        demo.action('/api/openai/unknown', {})


def test_app_settings_start_and_stop_the_loopback_server_and_close_stops_it(demo):
    value = demo.action('/api/openai/settings', {'enabled': True, 'port': 0})
    assert value['running'] is True and value['url'].startswith('http://127.0.0.1:')
    port = int(value['url'].rsplit(':', 1)[1].split('/')[0])
    assert port != demo.port
    socket.create_connection(('127.0.0.1', port), timeout=2).close()
    demo.close()
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', port), timeout=1).close()


def test_app_settings_survive_restart(tmp_path):
    first = DemoApp(tmp_path / 'private', authority_factory=Authority, check_platform=False).start()
    try:
        first.action('/api/openai/settings', {'enabled': True, 'port': 0})
    finally:
        first.close()
    second = DemoApp(tmp_path / 'private', authority_factory=Authority, check_platform=False).start()
    try:
        status = second.action('/api/openai/status', {})
        assert status['settings']['enabled'] is True and status['running'] is True
    finally:
        second.close()


def test_relay_exposure_setting_needs_the_acknowledgement_in_the_action(demo):
    with pytest.raises(DemoError) as caught:
        demo.action('/api/openai/settings', {'expose_via_relay': True})
    assert caught.value.args[0] == 'openai_relay_consent_required'
    value = demo.action('/api/openai/settings', {'expose_via_relay': True, 'relay_acknowledged': True})
    assert value['settings']['expose_via_relay'] is True and value['relay_url'] is None  # no relay running yet


def test_a_joined_mac_cannot_manage_keys(tmp_path):
    instance = DemoApp(tmp_path / 'private', authority_factory=Authority, check_platform=False).start()
    try:
        instance.installation_role = 'target'
        with pytest.raises(DemoError) as caught:
            instance.action('/api/openai/key/create', {'label': 'x'})
        assert caught.value.args[0] == 'owner_role_required'
    finally:
        instance.installation_role = 'owner'
        instance.close()


def test_app_serves_the_key_management_script(demo):
    connection = http.client.HTTPConnection('127.0.0.1', demo.port, timeout=3)
    connection.request('GET', '/openai.js')
    response = connection.getresponse()
    assert response.status == 200 and response.getheader('Content-Type').startswith('text/javascript')
    assert b'/api/openai/key/create' in response.read()
    connection.close()


# -- docs and UI stay in step with the code ------------------------------------------------------------------------
def test_every_error_code_in_the_user_doc_exists_in_the_code_and_the_ui_ids_exist():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    doc = (root / 'docs/user/OPENAI-API.md').read_text()
    source = (root / 'mycelium_a13_macos/openai_api.py').read_text() + (root / 'mycelium_a13_macos/openai_keys.py').read_text()
    table = doc.split('## Errors')[1].split('If a stream')[0]
    codes = set(re.findall(r'`([a-z_]{4,40})`', ' '.join(row.split('|')[3] for row in table.splitlines()
                                                         if row.startswith('| 4') or row.startswith('| 5'))))
    codes -= {'invalid_request_error', 'authentication_error', 'not_found_error', 'rate_limit_error', 'server_error'}
    assert codes >= {'swarm_busy', 'route_unavailable', 'rate_limit_exceeded', 'unsupported_parameter', 'model_not_found'}
    for code in codes:
        assert f"'{code}'" in source, code
    html = (root / 'mycelium_a13_macos/web/index.html').read_text()
    script = (root / 'mycelium_a13_macos/web/openai.js').read_text()
    for element in set(re.findall(r"\$\('([a-z-]+)'\)", script)):
        assert f'id="{element}"' in html, element
    assert '<script defer src="/openai.js"></script>' in html


def test_the_api_and_the_shared_chat_page_share_the_one_route(api, key, app):
    release, started = threading.Event(), threading.Event()
    Client.after_first = lambda: (started.set(), release.wait(5))
    worker = threading.Thread(target=lambda: chat(api, key))
    worker.start()
    assert started.wait(5)
    try:
        with pytest.raises(ChatError) as caught:
            app.chat.send('local', {'prompt': 'hi'})  # the chat page is refused while the API holds the route
        assert caught.value.code == 'chat_busy'
    finally:
        release.set()
        worker.join(5)
    assert app.chat._busy is None  # released
    app.chat._busy = 'chat-page-request'  # and the other way round
    before = api.keys.list()[0]['usage']['requests']
    status, headers, document = chat(api, key)
    assert status == 429 and error_of(document)['code'] == 'swarm_busy' and 'retry-after' in headers
    assert api.keys.list()[0]['usage']['requests'] == before
