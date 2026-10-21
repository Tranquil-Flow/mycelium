# SPDX-License-Identifier: AGPL-3.0-only
"""The owner relay must accept what a real Cloudflare quick tunnel delivers.

The header set below was captured from the real ``cloudflared`` bundled in the
demo app (``--protocol http2`` quick tunnel) on 2026-10-13, for the exact
``JoinWindowClient`` requests.  Client-identifying values (IP, ray id,
country, worker, tunnel id) are replaced by documentation values; names,
order and protocol-structural values are verbatim.  The key observation: an
empty-body POST (the link probe's RTT rounds) reaches the origin as
``Transfer-Encoding: chunked`` with a zero chunk and no Content-Length, while
non-empty bodies keep their Content-Length.
"""
from __future__ import annotations

import json
import socket

import pytest

from mycelium_a13_macos.public_relay import SeedRelayProxy

_TUNNEL_HEADERS = [
    ("Host", "example-quick-tunnel.trycloudflare.com"),
    ("User-Agent", "Python-urllib/3.14"),
    ("Accept-Encoding", "gzip"),
    ("Cdn-Loop", "cloudflare; loops=1; subreqs=1"),
    ("Cf-Connecting-Ip", "203.0.113.7"),
    ("Cf-Ew-Via", "15"),
    ("Cf-Ipcountry", "FR"),
    ("Cf-Ray", "8d0c0ffee0000000-CDG"),
    ("Cf-Visitor", '{"scheme":"https"}'),
    ("Cf-Warp-Tag-Id", "00000000-0000-4000-8000-000000000000"),
    ("Cf-Worker", "trycloudflare.com"),
    ("Connection", "keep-alive"),
    ("X-Forwarded-For", "203.0.113.7"),
    ("X-Forwarded-Proto", "https"),
]


def _raw(port, method, path, headers, body=b""):
    lines = ["%s %s HTTP/1.1" % (method, path)] + ["%s: %s" % pair for pair in headers]
    payload = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)  # a truncated body must end, not stall
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    head, _, rest = b"".join(chunks).partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), rest


@pytest.fixture()
def relay():
    proxy = SeedRelayProxy().start()
    seen = []
    proxy.bind_local(lambda method, path, body: (seen.append((method, path, body))
                                                 or (200, json.dumps({"ok": True}).encode())))
    try:
        yield int(proxy.origin.rsplit(":", 1)[1]), seen
    finally:
        proxy.close()


def test_real_tunnel_empty_probe_arrives_chunked_and_is_accepted(relay):
    port, seen = relay
    headers = _TUNNEL_HEADERS[:2] + [("Transfer-Encoding", "chunked")] + _TUNNEL_HEADERS[2:] + [
        ("Content-Type", "application/octet-stream")]
    status, raw = _raw(port, "POST", "/link-probe/v1/0", headers, b"0\r\n\r\n")
    assert (status, json.loads(raw)) == (200, {"ok": True})
    assert seen == [("POST", "/link-probe/v1/0", b"")]


def test_real_tunnel_chunked_body_is_reassembled_exactly(relay):
    port, seen = relay
    headers = _TUNNEL_HEADERS + [("Transfer-Encoding", "chunked"), ("Content-Type", "application/json")]
    status, _ = _raw(port, "POST", "/pairing-window/v1/ABCD/request", headers,
                     b"3\r\n{\"a\r\n4\r\n\":1}\r\n0\r\n\r\n")
    assert status == 200
    assert seen == [("POST", "/pairing-window/v1/ABCD/request", b'{"a":1}')]


@pytest.mark.parametrize("path,body,kind", [
    ("/link-probe/v1/0", b"\0" * 4096, "application/octet-stream"),
    ("/pairing-window/v1/ABCD/request", b'{"x":1}', "application/json"),
    ("/member-report/v1", b'{"x":1}', "application/json"),
], ids=["probe_upload", "pairing_submit", "member_report"])
def test_real_tunnel_content_length_requests_are_accepted(relay, path, body, kind):
    port, seen = relay
    headers = _TUNNEL_HEADERS[:2] + [("Content-Length", str(len(body)))] + _TUNNEL_HEADERS[2:] + [
        ("Content-Type", kind)]
    status, _ = _raw(port, "POST", path, headers, body)
    assert status == 200
    assert seen == [("POST", path, body)]


@pytest.mark.parametrize("extra,body", [
    # request smuggling shapes stay refused
    ([("Transfer-Encoding", "chunked"), ("Content-Length", "5")], b"0\r\n\r\n"),
    ([("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")], b"0\r\n\r\n"),
    ([("Transfer-Encoding", "gzip, chunked")], b"0\r\n\r\n"),
    ([("Transfer-Encoding", "identity")], b""),
    # credentials never cross the relay
    ([("Content-Length", "2"), ("Cookie", "a=b")], b"{}"),
    ([("Content-Length", "2"), ("Authorization", "Bearer x")], b"{}"),
], ids=["te_and_cl", "te_twice", "te_list", "te_identity", "cookie", "authorization"])
def test_relay_header_refusals_are_unchanged(relay, extra, body):
    port, seen = relay
    status, raw = _raw(port, "POST", "/pairing-window/v1/ABCD/request", _TUNNEL_HEADERS + extra, body)
    assert status == 400 and json.loads(raw) == {"error": "relay_headers_invalid"}
    assert seen == []


@pytest.mark.parametrize("body", [
    b"zz\r\n",                                   # not hex
    b"+5\r\nhello\r\n0\r\n\r\n",                 # sign
    b"5\r\nhel",                                 # truncated
    b"5\r\nhelloXX0\r\n\r\n",                    # missing chunk CRLF
    b"0\r\nX-Trailer: 1\r\n\r\n",                # trailers refused
    b"2;ext=1\r\n{}\r\n0\r\n\r\n",               # chunk extensions refused
    b"%x\r\n" % (1024 * 1024 + 1),               # over the frame bound
    b"80000\r\n" + b"A" * 0x80000 + b"\r\n80001\r\n",  # cumulative over the bound
], ids=["not_hex", "signed", "truncated", "missing_crlf", "trailer", "extension", "oversized", "cumulative"])
def test_malformed_or_oversized_chunked_bodies_are_refused(relay, body):
    port, seen = relay
    headers = _TUNNEL_HEADERS + [("Transfer-Encoding", "chunked"), ("Content-Type", "application/json")]
    status, raw = _raw(port, "POST", "/member-report/v1", headers, body)
    assert status == 400 and json.loads(raw) == {"error": "relay_frame_invalid"}
    assert seen == []


def test_seed_forwarding_still_refuses_chunked_bodies(relay):
    port, _ = relay
    headers = _TUNNEL_HEADERS + [("Transfer-Encoding", "chunked"), ("Content-Type", "application/json")]
    status, raw = _raw(port, "POST", "/seed/v1/x", headers, b"0\r\n\r\n")
    assert status == 400 and json.loads(raw) == {"error": "relay_headers_invalid"}


def test_get_with_chunked_framing_is_refused(relay):
    port, seen = relay
    status, raw = _raw(port, "GET", "/pairing-window/v1/ABCD/join/ABCDEFGH",
                       _TUNNEL_HEADERS + [("Transfer-Encoding", "chunked")], b"0\r\n\r\n")
    assert status == 400 and json.loads(raw)["error"] in {"relay_frame_invalid", "relay_headers_invalid"}
    assert seen == []
