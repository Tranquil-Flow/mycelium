"""Loopback public-origin proxy for Cloudflared-backed demo recordings.

The proxy is intentionally stdlib-only and small: it authenticates the local
Cloudflared-to-product hop with an owner-private capability file, overwrites the
trusted headers consumed by the product gateway, and exposes only the public
consumer-demo routes.
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import stat
from typing import Iterable
from urllib.parse import urlsplit


_CAPABILITY_BYTES = frozenset(b"0123456789abcdef")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._~-]{1,64}")
_BLOCKED_PREFIXES = (
    "/__mycelium",
    "/api/v1/swarm/invites",
    "/api/v1/swarm/join",
    "/api/v1/swarm/leave",
)
_PUBLIC_GET_API_PATHS = frozenset(
    {
        "/api/v1/bootstrap",
        "/api/v1/observatory/snapshot",
        "/api/v1/observatory/events",
        "/api/v1/qualification/current",
        "/api/v1/recovery/status",
        "/api/v1/product/snapshot",
        "/api/v1/product/events",
        "/api/v1/product/export",
        "/api/v1/swarm/status",
    }
)
_INFERENCE_PREFIX = "/api/v1/inference/"
_RAW_INFERENCE_PATH = "/v1/inference"
_STRIPPED_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-forwarded-proto",
        "x-mycelium-authenticated-user",
        "x-mycelium-proxy-capability",
        "host",
    }
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class PublicOriginProxyError(ValueError):
    """Fail-closed configuration or request validation error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    public_host: str
    capability: str
    authenticated_user: str = "owner"
    max_header_bytes: int = 16_384
    max_body_bytes: int = 2 * 1024 * 1024
    upstream_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not _is_loopback_host(self.listen_host):
            raise PublicOriginProxyError("non_loopback_listen_host")
        if not _is_loopback_host(self.upstream_host):
            raise PublicOriginProxyError("non_loopback_upstream_host")
        _validate_port("listen_port", self.listen_port)
        _validate_port("upstream_port", self.upstream_port)
        _validate_public_host(self.public_host)
        _validate_authenticated_user(self.authenticated_user)
        _validate_capability_text(self.capability)
        if not isinstance(self.max_header_bytes, int) or not (
            1 <= self.max_header_bytes <= 64 * 1024
        ):
            raise PublicOriginProxyError("invalid_max_header_bytes")
        if not isinstance(self.max_body_bytes, int) or not (
            1 <= self.max_body_bytes <= 16 * 1024 * 1024
        ):
            raise PublicOriginProxyError("invalid_max_body_bytes")
        if not isinstance(self.upstream_timeout_seconds, (int, float)) or not (
            0 < float(self.upstream_timeout_seconds) <= 300
        ):
            raise PublicOriginProxyError("invalid_upstream_timeout_seconds")


