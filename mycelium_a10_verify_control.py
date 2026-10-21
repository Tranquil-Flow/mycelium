# SPDX-License-Identifier: AGPL-3.0-only
"""Grant-gated A10 target-verification control mounted by the ordinary product.

The A10 physical gate drives the verifier matrix through the product dispatcher
(``LiveRouterPort.a10_target_verify``).  This module is the local-only door to
that dispatcher, and only that:

* it exists only while the serve process holds the exact fleet grant in its own
  environment, and every request must present the same grant again;
* it listens on its own direct IPv4 loopback socket beside the product origin,
  refuses a non-loopback peer and any forwarding header, and refuses to start
  when the product is served behind a trusted HTTPS proxy or a public origin;
* it validates the request shape and hands the operation to the dispatcher
  unchanged; the node's runtime produces the record and the dispatcher accepts
  only the record for exactly that operation.

A refusal carries only a stable code, never exception text.  A mounted control is
a control surface, not evidence: its status never claims a qualification.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

GRANT_ENVIRONMENT = "MYCELIUM_A10_FLEET_GRANT"
EXACT_FLEET_GRANT = "FLEET GRANTED TO A10"
CONTROL_PATH = "/a10/target-verify"
STATUS_PROTOCOL = "mycelium.a10_verify_control.v1"
_GRANT_HEADER = "x-mycelium-a10-fleet-grant"
_FORWARDING_HEADERS = frozenset({"forwarded", "via", "x-real-ip", "x-client-ip"})
_MAX_BODY = 64 * 1024
_CODE = re.compile(r"[a-z][a-z0-9_:]{0,80}\Z")
_REQUEST_FIELDS = frozenset({"node_id", "operation", "timeout_s"})

Dispatch = Callable[..., tuple[str, Mapping[str, Any]]]


class A10VerifyControlRefused(ValueError):
    """The product refuses to mount the A10 verification control."""


def exact_grant(value: object) -> bool:
    return isinstance(value, str) and hmac.compare_digest(
        value.encode("utf-8"), EXACT_FLEET_GRANT.encode("ascii")
    )


def grant_from_environment(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the exact grant from the serve process environment, else None."""

    value = (os.environ if environ is None else environ).get(GRANT_ENVIRONMENT)
    return value if exact_grant(value) else None


def _stable_code(error: BaseException) -> str | None:
    for candidate in (getattr(error, "code", None), str(error)):
        if isinstance(candidate, str) and _CODE.fullmatch(candidate):
            return candidate
    return None


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # never echo request material
        return

    def _reply(self, status: int, document: Mapping[str, Any]) -> None:
        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _refuse(self) -> None:
        self._reply(403, {"code": "a10_verify_control_refused"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        control = self.server.control
        headers = {key.lower(): value for key, value in self.headers.items()}
        if (
            self.client_address[0] != "127.0.0.1"
            or self.path != CONTROL_PATH
            or any(key in _FORWARDING_HEADERS or key.startswith("x-forwarded-") for key in headers)
            or not exact_grant(headers.get(_GRANT_HEADER))
            or headers.get("content-type") != "application/json"
        ):
            self._refuse()
            return
        length = headers.get("content-length", "")
        if not length.isdigit() or not 0 < int(length) <= _MAX_BODY:
            self._reply(400, {"code": "a10_verify_request_invalid"})
            return
        try:
            request = json.loads(self.rfile.read(int(length)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            request = None
        timeout = request.get("timeout_s") if isinstance(request, dict) else None
        if (
            not isinstance(request, dict)
            or set(request) != _REQUEST_FIELDS
            or not isinstance(request["node_id"], str)
            or not request["node_id"]
            or not isinstance(request["operation"], dict)
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
        ):
            self._reply(400, {"code": "a10_verify_request_invalid"})
            return
        try:
            kind, result = control.dispatch(
                request["node_id"], request["operation"], timeout_s=timeout
            )
        except Exception as error:  # noqa: BLE001 - only a stable code leaves
            code = _stable_code(error)
            if code is None:
                self._reply(500, {"code": "a10_verify_failed"})
            else:
                self._reply(409, {"code": code})
            return
        control.record_dispatch()
        self._reply(200, {"kind": kind, "result": dict(result)})

    def _method_refused(self) -> None:
        self._refuse()

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_refused  # noqa: N815


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    control: "A10VerifyControl"


class A10VerifyControl:
    """One product-owned control lifetime on one direct loopback listener."""

    def __init__(
        self,
        *,
        grant: str | None,
        dispatch: Dispatch,
        product_port: int,
        port: int = 0,
        public_origin: str | None = None,
        trusted_https_proxy: bool = False,
    ) -> None:
        if not exact_grant(grant):
            raise A10VerifyControlRefused("explicit_fleet_grant_required")
        if public_origin is not None or trusted_https_proxy is not False:
            raise A10VerifyControlRefused(
                "a10_verify_control_requires_direct_loopback_product"
            )
        if type(product_port) is not int or not 1 <= product_port <= 65_535:
            raise A10VerifyControlRefused("a10_verify_control_product_port_invalid")
        if type(port) is not int or (port != 0 and not 1_024 <= port <= 65_535):
            raise A10VerifyControlRefused("a10_verify_control_port_invalid")
        if port == product_port:
            raise A10VerifyControlRefused("a10_verify_control_port_is_product_origin")
        if not callable(dispatch):
            raise A10VerifyControlRefused("a10_verify_control_dispatcher_required")
        self.dispatch = dispatch
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._lifetime_id: str | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._dispatched = 0

    def start(self) -> None:
        if self._server is not None or self._closed:
            raise A10VerifyControlRefused("a10_verify_control_lifecycle_invalid")
        server = _Server(("127.0.0.1", self._port), _Handler)
        server.control = self
        self._lifetime_id = secrets.token_hex(32)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is not None and not self._closed:
            self._server.shutdown()
            self._server.server_close()
            assert self._thread is not None
            self._thread.join(timeout=3)
        self._closed = True

    def record_dispatch(self) -> None:
        with self._lock:
            self._dispatched += 1

    @property
    def port(self) -> int:
        if self._server is None or self._closed:
            raise A10VerifyControlRefused("a10_verify_control_not_listening")
        return int(self._server.server_address[1])

    def status(self) -> dict[str, Any]:
        """Privacy-reduced mount status; never a qualification or a record."""

        listening = self._server is not None and not self._closed
        with self._lock:
            dispatched = self._dispatched
        return {
            "protocol": STATUS_PROTOCOL,
            "mounted": listening,
            "url": f"http://127.0.0.1:{self.port}{CONTROL_PATH}" if listening else None,
            "lifetime_id": self._lifetime_id,
            "dispatched_count": dispatched,
            "grant_required_per_request": True,
            "qualification_claim": False,
            "physical_evidence": False,
        }


__all__ = [
    "A10VerifyControl",
    "A10VerifyControlRefused",
    "CONTROL_PATH",
    "EXACT_FLEET_GRANT",
    "GRANT_ENVIRONMENT",
    "STATUS_PROTOCOL",
    "exact_grant",
    "grant_from_environment",
]
