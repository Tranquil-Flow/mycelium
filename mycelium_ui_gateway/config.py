"""Fail-closed deployment configuration for the browser-facing gateway."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from urllib.parse import urlsplit


def _positive_integer(name: str, value: object, *, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"invalid_{name}")
    return value


def _loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _validated_origin(origin: object) -> str:
    if not isinstance(origin, str) or not origin or len(origin) > 2_048:
        raise ValueError("invalid_public_origin")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        raise ValueError("invalid_public_origin")
    return origin.rstrip("/")


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Configuration whose default can only be served on loopback."""

    bind_host: str = "127.0.0.1"
    tls_enabled: bool = False
    public_origin: str | None = None
    trusted_https_proxy: bool = False
    source_mode: str = "live"
    session_ttl_seconds: int = 3_600
    max_sessions: int = 32
    max_requests_per_session: int = 64
    max_request_body_bytes: int = 262_144
    max_json_response_bytes: int = 2 * 1024 * 1024
    max_sse_frame_bytes: int = 1_048_576
    stream_buffer_chunks: int = 2
    max_stream_seconds: int = 3_600
    upstream_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.bind_host, str) or not self.bind_host or len(self.bind_host) > 255:
            raise ValueError("invalid_bind_host")
        if type(self.tls_enabled) is not bool:
            raise ValueError("invalid_tls_enabled")
        if type(self.trusted_https_proxy) is not bool:
            raise ValueError("invalid_trusted_https_proxy")
        if self.source_mode not in {"fixture", "live", "replay"}:
            raise ValueError("invalid_source_mode")
        _positive_integer("session_ttl_seconds", self.session_ttl_seconds, maximum=86_400)
        _positive_integer("max_sessions", self.max_sessions, maximum=4_096)
        _positive_integer(
            "max_requests_per_session", self.max_requests_per_session, maximum=4_096
        )
        _positive_integer(
            "max_request_body_bytes", self.max_request_body_bytes, maximum=2 * 1024 * 1024
        )
        _positive_integer(
            "max_json_response_bytes", self.max_json_response_bytes, maximum=16 * 1024 * 1024
        )
        _positive_integer(
            "max_sse_frame_bytes", self.max_sse_frame_bytes, maximum=4 * 1024 * 1024
        )
        _positive_integer("stream_buffer_chunks", self.stream_buffer_chunks, maximum=32)
        _positive_integer("max_stream_seconds", self.max_stream_seconds, maximum=86_400)
        _positive_integer("upstream_timeout_seconds", self.upstream_timeout_seconds, maximum=300)

        origin = None if self.public_origin is None else _validated_origin(self.public_origin)
        if origin is not None:
            object.__setattr__(self, "public_origin", origin)
        if self.trusted_https_proxy:
            if not self.is_loopback:
                raise ValueError("trusted_https_proxy_requires_loopback")
            if not self.tls_enabled:
                raise ValueError("trusted_https_proxy_requires_tls")
            if origin is None or urlsplit(origin).scheme != "https":
                raise ValueError("trusted_https_proxy_requires_https_origin")
        elif not self.is_loopback:
            if not self.tls_enabled:
                raise ValueError("non_loopback_requires_tls")
            if origin is None or urlsplit(origin).scheme != "https":
                raise ValueError("non_loopback_requires_https_origin")
        elif origin is not None:
            parsed = urlsplit(origin)
            if parsed.hostname is None or not _loopback_host(parsed.hostname):
                raise ValueError("loopback_origin_required")
            if parsed.scheme == "https" and not self.tls_enabled:
                raise ValueError("https_origin_requires_tls")

    @property
    def is_loopback(self) -> bool:
        return _loopback_host(self.bind_host)

    @property
    def secure_cookie(self) -> bool:
        return self.tls_enabled


__all__ = ["GatewayConfig"]
