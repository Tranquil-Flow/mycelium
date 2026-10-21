# SPDX-License-Identifier: AGPL-3.0-only
"""Direct IPv4 loopback socket transport for A6LocalFaultReceiver.

Test compositions bind an ephemeral port; the product serve process may bind an
explicit loopback port only with the exact fleet grant. No proxy, public mount,
inferred peer, model or physical fault is supplied here.
One server thread handles requests serially, so the receiver's asyncio.Lock is
never contended by separate event loops. An injected seam remains the caller's
responsibility, not a claim of physical measurement.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import socket
import time
from socketserver import BaseRequestHandler, TCPServer
from threading import Thread
from typing import cast

from mycelium_a6_fault_control import EXACT_FLEET_GRANT
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
    """Context-managed listener bound only to 127.0.0.1.

    A test composition binds only the ephemeral port 0. A grant-gated product
    mount may bind one explicit unprivileged loopback port, never a wildcard.
    """

    def __init__(self, *, receiver: A6LocalFaultReceiver, bind_host: str,
                 port: int = 0, test_only: bool = False,
                 product_grant: str | None = None):
        granted = (test_only is not True and isinstance(product_grant, str)
                   and hmac.compare_digest(product_grant.encode("utf-8"),
                                           EXACT_FLEET_GRANT.encode("ascii")))
        if (type(receiver) is not A6LocalFaultReceiver or bind_host != "127.0.0.1"
                or type(port) is not int):
            raise ValueError("test_only_ephemeral_ipv4_loopback_required")
        if granted:
            if port != 0 and not 1024 <= port <= 65535:
                raise ValueError("product_loopback_port_invalid")
        elif test_only is not True or port != 0:
            raise ValueError("test_only_ephemeral_ipv4_loopback_required")
        self._port = port
        self._receiver = receiver
        self._server: _Server | None = None
        self._thread: Thread | None = None
        self._lifetime_id: str | None = None

    def __enter__(self) -> A6DirectSocketServer:
        if self._server is not None:
            raise RuntimeError("listener_already_started")
        server = _Server(("127.0.0.1", self._port), _Handler)
        self._lifetime_id = secrets.token_hex(32)
        server.receiver = self._receiver
        self._server = server
        self._thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    @property
    def lifetime_id(self) -> str:
        """One receiver lifetime per construction; two engines never share one."""
        if self._lifetime_id is None:
            raise RuntimeError("listener_not_started")
        return self._lifetime_id

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


def _fixture_worker() -> None:
    """Local transport-only witness for the three-engine lifecycle matrix.

    The seams below echo the injection point's declared pre-failure state: this
    is a *fixture* receiver and every consumer must report ``fixture`` mode with
    ``live_evidence=false``.  It never produces an A6 product or physical claim.
    """
    from mycelium_a6_fault_control import INJECTION_POINTS

    cases = ("ordinary_browser_source_failure_recovery", "no_successor")

    class FixtureSeam:
        def __init__(self, case_id: str):
            self.point = INJECTION_POINTS[case_id]
            self.seam_id = self.point.seam_id

        def arm(self, *, case_id: str) -> dict:
            return dict(self.point.required_pre_state)

        def apply(self, *, case_id: str) -> dict:
            return dict(self.point.expected_pre_recovery_state)

    seams = [FixtureSeam(case) for case in cases]
    receiver = A6LocalFaultReceiver(
        seams={seam.seam_id: seam for seam in seams}, clock_ms=lambda: 1000,
        path="/a6/controlled-fault", test_only=True,
    )
    with A6DirectSocketServer(receiver=receiver, bind_host="127.0.0.1",
                              port=0, test_only=True) as server:
        lifetime_id = server.lifetime_id
        print(json.dumps({"lifetime_id": lifetime_id, "port": server.port}), flush=True)
        if input() != "close":
            raise ValueError("fixture_close_required")
    print(json.dumps({"lifetime_id": lifetime_id,
                      "case_ids": receiver.executed_case_ids, "torn_down": True}), flush=True)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--fixture-worker"]:
        raise SystemExit("fixture_worker_only")
    _fixture_worker()
