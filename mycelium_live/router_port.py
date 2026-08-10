"""Adapt the request gateway's RouterPort onto a persistent LiveRoute."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from mycelium_router.contracts import ExecutionGraph, RequestContext

from .route import InferenceCancelled, LiveRoute


@dataclass
class _Pending:
    tokens: list[tuple[int, int]] = field(default_factory=list)
    cursor: int = 0
    terminal_status: str | None = None
    cancellation_requested: threading.Event = field(default_factory=threading.Event)
    release_requested: bool = False


class LiveRouterPort:
    """Drive one persistent physical route through the RouterPort contract."""

    def __init__(self, *, route: LiveRoute, execution_graph: ExecutionGraph) -> None:
        self._route = route
        self._graph = execution_graph
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._pending: dict[str, _Pending] = {}
        self._sinks: dict[str, object] = {}

    def current_deployment(self) -> ExecutionGraph:
        return self._graph

    def is_idle(self) -> bool:
        """Return true only after every admitted request has been released."""

        with self._lock:
            return not self._pending

    def admit(
        self,
        request: RequestContext,
        client_sink: object,
        *,
        pinned_deployment: ExecutionGraph | None = None,
        **kwargs: object,
    ) -> str:
        if not self._route.is_alive():
            raise RuntimeError("route_not_open")

        pending = _Pending()

        class _Collector:
            def emit(self, token_index: int, token_id: int) -> None:
                with self_outer._changed:
                    pending.tokens.append((token_index, token_id))
                    self_outer._changed.notify_all()

        self_outer = self
        with self._lock:
            self._pending[request.request_id] = pending
            self._sinks[request.request_id] = client_sink

        def run_route() -> None:
            try:
                self._route.infer(
                    request.prompt_token_ids,
                    max_new_tokens=request.max_new_tokens,
                    request_id=request.request_id,
                    sink=_Collector(),
                    cancel_requested=pending.cancellation_requested.is_set,
                )
            except InferenceCancelled:
                terminal = "CANCELLED"
            except BaseException:
                terminal = "FAILED"
            else:
                terminal = "COMPLETED"
            with self._changed:
                pending.terminal_status = terminal
                if pending.release_requested:
                    self._pending.pop(request.request_id, None)
                    self._sinks.pop(request.request_id, None)
                    self._route.release_request(request.request_id)
                self._changed.notify_all()

        threading.Thread(
            target=run_route,
            name=f"live-route-{request.request_id}",
            daemon=True,
        ).start()
        return request.request_id

    def decode_one(self, request_id: str) -> bool:
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            while (
                pending.cursor >= len(pending.tokens)
                and pending.terminal_status is None
            ):
                self._changed.wait(timeout=30.0)
            if pending.cursor >= len(pending.tokens):
                return False
            token_index, token_id = pending.tokens[pending.cursor]
            pending.cursor += 1
            sink = self._sinks[request_id]
        sink.emit(token_index, token_id)
        return True

    def request_status(self, request_id: str) -> str:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return "UNKNOWN"
            if (
                pending.terminal_status is not None
                and pending.cursor >= len(pending.tokens)
            ):
                return pending.terminal_status
            return "DECODING"

    def cancel(self, request_id: str) -> bool:
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None or pending.terminal_status is not None:
                return False
            pending.cancellation_requested.set()
            self._changed.notify_all()
            return True

    def release_request(self, request_id: str) -> None:
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            if pending.terminal_status is None:
                pending.release_requested = True
            else:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                self._route.release_request(request_id)
            self._changed.notify_all()


__all__ = ["LiveRouterPort"]
