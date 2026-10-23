# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback HTTP origin for the managed-control mailbox.

This module is the Cloudflare Tunnel origin seam for ``control.mycelium.now``:
Cloudflare terminates public HTTPS and forwards to this loopback-only HTTP
server.  Authentication remains the signed managed-control envelope; this
adapter only gates the HTTP origin shape before dispatching to an already-built
``ManagedControlMailbox``.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import threading
from typing import Any, Mapping, cast

from mycelium_qualification.evidence import canonical_json_bytes

from .managed_control import (
    MAX_FRAME_BYTES,
    ManagedControlError,
    ManagedControlMailbox,
    _canonical_loads,
    _require,
)


CONTROL_ORIGIN_HEALTH_PROTOCOL = "mycelium.managed_control.origin_health.v1"
MANAGED_CONTROL_ORIGIN_PATHS = frozenset(
    {
        "/managed-control/enqueue",
        "/managed-control/poll",
        "/managed-control/ack",
        "/managed-control/result",
        "/managed-control/token-progress",
    }
)


def _split_host_header(value: str) -> str:
    """Return the lowercase host from one HTTP Host value, validating any port."""

    _require(isinstance(value, str), "managed_control_origin_host_invalid")
    text = value.strip()
    _require(text == value and text != "", "managed_control_origin_host_invalid")
    _require("/" not in text and "@" not in text, "managed_control_origin_host_invalid")
    if text.startswith("["):
        end = text.find("]")
        _require(end > 1, "managed_control_origin_host_invalid")
        host = text[1:end]
        rest = text[end + 1 :]
        if rest:
            _require(rest.startswith(":"), "managed_control_origin_host_invalid")
            port_text = rest[1:]
            _require(port_text.isdigit(), "managed_control_origin_host_invalid")
            port = int(port_text)
            _require(1 <= port <= 65535, "managed_control_origin_host_invalid")
        return host.lower()
    _require("[" not in text and "]" not in text, "managed_control_origin_host_invalid")
    if text.count(":") == 1:
        host, port_text = text.rsplit(":", 1)
        _require(host != "" and port_text.isdigit(), "managed_control_origin_host_invalid")
        port = int(port_text)
        _require(1 <= port <= 65535, "managed_control_origin_host_invalid")
    else:
        _require(":" not in text, "managed_control_origin_host_invalid")
        host = text
    _require(host != "", "managed_control_origin_host_invalid")
    return host.lower()


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_loopback_bind_host(host: str) -> str:
    _require(isinstance(host, str) and host != "", "managed_control_origin_bind_invalid")
    if host == "localhost":
        return host
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ManagedControlError("managed_control_origin_bind_invalid") from exc
    _require(parsed.is_loopback, "managed_control_origin_bind_invalid")
    return str(parsed)


def _validate_public_host(public_host: str) -> str:
    host = _split_host_header(public_host)
    _require(not _is_loopback_host(host), "managed_control_origin_public_host_invalid")
    return host


class _ControlOriginHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        *,
        mailbox: ManagedControlMailbox,
        public_host: str,
        allow_loopback_hosts: bool,
        max_concurrent: int,
    ) -> None:
        self.mailbox = mailbox
        self.public_host = public_host
        self.allow_loopback_hosts = bool(allow_loopback_hosts)
        self.slots = threading.BoundedSemaphore(max_concurrent)
        super().__init__(address, _ControlOriginRequestHandler)


