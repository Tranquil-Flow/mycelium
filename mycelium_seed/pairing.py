# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-run Android pairing channel (``mycelium.pairing_channel.v1``).

The owner opens a bounded pairing window next to the seed. The device scans
the channel QR, posts its target-signed pairing request to ``/pair`` and gets
back the encrypted invitation handoff: a seed-signed invite bound to the
request's pairing nonce plus the owner's consent quota. The window closes
after its time budget or its approval budget, whichever comes first.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from mycelium_internet.bootstrap import (
    RateLimiter,
    canonical_https_origin,
    forwarded_scheme_is_cleartext,
)
from mycelium_mobile.android_onboarding import (
    AndroidOnboardingError,
    approve_android_pairing_request,
    validate_android_quota,
    verify_android_pairing_request,
)
from mycelium_qualification.evidence import canonical_json_bytes

from .coordinator import SeedCoordinator
from .http import _IPv6ThreadingHTTPServer, _validate_bind_address

PAIRING_CHANNEL_PROTOCOL = "mycelium.pairing_channel.v1"
PAIRING_PATH = "/pair"
QR_SCHEME = "mycelium:"
MAX_WINDOW_SECONDS = 3600
MAX_APPROVALS = 16
DEFAULT_MAX_BODY_BYTES = 16 * 1024
_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _https_pairing_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("pairing_url is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.path != PAIRING_PATH
        or parsed.query
        or parsed.fragment
        or value != f"https://{parsed.netloc}{PAIRING_PATH}"
    ):
        raise ValueError("pairing_url is invalid")
    canonical_https_origin(f"https://{parsed.netloc}")
    return value


