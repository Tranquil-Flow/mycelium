"""Bounded in-process ASGI composition primitives."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
FrameValidator = Callable[[bytes], bytes]


class ASGIApplication(Protocol):
    async def __call__(self, scope: Mapping[str, Any], receive: Receive, send: Send) -> None: ...


class UpstreamFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("upstream_failure")


@dataclass(frozen=True, slots=True)
class UpstreamResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes

    def header_values(self, name: bytes) -> tuple[bytes, ...]:
        wanted = name.lower()
        return tuple(value for key, value in self.headers if key.lower() == wanted)


def upstream_scope(
    *, method: str, path: str, headers: tuple[tuple[bytes, bytes], ...]
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
    }


class BoundedUpstreamClient:
    """Calls injected ASGI applications without forwarding browser metadata."""

    def __init__(self, *, response_limit: int, timeout_seconds: int) -> None:
        self._response_limit = response_limit
        self._timeout_seconds = timeout_seconds

    async def request(
        self,
        app: ASGIApplication,
        *,
        method: str,
        path: str,
        headers: tuple[tuple[bytes, bytes], ...],
        body: bytes = b"",
    ) -> UpstreamResponse:
        supplied = False
        wait_forever = asyncio.Event()
        started: tuple[int, tuple[tuple[bytes, bytes], ...]] | None = None
        chunks = bytearray()
        complete = False

        async def receive() -> dict[str, Any]:
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": body, "more_body": False}
            await wait_forever.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal started, complete
            message_type = message.get("type")
            if message_type == "http.response.start":
                if started is not None or chunks or complete:
                    raise UpstreamFailure("invalid_upstream_response")
                status = message.get("status")
                raw_headers = message.get("headers", [])
                if (
                    not isinstance(status, int)
                    or isinstance(status, bool)
                    or not 100 <= status <= 599
                    or not isinstance(raw_headers, (list, tuple))
                ):
                    raise UpstreamFailure("invalid_upstream_response")
                checked: list[tuple[bytes, bytes]] = []
                for item in raw_headers:
                    if (
                        not isinstance(item, (list, tuple))
                        or len(item) != 2
                        or not isinstance(item[0], bytes)
                        or not isinstance(item[1], bytes)
                    ):
                        raise UpstreamFailure("invalid_upstream_response")
                    checked.append((item[0], item[1]))
                started = (status, tuple(checked))
                return
            if message_type != "http.response.body" or started is None or complete:
                raise UpstreamFailure("invalid_upstream_response")
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise UpstreamFailure("invalid_upstream_response")
            if len(chunks) + len(chunk) > self._response_limit:
                raise UpstreamFailure("upstream_response_too_large")
            chunks.extend(chunk)
            if not message.get("more_body", False):
                complete = True

        try:
            async with asyncio.timeout(self._timeout_seconds):
                await app(
                    upstream_scope(method=method, path=path, headers=headers),
                    receive,
                    send,
                )
        except UpstreamFailure:
            raise
        except TimeoutError:
            raise UpstreamFailure("upstream_timeout") from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise UpstreamFailure("upstream_unavailable") from None
        if started is None or not complete:
            raise UpstreamFailure("invalid_upstream_response")
        status, response_headers = started
        content_lengths = tuple(
            value for name, value in response_headers if name.lower() == b"content-length"
        )
        if len(content_lengths) > 1:
            raise UpstreamFailure("invalid_upstream_response")
        if content_lengths:
            try:
                stated_length = int(content_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise UpstreamFailure("invalid_upstream_response") from None
            if stated_length != len(chunks):
                raise UpstreamFailure("invalid_upstream_response")
        return UpstreamResponse(status=status, headers=response_headers, body=bytes(chunks))


class BoundedSSEProxy:
    """A bounded queue and strict frame boundary between two ASGI applications."""

    def __init__(
        self,
        *,
        frame_limit: int,
        queue_chunks: int,
        startup_timeout_seconds: int,
        stream_timeout_seconds: int,
    ) -> None:
        self._frame_limit = frame_limit
        self._queue_chunks = queue_chunks
        self._startup_timeout_seconds = startup_timeout_seconds
        self._stream_timeout_seconds = stream_timeout_seconds

    async def forward(
        self,
        app: ASGIApplication,
        *,
        path: str,
        headers: tuple[tuple[bytes, bytes], ...],
        client_receive: Receive,
        client_send: Send,
        validate_frame: FrameValidator,
    ) -> UpstreamResponse | None:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=self._queue_chunks)
        disconnect = asyncio.Event()
        supplied = False
        response_started = False

        async def upstream_receive() -> dict[str, Any]:
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def upstream_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            message_type = message.get("type")
            if message_type == "http.response.start":
                if response_started:
                    raise UpstreamFailure("invalid_upstream_response")
                status = message.get("status")
                raw_headers = message.get("headers", [])
                if (
                    not isinstance(status, int)
                    or isinstance(status, bool)
                    or not 100 <= status <= 599
                    or not isinstance(raw_headers, (list, tuple))
                ):
                    raise UpstreamFailure("invalid_upstream_response")
                checked: list[tuple[bytes, bytes]] = []
                for item in raw_headers:
                    if (
                        not isinstance(item, (list, tuple))
                        or len(item) != 2
                        or not isinstance(item[0], bytes)
                        or not isinstance(item[1], bytes)
                    ):
                        raise UpstreamFailure("invalid_upstream_response")
                    checked.append((item[0], item[1]))
                response_started = True
                await queue.put(("start", (status, tuple(checked))))
                return
            if message_type != "http.response.body" or not response_started:
                raise UpstreamFailure("invalid_upstream_response")
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes) or len(chunk) > self._frame_limit * 2:
                raise UpstreamFailure("upstream_sse_chunk_too_large")
            await queue.put(("body", (chunk, bool(message.get("more_body", False)))))

        async def run_upstream() -> None:
            try:
                await app(
                    upstream_scope(method="GET", path=path, headers=headers),
                    upstream_receive,
                    upstream_send,
                )
            except asyncio.CancelledError:
                raise
            except UpstreamFailure as exc:
                await queue.put(("error", exc.code))
            except Exception:
                await queue.put(("error", "upstream_unavailable"))
            finally:
                await queue.put(("complete", None))

        async def watch_disconnect() -> None:
            while True:
                message = await client_receive()
                if message.get("type") == "http.disconnect":
                    disconnect.set()
                    return

        upstream_task = asyncio.create_task(run_upstream())
        disconnect_task = asyncio.create_task(watch_disconnect())
        try:
            try:
                first_kind, first_value = await asyncio.wait_for(
                    queue.get(), timeout=self._startup_timeout_seconds
                )
            except TimeoutError:
                raise UpstreamFailure("upstream_timeout") from None
            if first_kind == "error":
                raise UpstreamFailure(str(first_value))
            if first_kind != "start":
                raise UpstreamFailure("invalid_upstream_response")
            status, response_headers = first_value
            if status != 200:
                return await self._collect_error_response(
                    status, response_headers, queue, disconnect_task
                )
            content_types = tuple(
                value for name, value in response_headers if name.lower() == b"content-type"
            )
            if len(content_types) != 1 or content_types[0].split(b";", 1)[0].strip().lower() != b"text/event-stream":
                raise UpstreamFailure("invalid_upstream_response")
            await client_send(
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
            await self._relay_started_stream(
                queue,
                disconnect_task,
                client_send,
                validate_frame,
            )
            return None
        finally:
            disconnect.set()
            if not disconnect_task.done():
                disconnect_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await disconnect_task
            if not upstream_task.done():
                upstream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await upstream_task

    async def _collect_error_response(
        self,
        status: int,
        headers: tuple[tuple[bytes, bytes], ...],
        queue: asyncio.Queue[tuple[str, Any]],
        disconnect_task: asyncio.Task[None],
    ) -> UpstreamResponse:
        body = bytearray()
        while True:
            if disconnect_task.done():
                raise UpstreamFailure("client_disconnected")
            kind, value = await asyncio.wait_for(
                queue.get(), timeout=self._startup_timeout_seconds
            )
            if kind == "error":
                raise UpstreamFailure(str(value))
            if kind == "complete":
                raise UpstreamFailure("invalid_upstream_response")
            if kind != "body":
                raise UpstreamFailure("invalid_upstream_response")
            chunk, more_body = value
            if len(body) + len(chunk) > self._frame_limit:
                raise UpstreamFailure("upstream_response_too_large")
            body.extend(chunk)
            if not more_body:
                return UpstreamResponse(status=status, headers=headers, body=bytes(body))

    async def _relay_started_stream(
        self,
        queue: asyncio.Queue[tuple[str, Any]],
        disconnect_task: asyncio.Task[None],
        send: Send,
        validate_frame: FrameValidator,
    ) -> None:
        buffer = bytearray()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stream_timeout_seconds
        stream_closed = False
        while not stream_closed:
            if disconnect_task.done():
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            get_task = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait(
                {get_task, disconnect_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                get_task.cancel()
                with suppress(asyncio.CancelledError):
                    await get_task
                return
            if get_task not in done:
                get_task.cancel()
                with suppress(asyncio.CancelledError):
                    await get_task
                break
            kind, value = get_task.result()
            if kind in {"error", "complete"}:
                break
            if kind != "body":
                break
            chunk, more_body = value
            buffer.extend(chunk)
            while True:
                boundary = buffer.find(b"\n\n")
                if boundary < 0:
                    break
                frame_length = boundary + 2
                if frame_length > self._frame_limit:
                    stream_closed = True
                    break
                raw_frame = bytes(buffer[:frame_length])
                del buffer[:frame_length]
                try:
                    safe_frame = validate_frame(raw_frame)
                except Exception:
                    stream_closed = True
                    break
                await send(
                    {"type": "http.response.body", "body": safe_frame, "more_body": True}
                )
            if len(buffer) > self._frame_limit:
                stream_closed = True
            if not more_body:
                if buffer:
                    stream_closed = True
                break
        await send({"type": "http.response.body", "body": b"", "more_body": False})


__all__ = [
    "ASGIApplication",
    "BoundedSSEProxy",
    "BoundedUpstreamClient",
    "UpstreamFailure",
    "UpstreamResponse",
]