class _ControlOriginRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def origin_server(self) -> _ControlOriginHTTPServer:
        return cast(_ControlOriginHTTPServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve("HEAD")

    def do_PUT(self) -> None:  # noqa: N802
        self._serve("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._serve("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._serve("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._serve("OPTIONS")

    def _serve(self, method: str) -> None:
        if not self.origin_server.slots.acquire(blocking=False):
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"code": "managed_control_origin_capacity_exhausted"}},
            )
            return
        try:
            self._require_allowed_host()
            if self.path == "/healthz":
                _require(method == "GET", "managed_control_method_invalid")
                self._send(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "protocol": CONTROL_ORIGIN_HEALTH_PROTOCOL,
                        "service": "managed-control-origin",
                    },
                )
                return
            if self.path in MANAGED_CONTROL_ORIGIN_PATHS:
                _require(method == "POST", "managed_control_method_invalid")
                body = self._read_mailbox_body()
                status, response = self.origin_server.mailbox.handle_http("POST", self.path, body)
                self._send(status, response)
                return
            raise ManagedControlError("managed_control_route_unknown")
        except ManagedControlError as exc:
            self._send(_status_for_error(exc.code), {"error": {"code": exc.code}})
        except BaseException:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"code": "managed_control_internal_error"}},
            )
        finally:
            self.origin_server.slots.release()

    def _require_allowed_host(self) -> None:
        hosts = self.headers.get_all("Host", [])
        _require(len(hosts) == 1, "managed_control_origin_host_invalid")
        host = _split_host_header(hosts[0])
        if host == self.origin_server.public_host:
            return
        if self.origin_server.allow_loopback_hosts and _is_loopback_host(host):
            return
        raise ManagedControlError("managed_control_origin_host_invalid")

    def _read_mailbox_body(self) -> Mapping[str, Any]:
        _require(
            self.headers.get("Content-Type") == "application/json",
            "managed_control_content_type_invalid",
        )
        _require(
            not self.headers.get_all("Transfer-Encoding", []),
            "managed_control_headers_invalid",
        )
        _require(not self.headers.get_all("Cookie", []), "managed_control_headers_invalid")
        _require(
            not self.headers.get_all("Authorization", []),
            "managed_control_headers_invalid",
        )
        lengths = self.headers.get_all("Content-Length", [])
        _require(len(lengths) == 1, "managed_control_frame_invalid")
        length_text = lengths[0]
        _require(
            isinstance(length_text, str) and length_text.isdigit(),
            "managed_control_frame_invalid",
        )
        length = int(length_text)
        _require(0 < length <= MAX_FRAME_BYTES, "managed_control_frame_too_large")
        raw = self.rfile.read(length)
        _require(len(raw) == length, "managed_control_frame_invalid")
        return _canonical_loads(raw)

    def _send(self, status: int, document: Mapping[str, Any]) -> None:
        raw = canonical_json_bytes(dict(document))
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)
            self.wfile.flush()
        self.close_connection = True


def _status_for_error(code: str) -> HTTPStatus:
    if code == "managed_control_origin_capacity_exhausted":
        return HTTPStatus.SERVICE_UNAVAILABLE
    if code == "managed_control_origin_host_invalid":
        return HTTPStatus.FORBIDDEN
    if code == "managed_control_method_invalid":
        return HTTPStatus.METHOD_NOT_ALLOWED
    if code == "managed_control_route_unknown":
        return HTTPStatus.NOT_FOUND
    if code in {
        "managed_control_conflicting_retry",
        "managed_control_sequence_replayed",
        "managed_control_nonce_replayed",
        "managed_control_member_stale",
    }:
        return HTTPStatus.CONFLICT
    if code in {
        "managed_control_signature_invalid",
        "managed_control_key_pin_mismatch",
        "managed_control_message_expired",
        "managed_control_message_from_future",
    }:
        return HTTPStatus.UNAUTHORIZED
    return HTTPStatus.BAD_REQUEST


class ControlOriginServer:
    """Loopback-only HTTP origin for a managed-control mailbox."""

    def __init__(
        self,
        mailbox: ManagedControlMailbox,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        public_host: str = "control.mycelium.now",
        allow_loopback_hosts: bool = False,
        max_concurrent: int = 16,
    ) -> None:
        _require(isinstance(mailbox, ManagedControlMailbox), "managed_control_mailbox_invalid")
        bind_host = _validate_loopback_bind_host(host)
        _require(type(port) is int and 0 <= port <= 65535, "managed_control_origin_port_invalid")
        _require(
            type(max_concurrent) is int and 1 <= max_concurrent <= 1024,
            "managed_control_origin_capacity_invalid",
        )
        self._server = _ControlOriginHTTPServer(
            (bind_host, port),
            mailbox=mailbox,
            public_host=_validate_public_host(public_host),
            allow_loopback_hosts=allow_loopback_hosts,
            max_concurrent=max_concurrent,
        )
        self._thread: threading.Thread | None = None
        self.host = str(self._server.server_address[0])
        self.port = int(self._server.server_address[1])
        self.base_url = f"http://{self.host}:{self.port}"

    @property
    def server_address(self) -> tuple[str, int]:
        address = self._server.server_address
        return (str(address[0]), int(address[1]))

    def start(self) -> "ControlOriginServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="mycelium-control-origin-http",
                daemon=True,
            )
            self._thread.start()
        return self

    def replace_mailbox_for_test(self, mailbox: ManagedControlMailbox) -> None:
        _require(isinstance(mailbox, ManagedControlMailbox), "managed_control_mailbox_invalid")
        self._server.mailbox = mailbox

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        thread.join(timeout=5.0)
        if thread.is_alive():
            raise RuntimeError("control origin server cleanup failed")
        self._thread = None

    def __enter__(self) -> "ControlOriginServer":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


__all__ = [
    "CONTROL_ORIGIN_HEALTH_PROTOCOL",
    "ControlOriginServer",
    "MANAGED_CONTROL_ORIGIN_PATHS",
]
