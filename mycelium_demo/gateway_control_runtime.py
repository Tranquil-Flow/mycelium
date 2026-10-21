# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback gateway + control-origin runtime composition (PLAN v2 M1, steps 2-3).

``https://mycelium.now`` returned HTTP 502 because the tunnel was up and *no
origin listened*: every runtime piece existed (the request-gateway session API
and SSE adapter, the public-origin proxy, the managed-control origin, the cold
route restart owner) but nothing composed them into a listening process.  This
module is that composition, and nothing else.

What it starts
--------------
``public origin``   the loopback listener the tunnel ingress points at
                    (``mycelium.now -> http://127.0.0.1:4351``).  It is the
                    landed :class:`~mycelium_demo.public_origin_proxy.PublicOriginProxyServer`,
                    bound to a **configurable** loopback port (argument or
                    environment variable, never a hard-coded constant).
``gateway upstream`` the request gateway's ASGI application
                    (:class:`~mycelium_request_gateway.asgi.RequestGatewayASGIApplication`)
                    behind a bounded stdlib ASGI host, on its own loopback port.
                    A prompt submitted here is admitted by
                    :class:`~mycelium_request_gateway.service.RequestGatewayService`
                    and its token events stream back as SSE.
``control origin``  the managed-control mailbox HTTP origin
                    (:class:`~mycelium_a13_macos.control_origin.ControlOriginServer`)
                    on its own loopback port, for the ``control.mycelium.now``
                    seam.
``cold route``      the landed :class:`~mycelium_live.supervisor.ColdRouteRestartOwner`
                    is both the qualification source and the liveness gate: a
                    dead route makes the next submission return the *named*
                    ``cold_route_restart_in_progress`` (or ``_budget_exhausted``
                    / ``_failed``) error immediately instead of hanging, while
                    the owner rebuilds a replacement route for the request after
                    it.

Boundaries
----------
This composition is **loopback only**.  It installs no launchd job, edits no
Cloudflare/DNS/tunnel state, sends no announcement, and never binds
``127.0.0.1:4351`` unless a caller explicitly asks for that port.  The apex and
control cutover remain owner-authorized and are not this module's business.

Backend
-------
The runtime serves the **stub/echo backend** only
(:class:`EchoStubBackend`): it echoes the prompt's whitespace-delimited words
back as token events.  A real model, a real Router, and physical qualification
are explicitly out of scope here; the synthetic qualification that satisfies the
gateway's fail-closed admission gate is documented as *local contract proof
only* in :func:`load_stub_qualification`.

Credential provisioning
-----------------------
The gateway's bearer credential is **never accepted as a command-line
argument**: argv is world-readable on this host (``ps``) and is retained by
shell history, so a secret literal typed there has left the process boundary
before the process starts.  The CLI therefore accepts only the *name* of the
environment variable that holds the credential, and resolves its value from the
environment at invocation:

``--request-bearer-token-env <NAME>`` (default
:data:`ENV_REQUEST_BEARER_TOKEN` = ``MYCELIUM_REQUEST_GATEWAY_TOKEN``), read by
:func:`bearer_token_from_environment`.  This mirrors the landed
``mycelium-request --token-env`` shape in
:mod:`mycelium_request_gateway.cli`.  ``--capability-file`` keeps its own
meaning (the loopback proxy capability, not the bearer credential); the
constructor keyword ``request_bearer_token`` remains available to in-process
callers and tests, which do not cross an argv boundary.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import queue
import secrets
import select
import socket
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from mycelium_a13_macos.control_origin import (
    CONTROL_ORIGIN_HEALTH_PROTOCOL,
    ControlOriginServer,
)
from mycelium_a13_macos.managed_control import ManagedControlMailbox
from mycelium_invite import SqliteInviteRegistry
from mycelium_live.route import InferenceResult, RouteCounters, RouteIdentity
from mycelium_live.supervisor import (
    COLD_ROUTE_RESTART_ERROR_CODE,
    ColdRouteRestartError,
    ColdRouteRestartOwner,
)
from mycelium_node import load_or_create_node_signer
from mycelium_qualification.qualifier import qualify_route
from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
from mycelium_request_gateway.auth import StaticBearerAuthenticator
from mycelium_request_gateway.service import RequestGatewayService
from mycelium_seed import SeedCoordinator
from mycelium_seed.state import SqliteSeedState

from .public_origin_proxy import (
    ProxyConfig,
    PublicOriginProxyError,
    PublicOriginProxyServer,
    load_proxy_capability,
)

ROOT = Path(__file__).resolve().parents[1]

#: Default loopback ports.  Every one of them is an argument and an environment
#: variable; the constants are defaults, never the only accepted value.
DEFAULT_PUBLIC_ORIGIN_PORT = 4351
DEFAULT_CONTROL_ORIGIN_PORT = 4352
DEFAULT_PUBLIC_HOST = "mycelium.now"
DEFAULT_CONTROL_PUBLIC_HOST = "control.mycelium.now"
DEFAULT_STATE_ROOT = Path.home() / ".mycelium" / "m1-gateway-runtime"

ENV_PUBLIC_ORIGIN_PORT = "MYCELIUM_PUBLIC_ORIGIN_PORT"
ENV_CONTROL_ORIGIN_PORT = "MYCELIUM_CONTROL_ORIGIN_PORT"
ENV_GATEWAY_UPSTREAM_PORT = "MYCELIUM_GATEWAY_UPSTREAM_PORT"
ENV_REQUEST_BEARER_TOKEN = "MYCELIUM_REQUEST_GATEWAY_TOKEN"
ENV_STATE_ROOT = "MYCELIUM_GATEWAY_RUNTIME_STATE_ROOT"

#: Public-origin route surface (what the proxy exposes) and the gateway's own
#: session-API surface (what the request gateway serves).  The adapter below
#: maps the first onto the second.
PUBLIC_INFERENCE_PATH = "/api/v1/inference"
GATEWAY_INFERENCE_PATH = "/v1/inference"
_GATEWAY_PREFIX_MAP = (
    ("/api/v1/inference", "/v1/inference"),
    ("/api/v1/qualification/current", "/v1/qualification/current"),
    ("/api/v1/recovery/status", "/v1/recovery/status"),
)

ROUTE_STATUS_PATH = "/v1/route/status"
STUB_BACKEND_KIND = "echo-stub"
STUB_BACKEND_NOTE = (
    "deterministic echo of the prompt words; no model, no Router, no physical "
    "qualification"
)
MAX_REQUEST_BODY_BYTES = 262_144


class GatewayControlRuntimeError(ValueError):
    """Fail-closed composition error with a bounded machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise GatewayControlRuntimeError(code)


def _require_loopback(host: object, code: str) -> str:
    _require(isinstance(host, str) and host != "", code)
    text = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if text == "localhost":
        return host
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError as exc:
        raise GatewayControlRuntimeError(code) from exc
    _require(parsed.is_loopback, code)
    return host


def _require_port(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 65535):
        raise GatewayControlRuntimeError(code)
    return value


# --------------------------------------------------------------------------- #
# Stub backend (no model)
# --------------------------------------------------------------------------- #
class EchoStubBackend:
    """Deterministic echo backend for the stub runtime.

    ``run`` echoes the prompt's whitespace-delimited words back as token events
    through the gateway emitter, which is exactly the seam a real Router backend
    implements -- so the composition proves the *plumbing*, not the model.

    ``hold_after_tokens``/``hold_release`` let a caller freeze the backend after
    N tokens so a test can prove the SSE stream is delivered incrementally (the
    token arrives while the backend is still working) rather than buffered until
    the request completes.
    """

    kind = STUB_BACKEND_KIND
    note = STUB_BACKEND_NOTE
    is_simulated = True

    def __init__(
        self,
        *,
        token_delay_seconds: float = 0.0,
        hold_after_tokens: int | None = None,
        hold_release: threading.Event | None = None,
        hold_timeout_seconds: float = 20.0,
    ) -> None:
        _require(
            isinstance(token_delay_seconds, (int, float))
            and not isinstance(token_delay_seconds, bool)
            and 0.0 <= float(token_delay_seconds) <= 30.0,
            "invalid_token_delay_seconds",
        )
        if hold_after_tokens is not None:
            _require(
                isinstance(hold_after_tokens, int)
                and not isinstance(hold_after_tokens, bool)
                and hold_after_tokens >= 0,
                "invalid_hold_after_tokens",
            )
            _require(isinstance(hold_release, threading.Event), "invalid_hold_release")
        self.token_delay_seconds = float(token_delay_seconds)
        self.hold_after_tokens = hold_after_tokens
        self.hold_release = hold_release
        self.hold_timeout_seconds = float(hold_timeout_seconds)
        self.cancelled: list[str] = []
        self.started = threading.Event()
        self.finished = threading.Event()
        self.emitted_tokens = 0
        self._lock = threading.Lock()

    def run(
        self,
        request_id: str,
        submission: Any,
        emit_token: Callable[..., None],
        is_cancelled: Callable[[], bool],
    ) -> str:
        plan = stub_token_plan(submission.prompt, submission.max_new_tokens)
        self.started.set()
        try:
            for index, (token_id, text) in enumerate(plan):
                if is_cancelled():
                    return "cancelled"
                if (
                    self.hold_after_tokens is not None
                    and index == self.hold_after_tokens
                    and self.hold_release is not None
                ):
                    self.hold_release.wait(timeout=self.hold_timeout_seconds)
                emit_token(index, text, token_id)
                with self._lock:
                    self.emitted_tokens += 1
                if self.token_delay_seconds:
                    time.sleep(self.token_delay_seconds)
        finally:
            self.finished.set()
        return "completed"

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            self.cancelled.append(request_id)
        return True

    def remaining_after_hold(self) -> int:
        """Tokens still un-emitted while the backend is held (test aid)."""

        if self.hold_after_tokens is None:
            return 0
        with self._lock:
            return max(0, self.hold_after_tokens - self.emitted_tokens)


def stub_token_plan(prompt: str, max_new_tokens: int) -> tuple[tuple[int, str], ...]:
    """Deterministic ``(token_id, text)`` plan for one prompt; stub only."""

    _require(isinstance(prompt, str) and bool(prompt), "invalid_prompt")
    _require(
        isinstance(max_new_tokens, int)
        and not isinstance(max_new_tokens, bool)
        and 1 <= max_new_tokens <= 4096,
        "invalid_max_new_tokens",
    )
    words = prompt.split()
    if not words:
        words = [prompt]
    plan = [
        (1_000 + index, word + (" " if index < len(words) - 1 else ""))
        for index, word in enumerate(words)
    ]
    return tuple(plan[:max_new_tokens])


# --------------------------------------------------------------------------- #
# Stub route for the landed cold-route restart owner
# --------------------------------------------------------------------------- #
class _DiscardSink:
    """Token sink used by the cold-route liveness probe."""

    def emit(self, token_index: int, token_id: int) -> None:
        return


class StubRoute:
    """Minimal ``LiveRoute`` shape: liveness is observable, inference is not.

    The route deliberately has no inference path -- inference belongs to the
    echo backend behind the request gateway.  It exists so the *landed* cold
    route restart owner has a real route to open, retire and replace.
    """

    is_simulated = True
    startup_challenge = ((1,), (2,))

    def __init__(self, name: str, *, tokens: tuple[int, ...] = (7, 8, 9)) -> None:
        self.name = name
        self.tokens = tokens
        self.dead = True
        self.opened = 0
        self.closed = 0
        self.cleaned = 0
        self.released: list[str] = []
        self.execution_graph = _ExecutionGraphStub(name)

    def open(self) -> RouteIdentity:
        self.opened += 1
        self.dead = False
        return RouteIdentity(
            deployment_id=f"stub-deployment-{self.name}",
            model_id="stub-echo-model",
            resolved_commit="0" * 40,
            endpoint_ids=(f"stub-endpoint-{self.name}",),
        )

    def kill(self) -> None:
        """Mark the route dead, exactly as a lost physical route would be."""

        self.dead = True

    def infer(self, token_ids: Sequence[int], **options: Any) -> InferenceResult:
        raise RuntimeError("stub_route_has_no_inference")

    def release_request(self, request_id: str) -> None:
        self.released.append(request_id)

    def counters(self) -> RouteCounters:
        return RouteCounters(frames_sent=0, frames_received=0, applied_operation_count=0)

    def public_status(self) -> Mapping[str, Any]:
        return {
            "protocol": "mycelium.live_route_status.v1",
            "route_name": self.name,
            "route_alive": self.is_alive(),
            "stub": True,
        }

    def is_alive(self) -> bool:
        return self.opened > self.closed and not self.dead

    def close(self) -> None:
        self.closed += 1

    def cleanup(self) -> None:
        self.cleaned += 1


class _ExecutionGraphStub:
    def __init__(self, name: str) -> None:
        self.name = name


class ColdRouteAdmissionGate:
    """Drive the landed ``ColdRouteRestartOwner`` for a dead route.

    ``admit`` returns ``None`` when the current route can serve.  Otherwise it
    performs the bounded liveness probe that *is* the request which discovered
    the dead route: the owner retires the route, opens and publishes a
    replacement, and raises the named ``ColdRouteRestartError`` -- whose code is
    returned to the caller instead of a hang.
    """

    def __init__(self, owner: ColdRouteRestartOwner) -> None:
        _require(isinstance(owner, ColdRouteRestartOwner), "invalid_cold_route_owner")
        self._owner = owner
        self._probes = 0

    @property
    def owner(self) -> ColdRouteRestartOwner:
        return self._owner

    @property
    def probe_count(self) -> int:
        return self._probes

    def route_ready(self) -> bool:
        try:
            return self._owner.current() is not None
        except BaseException:
            return False

    def admit(self) -> str | None:
        """Return ``None`` to serve, else the named cold-route error code."""

        if self.route_ready():
            return None
        self._probes += 1
        try:
            self._owner.infer(
                (),
                max_new_tokens=1,
                request_id=f"cold-route-probe-{self._probes}",
                sink=_DiscardSink(),
            )
        except ColdRouteRestartError as exc:
            return exc.code
        except BaseException:
            return COLD_ROUTE_RESTART_ERROR_CODE
        return COLD_ROUTE_RESTART_ERROR_CODE

    def status(self) -> Mapping[str, Any]:
        try:
            return dict(self._owner.public_status())
        except BaseException:
            return {"protocol": "mycelium.live_route_status.v1", "route_alive": False}


# --------------------------------------------------------------------------- #
# ASGI host (stdlib only; streaming responses are the point)
# --------------------------------------------------------------------------- #
class _ReceiveChannel:
    """ASGI ``receive`` callable fed from the request handler's thread."""

    def __init__(self, body: bytes) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = [
            {"type": "http.request", "body": body, "more_body": False}
        ]
        self._disconnected = False

    def push_disconnect(self) -> None:
        with self._lock:
            self._disconnected = True

    async def __call__(self) -> dict[str, Any]:
        with self._lock:
            if self._pending:
                return self._pending.pop(0)
            disconnected = self._disconnected
        while not disconnected:
            await asyncio.sleep(0.02)
            with self._lock:
                disconnected = self._disconnected
        return {"type": "http.disconnect"}


class _BodyTooLarge(Exception):
    pass


class _BadRequest(Exception):
    pass


class _ASGIRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 request handler that drives one ASGI application.

    Every response is ``Connection: close``: no keep-alive state machine, and a
    streamed response without ``content-length`` is sent with chunked transfer
    encoding so SSE events leave the socket as the application produces them.
    """

    protocol_version = "HTTP/1.1"
    server_version = "MyceliumLoopbackASGI/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        _ = (format, args)
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server API
        self._handle("OPTIONS")

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802 - http.server API
        self._handle("PATCH")

    @property
    def _asgi_server(self) -> "LoopbackASGIServer":
        return self.server  # type: ignore[return-value]

    def _handle(self, method: str) -> None:
        try:
            body = self._read_body()
        except _BodyTooLarge:
            self._write_json(413, {"error": "request_body_too_large", "code": "request_body_too_large"})
            return
        except _BadRequest:
            self._write_json(400, {"error": "invalid_request_body", "code": "invalid_request_body"})
            return
        parsed = urlsplit(self.path)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": parsed.path or "/",
            "raw_path": (parsed.path or "/").encode("utf-8"),
            "query_string": parsed.query.encode("latin-1"),
            "root_path": "",
            "headers": [
                (name.encode("latin-1").lower(), value.encode("latin-1"))
                for name, value in self.headers.items()
            ],
            "client": (self.client_address[0], self.client_address[1]),
            "server": self.server.server_address,
        }
        receive = _ReceiveChannel(body)
        send_queue: "queue.SimpleQueue[dict[str, Any]]" = queue.SimpleQueue()
        future = asyncio.run_coroutine_threadsafe(
            self._run_application(scope, receive, send_queue),
            self._asgi_server.loop,
        )
        self._pump(future, send_queue, receive)

    async def _run_application(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Any],
        send_queue: "queue.SimpleQueue[dict[str, Any]]",
    ) -> None:
        async def send(message: Mapping[str, Any]) -> None:
            send_queue.put(dict(message))

        await self._asgi_server.asgi_app(scope, receive, send)

    def _pump(
        self,
        future: Any,
        send_queue: "queue.SimpleQueue[dict[str, Any]]",
        receive: _ReceiveChannel,
    ) -> None:
        deadline = time.monotonic() + self._asgi_server.response_timeout_seconds
        has_body = self.command != "HEAD"
        started = False
        chunked = False
        while True:
            try:
                message = send_queue.get(timeout=0.05)
            except queue.Empty:
                message = None
            if message is not None:
                kind = message.get("type")
                if kind == "http.response.start":
                    status = int(message["status"])
                    headers = [
                        (bytes(name), bytes(value))
                        for name, value in message.get("headers", ())
                    ]
                    names = {name.lower() for name, _ in headers}
                    chunked = has_body and status >= 200 and b"content-length" not in names
                    self.send_response(status)
                    for name, value in headers:
                        self.send_header(name.decode("latin-1"), value.decode("latin-1"))
                    if chunked:
                        self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    started = True
                elif kind == "http.response.body":
                    chunk = message.get("body", b"") or b""
                    if started and has_body and chunk:
                        if chunked:
                            self.wfile.write(
                                f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n"
                            )
                        else:
                            self.wfile.write(chunk)
                        self.wfile.flush()
                    if not message.get("more_body", False):
                        if started and has_body and chunked:
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        break
                continue
            if future.done() and send_queue.empty():
                break
            if not started and self._peer_closed():
                receive.push_disconnect()
            if time.monotonic() > deadline:
                break
        self.close_connection = True
        if not started:
            self._write_json(500, {"error": "asgi_application_failed", "code": "asgi_application_failed"})

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _BadRequest
        values = self.headers.get_all("Content-Length") or []
        if len(values) > 1:
            raise _BadRequest
        if not values:
            return b""
        try:
            length = int(values[0], 10)
        except ValueError:
            raise _BadRequest from None
        if length < 0:
            raise _BadRequest
        if length > self._asgi_server.max_request_body_bytes:
            raise _BodyTooLarge
        return self.rfile.read(length)

    def _peer_closed(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _write_json(self, status: int, document: Mapping[str, Any]) -> None:
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True


class LoopbackASGIServer(ThreadingHTTPServer):
    """Loopback HTTP/1.1 host for one ASGI application, with streaming."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        app: Any,
        *,
        max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES,
        response_timeout_seconds: float = 120.0,
    ) -> None:
        _require_loopback(address[0], "non_loopback_gateway_host")
        _require_port(address[1], "invalid_gateway_port")
        _require(callable(app), "invalid_asgi_application")
        _require(
            isinstance(max_request_body_bytes, int)
            and not isinstance(max_request_body_bytes, bool)
            and 1 <= max_request_body_bytes <= 16 * 1024 * 1024,
            "invalid_max_request_body_bytes",
        )
        _require(
            isinstance(response_timeout_seconds, (int, float))
            and not isinstance(response_timeout_seconds, bool)
            and 0 < float(response_timeout_seconds) <= 3600,
            "invalid_response_timeout_seconds",
        )
        self.asgi_app = app
        self.max_request_body_bytes = max_request_body_bytes
        self.response_timeout_seconds = float(response_timeout_seconds)
        self.loop = asyncio.new_event_loop()
        self._loop_thread: threading.Thread | None = None
        super().__init__(address, _ASGIRequestHandler)

    @property
    def host(self) -> str:
        return str(self.server_address[0])

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start(self) -> "LoopbackASGIServer":
        if self._loop_thread is None:
            self._loop_thread = threading.Thread(
                target=self._run_loop,
                name="mycelium-gateway-asyncio",
                daemon=True,
            )
            self._loop_thread.start()
        return self

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def serve_forever_thread(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="mycelium-gateway-http",
            daemon=True,
        )
        thread.start()
        return thread

    def stop(self, thread: threading.Thread | None = None) -> None:
        if thread is not None and thread.is_alive():
            self.shutdown()
            thread.join(timeout=5.0)
        self.server_close()
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)
            self._loop_thread = None
        if not self.loop.is_closed():
            self.loop.close()