def encode_channel_qr(document: Mapping[str, Any]) -> str:
    """``mycelium:`` plus unpadded base64url of the canonical channel JSON."""

    raw = canonical_json_bytes(dict(document))
    return QR_SCHEME + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _PairingFailure(Exception):
    def __init__(self, code: str, status: HTTPStatus) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class AndroidPairingChannel:
    """One bounded, owner-opened pairing window served on a loopback port."""

    def __init__(
        self,
        coordinator: SeedCoordinator,
        *,
        quota: Mapping[str, Any],
        host: str,
        port: int,
        pairing_url: str | None = None,
        trusted_https_proxy: bool = False,
        window_seconds: int = 600,
        max_approvals: int = 1,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_requests_per_second: int = 4,
        rate_capacity: int = 8,
        clock: Callable[[], float] = time.time,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        host, port = _validate_bind_address(host, port)
        if type(trusted_https_proxy) is not bool:
            raise ValueError("trusted_https_proxy is invalid")
        if trusted_https_proxy:
            if host not in _LOOPBACK_BINDS:
                raise ValueError("trusted https proxy requires a loopback bind")
            if pairing_url is None:
                raise ValueError("pairing_url is required behind a proxy")
            _https_pairing_url(pairing_url)
        elif pairing_url is not None:
            parsed = urlsplit(pairing_url)
            if parsed.scheme != "http":
                raise ValueError("pairing_url is invalid")
        try:
            self._quota = validate_android_quota(quota)
        except AndroidOnboardingError as exc:
            raise ValueError("quota is invalid") from exc
        self._window_seconds = _bounded_int(window_seconds, "window_seconds", 1, MAX_WINDOW_SECONDS)
        self._max_approvals = _bounded_int(max_approvals, "max_approvals", 1, MAX_APPROVALS)
        self._max_body_bytes = _bounded_int(max_body_bytes, "max_body_bytes", 1, 1 << 20)
        _bounded_int(max_requests_per_second, "max_requests_per_second", 1, 1024)
        _bounded_int(rate_capacity, "rate_capacity", 1, 1024)
        if not callable(clock):
            raise ValueError("clock is invalid")
        if on_event is not None and not callable(on_event):
            raise ValueError("on_event is invalid")
        if not isinstance(coordinator, SeedCoordinator):
            raise ValueError("coordinator is invalid")

        self._coordinator = coordinator
        self._trusted_https_proxy = trusted_https_proxy
        self._clock = clock
        self._on_event = on_event
        self._rate = RateLimiter(
            rate_per_second=float(max_requests_per_second),
            capacity=rate_capacity,
            clock=time.monotonic,
        )
        self._lock = threading.Lock()
        self._used_nonces: set[str] = set()
        self._approvals = 0
        self._closed = threading.Event()
        self._close_reason: str | None = None
        self._started = False

        server_class = (
            _IPv6ThreadingHTTPServer
            if host != "localhost" and ipaddress.ip_address(host).version == 6
            else ThreadingHTTPServer
        )
        self._server = server_class((host, port), _PairingRequestHandler)
        self._server.daemon_threads = True
        self._server.channel = self  # type: ignore[attr-defined]
        self._server.handle_error = lambda *_args: None  # type: ignore[method-assign]
        bound_host, bound_port = self._server.server_address[:2]
        formatted = f"[{bound_host}]" if ":" in str(bound_host) else str(bound_host)
        self.local_url = f"http://{formatted}:{bound_port}{PAIRING_PATH}"
        if pairing_url is None:
            self.pairing_url = self.local_url
        elif not trusted_https_proxy and pairing_url != self.local_url:
            self._server.server_close()
            raise ValueError("pairing_url does not match the local listener")
        else:
            self.pairing_url = pairing_url
        self.expires_at = int(self._clock()) + self._window_seconds
        self.channel_qr = encode_channel_qr(
            {
                "protocol": PAIRING_CHANNEL_PROTOCOL,
                "pairing_url": self.pairing_url,
                "expires_at": self.expires_at,
            }
        )

    @property
    def approvals(self) -> int:
        with self._lock:
            return self._approvals

    @property
    def max_approvals(self) -> int:
        return self._max_approvals

    @property
    def close_reason(self) -> str | None:
        return self._close_reason

    def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass

    def start(self) -> None:
        if self._started:
            raise RuntimeError("pairing channel already started")
        self._started = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while not self._closed.wait(0.05):
            if self._clock() >= self.expires_at:
                self._finish("window_expired")
            elif self.approvals >= self._max_approvals:
                self._finish("approvals_exhausted")

    def _finish(self, reason: str) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._close_reason = reason
            self._closed.set()
            approvals = self._approvals
        if self._started:
            self._server.shutdown()
        self._server.server_close()
        self._emit({"event": "pairing_channel_closed", "reason": reason, "approvals": approvals})

    def close(self) -> None:
        self._finish("closed")

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._closed.wait(timeout)

    def _check_open(self) -> None:
        if self._closed.is_set() or self._clock() >= self.expires_at:
            raise _PairingFailure("pairing_channel_closed", HTTPStatus.GONE)

    def handle(self, headers: Mapping[str, str], read_body: Callable[[int], bytes]) -> dict[str, Any]:
        self._check_open()
        if not self._rate.allow():
            raise _PairingFailure("pairing_rate_exhausted", HTTPStatus.TOO_MANY_REQUESTS)
        lowered = {key.lower(): value for key, value in headers.items()}
        if "transfer-encoding" in lowered:
            raise _PairingFailure("pairing_transfer_encoding_unsupported", HTTPStatus.BAD_REQUEST)
        if self._trusted_https_proxy and forwarded_scheme_is_cleartext(lowered):
            raise _PairingFailure("pairing_downgrade_refused", HTTPStatus.BAD_REQUEST)
        content_type = lowered.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise _PairingFailure("pairing_content_type_invalid", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        length_text = lowered.get("content-length")
        if length_text is None or not length_text.isdigit():
            raise _PairingFailure("pairing_content_length_invalid", HTTPStatus.LENGTH_REQUIRED)
        length = int(length_text)
        if length > self._max_body_bytes:
            raise _PairingFailure("pairing_body_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            body = json.loads(read_body(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _PairingFailure("pairing_body_invalid", HTTPStatus.BAD_REQUEST) from exc
        if not isinstance(body, dict) or set(body) != {"request"}:
            raise _PairingFailure("pairing_body_invalid", HTTPStatus.BAD_REQUEST)
        now = time.time()
        try:
            request = verify_android_pairing_request(body["request"], now=now)
        except AndroidOnboardingError as exc:
            raise _PairingFailure(exc.code, HTTPStatus.BAD_REQUEST) from exc
        nonce = request["pairing_nonce"]
        with self._lock:
            if self._closed.is_set() or self._approvals >= self._max_approvals:
                raise _PairingFailure("pairing_channel_closed", HTTPStatus.GONE)
            if nonce in self._used_nonces:
                raise _PairingFailure("pairing_nonce_reused", HTTPStatus.CONFLICT)
            self._used_nonces.add(nonce)
        ttl = int(float(request["expires_at"]) - now) - 1
        if ttl < 1:
            raise _PairingFailure("android_onboarding_request_expired", HTTPStatus.BAD_REQUEST)
        try:
            bundle = self._coordinator.mint_invite(nonce=nonce, ttl_seconds=ttl)
            handoff = approve_android_pairing_request(
                request=request, invite_bundle=bundle, quota=self._quota, now=time.time()
            )
        except AndroidOnboardingError as exc:
            raise _PairingFailure(exc.code, HTTPStatus.BAD_REQUEST) from exc
        with self._lock:
            self._approvals += 1
        self._emit(
            {
                "event": "pairing_approved",
                "device_ref": request["device_ref"],
                "identity_key_digest": request["identity_key_digest"],
            }
        )
        return {"handoff": handoff}


class _PairingRequestHandler(BaseHTTPRequestHandler):
    server_version = "mycelium-pairing"
    sys_version = ""

    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, value: Mapping[str, Any]) -> None:
        raw = canonical_json_bytes(dict(value))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _refuse(self, code: str, status: HTTPStatus) -> None:
        self.close_connection = True
        self._send(status, {"error": {"code": code}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        channel: AndroidPairingChannel = self.server.channel  # type: ignore[attr-defined]
        if self.path != PAIRING_PATH:
            self._refuse("pairing_route_not_found", HTTPStatus.NOT_FOUND)
            return
        try:
            result = channel.handle(dict(self.headers.items()), self.rfile.read)
        except _PairingFailure as exc:
            self._refuse(exc.code, exc.status)
            return
        except Exception:
            self._refuse("pairing_internal_error", HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send(HTTPStatus.OK, result)

    def _method_not_allowed(self) -> None:
        self._refuse("pairing_method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_not_allowed  # noqa: N815


__all__ = [
    "PAIRING_CHANNEL_PROTOCOL",
    "AndroidPairingChannel",
    "encode_channel_qr",
]
