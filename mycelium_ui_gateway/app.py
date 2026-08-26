"""Framework-free same-origin browser-facing product gateway."""
from __future__ import annotations

import asyncio
import hmac
import inspect
import ipaddress
import threading
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence, Union
from urllib.parse import urlsplit

from mycelium_product_spine import validate_product_event, validate_product_snapshot

from .config import GatewayConfig
from .coordinator import CoordinatorError, SwarmCoordinator
from .sessions import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    ProductSession,
    ProductSessionStore,
    RequestCapacityError,
    SessionCapacityError,
    session_cookie,
)
from .upstream import (
    ASGIApplication,
    BoundedSSEProxy,
    BoundedUpstreamClient,
    UpstreamFailure,
    UpstreamResponse,
)
from .validation import (
    GatewayValidationError,
    canonical_json,
    safe_code,
    strict_json_document,
    swarm_action,
    validate_cancel_response,
    validate_inference_accept,
    validate_last_event_id,
    validate_request_event_id,
    validate_observatory_envelope,
    validate_qualification,
    validate_stream_event,
    validate_submission,
    validate_swarm_result,
    validate_swarm_status,
)


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
AccessPolicyResult = Union[bool, Awaitable[bool]]
AccessPolicy = Callable[[Mapping[str, Any]], AccessPolicyResult]

_BOOTSTRAP = "/api/v1/bootstrap"
_OBSERVATORY_SNAPSHOT = "/api/v1/observatory/snapshot"
_OBSERVATORY_EVENTS = "/api/v1/observatory/events"
_QUALIFICATION = "/api/v1/qualification/current"
_INFERENCE = "/api/v1/inference"
_SWARM_STATUS = "/api/v1/swarm/status"
_SWARM_INVITES = "/api/v1/swarm/invites"
_SWARM_JOIN = "/api/v1/swarm/join"
_SWARM_LEAVE = "/api/v1/swarm/leave"
_PRODUCT_SNAPSHOT = "/api/v1/product/snapshot"
_PRODUCT_EVENTS = "/api/v1/product/events"
_PRODUCT_EXPORT = "/api/v1/product/export"
_API_PATHS = {
    "observatory_snapshot": _OBSERVATORY_SNAPSHOT,
    "observatory_events": _OBSERVATORY_EVENTS,
    "qualification_current": _QUALIFICATION,
    "inference_submit": _INFERENCE,
    "swarm_status": _SWARM_STATUS,
    "swarm_invites": _SWARM_INVITES,
    "swarm_join": _SWARM_JOIN,
    "swarm_leave": _SWARM_LEAVE,
    "product_snapshot": _PRODUCT_SNAPSHOT,
    "product_events": _PRODUCT_EVENTS,
    "product_export": _PRODUCT_EXPORT,
}


class BrowserRequestError(RuntimeError):
    def __init__(self, status: int, code: str, *, retryable: bool = False) -> None:
        self.status = status
        self.code = code
        self.retryable = retryable
        super().__init__("browser_request_error")


def _header_values(scope: Mapping[str, Any], wanted: bytes) -> list[bytes]:
    raw = scope.get("headers", ())
    if not isinstance(raw, (list, tuple)):
        raise BrowserRequestError(400, "invalid_headers")
    values: list[bytes] = []
    for item in raw:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise BrowserRequestError(400, "invalid_headers")
        if item[0].lower() == wanted.lower():
            values.append(item[1])
    return values


def _is_loopback(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _origin_from_request(scope: Mapping[str, Any], config: GatewayConfig) -> str:
    scheme = scope.get("scheme")
    required_scheme = "https" if config.tls_enabled else "http"
    if scheme != required_scheme:
        raise BrowserRequestError(403, "invalid_origin")
    host_values = _header_values(scope, b"host")
    if len(host_values) != 1 or len(host_values[0]) > 512:
        raise BrowserRequestError(403, "invalid_origin")
    try:
        host_text = host_values[0].decode("ascii")
        parsed = urlsplit(f"{required_scheme}://{host_text}")
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeDecodeError, ValueError):
        raise BrowserRequestError(403, "invalid_origin") from None
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserRequestError(403, "invalid_origin")
    if config.is_loopback and not config.trusted_https_proxy:
        if not _is_loopback(hostname):
            raise BrowserRequestError(403, "invalid_origin")
        return f"{required_scheme}://{host_text}"
    assert config.public_origin is not None
    public = urlsplit(config.public_origin)
    default_port = 443 if required_scheme == "https" else 80
    if (
        hostname.lower() != (public.hostname or "").lower()
        or (port or default_port) != (public.port or default_port)
    ):
        raise BrowserRequestError(403, "invalid_origin")
    return config.public_origin


