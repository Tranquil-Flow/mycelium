# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-only direct IPv4 loopback socket transport for A6LocalFaultReceiver.

No proxy, public mount, inferred peer, model or physical fault is supplied here.
One server thread handles requests serially, so the receiver's asyncio.Lock is
never contended by separate event loops. An injected seam remains the caller's
responsibility, not a claim of physical measurement.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from socketserver import BaseRequestHandler, TCPServer
from threading import Thread
from typing import cast

from mycelium_a6_http_fault_control import A6LocalFaultReceiver

_MAX_HEAD = 8192
_MAX_BODY = 512
_ALLOWED = frozenset({b"host", b"accept", b"content-type", b"content-length",
                      b"connection", b"user-agent", b"x-mycelium-a6-fleet-grant"})


class _Refuse(Exception):
    pass


class _Server(TCPServer):
    receiver: A6LocalFaultReceiver
    allow_reuse_address = False
    address_family = socket.AF_INET
    request_queue_size = 4


class _Handler(BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(2.0)
        status = 400
        try:
            status = self._dispatch(sock)
        except (OSError, ValueError, UnicodeError, _Refuse):
            pass
        except Exception:
            status = 500
        code = b"controlled_fault_executed" if status == 200 else b"controlled_fault_refused"
        body = json.dumps({"code": code.decode("ascii")}).encode("ascii")
        response = (f"HTTP/1.1 {status} {('OK' if status == 200 else 'Refused')}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii") + body
        try:
            sock.sendall(response)
        except OSError:
            pass  # A disconnected client gets no result; never re-invoke a seam.

    def _dispatch(self, sock: socket.socket) -> int:
        # These values are read from the accepted connection, never from a
        # caller's ASGI scope, Host, body or forwarding header.
        peer, local = sock.getpeername(), sock.getsockname()
        if (len(peer) != 2 or len(local) != 2 or peer[0] != "127.0.0.1"
                or local[0] != "127.0.0.1"):
            raise _Refuse()
        deadline = time.monotonic() + 2.0

        def read(size: int) -> bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Refuse()
            sock.settimeout(remaining)
            return sock.recv(size)

        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            if len(raw) > _MAX_HEAD:
                raise _Refuse()
            part = read(min(1024, _MAX_HEAD + 4 - len(raw)))
            if not part:
                raise _Refuse()
            raw.extend(part)
        head, body_start = bytes(raw).split(b"\r\n\r\n", 1)
        if len(head) > _MAX_HEAD or b"\n" in head.replace(b"\r\n", b""):
            raise _Refuse()
        lines = head.split(b"\r\n")
        request = lines.pop(0).split(b" ")
        if len(request) != 3 or request[0] != b"POST" or request[2] != b"HTTP/1.1":
            raise _Refuse()
        if not request[1].startswith(b"/") or any(c < 33 or c > 126 for c in request[1]):
            raise _Refuse()
        if len(lines) > 32:
            raise _Refuse()
        headers: dict[bytes, bytes] = {}
        for line in lines:
            if b":" not in line or line.startswith((b" ", b"\t")):
                raise _Refuse()
            name, value = line.split(b":", 1)
            if (not name or any(not (65 <= c <= 90 or 97 <= c <= 122
                                    or 48 <= c <= 57 or c == 45)
                                for c in name) or not value.startswith(b" ")
                    or len(value) > 257):
                raise _Refuse()
            name = name.lower()
            value = value[1:]
            if (name not in _ALLOWED or name in headers or
                    any(c < 32 or c > 126 for c in value)):
                raise _Refuse()
            headers[name] = value
        if headers.get(b"host") != f"127.0.0.1:{local[1]}".encode("ascii"):
            raise _Refuse()
        length = headers.get(b"content-length", b"")
        if not length or not length.isascii() or not length.isdigit() or (len(length) > 1 and length[0] == 48):
            raise _Refuse()
        size = int(length)
        if not 0 < size <= _MAX_BODY or len(body_start) > size:
            raise _Refuse()
        body = bytearray(body_start)
        while len(body) < size:
            part = read(size - len(body))
            if not part:
                raise _Refuse()
            body.extend(part)
        # Refuse already-arrived pipelining or bytes beyond declared framing.
        # No persistent connections are supported; late data is discarded.
        readable = sock.gettimeout()
        sock.setblocking(False)
        try:
            try:
                if sock.recv(1, socket.MSG_PEEK):
                    raise _Refuse()
            except BlockingIOError:
                pass
        finally:
            sock.settimeout(readable)
        path = request[1]
        scope = {"type": "http", "method": "POST", "path": path.decode("ascii"),
                 "raw_path": path, "query_string": b"",
                 "client": peer, "server": local,
                 "headers": list(headers.items())}
        sent = []

        async def receive():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(cast(_Server, self.server).receiver(scope, receive, send))
        if len(sent) != 2 or sent[0].get("type") != "http.response.start":
            return 500
        status = sent[0].get("status")
        return status if type(status) is int and status in {200, 400, 403, 409, 413, 500} else 500


class A6DirectSocketServer:
    """Context-managed test listener; bind only ephemeral 127.0.0.1:0."""

    def __init__(self, *, receiver: A6LocalFaultReceiver, bind_host: str,
                 port: int = 0, test_only: bool = False):
        if (test_only is not True or type(receiver) is not A6LocalFaultReceiver
                or bind_host != "127.0.0.1" or type(port) is not int or port != 0):
            raise ValueError("test_only_ephemeral_ipv4_loopback_required")
        self._receiver = receiver
        self._server: _Server | None = None
        self._thread: Thread | None = None

    def __enter__(self) -> A6DirectSocketServer:
        if self._server is not None:
            raise RuntimeError("listener_already_started")
        server = _Server(("127.0.0.1", 0), _Handler)
        server.receiver = self._receiver
        self._server = server
        self._thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    @property
    def host(self) -> str:
        if self._server is None:
            raise RuntimeError("listener_not_started")
        return cast(str, self._server.server_address[0])

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("listener_not_started")
        return self._server.server_address[1]

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            assert self._thread is not None
            self._thread.join(timeout=3)
            self._server = None
            self._thread = None
