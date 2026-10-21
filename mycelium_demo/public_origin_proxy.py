# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback public-origin proxy for Cloudflared-backed demo recordings.

The proxy is intentionally stdlib-only and small: it authenticates the local
Cloudflared-to-product hop with an owner-private capability file, overwrites the
trusted headers consumed by the product gateway, and exposes only the public
consumer-demo routes.
"""
from __future__ import annotations

import argparse
import http.client
import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import socket
import stat
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit

from mycelium_swarm_directory import hosted_defaults as _hosted


# Lingering close after an early refusal: bounded time and bytes of discarded upload (slowloris-safe).
_LINGER_SECONDS = 2.0
_LINGER_MAX_BYTES = 1024 * 1024
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
_DIRECTORY_ASSET_PATHS = frozenset(
    {
        _hosted.DIRECTORY_MOUNT_PREFIX,
        _hosted.DIRECTORY_MOUNT_PREFIX + "/",
        _hosted.DIRECTORY_MOUNT_PREFIX + "/swarms.js",
        _hosted.DIRECTORY_MOUNT_PREFIX + "/swarms.css",
    }
)
_DIRECTORY_API_PREFIX = _hosted.DIRECTORY_MOUNT_PREFIX + "/api/v1/"
_DIRECTORY_CONFIG_PATH = _DIRECTORY_API_PREFIX + "config"
_DIRECTORY_DOWNLOAD_PREFIX = _hosted.DIRECTORY_MOUNT_PREFIX + "/download/"
# The helper package: a regular, non-empty, non-symlink file no larger than this is hashed at startup.
_HELPER_ZIP_MAX_BYTES = 1024 * 1024 * 1024
_HELPER_ZIP_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}\.zip")
_HELPER_VERSION = re.compile(r"[A-Za-z0-9._+-]{1,40}")
_CONFIG_REWRITE_MAX_BYTES = 1024 * 1024
_DIRECTORY_API_PATH = re.compile(re.escape(_DIRECTORY_API_PREFIX) + r"[A-Za-z0-9._/-]{1,200}")
_DIRECTORY_FORBIDDEN_PATH_PARTS = ("..", "%", "\\", "?", "#", "//")
# Client-supplied identity and credentials never reach the directory; the one
# client address it sees is the validated Cf-Connecting-Ip set below.
_DIRECTORY_STRIPPED_REQUEST_HEADERS = frozenset(
    {
        "cookie",
        "cf-connecting-ip",
        "content-length",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-real-ip",
        "true-client-ip",
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
    # Optional path mount of the swarm directory at /swarms (off unless set).
    directory_host: str | None = None
    directory_port: int | None = None
    directory_require_client_ip: bool = True
    directory_max_body_bytes: int = _hosted.DIRECTORY_MAX_BODY_BYTES
    # Helper app zip served at /swarms/download/<name> (needs the mount); None = nothing to serve.
    helper_zip_path: str | None = None
    helper_version: str = _hosted.HELPER_DOWNLOAD["version"]
    # '/' redirects to the swarms page while the mount is on.  Owner decision 5 is still open: default ON.
    redirect_root_to_swarms: bool = True
    # With the directory mounted the legacy product-gateway surface is closed (404, no upstream connection): it
    # has no per-IP limit, no daily cap and no kill switch, and the proxy stamps its callers as the owner.
    # Only an explicit opt-in serves it next to the directory.
    enable_product_routes: bool = False
    # Bounds on the proxy itself (slowloris): live connections, seconds per socket operation (header, body and
    # response writes alike), and helper-zip downloads streaming at once.  Defaults match the directory behind it.
    connection_slots: int = _hosted.PROXY_CONNECTION_SLOTS
    socket_timeout_seconds: float = _hosted.PROXY_SOCKET_TIMEOUT_SECONDS
    max_concurrent_downloads: int = _hosted.PROXY_MAX_CONCURRENT_DOWNLOADS

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
        if (self.directory_host is None) != (self.directory_port is None):
            raise PublicOriginProxyError("directory_incomplete")
        if self.directory_host is not None:
            if not _is_loopback_host(self.directory_host):
                raise PublicOriginProxyError("non_loopback_directory_host")
            _validate_port("directory_port", self.directory_port)
        if not isinstance(self.directory_require_client_ip, bool):
            raise PublicOriginProxyError("invalid_directory_require_client_ip")
        if not isinstance(self.directory_max_body_bytes, int) or not (
            1 <= self.directory_max_body_bytes <= 1024 * 1024
        ):
            raise PublicOriginProxyError("invalid_directory_max_body_bytes")
        if not isinstance(self.redirect_root_to_swarms, bool):
            raise PublicOriginProxyError("invalid_redirect_root_to_swarms")
        if not isinstance(self.enable_product_routes, bool):
            raise PublicOriginProxyError("invalid_enable_product_routes")
        for name, value, high in (
            ("connection_slots", self.connection_slots, 4096),
            ("max_concurrent_downloads", self.max_concurrent_downloads, 64),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= high):
                raise PublicOriginProxyError(f"invalid_{name}")
        if (
            isinstance(self.socket_timeout_seconds, bool)
            or not isinstance(self.socket_timeout_seconds, (int, float))
            or not (0 < float(self.socket_timeout_seconds) <= 120)
        ):
            raise PublicOriginProxyError("invalid_socket_timeout_seconds")
        if not isinstance(self.helper_version, str) or _HELPER_VERSION.fullmatch(self.helper_version) is None:
            raise PublicOriginProxyError("invalid_helper_version")
        if self.helper_zip_path is not None:
            candidate = self.helper_zip_path
            if (
                not isinstance(candidate, str)
                or not candidate.startswith("/")
                or _HELPER_ZIP_NAME.fullmatch(os.path.basename(candidate)) is None
            ):
                raise PublicOriginProxyError("invalid_helper_zip_path")
            if self.directory_host is None:
                raise PublicOriginProxyError("helper_zip_requires_directory")


@dataclass(frozen=True, slots=True)
class HelperDownload:
    """The helper zip as it was when it was hashed (identity is re-checked on every use)."""

    path: str
    name: str
    sha256: str
    size: int
    identity: tuple[int, int, int, int]

    @property
    def url(self) -> str:
        return _DIRECTORY_DOWNLOAD_PREFIX + self.name


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def load_helper_download(path: str) -> HelperDownload | None:
    """Hash the helper zip, or None when it is missing, unreadable, empty, too large or not a plain file."""

    try:
        named = os.lstat(path)
        if not stat.S_ISREG(named.st_mode) or not (0 < named.st_size <= _HELPER_ZIP_MAX_BYTES):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(named):
                return None
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                while chunk := handle.read(1024 * 1024):
                    total += len(chunk)
                    if total > _HELPER_ZIP_MAX_BYTES:
                        return None
                    digest.update(chunk)
            if total != opened.st_size or _file_identity(os.fstat(descriptor)) != _file_identity(opened):
                return None
        finally:
            os.close(descriptor)
    except OSError:
        return None
    return HelperDownload(path, os.path.basename(path), digest.hexdigest(), total, _file_identity(named))


class PublicOriginProxyServer(ThreadingHTTPServer):
    """Threaded loopback proxy server with immutable validated configuration."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], config: ProxyConfig) -> None:
        self.proxy_config = config
        # Hashed once, here; None = configured but not publishable (the page then says "not published yet").
        self.helper_download = (
            load_helper_download(config.helper_zip_path) if config.helper_zip_path is not None else None
        )
        self.slots = threading.BoundedSemaphore(config.connection_slots)
        self.download_slots = threading.BoundedSemaphore(config.max_concurrent_downloads)
        super().__init__(server_address, PublicOriginProxyHandler)

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]
        # A bounded number of live connections, each on a socket timeout: a stalled client (no head, a half body,
        # a reader that never drains) holds one slot for at most the timeout, and the slot after the cap is refused.
        request.settimeout(float(self.proxy_config.socket_timeout_seconds))
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):  # type: ignore[no-untyped-def]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):  # type: ignore[no-untyped-def]
        return  # no tracebacks (they could carry request data); the connection just closes


class PublicOriginProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MyceliumPublicOriginProxy/1"
    sys_version = ""

    def parse_request(self) -> bool:
        self._body_consumed = False
        return super().parse_request()

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
        if (
            self._config.directory_host is not None
            and self._config.redirect_root_to_swarms
            and path == "/"
            and self.command in {"GET", "HEAD"}
        ):
            self._send_redirect(_hosted.DIRECTORY_MOUNT_PREFIX + "/")
            return
        if self._config.directory_host is not None and _is_directory_mount(path):
            self._handle_directory_request()
            return
        if self._config.directory_host is not None and not self._config.enable_product_routes:
            self._send_json_error(404, "public_route_not_found")
            return
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

    def _handle_directory_request(self) -> None:
        config = self._config
        if self._config.helper_zip_path is not None and self.command in {"GET", "HEAD"}:
            if self.path.startswith(_DIRECTORY_DOWNLOAD_PREFIX) and self._serve_helper_download():
                return
        allowed, status, code = _directory_route_decision(self.command, self.path)
        if not allowed:
            self._send_json_error(status, code)
            return
        try:
            self._reject_oversized_headers()
            client_ip = self._directory_client_ip()
            body = self._read_bounded_body(
                max_bytes=config.directory_max_body_bytes, allow_chunked=True
            )
            headers = self._directory_headers(len(body), client_ip)
            rewrite = (
                self._rewrite_config_download
                if config.helper_zip_path is not None
                and self.command == "GET"
                and self.path == _DIRECTORY_CONFIG_PATH
                else None
            )
            self._proxy_to_upstream(
                self.path,
                headers,
                body,
                target=(config.directory_host, config.directory_port),
                transform=rewrite,
            )
        except PublicOriginProxyError as exc:
            status = 413 if exc.code in {"request_headers_too_large", "request_body_too_large"} else 400
            self._send_json_error(status, exc.code)
        except (OSError, http.client.HTTPException, TimeoutError):
            self._send_json_error(502, "upstream_unavailable")

    def _current_helper(self) -> HelperDownload | None:
        """The hashed helper zip if the file on disk is still the one that was hashed."""

        helper = self.server.helper_download  # type: ignore[attr-defined]
        if helper is None:
            return None
        try:
            if _file_identity(os.lstat(helper.path)) != helper.identity:
                return None
        except OSError:
            return None
        return helper

    def _rewrite_config_download(self, payload: bytes) -> bytes:
        """Replace the directory's ``download`` claim with what this proxy actually holds."""

        try:
            document = json.loads(payload)
        except ValueError:
            return payload
        if not isinstance(document, dict):
            return payload
        helper = self._current_helper()
        document["download"] = (
            {
                "available": True,
                "url": helper.url,
                "sha256": helper.sha256,
                "version": self._config.helper_version,
                "notarized": False,
                "size": helper.size,
            }
            if helper is not None
            else {
                "available": False,
                "url": None,
                "sha256": None,
                "version": self._config.helper_version,
                "notarized": False,
            }
        )
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _serve_helper_download(self) -> bool:
        """Serve the hashed helper zip; True when this request was answered here."""

        declared = self.server.helper_download  # type: ignore[attr-defined]
        if declared is None or self.path != declared.url:
            return False
        if self._current_helper() is None:
            self._send_json_error(503, "download_changed")
            return True
        capped = self.command != "HEAD"
        download_slots = self.server.download_slots  # type: ignore[attr-defined]
        if capped and not download_slots.acquire(blocking=False):
            self._send_json_error(503, "download_busy")
            return True
        try:
            return self._stream_helper(declared)
        finally:
            if capped:
                download_slots.release()

    def _stream_helper(self, declared: HelperDownload) -> bool:
        try:
            descriptor = os.open(declared.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            self._send_json_error(503, "download_changed")
            return True
        try:
            if _file_identity(os.fstat(descriptor)) != declared.identity:
                self._send_json_error(503, "download_changed")
                return True
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(declared.size))
            self.send_header("Content-Disposition", f'attachment; filename="{declared.name}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    while chunk := handle.read(256 * 1024):
                        self.wfile.write(chunk)
        except OSError:
            pass  # the client went away or the disk failed mid-stream: the connection just closes
        finally:
            os.close(descriptor)
            self.close_connection = True
        return True

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _directory_client_ip(self) -> str | None:
        """The one validated client address, or None when allowed to be absent."""

        values = self.headers.get_all("Cf-Connecting-Ip") or []
        if len(values) > 1:
            raise PublicOriginProxyError("client_ip_invalid")
        if not values:
            if self._config.directory_require_client_ip:
                raise PublicOriginProxyError("client_ip_missing")
            return None
        value = values[0]
        if not value or "%" in value or value != value.strip():
            raise PublicOriginProxyError("client_ip_invalid")
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise PublicOriginProxyError("client_ip_invalid") from None

    def _directory_headers(self, body_length: int, client_ip: str | None) -> dict[str, str]:
        outbound: dict[str, str] = {}
        for name, value in self.headers.items():
            lower = name.lower()
            if (
                lower in _HOP_BY_HOP_HEADERS
                or lower in _STRIPPED_REQUEST_HEADERS
                or lower in _DIRECTORY_STRIPPED_REQUEST_HEADERS
            ):
                continue
            outbound[name] = value
        outbound["Host"] = self._config.public_host
        outbound["X-Forwarded-Proto"] = "https"
        outbound["Content-Length"] = str(body_length)
        if client_ip is not None:
            outbound["Cf-Connecting-Ip"] = client_ip
        return outbound

    def _reject_oversized_headers(self) -> None:
        total = 0
        for name, value in self.headers.items():
            total += len(name.encode("latin-1", "ignore")) + len(
                value.encode("latin-1", "ignore")
            ) + 4
        if total > self._config.max_header_bytes:
            raise PublicOriginProxyError("request_headers_too_large")

    def _read_bounded_chunked_body(self, max_bytes: int) -> bytes:
        codings = self.headers.get_all("Transfer-Encoding") or []
        if (
            len(codings) != 1
            or codings[0].strip().lower() != "chunked"
            or self.headers.get("Content-Length") is not None
        ):
            raise PublicOriginProxyError("unsupported_transfer_encoding")
        body = bytearray()
        while True:
            line = self.rfile.readline(12)
            if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}\r\n", line):
                raise PublicOriginProxyError("invalid_chunked_body")
            size = int(line[:-2], 16)
            if size == 0:
                break
            if len(body) + size > max_bytes:
                raise PublicOriginProxyError("request_body_too_large")
            data = self.rfile.read(size)
            if len(data) != size or self.rfile.read(2) != b"\r\n":
                raise PublicOriginProxyError("invalid_chunked_body")
            body += data
        if self.rfile.read(2) != b"\r\n":
            raise PublicOriginProxyError("invalid_chunked_body")
        self._body_consumed = True
        return bytes(body)

    def _read_bounded_body(
        self, *, max_bytes: int | None = None, allow_chunked: bool = False
    ) -> bytes:
        limit = self._config.max_body_bytes if max_bytes is None else max_bytes
        if self.headers.get("Transfer-Encoding") is not None:
            if not allow_chunked:
                raise PublicOriginProxyError("unsupported_transfer_encoding")
            return self._read_bounded_chunked_body(limit)
        values = self.headers.get_all("Content-Length") or []
        if len(values) > 1:
            raise PublicOriginProxyError("invalid_content_length")
        if not values:
            self._body_consumed = True
            return b""
        try:
            length = int(values[0], 10)
        except ValueError:
            raise PublicOriginProxyError("invalid_content_length") from None
        if length < 0:
            raise PublicOriginProxyError("invalid_content_length")
        if length > limit:
            raise PublicOriginProxyError("request_body_too_large")
        data = self.rfile.read(length)
        self._body_consumed = True
        return data

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
        self,
        path: str,
        headers: dict[str, str],
        body: bytes,
        *,
        target: tuple[str, int] | None = None,
        transform: Callable[[bytes], bytes] | None = None,
    ) -> None:
        host, port = target or (self._config.upstream_host, self._config.upstream_port)
        connection = http.client.HTTPConnection(
            host,
            port,
            timeout=float(self._config.upstream_timeout_seconds),
        )
        try:
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            if transform is not None and response.status == 200:
                payload = transform(response.read(_CONFIG_REWRITE_MAX_BYTES + 1)[:_CONFIG_REWRITE_MAX_BYTES])
                self.send_response(response.status, response.reason)
                for name, value in response.getheaders():
                    if name.lower() not in _HOP_BY_HOP_HEADERS | {"content-length"}:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                return
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
        self._linger_if_body_unread()

    def _linger_if_body_unread(self) -> None:
        """Lingering close: after an early refusal, read and discard what the client is still sending.

        Closing a socket with unread inbound bytes makes the kernel answer with a reset, which can destroy the
        refusal before the client reads it (and breaks the client's next send).  So: send FIN after the response,
        discard the peer's remaining upload for a bounded time and number of bytes, and let the normal close follow.
        """
        if getattr(self, "_body_consumed", True):
            return
        if self.headers.get("Content-Length") in (None, "0") and self.headers.get("Transfer-Encoding") is None:
            return
        connection = self.connection
        try:
            connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + _LINGER_SECONDS
            remaining = _LINGER_MAX_BYTES
            while remaining > 0 and (left := deadline - time.monotonic()) > 0:
                connection.settimeout(left)
                chunk = connection.recv(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass  # peer gone or timed out: the connection closes next


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
            helper_zip_path=args.helper_zip,
            helper_version=args.helper_version,
            connection_slots=args.connection_slots,
            socket_timeout_seconds=args.socket_timeout_seconds,
            max_concurrent_downloads=args.max_concurrent_downloads,
            redirect_root_to_swarms=args.redirect_root_to_swarms,
            enable_product_routes=args.enable_product_routes,
            **_directory_kwargs(args),
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
    parser.add_argument(
        "--directory-upstream",
        default=None,
        help=(
            "loopback swarm directory to mount at /swarms (off when omitted), "
            f"e.g. http://127.0.0.1:{_hosted.DIRECTORY_PORT}"
        ),
    )
    parser.add_argument(
        "--directory-allow-missing-client-ip",
        action="store_true",
        help="forward /swarms requests that carry no Cf-Connecting-Ip (local testing only)",
    )
    parser.add_argument(
        "--helper-zip",
        default=None,
        help=(
            "absolute path of the helper app zip to serve at /swarms/download/<name> "
            "(hashed at startup; needs --directory-upstream; missing file = 'not published yet')"
        ),
    )
    parser.add_argument("--helper-version", default=_hosted.HELPER_DOWNLOAD["version"])
    parser.add_argument(
        "--redirect-root-to-swarms",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="redirect GET / to /swarms/ while the directory is mounted (default on; owner decision 5)",
    )
    parser.add_argument("--connection-slots", type=int, default=_hosted.PROXY_CONNECTION_SLOTS)
    parser.add_argument("--socket-timeout-seconds", type=float, default=_hosted.PROXY_SOCKET_TIMEOUT_SECONDS)
    parser.add_argument("--max-concurrent-downloads", type=int, default=_hosted.PROXY_MAX_CONCURRENT_DOWNLOADS)
    parser.add_argument(
        "--enable-product-routes",
        action="store_true",
        help=(
            "serve the product-gateway routes (/api/v1/*, the app bundle) next to the mounted directory; "
            "off by default: with --directory-upstream they answer 404 and nothing reaches the gateway"
        ),
    )
    parser.add_argument(
        "--directory-max-body-bytes", type=int, default=_hosted.DIRECTORY_MAX_BODY_BYTES
    )
    return parser


def _directory_kwargs(args: argparse.Namespace) -> dict[str, object]:
    if args.directory_upstream is None:
        return {"directory_max_body_bytes": args.directory_max_body_bytes}
    try:
        host, port = _parse_upstream(args.directory_upstream)
    except PublicOriginProxyError as exc:
        raise PublicOriginProxyError(
            "non_loopback_directory_host"
            if exc.code == "non_loopback_upstream_host"
            else "invalid_directory_upstream"
        ) from None
    return {
        "directory_host": host,
        "directory_port": port,
        "directory_require_client_ip": not args.directory_allow_missing_client_ip,
        "directory_max_body_bytes": args.directory_max_body_bytes,
    }


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


def _is_directory_mount(path: str) -> bool:
    prefix = _hosted.DIRECTORY_MOUNT_PREFIX
    return path == prefix or path.startswith(prefix + "/")


def _directory_route_decision(method: str, raw_path: str) -> tuple[bool, int, str]:
    """Allow only the directory's own page assets and ``/swarms/api/v1/*``.

    Judged on the raw request target: no dot segments, escapes, queries,
    fragments, backslashes or empty segments ever reach the directory.
    """

    if not raw_path.startswith("/") or any(
        part in raw_path for part in _DIRECTORY_FORBIDDEN_PATH_PARTS
    ):
        return False, 404, "public_route_not_found"
    if method in {"GET", "HEAD"}:
        if raw_path in _DIRECTORY_ASSET_PATHS or _DIRECTORY_API_PATH.fullmatch(raw_path):
            return True, 200, "ok"
    elif method == "POST":
        if _DIRECTORY_API_PATH.fullmatch(raw_path):
            return True, 200, "ok"
    elif method not in {"DELETE"}:
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
