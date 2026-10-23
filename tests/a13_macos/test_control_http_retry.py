# SPDX-License-Identifier: AGPL-3.0-only
"""Proxy failures: real HTTPError parsing, durable claims and bounded replay.

Only loopback TLS and disposable identities are used. The response-loss injector
commits through the real mailbox before replacing its reply, not before dispatch.
"""
import io
import json
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from mycelium_a13_macos import managed_control as control
from mycelium_a13_macos.control_pump import ControlPumpError, TargetControlPump
from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime, _bounded_control_reason
from test_control_pump import _world
from test_managed_control import RUN_ID, DEPLOYMENT_ID, _enqueue
from test_worker import worker
from tests.live.test_token_path_admission import world


def test_proxy_lost_token_receipt_ack_seals_once(tmp_path, monkeypatch, world):
    from copy import deepcopy
    from test_managed_control import _world as separate_world
    from test_token_progress_delivery import prepared
    from test_token_receipt_mailbox import provision
    from mycelium_a13_macos.durable_requests import replay
    from mycelium_router.token_path import TokenBatch
    with separate_world(tmp_path / 'swarm') as a:
        stream, journal, sealed, claim = provision(world, a)
        receipt = journal.append(TokenBatch(0, 0, (10,), 'DECODING'))
        packet = prepared(a, claim, receipt)
        saved = deepcopy(packet)
        seen = lose_committed_reply(monkeypatch, '/managed-control/token-progress', 530)
        response = replay(a['target_client'], packet, target=a['member'])
        assert len(seen) == 2 and seen[0] == seen[1] == response
        assert packet == saved and len(sealed) == 1
        assert stream.read(timeout=0).token_ids == (10,)
        with pytest.raises(TimeoutError):
            stream.read(timeout=0)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def client_fixture(monkeypatch, *, status=503, body=b'<html>proxy outage</html>',
                   content_type='text/html', failures=1):
    client = object.__new__(control.ManagedControlClient)
    client.base_url = 'https://unused.invalid'
    client.timeout = 10.0
    calls = []
    clock = Clock()
    # Module-local clock injection, never mutate Python's shared time module.
    monkeypatch.setattr(control, 'time', clock)
    monkeypatch.setattr(client, '_read_response', lambda response: response.read())

    def open_(request, *, timeout):
        calls.append((request.full_url, request.data, timeout))
        if len(calls) <= failures:
            raise HTTPError(request.full_url, status, 'proxy',
                            {'Content-Type': content_type}, io.BytesIO(body))
        return io_response()

    class io_response(io.BytesIO):
        headers = {'Content-Type': 'application/json'}
        def __init__(self):
            super().__init__(b'{"accepted":true}')

    client._opener = SimpleNamespace(open=open_)
    return client, calls, clock


@pytest.mark.parametrize('status', [502, 503, 504, 530])
@pytest.mark.parametrize('path', ['/managed-control/poll', '/managed-control/enqueue',
                                  '/managed-control/ack', '/managed-control/result',
                                  '/managed-control/token-progress'])
def test_transient_unstructured_http_reuses_exact_request(monkeypatch, status, path):
    client, calls, clock = client_fixture(monkeypatch, status=status)
    assert client._request(path, {'signed': 'fixture'}) == {'accepted': True}
    assert len(calls) == 2
    assert calls[0][:2] == calls[1][:2]
    assert clock.waits == [.1]
    assert control._REQUEST_DEADLINE.get() is None


def test_http_retry_constants_are_explicit():
    assert (control.HTTP_RETRY_ATTEMPTS, control.HTTP_RETRY_WINDOW_SECONDS,
            control.HTTP_RETRY_INITIAL_SECONDS, control.HTTP_RETRY_MAX_SECONDS) == (8, 10., .1, 1.)


