"""Adapt the request gateway's RouterPort onto a persistent LiveRoute."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from mycelium_m16_runtime import M16AdmissionError, M16RuntimeCoordinator
from mycelium_router.contracts import ExecutionGraph, RequestContext

from .route import InferenceCancelled, LiveRoute


@dataclass
class _Pending:
    request: RequestContext | None = None
    tokens: list[tuple[int, int]] = field(default_factory=list)
    cursor: int = 0
    terminal_status: str | None = None
    cancellation_requested: threading.Event = field(default_factory=threading.Event)
    release_requested: bool = False


class LiveRouterPort:
    """Drive one persistent physical route through the RouterPort contract."""

    def __init__(
        self,
        *,
        route: LiveRoute,
        execution_graph: ExecutionGraph,
        runtime_coordinator: M16RuntimeCoordinator | None = None,
    ) -> None:
        self._route = route
        self._graph = execution_graph
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._pending: dict[str, _Pending] = {}
        self._sinks: dict[str, object] = {}
        self._coordinator = runtime_coordinator
        self._dispatcher = None
        if runtime_coordinator is not None:
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name=f"m16-dispatch-{execution_graph.deployment_id[:16]}",
                daemon=True,
            )
            self._dispatcher.start()

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

        pending = _Pending(request=request)
        coordinator = self._coordinator
        if coordinator is not None:
            profile = kwargs.get("workload_profile_id")
            if profile is None:
                profile = (
                    "sustained_batch_v1"
                    if request.qos_class == "batch"
                    else "interactive_chat_v1"
                )
            if not isinstance(profile, str):
                raise M16AdmissionError("workload_not_qualified")
            coordinator.admit(request, workload_profile_id=profile)
        with self._lock:
            if request.request_id in self._pending:
                if coordinator is not None:
                    coordinator.cancel(request.request_id)
                raise RuntimeError("duplicate_request_id")
            self._pending[request.request_id] = pending
            self._sinks[request.request_id] = client_sink
            self._changed.notify_all()

        if coordinator is None:
            threading.Thread(
                target=self._run_route,
                args=(request.request_id, request, pending),
                name=f"live-route-{request.request_id}",
                daemon=True,
            ).start()
        return request.request_id

    def _dispatch_loop(self) -> None:
        coordinator = self._coordinator
        assert coordinator is not None
        while True:
            with self._changed:
                request_id = coordinator.next_dispatch()
                if request_id is None:
                    self._changed.wait(timeout=0.05)
                    continue
                pending = self._pending.get(request_id)
                stored = None if pending is None else pending.request
            if pending is None or stored is None:
                coordinator.complete(request_id, state="failed")
                continue
            self._run_route(request_id, stored, pending)

    def _run_route(
        self,
        request_id: str,
        request: RequestContext,
        pending: _Pending,
    ) -> None:
        coordinator = self._coordinator
        first_token = False
        self_outer = self

        class _Collector:
            def emit(self, token_index: int, token_id: int) -> None:
                nonlocal first_token
                if coordinator is not None:
                    coordinator.mark_phase(
                        request_id,
                        "first_token" if not first_token else "decode",
                    )
                    first_token = True
                with self_outer._changed:
                    pending.tokens.append((token_index, token_id))
                    self_outer._changed.notify_all()

        try:
            self._route.infer(
                request.prompt_token_ids,
                max_new_tokens=request.max_new_tokens,
                request_id=request_id,
                sink=_Collector(),
                cancel_requested=pending.cancellation_requested.is_set,
            )
        except InferenceCancelled:
            terminal = "CANCELLED"
        except BaseException:
            terminal = "FAILED"
        else:
            terminal = "COMPLETED"
        if coordinator is not None:
            coordinator.mark_phase(request_id, "cleanup")
            coordinator.complete(request_id, state=terminal.lower())
        should_release = False
        with self._changed:
            pending.terminal_status = terminal
            if pending.release_requested:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                should_release = True
            self._changed.notify_all()
        if should_release:
            self._route.release_request(request_id)

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
            coordinator = self._coordinator
            if coordinator is not None and coordinator.phase(request_id) == "queued":
                coordinator.cancel(request_id)
                pending.terminal_status = "CANCELLED"
                self._changed.notify_all()
                return True
            pending.cancellation_requested.set()
            self._changed.notify_all()
            return True

    def runtime_status(self) -> dict[str, Any] | None:
        coordinator = self._coordinator
        return None if coordinator is None else coordinator.status()

    def release_request(self, request_id: str) -> None:
        should_release = False
        with self._changed:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            if pending.terminal_status is None:
                pending.release_requested = True
            else:
                self._pending.pop(request_id, None)
                self._sinks.pop(request_id, None)
                should_release = True
            self._changed.notify_all()
        if should_release:
            self._route.release_request(request_id)


__all__ = ["LiveRouterPort"]