# --------------------------------------------------------------------------- #
# Public-origin surface adaptation and cold-route admission
# --------------------------------------------------------------------------- #
class OriginAuthenticator:
    """Accept the loopback proxy hop's capability, or a direct bearer token.

    The public-origin proxy strips the client ``Authorization`` header and
    injects ``X-Mycelium-Authenticated-User`` plus an owner-private
    ``X-Mycelium-Proxy-Capability`` (see ``mycelium_demo.public_origin_proxy``),
    so the upstream must trust exactly that pair -- and nothing else.  A bearer
    token stays available for direct loopback diagnostics against the gateway's
    own port, which is what ``mycelium_request_gateway.client.HTTPGatewayClient``
    speaks.
    """

    _USER_HEADER = b"x-mycelium-authenticated-user"
    _CAPABILITY_HEADER = b"x-mycelium-proxy-capability"

    def __init__(
        self,
        *,
        capability: str | None = None,
        authenticated_user: str = "owner",
        bearer_token: str | None = None,
    ) -> None:
        self._capability: bytes | None = None
        if capability is not None:
            try:
                encoded = capability.encode("ascii")
            except UnicodeEncodeError as exc:
                raise GatewayControlRuntimeError("invalid_proxy_capability") from exc
            _require(
                len(encoded) == 64 and all(value in b"0123456789abcdef" for value in encoded),
                "invalid_proxy_capability",
            )
            self._capability = encoded
        try:
            self._user = authenticated_user.encode("ascii")
        except UnicodeEncodeError as exc:
            raise GatewayControlRuntimeError("invalid_authenticated_user") from exc
        self._bearer = (
            None if bearer_token is None else StaticBearerAuthenticator(bearer_token)
        )
        _require(self._capability is not None or self._bearer is not None, "no_origin_credential")

    def is_authorized(self, scope: Mapping[str, Any]) -> bool:
        if self._capability is not None and self._capability_authorized(scope):
            return True
        if self._bearer is not None:
            return self._bearer.is_authorized(scope)
        return False

    def _capability_authorized(self, scope: Mapping[str, Any]) -> bool:
        headers = scope.get("headers", ())
        if not isinstance(headers, (list, tuple)):
            return False
        users: list[bytes] = []
        capabilities: list[bytes] = []
        for item in headers:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not isinstance(item[0], bytes)
                or not isinstance(item[1], bytes)
            ):
                return False
            name = item[0].lower()
            if name == self._USER_HEADER:
                users.append(item[1])
            elif name == self._CAPABILITY_HEADER:
                capabilities.append(item[1])
        import hmac

        return (
            len(users) == 1
            and users[0] == self._user
            and len(capabilities) == 1
            and hmac.compare_digest(capabilities[0], self._capability or b"")
        )


