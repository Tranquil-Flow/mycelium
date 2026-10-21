# SPDX-License-Identifier: AGPL-3.0-only
"""Control carrier public seam plus real authenticated local record framing."""
import socket
import threading

import pytest

from mycelium_iroh_sidecar import ProtocolError, SidecarClient, SidecarError
from mycelium_iroh_sidecar import client as wire

PEER = '55' * 32


def test_control_call_public_seam_uses_one_authenticated_record():
    client = SidecarClient('/unused', b's' * 32)
    local, peer = socket.socketpair()
    local.settimeout(2)
    peer.settimeout(2)
    client._socket = local
    client._send_key = bytearray(b'a' * 32)
    client._receive_key = bytearray(b'b' * 32)
    seen = []
    errors = []

    def respond():
        try:
            raw = wire._read_prefixed(peer, wire._MAX_RECORD_BYTES)
            kind, message_id, payload = wire._decode_record(raw, b'a' * 32, 0)
            seen.append((kind, payload))
            response = wire._encode_record(19, 0, message_id, b'{"ok":true}', b'b' * 32)
            wire._write_prefixed(peer, response)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=respond)
    worker.start()
    try:
        result = client.control_call(peer_endpoint_id=PEER, request_json='{"command":"health"}', timeout_seconds=0.5)
        assert result == {'peer_endpoint_id': PEER, 'response_json': '{"ok":true}'}
        assert seen == [(18, bytes.fromhex(PEER) + (500).to_bytes(8, 'big') + b'{"command":"health"}')]
        assert client._send_sequence == 1
    finally:
        local.shutdown(socket.SHUT_RDWR)
        client.close()
        peer.close()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors


@pytest.mark.parametrize('response, code', [(b'', 'empty_control_call_result'), (b'x' * (4 * 1024 * 1024 + 1), 'control_response_too_large'), (b'\xff', 'invalid_control_response'), (b'not-json', 'invalid_control_response')], ids=['empty','oversize','non-utf8','non-json'])
def test_control_call_rejects_invalid_response(monkeypatch, response, code):
    client = SidecarClient('/unused', b's' * 32)
    monkeypatch.setattr(client, '_request', lambda kind, mid, payload, **kw: (19, mid, response))
    with pytest.raises(ProtocolError, match=code):
        client.control_call(peer_endpoint_id=PEER, request_json='{}', timeout_seconds=1)


@pytest.mark.parametrize('budget', [0, -1, float('nan'), float('inf'), True, '1', 0.0001])
def test_control_call_rejects_invalid_budget_before_io(monkeypatch, budget):
    client = SidecarClient('/unused', b's' * 32)
    monkeypatch.setattr(client, '_request', lambda *a, **kw: pytest.fail('invalid input reached IO'))
    with pytest.raises(ValueError):
        client.control_call(peer_endpoint_id=PEER, request_json='{}', timeout_seconds=budget)


@pytest.mark.parametrize('request_text', ['', 'not-json', 'NaN', '"' + 'é' * (2 * 1024 * 1024) + '"', b'{}'], ids=['empty','invalid','nan','oversize','bytes'])
def test_control_call_rejects_invalid_request_before_io(monkeypatch, request_text):
    client = SidecarClient('/unused', b's' * 32)
    monkeypatch.setattr(client, '_request', lambda *a, **kw: pytest.fail('invalid input reached IO'))
    with pytest.raises(ValueError):
        client.control_call(peer_endpoint_id=PEER, request_json=request_text, timeout_seconds=1)


def test_control_call_caps_timeout_and_does_not_retry(monkeypatch):
    client = SidecarClient('/unused', b's' * 32)
    seen = []
    def fail(kind, mid, payload, **kw):
        seen.append((int.from_bytes(payload[32:40], 'big'), kw))
        return wire._ERROR, mid, b'{"code":"control_read_timeout"}'
    monkeypatch.setattr(client, '_request', fail)
    with pytest.raises(SidecarError, match='control_read_timeout'):
        client.control_call(peer_endpoint_id=PEER, request_json='{}', timeout_seconds=120)
    assert len(seen) == 1
    assert seen[0][0] == 60000


def test_control_call_lock_wait_is_in_total_budget():
    client = SidecarClient('/unused', b's' * 32)
    client._lock.acquire()
    try:
        with pytest.raises(socket.timeout):
            client.control_call(peer_endpoint_id=PEER, request_json='{}', timeout_seconds=0.02)
    finally:
        client._lock.release()