def _valid_service_token(name: str, token: str | None, *, required: bool) -> bytes | None:
    if token is None and not required:
        return None
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4_096
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        raise ValueError(f"invalid_{name}")
    return token.encode("ascii")


def _media_type(scope: Mapping[str, Any]) -> str | None:
    values = _header_values(scope, b"content-type")
    if len(values) != 1 or len(values[0]) > 128:
        return None
    try:
        text = values[0].decode("ascii").lower()
    except UnicodeDecodeError:
        return None
    media, separator, parameters = text.partition(";")
    if media.strip() != "application/json":
        return None
    if separator and parameters.strip() != "charset=utf-8":
        return None
    return "application/json"


def _cookie_session_id(scope: Mapping[str, Any]) -> str | None:
    values = _header_values(scope, b"cookie")
    if not values:
        return None
    if len(values) != 1 or len(values[0]) > 8_192:
        raise BrowserRequestError(401, "session_required")
    try:
        text = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise BrowserRequestError(401, "session_required") from None
    found: list[str] = []
    for part in text.split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name:
            raise BrowserRequestError(401, "session_required")
        if name == SESSION_COOKIE_NAME:
            found.append(value)
    if len(found) > 1 or (found and (not found[0] or len(found[0]) > 512)):
        raise BrowserRequestError(401, "session_required")
    return found[0] if found else None


def _retryable_error(code: str, status: int) -> bool:
    return status >= 500 or code in {
        "snapshot_unavailable",
        "subscriber_limit",
        "upstream_timeout",
        "upstream_unavailable",
    }


