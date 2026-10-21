# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded read-only ASGI source for the unified product snapshot."""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .contracts import validate_product_event
from .projector import ProductProjector, _unknown_internet_native
from .state import ProductEvidenceStateStore, TransportHistoryStateStore
from .transport_history import TransportTransitionHistory

_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ProductSourceUnavailable(RuntimeError):
    """A source is transiently unavailable for a bounded, public reason.

    A composition raises it from a source reader (for example while the route
    owner cold-restarts); the snapshot then names ``reason_code`` for that
    source instead of the generic ``<source>_failed`` code. Any other error a
    reader raises keeps the generic code, so no private detail is published.
    """

    def __init__(self, reason_code: str) -> None:
        if not isinstance(reason_code, str) or _REASON.fullmatch(reason_code) is None:
            raise ValueError("product_source_reason_invalid")
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reason(exc: Exception, fallback: str) -> str:
    return exc.reason_code if isinstance(exc, ProductSourceUnavailable) else fallback


class ProductEvidenceApplication:
    """Project immutable authority reads; never invoke a mutating subsystem."""

    def __init__(
        self,
        *,
        projector: ProductProjector,
        membership_source: Callable[[], Sequence[Mapping[str, Any]]],
        assignment_source: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        route_source: Callable[[], Mapping[str, Any] | None],
        qualification_source: Callable[[], Mapping[str, Any] | None],
        internet_native_source: Callable[[], Mapping[str, Any]] | None = None,
        recovery_source: Callable[[], Mapping[str, Any] | None] | None = None,
        coarse_region_source: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        clock_unix_ms: Callable[[], int] | None = None,
        replay_limit: int = 128,
        state_root: str | Path | None = None,
        transport_binding: str | None = None,
        a7_source: Callable[[], Mapping[str, Any] | None] | None = None,
    ) -> None:
        if not isinstance(projector, ProductProjector):
            raise ValueError("product_projector_invalid")
        if not all(
            callable(source)
            for source in (membership_source, route_source, qualification_source)
        ):
            raise ValueError("product_source_invalid")
        if internet_native_source is not None and not callable(internet_native_source):
            raise ValueError("product_source_invalid")
        if (
            not isinstance(replay_limit, int)
            or isinstance(replay_limit, bool)
            or not 1 <= replay_limit <= 4_096
        ):
            raise ValueError("product_replay_limit_invalid")
        self._projector = projector
        self._membership_source = membership_source
        self._assignment_source = assignment_source
        self._route_source = route_source
        if recovery_source is not None and not callable(recovery_source):
            raise ValueError("product_source_invalid")
        if coarse_region_source is not None and not callable(coarse_region_source):
            raise ValueError("product_source_invalid")
        self._coarse_region_source = coarse_region_source
        self._qualification_source = qualification_source
        self._internet_native_source = internet_native_source
        if transport_binding is not None and state_root is None:
            raise ValueError("product_transport_binding_requires_state_root")
        # Live transport observations become immutable transition history here,
        # independent of the route object's lifetime (cold route restarts). With
        # a deployment binding the ledger also survives a planned serve restart.
        self._transport_history = TransportTransitionHistory(
            store=(
                None
                if transport_binding is None
                else TransportHistoryStateStore(state_root, binding=transport_binding)
            )
        )
        self._recovery_source = recovery_source
        if a7_source is not None and not callable(a7_source):
            raise ValueError("product_source_invalid")
        self._a7_source = a7_source
        self._clock_unix_ms = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._store = (
            None
            if state_root is None
            else ProductEvidenceStateStore(state_root, replay_limit=replay_limit)
        )
        restored = [] if self._store is None else self._store.load()
        self._events: deque[dict[str, Any]] = deque(restored, maxlen=replay_limit)
        if restored:
            publication = restored[-1]["snapshot"]["publication"]
            projector.restore_publication(
                generation=publication["generation"],
                cursor=publication["cursor"],
            )
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                if message.get("type") == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message.get("type") == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope.get("type") != "http" or scope.get("method") != "GET":
            await self._send_json(send, 404, {"error": "not_found"})
            return
        path = scope.get("path")
        if path not in {
            "/v1/product/snapshot",
            "/v1/product/export",
            "/v1/product/events",
        }:
            await self._send_json(send, 404, {"error": "not_found"})
            return
        try:
            snapshot = self._publish()
        except Exception:
            await self._send_json(send, 503, {"error": "product_snapshot_unavailable"})
            return
        if path in {"/v1/product/snapshot", "/v1/product/export"}:
            await self._send_json(send, 200, snapshot)
            return
        if path == "/v1/product/events":
            cursor = self._last_event_id(scope)
            stale = False
            with self._lock:
                floor = self._events[0]["cursor"] if self._events else snapshot["publication"]["cursor"]
                if cursor is not None and cursor < floor - 1:
                    stale = True
                    events = []
                else:
                    events = [
                        event
                        for event in self._events
                        if cursor is None or event["cursor"] > cursor
                    ]
            if stale:
                await self._send_json(send, 409, {"error": "product_cursor_stale"})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            for event in events:
                body = (
                    f"id: {event['cursor']}\n"
                    "event: product_snapshot\n"
                    f"data: {self._encoded(event).decode('utf-8')}\n\n"
                ).encode("utf-8")
                await send(
                    {"type": "http.response.body", "body": body, "more_body": True}
                )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await self._send_json(send, 404, {"error": "not_found"})

    def _publish(self) -> dict[str, Any]:
        with self._lock:
            source_errors: dict[str, str] = {}
            try:
                members = self._membership_source()
            except Exception as exc:
                members = []
                source_errors["membership-source"] = _reason(exc, "membership_source_failed")
            assignments = None
            if self._assignment_source is not None:
                try:
                    assignments = self._assignment_source()
                except Exception as exc:
                    assignments = []
                    source_errors["assignment-source"] = _reason(
                        exc, "assignment_source_failed")
            try:
                route_status = self._route_source()
            except Exception as exc:
                route_status = None
                source_errors["route-source"] = _reason(exc, "route_source_failed")
            try:
                qualification = self._qualification_source()
            except Exception as exc:
                qualification = None
                source_errors["qualification-source"] = _reason(
                    exc, "qualification_source_failed")
            internet_native = None
            if self._internet_native_source is not None:
                observed_at = self._clock_unix_ms()
                try:
                    document = self._internet_native_source()
                except Exception as exc:
                    # An unreadable source publishes the existing unknown A8
                    # projection: the current path is unknown, never invented,
                    # and the retained transition history stays published. A
                    # typed transient unavailability (a restarting route) also
                    # says the route is unavailable. Evidence the source did
                    # return is still validated below and fails closed.
                    document = _unknown_internet_native(observed_at)
                    if isinstance(exc, ProductSourceUnavailable):
                        document["bootstrap_status"]["route_state"] = "unavailable"
                internet_native = self._transport_history.observe(
                    document,
                    now_unix_ms=observed_at,
                )
            recovery_status = None
            if self._recovery_source is not None:
                try:
                    recovery_status = self._recovery_source()
                except Exception as exc:
                    source_errors["recovery-source"] = _reason(exc, "recovery_source_failed")
            a7_status = None
            if self._a7_source is not None:
                try:
                    a7_status = self._a7_source()
                except Exception as exc:
                    source_errors["a7-source"] = _reason(exc, "a7_source_failed")
            coarse_region_evidence = None
            if self._coarse_region_source is not None:
                try:
                    records = list(self._coarse_region_source())
                except Exception:
                    records = []  # an unreadable declaration publishes nothing
                # Without a record the A14a projection stays exactly as it was.
                coarse_region_evidence = records or None
            snapshot = self._projector.project(
                members=members,
                assignments=assignments,
                route_status=route_status,
                qualification=qualification,
                internet_native=internet_native,
                recovery_status=recovery_status,
                now_unix_ms=self._clock_unix_ms(),
                source_errors=source_errors,
                a7_status=a7_status,
                a7_configured=self._a7_source is not None and (a7_status is not None or "a7-source" in source_errors),
                coarse_region_evidence=coarse_region_evidence,
            )
            cursor = snapshot["publication"]["cursor"]
            event = validate_product_event(
                {
                    "protocol": "mycelium.product_event.v1",
                    "cursor": cursor,
                    "previous_cursor": cursor - 1,
                    "event_kind": "snapshot_published",
                    "snapshot": snapshot,
                }
            )
            self._events.append(event)
            if self._store is not None:
                retained = self._store.write(list(self._events))
                if len(retained) != len(self._events):
                    self._events = deque(retained, maxlen=self._events.maxlen)
            return snapshot

    @staticmethod
    def _last_event_id(scope: Mapping[str, Any]) -> int | None:
        values = [
            value
            for name, value in scope.get("headers", ())
            if isinstance(name, bytes)
            and isinstance(value, bytes)
            and name.lower() == b"last-event-id"
        ]
        if not values:
            return None
        if len(values) != 1:
            raise ValueError("product_cursor_invalid")
        text = values[0].decode("ascii")
        if not text.isdecimal():
            raise ValueError("product_cursor_invalid")
        return int(text)

    @staticmethod
    def _encoded(document: Mapping[str, Any]) -> bytes:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    async def _send_json(send, status: int, document: Mapping[str, Any]) -> None:
        body = ProductEvidenceApplication._encoded(document)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
