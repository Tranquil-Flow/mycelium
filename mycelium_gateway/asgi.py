"""Framework-free read-only ASGI surface for Observatory snapshots and SSE."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import json
import queue
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence, Union

from .observatory import MAX_SAFE_GENERATION, SubscriberLimitError


SNAPSHOT_PATH = "/v1/observatory/snapshot"
EVENTS_PATH = "/v1/observatory/events"
ReadPolicyResult = Union[bool, Awaitable[bool]]
ReadPolicy = Callable[[Mapping[str, Any]], ReadPolicyResult]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class PublishedEnvelope(Protocol):
    @property
    def generation(self) -> int: ...

    @property
    def envelope_json(self) -> bytes: ...


class SnapshotSubscriptionLike(Protocol):
    @property
    def replay(self) -> Sequence[PublishedEnvelope]: ...

    @property
    def closed(self) -> bool: ...

    def get_nowait(self) -> PublishedEnvelope: ...

    def close(self) -> None: ...


class SnapshotPublisherLike(Protocol):
    def snapshot_json(self) -> Optional[bytes]: ...

    def subscribe(self, *, last_event_id: Optional[int] = None) -> SnapshotSubscriptionLike: ...


def _headers(scope: Mapping[str, Any], name: bytes) -> list[bytes]:
    wanted = name.lower()
    values: list[bytes] = []
    for candidate_name, value in scope.get("headers", []):
        if isinstance(candidate_name, bytes) and candidate_name.lower() == wanted and isinstance(value, bytes):
            values.append(value)
    return values


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


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


def _event_bytes(publication: PublishedEnvelope) -> bytes:
    return (
        f"id: {publication.generation}\nevent: snapshot\ndata: ".encode("ascii")
        + publication.envelope_json
        + b"\n\n"
    )


async def _wait_for_http_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return


class ObservatoryASGIApplication:
    """Read-only snapshot and inbound-only SSE application."""

    def __init__(
        self,
        publisher: SnapshotPublisherLike,
        *,
        read_policy: Optional[ReadPolicy] = None,
        heartbeat_interval: float = 15.0,
        poll_interval: float = 0.05,
    ) -> None:
        if isinstance(heartbeat_interval, bool) or not isinstance(heartbeat_interval, (int, float)):
            raise ValueError("heartbeat_interval must be numeric")
        if not 0.01 <= float(heartbeat_interval) <= 60.0:
            raise ValueError("heartbeat_interval must be between 0.01 and 60 seconds")
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)):
            raise ValueError("poll_interval must be numeric")
        if not 0.001 <= float(poll_interval) <= 1.0:
            raise ValueError("poll_interval must be between 0.001 and 1 second")
        self._publisher = publisher
        self._read_policy = read_policy
        self._heartbeat_interval = float(heartbeat_interval)
        self._poll_interval = float(poll_interval)

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
        if path not in {SNAPSHOT_PATH, EVENTS_PATH}:
            await _send_json(send, 404, {"error": "not_found"})
            return
        if scope.get("method") != "GET":
            await _send_json(
                send,
                405,
                {"error": "method_not_allowed"},
                extra_headers=((b"allow", b"GET"),),
            )
            return
        if _headers(scope, b"upgrade"):
            await _send_json(send, 400, {"error": "upgrade_not_supported"})
            return
        if not await self._is_authorized(scope):
            await _send_json(send, 403, {"error": "forbidden"})
            return

        if path == SNAPSHOT_PATH:
            await self._snapshot(send)
            return
        await self._events(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message_type == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _is_authorized(self, scope: Mapping[str, Any]) -> bool:
        policy = self._read_policy
        if policy is None:
            return False
        try:
            decision = policy(scope)
            if inspect.isawaitable(decision):
                decision = await decision
            return decision is True
        except Exception:
            return False

    async def _snapshot(self, send: Send) -> None:
        try:
            payload = self._publisher.snapshot_json()
        except Exception:
            await _send_json(send, 500, {"error": "internal_error"})
            return
        if payload is None:
            await _send_json(send, 503, {"error": "snapshot_unavailable"})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})

    def _last_event_id(self, scope: Mapping[str, Any]) -> Optional[int]:
        values = _headers(scope, b"last-event-id")
        if not values:
            return None
        if len(values) != 1:
            raise ValueError("duplicate Last-Event-ID")
        try:
            text = values[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Last-Event-ID is not ASCII") from exc
        if not text or not text.isdecimal():
            raise ValueError("Last-Event-ID is not a non-negative integer")
        generation = int(text)
        if generation > MAX_SAFE_GENERATION:
            raise ValueError("Last-Event-ID exceeds safe integer range")
        return generation

    async def _events(self, scope: Mapping[str, Any], receive: Receive, send: Send) -> None:
        try:
            last_event_id = self._last_event_id(scope)
        except ValueError:
            await _send_json(send, 400, {"error": "invalid_last_event_id"})
            return
        try:
            subscription = self._publisher.subscribe(last_event_id=last_event_id)
        except SubscriberLimitError:
            await _send_json(send, 503, {"error": "subscriber_limit"})
            return
        except Exception:
            await _send_json(send, 500, {"error": "internal_error"})
            return

        disconnect_task = asyncio.create_task(_wait_for_http_disconnect(receive))
        try:
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
            for publication in subscription.replay:
                await send(
                    {
                        "type": "http.response.body",
                        "body": _event_bytes(publication),
                        "more_body": True,
                    }
                )

            loop = asyncio.get_running_loop()
            next_heartbeat = loop.time() + self._heartbeat_interval
            while not disconnect_task.done() and not subscription.closed:
                sent_snapshot = False
                while True:
                    try:
                        publication = subscription.get_nowait()
                    except queue.Empty:
                        break
                    await send(
                        {
                            "type": "http.response.body",
                            "body": _event_bytes(publication),
                            "more_body": True,
                        }
                    )
                    sent_snapshot = True
                if sent_snapshot:
                    next_heartbeat = loop.time() + self._heartbeat_interval
                    continue

                remaining = next_heartbeat - loop.time()
                if remaining <= 0:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b": heartbeat\n\n",
                            "more_body": True,
                        }
                    )
                    next_heartbeat = loop.time() + self._heartbeat_interval
                    continue
                await asyncio.wait(
                    {disconnect_task},
                    timeout=min(self._poll_interval, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Response may already be streaming; close silently instead of sending a second response.
            return
        finally:
            subscription.close()
            if not disconnect_task.done():
                disconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect_task
