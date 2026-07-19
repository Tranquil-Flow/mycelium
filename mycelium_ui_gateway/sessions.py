"""Per-launch bounded browser sessions; no durable browser credential store."""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import secrets
import threading
from typing import Callable


SESSION_COOKIE_NAME = "mycelium_product_session"
CSRF_HEADER_NAME = "X-Mycelium-CSRF"


@dataclass(slots=True)
class ProductSession:
    session_id: str
    csrf_token: str
    expires_at_unix_ms: int
    request_ids: set[str] = field(default_factory=set)
    pending_requests: int = 0


class SessionCapacityError(RuntimeError):
    pass


class RequestCapacityError(RuntimeError):
    pass


class ProductSessionStore:
    """A fixed-size in-memory table that disappears with the gateway launch."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        ttl_seconds: int,
        max_sessions: int,
        max_requests_per_session: int,
        token_source: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._max_requests_per_session = max_requests_per_session
        self._token_source = token_source
        self._sessions: dict[str, ProductSession] = {}
        self._lock = threading.RLock()

    def _now_ms(self) -> int:
        return int(self._clock() * 1_000)

    def _prune(self, now_ms: int) -> None:
        for identifier, session in tuple(self._sessions.items()):
            if session.expires_at_unix_ms <= now_ms:
                self._sessions.pop(identifier, None)

    def get(self, session_id: str | None) -> ProductSession | None:
        if session_id is None:
            return None
        now_ms = self._now_ms()
        with self._lock:
            self._prune(now_ms)
            return self._sessions.get(session_id)

    def open(self, existing_id: str | None = None) -> ProductSession:
        now_ms = self._now_ms()
        with self._lock:
            self._prune(now_ms)
            existing = self._sessions.get(existing_id or "")
            if existing is not None:
                return existing
            if len(self._sessions) >= self._max_sessions:
                raise SessionCapacityError
            session_id = self._new_unique_token()
            csrf_token = self._token_source(32)
            session = ProductSession(
                session_id=session_id,
                csrf_token=csrf_token,
                expires_at_unix_ms=now_ms + self._ttl_seconds * 1_000,
            )
            self._sessions[session_id] = session
            return session

    def _new_unique_token(self) -> str:
        for _attempt in range(8):
            candidate = self._token_source(32)
            if candidate and candidate not in self._sessions:
                return candidate
        raise SessionCapacityError

    @staticmethod
    def csrf_matches(session: ProductSession, candidate: str) -> bool:
        return hmac.compare_digest(session.csrf_token, candidate)

    def reserve_request(self, session: ProductSession) -> None:
        with self._lock:
            current = self._sessions.get(session.session_id)
            if current is not session or (
                len(session.request_ids) + session.pending_requests
                >= self._max_requests_per_session
            ):
                raise RequestCapacityError
            session.pending_requests += 1

    def commit_request(self, session: ProductSession, request_id: str) -> None:
        with self._lock:
            if session.pending_requests < 1:
                raise RequestCapacityError
            session.pending_requests -= 1
            if request_id in session.request_ids:
                raise RequestCapacityError
            session.request_ids.add(request_id)

    def release_request_reservation(self, session: ProductSession) -> None:
        with self._lock:
            if session.pending_requests > 0:
                session.pending_requests -= 1

    @staticmethod
    def owns_request(session: ProductSession, request_id: str) -> bool:
        return request_id in session.request_ids


def session_cookie(session: ProductSession, *, secure: bool, ttl_seconds: int) -> str:
    attributes = [
        f"{SESSION_COOKIE_NAME}={session.session_id}",
        "Path=/",
        f"Max-Age={ttl_seconds}",
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


__all__ = [
    "CSRF_HEADER_NAME",
    "ProductSession",
    "ProductSessionStore",
    "RequestCapacityError",
    "SESSION_COOKIE_NAME",
    "SessionCapacityError",
    "session_cookie",
]