def map_public_path_to_gateway_path(path: object) -> str | None:
    """Map one public-origin route onto the gateway's session-API route."""

    if not isinstance(path, str) or not path.startswith("/"):
        return None
    for public_prefix, gateway_prefix in _GATEWAY_PREFIX_MAP:
        if path == public_prefix:
            return gateway_prefix
        if path.startswith(public_prefix + "/"):
            tail = path[len(public_prefix) :]
            if public_prefix == PUBLIC_INFERENCE_PATH and tail.endswith("/cancel"):
                # The public surface names the cancel route explicitly; the
                # gateway's session API addresses the request directly.
                return gateway_prefix + tail[: -len("/cancel")]
            return gateway_prefix + tail
    return None


class PublicOriginPathAdapter:
    """Serve the gateway session API on the public-origin route surface."""

    def __init__(self, app: Any, *, gateway_path: str = GATEWAY_INFERENCE_PATH) -> None:
        self._app = app
        self._gateway_path = gateway_path

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        rewritten = map_public_path_to_gateway_path(scope.get("path"))
        if rewritten is None:
            await self._app(scope, receive, send)
            return
        child = dict(scope)
        child["path"] = rewritten
        child["raw_path"] = rewritten.encode("utf-8")
        await self._app(child, receive, send)


async def _send_json(send: Any, status: int, document: Mapping[str, Any], *, retry_after: str | None = None) -> None:
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", retry_after.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class ColdRouteAdmissionMiddleware:
    """Refuse a submission with the *named* cold-route error instead of waiting."""

    def __init__(
        self,
        app: Any,
        gate: ColdRouteAdmissionGate,
        *,
        submit_path: str = GATEWAY_INFERENCE_PATH,
        status_path: str = ROUTE_STATUS_PATH,
    ) -> None:
        _require(isinstance(gate, ColdRouteAdmissionGate), "invalid_cold_route_gate")
        self._app = app
        self._gate = gate
        self._submit_path = submit_path
        self._status_path = status_path

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path")
        if path == self._status_path:
            if scope.get("method") != "GET":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            await _send_json(
                send,
                200,
                {
                    "protocol": "mycelium.cold_route_restart_status.v1",
                    "backend": STUB_BACKEND_KIND,
                    "route_ready": self._gate.route_ready(),
                    "probes": self._gate.probe_count,
                    "cold_route_restart": dict(self._gate.status().get("cold_route_restart", {})),
                    "route": dict(self._gate.status()),
                },
            )
            return
        if scope.get("method") == "POST" and path == self._submit_path:
            code = await asyncio.to_thread(self._gate.admit)
            if code is not None:
                await _send_json(
                    send,
                    503,
                    {
                        "error": code,
                        "code": code,
                        "retryable": True,
                        "cold_route_restart": dict(self._gate.status().get("cold_route_restart", {})),
                    },
                    retry_after="1",
                )
                return
        await self._app(scope, receive, send)