class ProductGatewayASGIApplication:
    """A bounded authority-preserving BFF over injected local ASGI services."""

    def __init__(
        self,
        *,
        config: GatewayConfig,
        observatory_app: ASGIApplication,
        request_gateway_app: ASGIApplication,
        product_app: ASGIApplication | None = None,
        swarm_coordinator: SwarmCoordinator,
        request_gateway_bearer_token: str,
        observatory_bearer_token: str | None = None,
        access_policy: AccessPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(config, GatewayConfig):
            raise ValueError("invalid_gateway_config")
        if not callable(observatory_app) or not callable(request_gateway_app):
            raise ValueError("invalid_upstream_application")
        if product_app is not None and not callable(product_app):
            raise ValueError("invalid_product_application")
        if swarm_coordinator is None:
            raise ValueError("invalid_swarm_coordinator")
        if config.trusted_https_proxy and access_policy is None:
            raise ValueError("trusted_proxy_requires_access_policy")
        if not config.is_loopback and access_policy is None:
            raise ValueError("non_loopback_requires_access_policy")
        if access_policy is not None and not callable(access_policy):
            raise ValueError("invalid_access_policy")
        request_token = _valid_service_token(
            "request_gateway_bearer_token", request_gateway_bearer_token, required=True
        )
        observatory_token = _valid_service_token(
            "observatory_bearer_token", observatory_bearer_token, required=False
        )
        assert request_token is not None
        if observatory_token is not None and hmac.compare_digest(request_token, observatory_token):
            raise ValueError("upstream_credentials_must_be_distinct")
        self._config = config
        self._observatory_app = observatory_app
        self._request_gateway_app = request_gateway_app
        self._product_app = product_app
        self._coordinator = swarm_coordinator
        self._request_token = request_token
        self._observatory_token = observatory_token
        self._credential_bytes = tuple(
            value for value in (request_token, observatory_token) if value is not None
        )
        self._request_owner_tokens: dict[str, bytes] = {}
        self._request_owner_tokens_lock = threading.RLock()
        self._access_policy = access_policy
        self._sessions = ProductSessionStore(
            clock=clock,
            ttl_seconds=config.session_ttl_seconds,
            max_sessions=config.max_sessions,
            max_requests_per_session=config.max_requests_per_session,
        )
        self._client = BoundedUpstreamClient(
            response_limit=config.max_json_response_bytes,
            timeout_seconds=config.upstream_timeout_seconds,
        )
        self._sse = BoundedSSEProxy(
            frame_limit=config.max_sse_frame_bytes,
            queue_chunks=config.stream_buffer_chunks,
            startup_timeout_seconds=config.upstream_timeout_seconds,
            stream_timeout_seconds=config.max_stream_seconds,
        )

    async def __call__(self, scope: Mapping[str, Any], receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope_type != "http":
            return
        try:
            expected_origin = await self._authorize_network_boundary(scope)
            path = scope.get("path")
            route, route_value, allowed = self._route(path)
            if route is None:
                raise BrowserRequestError(404, "not_found")
            method = scope.get("method")
            if method not in allowed:
                await self._send_error(send, 405, "method_not_allowed", allow=allowed)
                return
            query = scope.get("query_string", b"")
            if not isinstance(query, bytes) or query:
                raise BrowserRequestError(400, "query_not_allowed")
            if _header_values(scope, b"upgrade"):
                raise BrowserRequestError(400, "upgrade_not_supported")
            if route == "bootstrap":
                await self._bootstrap(scope, send)
                return
            session = self._required_session(scope)
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                self._verify_csrf(scope, session, expected_origin)
            if route == "observatory_snapshot":
                await self._observatory_snapshot(send)
            elif route == "observatory_events":
                await self._stream_observatory(scope, receive, send)
            elif route == "qualification":
                await self._qualification(send)
            elif route == "inference_submit":
                await self._inference_submit(scope, receive, send, session)
            elif route == "inference_events":
                await self._stream_inference(scope, receive, send, session, route_value or "")
            elif route == "inference_cancel":
                await self._inference_cancel(send, session, route_value or "")
            elif route == "swarm_status":
                await self._swarm_status(send)
            elif route == "product_snapshot":
                await self._product_snapshot(send)
            elif route == "product_events":
                await self._stream_product(scope, receive, send)
            elif route == "product_export":
                await self._product_export(send)
            else:
                await self._swarm_action(scope, receive, send, route)
        except BrowserRequestError as exc:
            await self._send_error(send, exc.status, exc.code, retryable=exc.retryable)
        except (GatewayValidationError, UpstreamFailure) as exc:
            code = exc.code
            await self._send_error(send, 502, code, retryable=_retryable_error(code, 502))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._send_error(send, 500, "internal_error", retryable=True)

    @staticmethod
    def _route(path: object) -> tuple[str | None, str | None, frozenset[str]]:
        fixed = {
            _BOOTSTRAP: ("bootstrap", frozenset({"GET"})),
            _OBSERVATORY_SNAPSHOT: ("observatory_snapshot", frozenset({"GET"})),
            _OBSERVATORY_EVENTS: ("observatory_events", frozenset({"GET"})),
            _QUALIFICATION: ("qualification", frozenset({"GET"})),
            _INFERENCE: ("inference_submit", frozenset({"POST"})),
            _SWARM_STATUS: ("swarm_status", frozenset({"GET"})),
            _SWARM_INVITES: ("swarm_invites", frozenset({"POST"})),
            _SWARM_JOIN: ("swarm_join", frozenset({"POST"})),
            _SWARM_LEAVE: ("swarm_leave", frozenset({"POST"})),
            _PRODUCT_SNAPSHOT: ("product_snapshot", frozenset({"GET"})),
            _PRODUCT_EVENTS: ("product_events", frozenset({"GET"})),
            _PRODUCT_EXPORT: ("product_export", frozenset({"GET"})),
        }
        if isinstance(path, str) and path in fixed:
            route, methods = fixed[path]
            return route, None, methods
        if isinstance(path, str) and path.startswith(_INFERENCE + "/"):
            tail = path[len(_INFERENCE) + 1 :]
            if tail.endswith("/events"):
                value = tail[: -len("/events")]
                if value and "/" not in value:
                    return "inference_events", value, frozenset({"GET"})
            if tail.endswith("/cancel"):
                value = tail[: -len("/cancel")]
                if value and "/" not in value:
                    return "inference_cancel", value, frozenset({"DELETE"})
        return None, None, frozenset()

    async def _authorize_network_boundary(self, scope: Mapping[str, Any]) -> str:
        if self._config.is_loopback:
            client = scope.get("client")
            if (
                not isinstance(client, (tuple, list))
                or not client
                or not _is_loopback(client[0])
            ):
                raise BrowserRequestError(403, "loopback_required")
        if not self._config.is_loopback or self._config.trusted_https_proxy:
            assert self._access_policy is not None
            try:
                decision = self._access_policy(scope)
                if inspect.isawaitable(decision):
                    decision = await asyncio.wait_for(
                        decision, timeout=self._config.upstream_timeout_seconds
                    )
            except Exception:
                decision = False
            if decision is not True:
                raise BrowserRequestError(403, "access_denied")
        return _origin_from_request(scope, self._config)

    def _required_session(self, scope: Mapping[str, Any]) -> ProductSession:
        session = self._sessions.get(_cookie_session_id(scope))
        if session is None:
            raise BrowserRequestError(401, "session_required")
        return session

    def _verify_csrf(
        self, scope: Mapping[str, Any], session: ProductSession, expected_origin: str
    ) -> None:
        csrf_values = _header_values(scope, CSRF_HEADER_NAME.encode("ascii"))
        if len(csrf_values) != 1 or len(csrf_values[0]) > 512:
            raise BrowserRequestError(403, "csrf_required")
        try:
            csrf = csrf_values[0].decode("ascii")
        except UnicodeDecodeError:
            raise BrowserRequestError(403, "csrf_required") from None
        if not self._sessions.csrf_matches(session, csrf):
            raise BrowserRequestError(403, "csrf_required")
        origin_values = _header_values(scope, b"origin")
        if len(origin_values) != 1:
            raise BrowserRequestError(403, "origin_required")
        try:
            origin = origin_values[0].decode("ascii")
        except UnicodeDecodeError:
            raise BrowserRequestError(403, "origin_mismatch") from None
        if not hmac.compare_digest(origin, expected_origin):
            raise BrowserRequestError(403, "origin_mismatch")

    async def _bootstrap(self, scope: Mapping[str, Any], send: Send) -> None:
        existing = _cookie_session_id(scope)
        try:
            session = self._sessions.open(existing)
        except SessionCapacityError:
            raise BrowserRequestError(503, "session_capacity", retryable=True) from None
        document = {
            "protocol": "mycelium.product_ui.bootstrap.v1",
            "source_mode": self._config.source_mode,
            "session": {
                "csrf_header": CSRF_HEADER_NAME,
                "csrf_token": session.csrf_token,
                "expires_at_unix_ms": session.expires_at_unix_ms,
            },
            "api": _API_PATHS,
            "limits": {"max_prompt_utf8_bytes": 131_072, "max_new_tokens": 4_096},
            "qualification_authority": (
                "mycelium_qualification" + ".qualifier:RouteQualificationV1"
            ),
        }
        await self._send_json(
            send,
            200,
            document,
            extra_headers=(
                (
                    b"set-cookie",
                    session_cookie(
                        session,
                        secure=self._config.secure_cookie,
                        ttl_seconds=self._config.session_ttl_seconds,
                    ).encode("ascii"),
                ),
            ),
        )

    def _upstream_headers(
        self,
        token: bytes | None,
        *,
        cursor: bytes | None = None,
        json_body: bool = False,
        session_token: bytes | None = None,
    ) -> tuple[tuple[bytes, bytes], ...]:
        headers: list[tuple[bytes, bytes]] = []
        if token is not None:
            headers.append((b"authorization", b"Bearer " + token))
        if cursor is not None:
            headers.append((b"last-event-id", cursor))
        if session_token is not None:
            headers.append((b"x-mycelium-session", session_token))
        if json_body:
            headers.append((b"content-type", b"application/json"))
        return tuple(headers)

    def _check_no_credentials(self, payload: bytes) -> None:
        if any(value in payload for value in self._credential_bytes):
            raise UpstreamFailure("credential_leak_blocked")

    @staticmethod
    def _require_json_content_type(response: UpstreamResponse) -> None:
        values = response.header_values(b"content-type")
        if len(values) != 1 or values[0].split(b";", 1)[0].strip().lower() != b"application/json":
            raise UpstreamFailure("invalid_upstream_response")

    async def _json_upstream(
        self,
        app: ASGIApplication,
        *,
        method: str,
        path: str,
        token: bytes | None,
        body: bytes = b"",
        session_token: bytes | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        response = await self._client.request(
            app,
            method=method,
            path=path,
            headers=self._upstream_headers(
                token,
                json_body=bool(body),
                session_token=session_token,
            ),
            body=body,
        )
        self._check_no_credentials(response.body)
        self._require_json_content_type(response)
        if not 200 <= response.status < 300:
            self._raise_upstream_error(response)
        return response.status, strict_json_document(response.body, code="invalid_upstream_response")

    @staticmethod
    def _raise_upstream_error(response: UpstreamResponse) -> None:
        try:
            document = strict_json_document(response.body, code="invalid_upstream_error")
            code = document.get("error")
        except GatewayValidationError:
            code = None
        if not safe_code(code):
            code = "upstream_rejected" if response.status < 500 else "upstream_unavailable"
        status = response.status if 400 <= response.status <= 599 else 502
        raise BrowserRequestError(
            status, str(code), retryable=_retryable_error(str(code), status)
        )

    async def _observatory_snapshot(self, send: Send) -> None:
        _status, document = await self._json_upstream(
            self._observatory_app,
            method="GET",
            path="/v1/observatory/snapshot",
            token=self._observatory_token,
        )
        validate_observatory_envelope(document)
        await self._send_json(
            send,
            200,
            document,
            extra_headers=((b"x-mycelium-source-status", b"connected"),),
        )

    async def _qualification(self, send: Send) -> None:
        _status, document = await self._json_upstream(
            self._request_gateway_app,
            method="GET",
            path="/v1/qualification/current",
            token=self._request_token,
        )
        validate_qualification(document)
        await self._send_json(send, 200, document)

    async def _product_snapshot(self, send: Send) -> None:
        if self._product_app is None:
            raise BrowserRequestError(
                503,
                "product_snapshot_unavailable",
                retryable=True,
            )
        _status, document = await self._json_upstream(
            self._product_app,
            method="GET",
            path="/v1/product/snapshot",
            token=None,
        )
        try:
            validated = validate_product_snapshot(document)
        except Exception:
            raise UpstreamFailure("invalid_upstream_response") from None
        await self._send_json(send, 200, validated)

    async def _product_export(self, send: Send) -> None:
        if self._product_app is None:
            raise BrowserRequestError(503, "product_export_unavailable", retryable=True)
        _status, document = await self._json_upstream(
            self._product_app,
            method="GET",
            path="/v1/product/export",
            token=None,
        )
        try:
            validated = validate_product_snapshot(document)
        except Exception:
            raise UpstreamFailure("invalid_upstream_response") from None
        await self._send_json(send, 200, validated)

    async def _stream_product(
        self,
        scope: Mapping[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        if self._product_app is None:
            raise BrowserRequestError(503, "product_events_unavailable", retryable=True)
        try:
            cursor = validate_last_event_id(_header_values(scope, b"last-event-id"))
        except GatewayValidationError as exc:
            raise BrowserRequestError(400, exc.code) from None

        def validate(raw: bytes) -> bytes:
            self._check_no_credentials(raw)
            lines = raw[:-2].split(b"\n")
            if (
                len(lines) != 3
                or not lines[0].startswith(b"id: ")
                or lines[1] != b"event: product_snapshot"
                or not lines[2].startswith(b"data: ")
            ):
                raise GatewayValidationError("invalid_upstream_event")
            event_id = validate_last_event_id([lines[0][4:]])
            document = strict_json_document(lines[2][6:], code="invalid_upstream_event")
            try:
                event = validate_product_event(document)
            except Exception:
                raise GatewayValidationError("invalid_upstream_event") from None
            if event_id is None or int(event_id) != event["cursor"]:
                raise GatewayValidationError("invalid_upstream_event")
            return (
                b"id: "
                + event_id
                + b"\nevent: product_snapshot\ndata: "
                + canonical_json(event)
                + b"\n\n"
            )

        await self._forward_sse(
            self._product_app,
            path="/v1/product/events",
            token=None,
            cursor=cursor,
            receive=receive,
            send=send,
            validator=validate,
        )

    async def _read_json_body(
        self, scope: Mapping[str, Any], receive: Receive
    ) -> tuple[Mapping[str, Any], bytes]:
        if _media_type(scope) is None:
            raise BrowserRequestError(415, "unsupported_media_type")
        lengths = _header_values(scope, b"content-length")
        stated_length: int | None = None
        if len(lengths) > 1:
            raise BrowserRequestError(400, "invalid_content_length")
        if lengths:
            try:
                text = lengths[0].decode("ascii")
                if not text.isdecimal():
                    raise ValueError
                stated_length = int(text)
            except (UnicodeDecodeError, ValueError):
                raise BrowserRequestError(400, "invalid_content_length") from None
            if stated_length > self._config.max_request_body_bytes:
                raise BrowserRequestError(413, "request_body_too_large")
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                raise BrowserRequestError(400, "client_disconnected")
            if message.get("type") != "http.request":
                raise BrowserRequestError(400, "invalid_request_body")
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise BrowserRequestError(400, "invalid_request_body")
            body.extend(chunk)
            if len(body) > self._config.max_request_body_bytes:
                raise BrowserRequestError(413, "request_body_too_large")
            if not message.get("more_body", False):
                break
        if stated_length is not None and stated_length != len(body):
            raise BrowserRequestError(400, "invalid_content_length")
        payload = bytes(body)
        try:
            document = strict_json_document(payload)
        except GatewayValidationError as exc:
            raise BrowserRequestError(400, exc.code) from None
        return document, payload

    async def _inference_submit(
        self,
        scope: Mapping[str, Any],
        receive: Receive,
        send: Send,
        session: ProductSession,
    ) -> None:
        document, _received = await self._read_json_body(scope, receive)
        try:
            validate_submission(document)
        except GatewayValidationError as exc:
            raise BrowserRequestError(400, exc.code) from None
        body = canonical_json(document)
        try:
            self._sessions.reserve_request(session)
        except RequestCapacityError:
            raise BrowserRequestError(429, "request_capacity") from None
        try:
            _status, upstream = await self._json_upstream(
                self._request_gateway_app,
                method="POST",
                path="/v1/inference",
                token=self._request_token,
                body=body,
            )
            identifier, owner_token = validate_inference_accept(upstream)
            self._sessions.commit_request(session, identifier)
            with self._request_owner_tokens_lock:
                self._request_owner_tokens[identifier] = owner_token.encode("ascii")
        except Exception:
            self._sessions.release_request_reservation(session)
            raise
        response = {
            "protocol": "mycelium.request_gateway.v1",
            "request_id": identifier,
            "accepted": True,
            "event_path": f"/api/v1/inference/{identifier}/events",
            "cancel_path": f"/api/v1/inference/{identifier}/cancel",
        }
        await self._send_json(send, 202, response)

    def _require_owned_request(self, session: ProductSession, identifier: str) -> None:
        if not self._sessions.owns_request(session, identifier):
            raise BrowserRequestError(404, "request_not_found")

    async def _inference_cancel(
        self, send: Send, session: ProductSession, identifier: str
    ) -> None:
        self._require_owned_request(session, identifier)
        with self._request_owner_tokens_lock:
            owner_token = self._request_owner_tokens.get(identifier)
        if owner_token is None:
            raise BrowserRequestError(404, "request_not_found")
        status, document = await self._json_upstream(
            self._request_gateway_app,
            method="DELETE",
            path=f"/v1/inference/{identifier}",
            token=self._request_token,
            session_token=owner_token,
        )
        cancelled = validate_cancel_response(document, identifier)
        if cancelled and status != 202:
            raise UpstreamFailure("invalid_upstream_response")
        if not cancelled and status != 200:
            raise UpstreamFailure("invalid_upstream_response")
        await self._send_json(
            send,
            status,
            {
                "protocol": "mycelium.request_gateway.v1",
                "request_id": identifier,
                "cancelled": cancelled,
            },
        )

    async def _stream_observatory(
        self, scope: Mapping[str, Any], receive: Receive, send: Send
    ) -> None:
        try:
            cursor = validate_last_event_id(_header_values(scope, b"last-event-id"))
        except GatewayValidationError as exc:
            raise BrowserRequestError(400, exc.code) from None

        def validate(raw: bytes) -> bytes:
            self._check_no_credentials(raw)
            if raw == b": heartbeat\n\n":
                return raw
            lines = raw[:-2].split(b"\n")
            if len(lines) != 3 or not lines[0].startswith(b"id: ") or lines[1] != b"event: snapshot" or not lines[2].startswith(b"data: "):
                raise GatewayValidationError("invalid_upstream_event")
            event_id = validate_last_event_id([lines[0][4:]])
            document = strict_json_document(
                lines[2][6:], code="invalid_observatory_response"
            )
            validate_observatory_envelope(document)
            if event_id is None or int(event_id) != document["generation"]:
                raise GatewayValidationError("invalid_upstream_event")
            return b"id: " + event_id + b"\nevent: snapshot\ndata: " + canonical_json(document) + b"\n\n"

        await self._forward_sse(
            self._observatory_app,
            path="/v1/observatory/events",
            token=self._observatory_token,
            cursor=cursor,
            receive=receive,
            send=send,
            validator=validate,
        )

    async def _stream_inference(
        self,
        scope: Mapping[str, Any],
        receive: Receive,
        send: Send,
        session: ProductSession,
        identifier: str,
    ) -> None:
        self._require_owned_request(session, identifier)
        with self._request_owner_tokens_lock:
            owner_token = self._request_owner_tokens.get(identifier)
        if owner_token is None:
            raise BrowserRequestError(404, "request_not_found")
        try:
            cursor = validate_request_event_id(
                _header_values(scope, b"last-event-id")
            )
        except GatewayValidationError as exc:
            raise BrowserRequestError(400, exc.code) from None

        def validate(raw: bytes) -> bytes:
            self._check_no_credentials(raw)
            lines = raw[:-2].split(b"\n")
            if len(lines) != 3 or not lines[0].startswith(b"id: ") or not lines[1].startswith(b"event: ") or not lines[2].startswith(b"data: "):
                raise GatewayValidationError("invalid_upstream_event")
            event_id = validate_request_event_id([lines[0][4:]])
            document = strict_json_document(lines[2][6:], code="invalid_upstream_event")
            sequence, event_type = validate_stream_event(document, identifier)
            if (
                event_id is None
                or event_id.decode("ascii")
                != f"{document['publisher_generation']}:{sequence}"
                or lines[1][7:].decode("ascii", errors="ignore") != event_type
            ):
                raise GatewayValidationError("invalid_upstream_event")
            return (
                b"id: "
                + event_id
                + b"\nevent: "
                + event_type.encode("ascii")
                + b"\ndata: "
                + canonical_json(document)
                + b"\n\n"
            )

        await self._forward_sse(
            self._request_gateway_app,
            path=f"/v1/inference/{identifier}/events",
            token=self._request_token,
            cursor=cursor,
            receive=receive,
            send=send,
            validator=validate,
            session_token=owner_token,
        )

    async def _forward_sse(
        self,
        app: ASGIApplication,
        *,
        path: str,
        token: bytes | None,
        cursor: bytes | None,
        receive: Receive,
        send: Send,
        validator: Callable[[bytes], bytes],
        session_token: bytes | None = None,
    ) -> None:
        response = await self._sse.forward(
            app,
            path=path,
            headers=self._upstream_headers(
                token,
                cursor=cursor,
                session_token=session_token,
            ),
            client_receive=receive,
            client_send=send,
            validate_frame=validator,
        )
        if response is not None:
            self._check_no_credentials(response.body)
            self._raise_upstream_error(response)

    async def _coordinator_call(self, action: str, document: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        method = getattr(self._coordinator, action, None)
        if not callable(method):
            raise UpstreamFailure("coordinator_unavailable")
        try:
            if document is None:
                result = method()
            else:
                result = method(document)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(
                    result, timeout=self._config.upstream_timeout_seconds
                )
        except CoordinatorError as exc:
            if not safe_code(exc.code):
                raise UpstreamFailure("coordinator_unavailable") from None
            raise BrowserRequestError(
                503 if exc.retryable else 409,
                exc.code,
                retryable=exc.retryable,
            ) from None
        except TimeoutError:
            raise BrowserRequestError(504, "coordinator_timeout", retryable=True) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise UpstreamFailure("coordinator_unavailable") from None
        if not isinstance(result, Mapping):
            raise GatewayValidationError("invalid_coordinator_response")
        payload = canonical_json(result)
        if len(payload) > self._config.max_json_response_bytes:
            raise UpstreamFailure("upstream_response_too_large")
        self._check_no_credentials(payload)
        return strict_json_document(payload, code="invalid_coordinator_response")

    async def _swarm_status(self, send: Send) -> None:
        document = await self._coordinator_call("status")
        validate_swarm_status(document)
        await self._send_json(send, 200, document)

    async def _swarm_action(
        self, scope: Mapping[str, Any], receive: Receive, send: Send, route: str
    ) -> None:
        actions = {
            "swarm_invites": ("create_invite", "create_invite", 201),
            "swarm_join": ("join", "join", 200),
            "swarm_leave": ("leave", "leave", 200),
        }
        action, method_name, status = actions[route]
        document, _body = await self._read_json_body(scope, receive)
        try:
            swarm_action(document, action)
        except GatewayValidationError as exc:
            raise BrowserRequestError(400, exc.code) from None
        response = await self._coordinator_call(method_name, document)
        validate_swarm_result(response, action, document)
        await self._send_json(send, status, response)

    @staticmethod
    async def _lifespan(receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message.get("type") == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message.get("type") == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _send_json(
        send: Send,
        status: int,
        document: Mapping[str, Any],
        *,
        extra_headers: Sequence[tuple[bytes, bytes]] = (),
    ) -> None:
        body = canonical_json(document)
        headers = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
            *extra_headers,
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @classmethod
    async def _send_error(
        cls,
        send: Send,
        status: int,
        code: str,
        *,
        retryable: bool = False,
        allow: frozenset[str] = frozenset(),
    ) -> None:
        safe = code if safe_code(code) else "internal_error"
        headers: tuple[tuple[bytes, bytes], ...] = ()
        if allow:
            headers = ((b"allow", b", ".join(value.encode("ascii") for value in sorted(allow))),)
        await cls._send_json(
            send,
            status,
            {
                "protocol": "mycelium.product_ui.error.v1",
                "code": safe,
                "retryable": retryable,
            },
            extra_headers=headers,
        )


def create_product_gateway_application(
    *,
    config: GatewayConfig,
    observatory_app: ASGIApplication,
    request_gateway_app: ASGIApplication,
    product_app: ASGIApplication | None = None,
    swarm_coordinator: SwarmCoordinator,
    request_gateway_bearer_token: str,
    observatory_bearer_token: str | None = None,
    access_policy: AccessPolicy | None = None,
    clock: Callable[[], float] = time.time,
) -> ProductGatewayASGIApplication:
    return ProductGatewayASGIApplication(
        config=config,
        observatory_app=observatory_app,
        request_gateway_app=request_gateway_app,
        product_app=product_app,
        swarm_coordinator=swarm_coordinator,
        request_gateway_bearer_token=request_gateway_bearer_token,
        observatory_bearer_token=observatory_bearer_token,
        access_policy=access_policy,
        clock=clock,
    )


__all__ = [
    "AccessPolicy",
    "ProductGatewayASGIApplication",
    "create_product_gateway_application",
]