def test_backoff_observes_existing_owner_cancellation(monkeypatch):
    from mycelium_a13_macos.control_session import _CommandWaiter
    client, calls, clock = client_fixture(monkeypatch, failures=1000)
    waiter = _CommandWaiter()
    original = waiter.register
    def cancel_at_wait(callback):
        remove = original(callback)
        waiter.interrupt('managed_control_command_interrupted')
        return remove
    monkeypatch.setattr(waiter, 'register', cancel_at_wait)
    with control.request_deadline(clock.now + 20, interruption=waiter):
        with pytest.raises(control.ManagedControlError, match='managed_control_command_interrupted'):
            client._request('/managed-control/poll', {'signed': 'fixture'})
        assert control._REQUEST_INTERRUPTION.get() is waiter
    assert len(calls) == 1 and not waiter.callbacks
    assert control._REQUEST_INTERRUPTION.get() is None


def test_retry_io_time_consumes_window_without_a_new_attempt(monkeypatch):
    client, calls, clock = client_fixture(monkeypatch, failures=1000)
    original = client._opener.open
    def slow(request, **kwargs):
        try:
            return original(request, **kwargs)
        finally:
            clock.now += 10.
    client._opener.open = slow
    with pytest.raises(control.ManagedControlError) as caught:
        client._request('/managed-control/poll', {'signed': 'fixture'})
    assert caught.value.status == 503 and len(calls) == 1 and not clock.waits


def test_real_http_error_body_stall_is_bounded_and_keeps_status(tmp_path, monkeypatch):
    from test_managed_control import _world as separate_world
    from mycelium_a13_macos.durable_requests import prepare, replay
    release = threading.Event()
    def stall(self, status, document):
        self.send_response(503)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', '100')
        self.end_headers()
        self.wfile.write(b'<'); self.wfile.flush()
        release.wait(.5)
    with separate_world(tmp_path / 'swarm') as a:
        monkeypatch.setattr(control._ManagedControlRequestHandler, '_send', stall)
        packet = prepare(a['target_client'], 'poll', target=a['member'],
            run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, sequence=1,
            nonce='stall', request_id='stall')
        try:
            start = time.monotonic()
            with control.request_deadline(start + .1):
                with pytest.raises(control.ManagedControlError) as caught:
                    replay(a['target_client'], packet, target=a['member'])
            assert time.monotonic() - start < .4
            assert caught.value.status == 503
            assert caught.value.request_path == '/managed-control/poll'
            assert caught.value.code == 'managed_control_remote_error'
        finally:
            release.set()


def test_sustained_outage_has_attempt_and_time_bounds(monkeypatch):
    client, calls, clock = client_fixture(monkeypatch, failures=1000)
    with pytest.raises(control.ManagedControlError) as caught:
        client._request('/managed-control/poll', {'signed': 'fixture'})
    assert caught.value.code == 'managed_control_remote_error'
    assert len(calls) == 8
    assert clock.waits == [.1, .2, .4, .8, 1., 1., 1.]
    assert clock.now <= 110
    assert len({data for _, data, _ in calls}) == 1
    assert caught.value.status == 503


def test_retry_budget_never_extends_the_callers_deadline(monkeypatch):
    client, calls, clock = client_fixture(monkeypatch, failures=1000)
    with control.request_deadline(clock.now + .15):
        with pytest.raises(control.ManagedControlError):
            client._request('/managed-control/poll', {'signed': 'fixture'})
        assert control._REQUEST_DEADLINE.get() == 100.15
    assert len(calls) == 2
    assert clock.now <= 100.15
    assert all(timeout <= .15 + 1e-10 for _, _, timeout in calls)
    assert control._REQUEST_DEADLINE.get() is None


@pytest.mark.parametrize('status', [401, 403, 404, 409, 422])
def test_nontransient_http_is_never_retried(monkeypatch, status):
    client, calls, clock = client_fixture(monkeypatch, status=status)
    with pytest.raises(control.ManagedControlError) as caught:
        client._request('/managed-control/poll', {'signed': 'fixture'})
    assert caught.value.status == status
    assert len(calls) == 1 and clock.waits == []