# --------------------------------------------------------------------------- #
# Synthetic qualification for the stub runtime (local contract proof only)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def load_stub_qualification() -> Any:
    """Issue one ``route_ready=True`` qualification for the stub runtime.

    **This is not physical evidence.**  It is the repository's own
    ``synthetic_test_fixture`` document set (``tests/qualification/conftest.py``)
    run through the *real* :func:`mycelium_qualification.qualifier.qualify_route`
    with test-only signature verifiers -- exactly how the request-conformance
    suite obtains a working record.  A production origin never calls this: the
    live route owner publishes the qualification the qualifier authority issued
    for the physical route (``ColdRouteRestartOwner.acts_as_qualification_source``).
    """

    spec = importlib.util.spec_from_file_location(
        "m1_gateway_stub_qualification_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    _require(spec is not None and spec.loader is not None, "stub_qualification_fixture_missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    case = module.make_case()
    files, manifest = case.render()

    def verify(statement: bytes, signature: Mapping[str, Any]) -> bool:
        from mycelium_qualification.evidence import sha256_bytes

        return (
            signature.get("algorithm") == "ed25519"
            and signature.get("signature") == "synthetic-test-signature-never-production"
            and signature.get("signed_statement_digest") == sha256_bytes(statement)
        )

    qualification = qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=verify,
        verify_load_proof_signature=verify,
    )
    _require(qualification.route_ready is True, "stub_qualification_not_ready")
    return qualification