class PublicOriginProxyServer(ThreadingHTTPServer):
    """Threaded loopback proxy server with immutable validated configuration."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], config: ProxyConfig) -> None:
        self.proxy_config = config
        super().__init__(server_address, PublicOriginProxyHandler)


class PublicOriginProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MyceliumPublicOriginProxy/1"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._handle_public_request()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._handle_public_request()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._handle_public_request()

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        self._handle_public_request()

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server API
        self._send_json_error(403, "public_method_not_allowed")

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        self._send_json_error(403, "public_method_not_allowed")

    def do_PATCH(self) -> None:  # noqa: N802 - http.server API
        self._send_json_error(403, "public_method_not_allowed")

    def log_message(self, format: str, *args: object) -> None:
        _ = (format, args)
        return

    @property
    def _config(self) -> ProxyConfig:
        return self.server.proxy_config  # type: ignore[attr-defined]

    def _handle_public_request(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        allowed, status, code = _route_decision(self.command, path)
        if not allowed:
            self._send_json_error(status, code)
            return
        try:
            self._reject_oversized_headers()
            body = self._read_bounded_body()
            headers = self._forward_headers(len(body) if body is not None else None)
            upstream_path = self.path if self.path.startswith("/") else parsed.path or "/"
            self._proxy_to_upstream(upstream_path, headers, body)
        except PublicOriginProxyError as exc:
            status = 413 if exc.code in {"request_headers_too_large", "request_body_too_large"} else 400
            self._send_json_error(status, exc.code)
        except (OSError, http.client.HTTPException, TimeoutError):
            self._send_json_error(502, "upstream_unavailable")

    def _reject_oversized_headers(self) -> None:
        total = 0
        for name, value in self.headers.items():
            total += len(name.encode("latin-1", "ignore")) + len(
                value.encode("latin-1", "ignore")
            ) + 4
        if total > self._config.max_header_bytes:
            raise PublicOriginProxyError("request_headers_too_large")

    def _read_bounded_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding") is not None:
            raise PublicOriginProxyError("unsupported_transfer_encoding")
        values = self.headers.get_all("Content-Length") or []
        if len(values) > 1:
            raise PublicOriginProxyError("invalid_content_length")
        if not values:
            return b""
        try:
            length = int(values[0], 10)
        except ValueError:
            raise PublicOriginProxyError("invalid_content_length") from None
        if length < 0:
            raise PublicOriginProxyError("invalid_content_length")
        if length > self._config.max_body_bytes:
            raise PublicOriginProxyError("request_body_too_large")
        return self.rfile.read(length)

    def _forward_headers(self, body_length: int | None) -> dict[str, str]:
        outbound: dict[str, str] = {}
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in _HOP_BY_HOP_HEADERS or lower in _STRIPPED_REQUEST_HEADERS:
                continue
            outbound[name] = value
        outbound["Host"] = self._config.public_host
        outbound["X-Forwarded-Proto"] = "https"
        outbound["X-Mycelium-Authenticated-User"] = self._config.authenticated_user
        outbound["X-Mycelium-Proxy-Capability"] = self._config.capability
        if body_length is not None:
            outbound["Content-Length"] = str(body_length)
        return outbound

    def _proxy_to_upstream(
        self, path: str, headers: dict[str, str], body: bytes
    ) -> None:
        connection = http.client.HTTPConnection(
            self._config.upstream_host,
            self._config.upstream_port,
            timeout=float(self._config.upstream_timeout_seconds),
        )
        try:
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in _HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                reader = getattr(response, "read1", response.read)
                while True:
                    chunk = reader(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        finally:
            self.close_connection = True
            connection.close()

    def _send_json_error(self, status: int, code: str) -> None:
        body = json.dumps(
            {
                "protocol": "mycelium.public_origin_proxy.v1",
                "error": {"code": code},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True


def load_proxy_capability(path: Path | str) -> str:
    """Read one owner-private 64-lowercase-hex capability from an absolute file."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise PublicOriginProxyError("relative_capability_path")
    try:
        named = os.lstat(candidate)
    except FileNotFoundError:
        raise PublicOriginProxyError("capability_file_missing") from None
    if stat.S_ISLNK(named.st_mode):
        raise PublicOriginProxyError("capability_file_symlink")
    if not stat.S_ISREG(named.st_mode):
        raise PublicOriginProxyError("capability_file_not_regular")
    if named.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PublicOriginProxyError("capability_file_not_owner_private")
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise PublicOriginProxyError("capability_file_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(66)
    finally:
        os.close(descriptor)
    if data.endswith(b"\n") and len(data) == 65:
        data = data[:-1]
    _validate_capability_bytes(data)
    return data.decode("ascii")


def build_config_from_args(argv: Iterable[str] | None = None) -> ProxyConfig:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        capability = load_proxy_capability(args.trusted_proxy_capability_file)
        upstream_host, upstream_port = _parse_upstream(args.upstream)
        return ProxyConfig(
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
            public_host=args.public_host,
            capability=capability,
            authenticated_user=args.authenticated_user,
            max_header_bytes=args.max_header_bytes,
            max_body_bytes=args.max_body_bytes,
            upstream_timeout_seconds=args.upstream_timeout_seconds,
        )
    except PublicOriginProxyError as exc:
        parser.error(exc.code)


def main(argv: Iterable[str] | None = None) -> int:
    config = build_config_from_args(argv)
    with PublicOriginProxyServer((config.listen_host, config.listen_port), config) as server:
        server.serve_forever(poll_interval=0.25)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_demo.public_origin_proxy",
        description="Stdlib loopback proxy for the Mycelium public demo origin.",
    )
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=_port_argument, default=4350)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--public-host", default="mycelium.now")
    parser.add_argument(
        "--trusted-proxy-capability-file",
        type=Path,
        required=True,
        help="absolute owner-private file containing one 64-lowercase-hex token",
    )
    parser.add_argument("--authenticated-user", default="owner")
    parser.add_argument("--max-header-bytes", type=int, default=16_384)
    parser.add_argument("--max-body-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--upstream-timeout-seconds", type=float, default=30.0)
    return parser