@pytest.mark.parametrize('content_type', ['application/json', 'application/json; charset=utf-8', 'text/plain'])
@pytest.mark.parametrize('code', ['managed_control_signature_invalid',
                                 'managed_control_member_stale', 'managed_control_remote_error'])
@pytest.mark.parametrize('status', [403, 503, 530])
def test_structured_refusal_is_never_retried(monkeypatch, status, code, content_type):
    client, calls, clock = client_fixture(monkeypatch, status=status,
        body=json.dumps({'error': {'code': code}}, separators=(',', ':'), sort_keys=True).encode(),
        content_type=content_type)
    with pytest.raises(control.ManagedControlError) as caught:
        client._request('/managed-control/poll', {'signed': 'fixture'})
    assert caught.value.code == (code if content_type == 'application/json' else 'managed_control_remote_error')
    assert len(calls) == 1 and clock.waits == []


@pytest.mark.parametrize('body', [b'{"error": {"code": "authority_refused"}}',
    b'{"error":{"code":"authority_refused","detail":"no"}}',
    b'{"error":"authority_refused"}'])
def test_json_refusal_without_canonical_shape_cannot_grant_retry(monkeypatch, body):
    client, calls, clock = client_fixture(monkeypatch, body=body, content_type='text/plain')
    with pytest.raises(control.ManagedControlError):
        client._request('/managed-control/poll', {'signed': 'fixture'})
    assert len(calls) == 1 and not clock.waits


def test_unknown_post_path_does_not_gain_retry_authority(monkeypatch):
    client, calls, clock = client_fixture(monkeypatch)
    with pytest.raises(control.ManagedControlError):
        client._request('/unreviewed-effect', {'signed': 'fixture'})
    assert len(calls) == 1 and clock.waits == []


def lose_committed_reply(monkeypatch, path, status):
    """Use the real TLS handler; a proxy can lose a reply AFTER commit."""
    handler = control._ManagedControlRequestHandler
    original = handler._send
    seen = []

    def send(self, code, document):
        if self.path == path and code == 200:
            seen.append(document)
            if len(seen) == 1:
                self.send_response(status)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', '5')
                self.end_headers()
                self.wfile.write(b'proxy')
                return
        return original(self, code, document)
    monkeypatch.setattr(handler, '_send', send)
    return seen


@pytest.mark.parametrize('pump_type', [TargetControlPump, ConcurrentTargetControlPump])
@pytest.mark.parametrize('path', ['/managed-control/poll', '/managed-control/ack'])
def test_committed_poll_or_ack_is_replayed_without_second_native_effect(tmp_path, monkeypatch, pump_type, path):
    # Real signed replay, real TLS, real SQLite, real no-model native hello.
    from test_managed_control import _world as separate_world
    with separate_world(tmp_path / 'swarm') as world:
        seen = lose_committed_reply(monkeypatch, path, 503)
        root = tmp_path / 'native'; root.mkdir()
        w = worker(root, node_id=world['member']['node_id'], run_id=RUN_ID,
                   deployment_id=DEPLOYMENT_ID)
        pump = pump_type(tmp_path / 'pump', client=world['target_client'], worker=w,
                        current_member=lambda: world['member'])
        try:
            _enqueue(world, 'once')
            w.start()
            result = pump.step()
            if pump_type is ConcurrentTargetControlPump:
                assert result['state'] == 'dispatched'
                pump._futures['once'].result(timeout=2)
                result = pump.step()
            assert result['state'] == 'completed'
            assert w.status()['command_count'] == 1
            assert world['mailbox'].command_receipt('once')['state'] == 'completed'
            assert len(seen) == 2
            assert seen[0] == seen[1]  # same claim/token or same terminal ack
            assert pump.step()['state'] == 'empty'
            assert w.status()['command_count'] == 1
        finally:
            pump.close(); w.close()