# --------------------------------------------------------------------------- #
# Control-origin mailbox
# --------------------------------------------------------------------------- #
def build_control_mailbox(state_root: Path) -> ManagedControlMailbox:
    """Build the managed-control mailbox that the control origin serves."""

    root = Path(state_root).expanduser().resolve() / "control-origin"
    identity = root / "identity"
    identity.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(identity, 0o700)
    signer = load_or_create_node_signer(identity / "control-origin.key")
    database = root / "control-origin.sqlite3"
    return ManagedControlMailbox(
        database,
        coordinator=SeedCoordinator(
            swarm_id="m1-gateway-control-runtime",
            seed_node_id="m1-control-origin",
            seed_url=None,
            signer=signer,
            invite_registry=SqliteInviteRegistry(database),
            incarnation="m1-control-origin-incarnation",
            state=SqliteSeedState(database),
        ),
    )


# --------------------------------------------------------------------------- #
# Runtime composition
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RuntimePorts:
    public_origin: int
    gateway_upstream: int
    control_origin: int | None


class GatewayControlRuntime:
    """One loopback process serving the public origin, gateway and control origin."""

    def __init__(
        self,
        *,
        public_origin_port: int = DEFAULT_PUBLIC_ORIGIN_PORT,
        gateway_upstream_port: int = 0,
        control_origin_port: int = DEFAULT_CONTROL_ORIGIN_PORT,
        control_origin_enabled: bool = True,
        public_host: str = DEFAULT_PUBLIC_HOST,
        control_public_host: str = DEFAULT_CONTROL_PUBLIC_HOST,
        capability: str | None = None,
        capability_file: Path | str | None = None,
        authenticated_user: str = "owner",
        request_bearer_token: str | None = None,
        state_root: Path | str = DEFAULT_STATE_ROOT,
        backend: Any | None = None,
        max_buffered_events: int = 64,
        restart_budget: int = 1,
        upstream_timeout_seconds: float = 30.0,
    ) -> None:
        _require_port(public_origin_port, "invalid_public_origin_port")
        _require_port(gateway_upstream_port, "invalid_gateway_upstream_port")
        _require(
            control_origin_port is None or 0 <= int(control_origin_port) <= 65535,
            "invalid_control_origin_port",
        )
        _require_loopback("127.0.0.1", "non_loopback_gateway_host")
        capability_value = capability
        if capability_file is not None:
            _require(capability is None, "capability_ambiguous")
            try:
                capability_value = load_proxy_capability(capability_file)
            except PublicOriginProxyError as exc:
                raise GatewayControlRuntimeError(exc.code) from exc
        self._capability_generated = False
        if capability_value is None:
            capability_value = secrets.token_hex(32)
            self._capability_generated = True
        self.public_origin_port = int(public_origin_port)
        self.gateway_upstream_port = int(gateway_upstream_port)
        self.control_origin_port = None if not control_origin_enabled else int(control_origin_port or 0)
        self.public_host = public_host
        self.control_public_host = control_public_host
        self.state_root = Path(state_root).expanduser()
        self.upstream_timeout_seconds = float(upstream_timeout_seconds)
        self._capability = capability_value
        self._authenticated_user = authenticated_user
        self._request_bearer_token = request_bearer_token
        self._max_buffered_events = int(max_buffered_events)
        self._restart_budget = int(restart_budget)

        self.qualification = load_stub_qualification()
        self.routes: list[StubRoute] = []
        self._current_stub_route: StubRoute | None = None
        self._route_counter = 0
        self._request_counter = 0

        def route_factory() -> StubRoute:
            self._route_counter += 1
            route = StubRoute(f"stub-route-{self._route_counter}")
            self.routes.append(route)
            return route

        def open_and_qualify(route: StubRoute, generation: int) -> tuple[RouteIdentity, Any]:
            identity = route.open()
            _ = generation
            self._current_stub_route = route
            return identity, self.qualification

        self.route_owner = ColdRouteRestartOwner(
            route_factory=route_factory,
            open_and_qualify=open_and_qualify,
            restart_budget=self._restart_budget,
        )
        self.cold_route_gate = ColdRouteAdmissionGate(self.route_owner)
        self.backend = backend if backend is not None else EchoStubBackend()

        def next_request_id() -> str:
            self._request_counter += 1
            return f"m1-{self._request_counter}-{secrets.token_hex(6)}"

        self.service = RequestGatewayService(
            qualification_source=self.route_owner,
            backend=self.backend,
            request_id_source=next_request_id,
            max_buffered_events=self._max_buffered_events,
        )
        self.authenticator = OriginAuthenticator(
            capability=self._capability,
            authenticated_user=self._authenticated_user,
            bearer_token=self._request_bearer_token,
        )
        gateway_app = RequestGatewayASGIApplication(
            service=self.service,
            authenticator=self.authenticator,
        )
        composed = ColdRouteAdmissionMiddleware(gateway_app, self.cold_route_gate)
        self.asgi_app = PublicOriginPathAdapter(composed)
        self.gateway_server = LoopbackASGIServer(
            ("127.0.0.1", self.gateway_upstream_port),
            self.asgi_app,
        )
        self.public_origin = PublicOriginProxyServer(
            ("127.0.0.1", self.public_origin_port),
            ProxyConfig(
                listen_host="127.0.0.1",
                listen_port=self.public_origin_port,
                upstream_host="127.0.0.1",
                upstream_port=self.gateway_server.port,
                public_host=public_host,
                capability=capability_value,
                authenticated_user=authenticated_user,
                upstream_timeout_seconds=self.upstream_timeout_seconds,
            ),
        )
        self.control_origin: ControlOriginServer | None = None
        self._mailbox: ManagedControlMailbox | None = None
        self._threads: list[threading.Thread] = []
        self._started = False

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> "GatewayControlRuntime":
        _require(not self._started, "runtime_already_started")
        self.route_owner.open()
        self.gateway_server.start()
        self._threads.append(self.gateway_server.serve_forever_thread())
        self._threads.append(self._serve(
            self.public_origin.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="mycelium-public-origin-proxy",
        ))
        if self.control_origin_port is not None:
            self._mailbox = build_control_mailbox(self.state_root)
            self.control_origin = ControlOriginServer(
                self._mailbox,
                host="127.0.0.1",
                port=self.control_origin_port,
                public_host=self.control_public_host,
                allow_loopback_hosts=True,
            ).start()
        self._started = True
        return self

    def _serve(self, target: Callable[..., Any], *, kwargs: Mapping[str, Any], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, kwargs=dict(kwargs), name=name, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        if self.control_origin is not None:
            self.control_origin.close()
            self.control_origin = None
        if self._mailbox is not None:
            try:
                self._mailbox.close()
            except BaseException:
                pass
            self._mailbox = None
        if self._started:
            try:
                self.public_origin.shutdown()
            except BaseException:
                pass
        self.public_origin.server_close()
        gateway_thread = self._threads[0] if self._threads else None
        self.gateway_server.stop(gateway_thread)
        try:
            self.service.close()
        finally:
            self.route_owner.close()
            self._threads = []
            self._started = False

    def __enter__(self) -> "GatewayControlRuntime":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.stop()

    # -- observations and probes -------------------------------------------- #
    @property
    def ports(self) -> RuntimePorts:
        return RuntimePorts(
            public_origin=self.public_origin_port,
            gateway_upstream=self.gateway_server.port,
            control_origin=self.control_origin.port if self.control_origin is not None else None,
        )

    @property
    def public_origin_base_url(self) -> str:
        return f"http://127.0.0.1:{self.public_origin_port}"

    @property
    def gateway_base_url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_server.port}"

    @property
    def control_origin_base_url(self) -> str | None:
        if self.control_origin is None:
            return None
        return f"http://127.0.0.1:{self.control_origin.port}"

    def kill_current_route(self) -> None:
        """Mark the serving route dead, as a lost route would be."""

        current = self._current_stub_route
        _require(current is not None and current.is_alive(), "no_current_route")
        current.kill()

    def status(self) -> Mapping[str, Any]:
        return {
            "protocol": "mycelium.gateway_control_runtime.v1",
            "ports": {
                "public_origin": self.public_origin_port,
                "gateway_upstream": self.gateway_server.port,
                "control_origin": self.control_origin.port if self.control_origin else None,
            },
            "public_origin_url": self.public_origin_base_url,
            "gateway_upstream_url": self.gateway_base_url,
            "control_origin_url": self.control_origin_base_url,
            "public_host": self.public_host,
            "control_public_host": self.control_public_host,
            "control_origin_health_protocol": CONTROL_ORIGIN_HEALTH_PROTOCOL,
            "backend": {"kind": self.backend.kind, "note": self.backend.note},
            "qualification": {
                "route_ready": self.qualification.route_ready,
                "qualification_id": self.qualification.qualification_id,
                "evidence_class": self.qualification.evidence_class,
                "claim_boundary": "stub runtime: synthetic shape fixture, not physical evidence",
            },
            "capability_source": "generated" if self._capability_generated else "file",
            "cold_route_restart": dict(
                self.cold_route_gate.status().get("cold_route_restart", {})
            ),
            "live_host_mutations": 0,
        }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _port_argument(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be from 0 through 65535")
    return port


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        raise GatewayControlRuntimeError(f"invalid_env_port:{name}") from None
    _require_port(value, f"invalid_env_port:{name}")
    return value


def bearer_token_from_environment(variable: str = ENV_REQUEST_BEARER_TOKEN) -> str | None:
    """Read the gateway bearer credential from ``variable`` in the environment.

    The credential is provisioned out of band (the environment of the serving
    process) and is deliberately **not** an option value: see the module
    docstring's *Credential provisioning* section.  ``variable`` is only the
    *name* of the environment variable, so naming it on the command line carries
    no secret.  An unset or empty variable means "no bearer credential", exactly
    as the removed ``--request-bearer-token`` default did.
    """

    return os.environ.get(variable)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_demo.gateway_control_runtime",
        description=(
            "Loopback gateway + control-origin runtime with the stub/echo backend. "
            "Never installs launchd jobs and never edits DNS or tunnel state."
        ),
        # Abbreviations are disabled on purpose: with them, ``--request-bearer-token``
        # would resolve to ``--request-bearer-token-env`` and silently swallow the
        # *credential* as if it were the variable name.  The credential is only ever
        # read from the environment (see ``bearer_token_from_environment``), so this
        # also keeps the argv surface free of secret-shaped values.
        allow_abbrev=False,
    )
    parser.add_argument(
        "--public-port",
        type=_port_argument,
        default=_env_port(ENV_PUBLIC_ORIGIN_PORT, DEFAULT_PUBLIC_ORIGIN_PORT),
        help=f"public-origin loopback port (env {ENV_PUBLIC_ORIGIN_PORT}; 0 = ephemeral)",
    )
    parser.add_argument(
        "--gateway-port",
        type=_port_argument,
        default=_env_port(ENV_GATEWAY_UPSTREAM_PORT, 0),
        help=f"gateway upstream loopback port (env {ENV_GATEWAY_UPSTREAM_PORT}; default 0)",
    )
    parser.add_argument(
        "--control-port",
        type=_port_argument,
        default=_env_port(ENV_CONTROL_ORIGIN_PORT, DEFAULT_CONTROL_ORIGIN_PORT),
        help=f"control-origin loopback port (env {ENV_CONTROL_ORIGIN_PORT})",
    )
    parser.add_argument("--no-control-origin", action="store_true")
    parser.add_argument("--public-host", default=DEFAULT_PUBLIC_HOST)
    parser.add_argument("--control-public-host", default=DEFAULT_CONTROL_PUBLIC_HOST)
    parser.add_argument("--capability-file", type=Path)
    parser.add_argument("--authenticated-user", default="owner")
    parser.add_argument(
        "--request-bearer-token-env",
        default=ENV_REQUEST_BEARER_TOKEN,
        metavar="NAME",
        help=(
            "name of the environment variable holding the gateway bearer "
            f"credential (default {ENV_REQUEST_BEARER_TOKEN}); the credential "
            "value itself is never accepted as an argument"
        ),
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path(os.environ.get(ENV_STATE_ROOT) or DEFAULT_STATE_ROOT),
    )
    parser.add_argument("--max-buffered-events", type=int, default=64)
    parser.add_argument("--restart-budget", type=int, default=1)
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="print the composition status document and exit without serving",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        runtime = GatewayControlRuntime(
            public_origin_port=args.public_port,
            gateway_upstream_port=args.gateway_port,
            control_origin_port=args.control_port,
            control_origin_enabled=not args.no_control_origin,
            public_host=args.public_host,
            control_public_host=args.control_public_host,
            capability_file=args.capability_file,
            authenticated_user=args.authenticated_user,
            request_bearer_token=bearer_token_from_environment(
                args.request_bearer_token_env
            ),
            state_root=args.state_root,
            max_buffered_events=args.max_buffered_events,
            restart_budget=args.restart_budget,
        )
    except GatewayControlRuntimeError as exc:
        print(f"gateway-control-runtime: {exc.code}", file=sys.stderr)
        return 2
    if args.status_only:
        print(json.dumps(runtime.status(), sort_keys=True, indent=2))
        runtime.stop()
        return 0
    with runtime:
        print(
            json.dumps(
                {
                    "protocol": "mycelium.gateway_control_runtime.ready.v1",
                    "ports": {
                        "public_origin": runtime.public_origin_port,
                        "gateway_upstream": runtime.gateway_server.port,
                        "control_origin": runtime.control_origin.port if runtime.control_origin else None,
                    },
                    "backend": runtime.backend.kind,
                    "live_host_mutations": 0,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI test
    raise SystemExit(main())
