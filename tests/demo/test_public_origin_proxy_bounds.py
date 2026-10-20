# SPDX-License-Identifier: AGPL-3.0-only
"""M4: the apex proxy bounds its own connections like the directory behind it (32 slots, 10 s).

FAKES (labelled): the product and directory upstreams are recording stubs.  Everything else is the shipped proxy.
Review: R3 F3 (no socket timeout and no connection cap: slowloris, unbounded threads, unmetered helper downloads).
"""
from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from mycelium_demo import public_origin_proxy as proxy
from mycelium_swarm_directory import hosted_defaults as defaults
from _proxy_world import CAPABILITY, IP, call, world

NAME = "Mycelium-helper-mac.zip"
URL = "/swarms/download/" + NAME
GET_CONFIG = b"GET /swarms/api/v1/config HTTP/1.1\r\nHost: x\r\nCf-Connecting-Ip: 203.0.113.7\r\n\r\n"


def _closed_within(sock: socket.socket, seconds: float) -> bool:
    """True when the peer closed (EOF or reset) or answered-and-closed within the time."""
    sock.settimeout(seconds)
    try:
        while True:
            if sock.recv(65536) == b"":
                return True
    except socket.timeout:
        return False
    except OSError:
        return True


def test_defaults_match_the_directory() -> None:
    assert defaults.PROXY_CONNECTION_SLOTS == 32
    assert defaults.PROXY_SOCKET_TIMEOUT_SECONDS == 10
    base = dict(listen_host="127.0.0.1", listen_port=4350, upstream_host="127.0.0.1", upstream_port=4351,
                public_host="mycelium.now", capability=CAPABILITY)
    config = proxy.ProxyConfig(**base)
    assert (config.connection_slots, config.socket_timeout_seconds) == (32, 10)
    for bad in ({"connection_slots": 0}, {"connection_slots": 100_000}, {"connection_slots": True},
                {"socket_timeout_seconds": 0}, {"socket_timeout_seconds": 999},
                {"max_concurrent_downloads": 0}):
        with pytest.raises(proxy.PublicOriginProxyError):
            proxy.ProxyConfig(**base, **bad)


def test_stalled_connections_hold_the_slots_the_extra_one_is_refused_and_the_held_ones_time_out() -> None:
    with world(connection_slots=3, socket_timeout_seconds=1.0) as (public, _product, _directory):
        port = public.server_port
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
        try:
            time.sleep(0.2)  # let the accept loop hand each to a slot
            extra = socket.create_connection(("127.0.0.1", port))
            try:
                assert _closed_within(extra, 0.7), "the over-cap connection must be refused promptly"
            finally:
                extra.close()
            assert all(_closed_within(sock, 3.0) for sock in held), "stalled slots must time out"
            assert call(port, "GET", "/swarms/api/v1/config", headers=IP)[0] == 200  # slots are free again
        finally:
            for sock in held:
                sock.close()


def test_a_stalled_header_and_a_stalled_body_are_both_dropped_after_the_timeout() -> None:
    with world(socket_timeout_seconds=0.8) as (public, _product, directory):
        port = public.server_port
        with socket.create_connection(("127.0.0.1", port)) as slow_head:
            slow_head.sendall(b"GET /swarms/api/v1/config HTTP/1.1\r\nHost: x\r\n")  # never finishes the head
            assert _closed_within(slow_head, 3.0)
        with socket.create_connection(("127.0.0.1", port)) as slow_body:
            slow_body.sendall(b"POST /swarms/api/v1/records HTTP/1.1\r\nHost: x\r\nCf-Connecting-Ip: 203.0.113.7\r\n"
                              b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
            started = time.monotonic()
            assert _closed_within(slow_body, 3.0)
            assert time.monotonic() - started < 2.5
        assert directory.requests == []  # a half-sent body is never forwarded


def test_a_normal_request_after_the_timeouts_is_unaffected() -> None:
    with world(connection_slots=2, socket_timeout_seconds=0.5) as (public, _product, directory):
        for _ in range(6):
            assert call(public.server_port, "GET", "/swarms/api/v1/config", headers=IP)[0] == 200
        assert len(directory.requests) == 6


def test_concurrent_helper_downloads_are_capped(tmp_path: Path) -> None:
    path = tmp_path / NAME
    path.write_bytes(b"PK\x03\x04" + b"x" * (24 * 1024 * 1024))  # far larger than the socket buffers
    path.chmod(0o600)
    with world(helper_zip_path=str(path), max_concurrent_downloads=2, socket_timeout_seconds=8) as (public, _p, _d):
        port = public.server_port
        request = f"GET {URL} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        holders = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        try:
            for sock in holders:
                sock.sendall(request)
                assert sock.recv(64).startswith(b"HTTP/1.1 200")  # streaming started, the rest is left unread
            time.sleep(0.3)
            status, _headers, body = call(port, "GET", URL)
            assert status == 503 and b"download_busy" in body
            assert call(port, "GET", "/swarms/api/v1/config", headers=IP)[0] == 200  # the page itself is unaffected
        finally:
            for sock in holders:
                sock.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # slots come back once the holders are gone
            with socket.create_connection(("127.0.0.1", port)) as probe:
                probe.sendall(request)
                if probe.recv(64).startswith(b"HTTP/1.1 200"):
                    break
            time.sleep(0.1)
        else:
            raise AssertionError("download slots were never released")