@pytest.mark.parametrize('path', ['/managed-control/enqueue', '/managed-control/result'])
def test_owner_lost_reply_does_not_reenqueue_native_work(tmp_path, monkeypatch, path):
    # Owner and gateway sessions use the same managed HTTPS client.
    from test_control_session import connected, owner, session
    with connected(tmp_path) as (world, w):
        # connected mounts on the seed handler, not the standalone handler.
        client = world['owner_client']
        original = client._opener.open
        seen = []
        def open_(request, **kwargs):
            response = original(request, **kwargs)
            if request.full_url.endswith(path):
                seen.append(request.data)
                if len(seen) == 1:
                    response.read(); response.close()
                    raise HTTPError(request.full_url, 502, 'proxy',
                        {'Content-Type': 'text/html'}, io.BytesIO(b'proxy'))
            return response
        # Read our finite in-memory proxy body, otherwise retain real deadline IO.
        read = client._read_response
        monkeypatch.setattr(client, '_read_response',
            lambda r: r.read() if isinstance(getattr(r, 'fp', None), io.BytesIO) else read(r))
        monkeypatch.setattr(client._opener, 'open', open_)
        sender = owner(tmp_path / 'sender', world)
        try:
            reply = session(sender, world).send(command_id='owner-once', command='hello', payload={})
            assert reply['ok'] is True
            assert len(seen) >= 2 and seen[0] == seen[1]
            assert w.status()['command_count'] == 1
        finally:
            sender.close()


def test_sustained_outage_stops_runtime_with_persisted_http_identity(tmp_path, monkeypatch):
    client, calls, clock = client_fixture(monkeypatch, failures=1000)
    # Real pump replay path, with a single prepared fixture and no native dispatch.
    import mycelium_a13_macos.control_pump as module
    pump = object.__new__(TargetControlPump)
    pump.client = client
    pump._state = {'outbound': {'operation': 'poll', 'envelope': {'signed': 'fixture'}}}
    monkeypatch.setattr(module, 'replay', lambda c, p, target: c._request('/managed-control/poll', p['envelope']))
    runtime = object.__new__(ManagedTargetRuntime)
    runtime.root = tmp_path; runtime.run_id = 'outage'
    runtime._stop = threading.Event(); runtime._lock = threading.RLock()
    runtime._state = 'running'; runtime._adaptive = None; runtime._adaptive_drain = None
    runtime._member = lambda: None
    runtime._replace_worker_if_dead = lambda: None
    closed = []
    runtime.pump = SimpleNamespace(step=lambda: pump._replay({}), close=lambda: closed.append(True))
    runtime._serve()
    record = json.loads((tmp_path / 'runtime-control-failure.private.json').read_text())
    assert len(calls) == 8
    assert runtime._state == 'blocked' and runtime._stop.is_set() and closed == [True]
    assert record['error_code'] == 'control_poll_reconciliation_required'
    assert record['http_status'] == 503
    assert record['request_path'] == '/managed-control/poll'
    assert 'proxy outage' not in json.dumps(record)


def test_diagnostic_retains_only_typed_status_and_allowlisted_path():
    cause = control.ManagedControlError('managed_control_remote_error', status=530,
        request_path='/managed-control/poll?token=PRIVATE#secret')
    exc = ControlPumpError('control_poll_reconciliation_required')
    exc.__cause__ = cause
    reason = _bounded_control_reason(exc)
    assert reason['http_status'] == 530
    assert reason['request_path'] == '/managed-control/poll'
    assert 'PRIVATE' not in json.dumps(reason) and 'secret' not in json.dumps(reason)


@pytest.mark.parametrize('status,path', [(True, '/managed-control/poll'),
    ('503', '/managed-control/poll'), (999, '/managed-control/poll'),
    (503, 'https://user:secret@host/poll'), (503, '/managed-control/SECRET')])
def test_diagnostic_rejects_untyped_status_and_unreviewed_paths(status, path):
    exc = control.ManagedControlError('managed_control_remote_error', status=status, request_path=path)
    exc.__cause__ = exc
    reason = _bounded_control_reason(exc)
    assert ('http_status' in reason) == (type(status) is int and 100 <= status <= 599)
    assert ('request_path' in reason) == (path == '/managed-control/poll')
    assert 'SECRET' not in json.dumps(reason)
