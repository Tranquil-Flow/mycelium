"""Framework-free ASGI adapter for the isolated request gateway."""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Awaitable, Callable, Mapping

from .auth import Authenticator
from .contracts import AdmissionError, InferenceSubmission, StreamEvent
from .service import EventSubscription, RequestGatewayService

HEALTH_PATH = "/healthz"
QUALIFICATION_PATH = "/v1/qualification/current"
INFERENCE_PATH = "/v1/inference"
MAX_REQUEST_BODY_BYTES = 262_144
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


async def _send_json(
    send: Send,
    status: int,
    document: Mapping[str, Any],
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    body = _json_bytes(document)
    headers = (
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
    ) + extra_headers
    await send({"type": "http.response.start", "status": status, "headers": list(headers)})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _read_json(receive: Receive) -> Mapping[str, Any]:
    body = bytearray()
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            raise AdmissionError("client_disconnected")
        if message.get("type") != "http.request":
            raise AdmissionError("invalid_request_body")
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            raise AdmissionError("invalid_request_body")
        body.extend(chunk)
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise AdmissionError("request_body_too_large")
        if not message.get("more_body", False):
            break
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError("invalid_request_body") from exc
    if not isinstance(document, Mapping):
        raise AdmissionError("invalid_request_body")
    return document


def _inference_route(path: object) -> tuple[str, str | None] | None:
    if path == INFERENCE_PATH:
        return "submit", None
    if not isinstance(path, str) or not path.startswith(INFERENCE_PATH + "/"):
        return None
    tail = path[len(INFERENCE_PATH) + 1 :]
    if tail.endswith("/events"):
        request_id = tail[: -len("/events")]
        if request_id and "/" not in request_id:
            return "events", request_id
        return None
    if tail and "/" not in tail:
        return "request", tail
    return None


def _last_event_id(scope: Mapping[str, Any]) -> tuple[int, int] | None:
    headers = scope.get("headers", ())
    if not isinstance(headers, (list, tuple)):
        raise AdmissionError("invalid_last_event_id")
    values: list[bytes] = []
    for item in headers:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise AdmissionError("invalid_last_event_id")
        name, value = item
        if isinstance(name, bytes) and name.lower() == b"last-event-id":
            if not isinstance(value, bytes):
                raise AdmissionError("invalid_last_event_id")
            values.append(value)
    if not values:
        return None
    if len(values) != 1:
        raise AdmissionError("invalid_last_event_id")
    try:
        text = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise AdmissionError("invalid_last_event_id") from None
    parts = text.split(":")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise AdmissionError("invalid_last_event_id")
    generation, sequence = (int(part) for part in parts)
    if generation < 1 or max(generation, sequence) > 9_223_372_036_854_775_807:
        raise AdmissionError("invalid_last_event_id")
    return generation, sequence


def _session_token(scope: Mapping[str, Any]) -> str | None:
    headers = scope.get("headers", ())
    if not isinstance(headers, (list, tuple)):
        raise AdmissionError("invalid_session_token")
    values: list[bytes] = []
    for item in headers:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise AdmissionError("invalid_session_token")
        name, value = item
        if isinstance(name, bytes) and name.lower() == b"x-mycelium-session":
            if not isinstance(value, bytes):
                raise AdmissionError("invalid_session_token")
            values.append(value)
    if not values:
        return None
    if len(values) != 1:
        raise AdmissionError("invalid_session_token")
    try:
        token = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise AdmissionError("invalid_session_token") from None
    if not 32 <= len(token) <= 256:
        raise AdmissionError("invalid_session_token")
    return token


def _sse_bytes(event: StreamEvent) -> bytes:
    data = _json_bytes(event.to_dict()).decode("utf-8")
    return (
        f"id: {event.publisher_generation}:{event.sequence}\n"
        f"event: {event.kind}\n"
        f"data: {data}\n\n"
    ).encode("utf-8")


async def _wait_for_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return


class RequestGatewayASGIApplication:
    """Authenticated control/stream app, intentionally separate from Observatory."""

    def __init__(
        self,
        service: RequestGatewayService,
        *,
        authenticator: Authenticator,
        session_token_source: Callable[[], str] | None = None,
    ) -> None:
        self._service = service
        self._authenticator = authenticator
        self._session_token_source = session_token_source or (
            lambda: secrets.token_urlsafe(32)
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

        path = scope.get("path")
        method = scope.get("method")
        if path == HEALTH_PATH:
            if method != "GET":
                await self._method_not_allowed(send, "GET")
                return
            await _send_json(send, 200, self._service.health())
            return

        route = _inference_route(path)
        recognized = path == QUALIFICATION_PATH or route is not None
        if not recognized:
            await _send_json(send, 404, {"error": "not_found"})
            return
        if not self._authenticator.is_authorized(scope):
            await _send_json(
                send,
                401,
                {"error": "unauthorized"},
                extra_headers=((b"www-authenticate", b"Bearer"),),
            )
            return

        if path == QUALIFICATION_PATH:
            if method != "GET":
                await self._method_not_allowed(send, "GET")
                return
            try:
                projection = self._service.current_qualification()
            except AdmissionError as exc:
                await _send_json(send, 503, {"error": exc.code})
                return
            await _send_json(send, 200, projection)
            return

        if route is None:
            await _send_json(send, 404, {"error": "not_found"})
            return
        route_kind, request_id = route
        if route_kind == "submit":
            await self._submit(method, receive, send)
        elif route_kind == "events":
            await self._events(method, request_id or "", scope, receive, send)
        else:
            await self._cancel(method, request_id or "", scope, send)

    async def _submit(self, method: object, receive: Receive, send: Send) -> None:
        if method != "POST":
            await self._method_not_allowed(send, "POST")
            return
        try:
            document = await _read_json(receive)
            submission = InferenceSubmission.from_dict(document)
            owner_token = self._session_token_source()
            request_id = self._service.submit(submission, owner_token=owner_token)
        except AdmissionError as exc:
            status = 409 if exc.code in {
                "deployment_epoch_changed",
                "path_changed",
                "qualification_mismatch",
                "readiness_revoked",
                "route_dropped",
                "stale_qualification",
            } else 400
            await _send_json(send, status, {"error": exc.code})
            return
        await _send_json(
            send,
            202,
            {
                "request_id": request_id,
                "stream_path": f"/v1/inference/{request_id}/events",
                "cancel_path": f"/v1/inference/{request_id}",
                "session_token": owner_token,
            },
        )

    async def _events(
        self,
        method: object,
        request_id: str,
        scope: Mapping[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        if method != "GET":
            await self._method_not_allowed(send, "GET")
            return
        try:
            cursor = _last_event_id(scope)
            subscription = self._service.subscribe(
                request_id,
                last_event_id=cursor,
                owner_token=_session_token(scope),
            )
        except AdmissionError as exc:
            status = 404 if exc.code == "unknown_request" else 409
            await _send_json(send, status, {"error": exc.code})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"x-accel-buffering", b"no"),
                ],
            }
        )
        disconnect_task = asyncio.create_task(_wait_for_disconnect(receive))
        try:
            await self._stream_subscription(subscription, disconnect_task, send)
        finally:
            subscription.close()
            disconnect_task.cancel()
            try:
                await disconnect_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    @staticmethod
    async def _stream_subscription(
        subscription: EventSubscription,
        disconnect_task: asyncio.Task[None],
        send: Send,
    ) -> None:
        while True:
            if disconnect_task.done():
                return
            try:
                event = await asyncio.to_thread(subscription.next_event, timeout=0.1)
            except TimeoutError:
                continue
            if event is None:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            await send(
                {
                    "type": "http.response.body",
                    "body": _sse_bytes(event),
                    "more_body": True,
                }
            )
            subscription.ack(event.sequence)

    async def _cancel(
        self,
        method: object,
        request_id: str,
        scope: Mapping[str, Any],
        send: Send,
    ) -> None:
        if method != "DELETE":
            await self._method_not_allowed(send, "DELETE")
            return
        try:
            cancelled = self._service.cancel(
                request_id,
                owner_token=_session_token(scope),
            )
        except AdmissionError as exc:
            status = 404 if exc.code == "unknown_request" else 409
            await _send_json(send, status, {"error": exc.code})
            return
        await _send_json(
            send,
            202 if cancelled else 200,
            {
                "request_id": request_id,
                "status": "cancelling" if cancelled else "terminal",
            },
        )

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message.get("type") == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message.get("type") == "lifespan.shutdown":
                self._service.close()
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _method_not_allowed(send: Send, allow: str) -> None:
        await _send_json(
            send,
            405,
            {"error": "method_not_allowed"},
            extra_headers=((b"allow", allow.encode("ascii")),),
        )
