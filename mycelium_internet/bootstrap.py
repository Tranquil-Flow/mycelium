# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public HTTPS bootstrap boundary (spec §3, §10.1).

The boundary enforces the canonical HTTPS origin, the closed five-route
allowlist, downgrade/redirect refusal, exact JSON content typing, frame,
concurrency, rate, and per-invite attempt bounds, the read timeout, and
``Cache-Control: no-store`` on every response. It carries no secrets: invite
attempts are tracked by token digest, never by the token itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import re
import threading
from types import MappingProxyType
from typing import Callable, Mapping

PUBLIC_ROUTE_ALLOWLIST: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "GET": frozenset({"/seed/identity", "/seed/rotation"}),
        "POST": frozenset({"/seed/join", "/seed/resume", "/seed/message"}),
    }
)

DEFAULT_MAX_FRAME_BYTES = 1024 * 1024
DEFAULT_MAX_CONCURRENT_REQUESTS = 32
DEFAULT_MAX_REQUESTS_PER_SECOND = 64
DEFAULT_MAX_JOIN_ATTEMPTS_PER_INVITE = 8
DEFAULT_READ_TIMEOUT_SECONDS = 2.0

_BOUNDARY_CODES = frozenset(
    {
        "route_not_allowed",
        "method_not_allowed",
        "target_invalid",
        "downgrade_refused",
        "redirect_refused",
        "content_type_invalid",
        "transfer_encoding_unsupported",
        "upgrade_rejected",
        "cookie_rejected",
        "authorization_rejected",
        "frame_too_large",
        "concurrency_exhausted",
        "rate_exhausted",
        "invite_attempts_exhausted",
        "body_required",
        "body_forbidden",
    }
)
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_MAX_TRACKED_INVITES = 1024


class BoundaryError(RuntimeError):
    """A bounded public bootstrap-boundary rejection."""

    def __init__(self, code: str) -> None:
        if code not in _BOUNDARY_CODES:
            raise ValueError("boundary error code is invalid")
        self.code = code
        super().__init__(code)