def _port_argument(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    try:
        _validate_port("port", port)
    except PublicOriginProxyError:
        raise argparse.ArgumentTypeError("port must be from 1 through 65535") from None
    return port


def _validate_port(_name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
        raise PublicOriginProxyError("invalid_port")


def _is_loopback_host(host: object) -> bool:
    if not isinstance(host, str) or not host:
        return False
    if host.lower() == "localhost":
        return True
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _validate_public_host(host: object) -> str:
    if not isinstance(host, str) or not host or len(host) > 255:
        raise PublicOriginProxyError("invalid_public_host")
    if any(ord(character) <= 0x20 or ord(character) >= 0x7F for character in host):
        raise PublicOriginProxyError("invalid_public_host")
    parsed = urlsplit(f"//{host}")
    if (
        parsed.scheme
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PublicOriginProxyError("invalid_public_host")
    return host


def _validate_authenticated_user(value: object) -> str:
    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
        raise PublicOriginProxyError("invalid_authenticated_user")
    return value


def _validate_capability_text(value: object) -> str:
    if not isinstance(value, str):
        raise PublicOriginProxyError("invalid_proxy_capability")
    try:
        data = value.encode("ascii")
    except UnicodeEncodeError:
        raise PublicOriginProxyError("invalid_proxy_capability") from None
    _validate_capability_bytes(data)
    return value


def _validate_capability_bytes(data: bytes) -> None:
    if len(data) != 64:
        raise PublicOriginProxyError("invalid_proxy_capability")
    if any(value not in _CAPABILITY_BYTES for value in data):
        raise PublicOriginProxyError("invalid_proxy_capability")


def _parse_upstream(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PublicOriginProxyError("invalid_upstream")
    if not _is_loopback_host(parsed.hostname):
        raise PublicOriginProxyError("non_loopback_upstream_host")
    return parsed.hostname, parsed.port or 80


def _route_decision(method: str, path: str) -> tuple[bool, int, str]:
    if not path.startswith("/"):
        return False, 404, "public_route_not_found"
    if path == _RAW_INFERENCE_PATH or path.startswith(_RAW_INFERENCE_PATH + "/"):
        return False, 404, "public_route_not_found"
    if _is_blocked_path(path):
        return False, 403, "public_route_forbidden"
    if method in {"GET", "HEAD"} and (path == "/" or not path.startswith("/api/")):
        return True, 200, "ok"
    if method == "GET" and path in _PUBLIC_GET_API_PATHS:
        return True, 200, "ok"
    if method == "POST" and path == "/api/v1/inference":
        return True, 200, "ok"
    if method == "GET" and _is_inference_events_path(path):
        return True, 200, "ok"
    if method == "DELETE" and _is_inference_cancel_path(path):
        return True, 200, "ok"
    if method not in {"GET", "HEAD", "POST", "DELETE"}:
        return False, 403, "public_method_not_allowed"
    return False, 404, "public_route_not_found"


def _is_blocked_path(path: str) -> bool:
    for prefix in _BLOCKED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _is_inference_events_path(path: str) -> bool:
    if not path.startswith(_INFERENCE_PREFIX) or not path.endswith("/events"):
        return False
    request_id = path[len(_INFERENCE_PREFIX) : -len("/events")]
    return bool(request_id) and "/" not in request_id


def _is_inference_cancel_path(path: str) -> bool:
    if not path.startswith(_INFERENCE_PREFIX) or not path.endswith("/cancel"):
        return False
    request_id = path[len(_INFERENCE_PREFIX) : -len("/cancel")]
    return bool(request_id) and "/" not in request_id


if __name__ == "__main__":  # pragma: no cover - exercised through CLI entrypoint
    raise SystemExit(main())