def canonical_https_origin(value: str) -> str:
    """Return the one canonical ``https://host[:port]`` form or raise."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
        or not value.startswith("https://")
    ):
        raise ValueError("origin is invalid")
    netloc = value[len("https://") :]
    if (
        not netloc
        or netloc != netloc.strip()
        or any(character in netloc for character in "/?#@")
    ):
        raise ValueError("origin is invalid")
    host, separator, port_text = netloc.partition(":")
    if not host:
        raise ValueError("origin is invalid")
    port: int | None = None
    if separator:
        if not port_text.isdigit():
            raise ValueError("origin is invalid")
        port = int(port_text)
        if port == 0 or port > 65535:
            raise ValueError("origin is invalid")
    if ":" in host:
        raise ValueError("origin is invalid")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("origin is invalid") from exc
    try:
        ip_value = ipaddress.ip_address(host)
    except ValueError:
        labels = host.rstrip(".").split(".")
        if (
            host.endswith(".")
            or not labels
            or any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels)
        ):
            raise ValueError("origin is invalid")
        canonical_host = host.lower()
    else:
        canonical_host = ip_value.compressed
    canonical = f"https://{canonical_host}"
    if port is not None:
        canonical = f"{canonical}:{port}"
    if value != canonical:
        raise ValueError("origin is invalid")
    return canonical


def downgrade_verdict(value: str) -> str | None:
    """Return ``downgrade_refused`` for any non-HTTPS origin, else None."""

    if not isinstance(value, str) or not value.startswith("https://"):
        return "downgrade_refused"
    try:
        canonical_https_origin(value)
    except ValueError:
        return "downgrade_refused"
    return None


def redirect_verdict(source: str, target: str) -> str:
    """Every redirect is refused; downgrade-shaped ones get the sharper code."""

    if downgrade_verdict(target) is not None:
        return "downgrade_refused"
    return "redirect_refused"


class RateLimiter:
    """Deterministic token bucket over an injected clock."""

    def __init__(
        self,
        *,
        rate_per_second: float,
        capacity: int,
        clock: Callable[[], float],
    ) -> None:
        if (
            isinstance(rate_per_second, bool)
            or not isinstance(rate_per_second, (int, float))
            or rate_per_second <= 0
        ):
            raise ValueError("rate_per_second is invalid")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity is invalid")
        if not callable(clock):
            raise ValueError("clock is invalid")
        self._rate = float(rate_per_second)
        self._capacity = capacity
        self._clock = clock
        self._tokens = float(capacity)
        self._last = float(clock())
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = float(self._clock())
            elapsed = max(0.0, now - self._last)
            self._tokens = min(
                float(self._capacity),
                self._tokens + elapsed * self._rate,
            )
            self._last = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


class InviteAttemptTracker:
    """Per-invite attempt bound keyed by token digest (never the token)."""

    def __init__(
        self,
        *,
        max_attempts: int,
        clock: Callable[[], float],
    ) -> None:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts is invalid")
        if not callable(clock):
            raise ValueError("clock is invalid")
        self._max_attempts = max_attempts
        self._clock = clock
        self._attempts: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def allow(self, invite_token: str) -> bool:
        if not isinstance(invite_token, str) or not invite_token:
            raise ValueError("invite_token is invalid")
        digest = hashlib.sha256(invite_token.encode("utf-8")).hexdigest()
        with self._lock:
            count, _ = self._attempts.get(digest, (0, 0.0))
            if count >= self._max_attempts:
                return False
            self._attempts[digest] = (count + 1, float(self._clock()))
            if len(self._attempts) > _MAX_TRACKED_INVITES:
                self._prune_locked()
            return True

    def _prune_locked(self) -> None:
        for digest in sorted(
            self._attempts,
            key=lambda item: self._attempts[item][1],
        )[: len(self._attempts) - _MAX_TRACKED_INVITES]:
            del self._attempts[digest]

    def _snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                digest: count for digest, (count, _) in self._attempts.items()
            }


@dataclass(frozen=True)
class PublicBootstrapPolicy:
    """Immutable boundary configuration plus thread-safe bounded gauges."""

    canonical_origin: str
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    max_requests_per_second: int = DEFAULT_MAX_REQUESTS_PER_SECOND
    rate_bucket_capacity: int = DEFAULT_MAX_REQUESTS_PER_SECOND
    max_join_attempts_per_invite: int = DEFAULT_MAX_JOIN_ATTEMPTS_PER_INVITE
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    clock: Callable[[], float] = field(
        default_factory=lambda: __import__("time").time
    )
    _active: int = field(default=0, init=False)
    _gauge_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _rate_limiter: RateLimiter = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    _invite_attempts: InviteAttemptTracker = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_origin",
            canonical_https_origin(self.canonical_origin),
        )
        for name, minimum in (
            ("max_frame_bytes", 1),
            ("max_concurrent_requests", 1),
            ("max_requests_per_second", 1),
            ("rate_bucket_capacity", 1),
            ("max_join_attempts_per_invite", 1),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
                or value > (1 << 20)
            ):
                raise ValueError(f"{name} is invalid")
        if (
            isinstance(self.read_timeout_seconds, bool)
            or not isinstance(self.read_timeout_seconds, (int, float))
            or not 0.0 < float(self.read_timeout_seconds) <= 30.0
        ):
            raise ValueError("read_timeout_seconds is invalid")
        if not callable(self.clock):
            raise ValueError("clock is invalid")
        object.__setattr__(self, "_active", 0)
        object.__setattr__(self, "_gauge_lock", threading.Lock())
        object.__setattr__(
            self,
            "_rate_limiter",
            RateLimiter(
                rate_per_second=float(self.max_requests_per_second),
                capacity=self.rate_bucket_capacity,
                clock=self.clock,
            ),
        )
        object.__setattr__(
            self,
            "_invite_attempts",
            InviteAttemptTracker(
                max_attempts=self.max_join_attempts_per_invite,
                clock=self.clock,
            ),
        )

    def validate_request(
        self,
        *,
        method: str,
        target: str,
        content_type: str | None,
        body_length: int,
        headers: Mapping[str, str] | None = None,
        invite_token: str | None = None,
    ) -> None:
        if not isinstance(method, str) or method not in PUBLIC_ROUTE_ALLOWLIST:
            raise BoundaryError("method_not_allowed")
        if not isinstance(target, str) or not target.startswith("/"):
            raise BoundaryError("target_invalid")
        if "?" in target or "#" in target:
            raise BoundaryError("target_invalid")
        if target not in PUBLIC_ROUTE_ALLOWLIST[method]:
            raise BoundaryError("route_not_allowed")
        if not isinstance(body_length, int) or isinstance(body_length, bool):
            raise ValueError("body_length is invalid")
        if method == "GET":
            if body_length != 0:
                raise BoundaryError("body_forbidden")
        else:
            if body_length > self.max_frame_bytes:
                raise BoundaryError("frame_too_large")
            if content_type != "application/json":
                raise BoundaryError("content_type_invalid")
        if headers is not None:
            lowered = {
                (str(key).lower() if isinstance(key, str) else ""): value
                for key, value in headers.items()
            }
            if "transfer-encoding" in lowered:
                raise BoundaryError("transfer_encoding_unsupported")
            if "upgrade" in lowered:
                raise BoundaryError("upgrade_rejected")
            if "cookie" in lowered:
                raise BoundaryError("cookie_rejected")
            if "authorization" in lowered:
                raise BoundaryError("authorization_rejected")
        if target == "/seed/join" and invite_token is not None:
            if not self._invite_attempts.allow(invite_token):
                raise BoundaryError("invite_attempts_exhausted")

    def acquire(self) -> None:
        with self._gauge_lock:
            if self._active >= self.max_concurrent_requests:
                raise BoundaryError("concurrency_exhausted")
            object.__setattr__(self, "_active", self._active + 1)

    def release(self) -> None:
        with self._gauge_lock:
            if self._active <= 0:
                raise ValueError("concurrency gauge underflow")
            object.__setattr__(self, "_active", self._active - 1)

    def check_rate(self) -> None:
        if not self._rate_limiter.allow():
            raise BoundaryError("rate_exhausted")

    @staticmethod
    def response_headers() -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        }


__all__ = [
    "PUBLIC_ROUTE_ALLOWLIST",
    "BoundaryError",
    "InviteAttemptTracker",
    "PublicBootstrapPolicy",
    "RateLimiter",
    "canonical_https_origin",
    "downgrade_verdict",
    "redirect_verdict",
]
