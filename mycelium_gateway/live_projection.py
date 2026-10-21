# SPDX-License-Identifier: AGPL-3.0-only
"""Bind the durable read-only Observatory gateway to a live projection source.

The live route supervisor owns a per-request Observatory projection.  This module
binds that projection to the *real* gateway server (:mod:`mycelium_gateway.asgi`)
instead of an ad-hoc stand-in:

* the durable publisher is seeded at a wall-clock generation floor, so a consumer
  that persists a fail-closed high-water mark (the product UI does) never sees a
  generation below the one it already accepted after a supervisor restart;
* a gateway read republishes only when the projection actually changed, so the
  durable envelope stays current without a write per read.

Nothing here adds a control surface: the gateway still publishes only complete,
already-projected, read-only bundles that pass the publisher's own deny layer.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Optional, Union

from .asgi import ObservatoryASGIApplication, ReadPolicy
from .observatory import CoherentSnapshotPublisher, Publication

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_AUTHORIZATION_HEADER = b"authorization"
_PROXY_IDENTITY_HEADERS = (
    b"x-mycelium-authenticated-user",
    b"x-mycelium-proxy-capability",
)


def loopback_upstream_read_policy() -> ReadPolicy:
    """Admit only the in-process product gateway's loopback upstream scope.

    ``mycelium_ui_gateway.upstream.upstream_scope`` builds that scope with
    ``client == ("127.0.0.1", 0)`` and forwards no browser metadata, so the
    policy keys on the loopback peer and refuses any scope that carries an
    Authorization or browser-forgeable proxy identity header.
    """

    def authorize(scope: Any) -> bool:
        if not isinstance(scope, Mapping):
            return False
        client = scope.get("client")
        if (
            not isinstance(client, (list, tuple))
            or not client
            or str(client[0]) not in LOOPBACK_HOSTS
        ):
            return False
        headers = scope.get("headers", ())
        if not isinstance(headers, (list, tuple)):
            return False
        for item in headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return False
            name, value = item
            if not isinstance(name, bytes) or not isinstance(value, bytes):
                return False
            lowered = name.lower()
            if lowered in _PROXY_IDENTITY_HEADERS or lowered == _AUTHORIZATION_HEADER:
                return False
        return True

    return authorize


def _fingerprint(bundle: Mapping[str, Any]) -> bytes:
    """Canonical bytes of one projection, used only for change detection."""

    return json.dumps(
        bundle,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class LiveProjectionPublisher(CoherentSnapshotPublisher):
    """Durable publisher that tracks exactly one live, already-projected bundle."""

    def __init__(
        self,
        state_path: Union[str, Path],
        *,
        projection_source: Callable[[], Optional[Mapping[str, Any]]],
        generation_floor: Callable[[], int],
        max_payload_bytes: int = 2 * 1024 * 1024,
        max_nesting: int = 32,
        replay_capacity: int = 32,
        max_subscribers: int = 64,
        subscriber_queue_size: int = 8,
    ) -> None:
        self._projection_source = projection_source
        self._generation_floor = generation_floor
        self._refresh_mutex = threading.RLock()
        self._last_fingerprint: Optional[bytes] = None
        super().__init__(
            state_path,
            max_payload_bytes=max_payload_bytes,
            max_nesting=max_nesting,
            replay_capacity=replay_capacity,
            max_subscribers=max_subscribers,
            subscriber_queue_size=subscriber_queue_size,
        )
        seed = self._projection_source()
        if seed is not None:
            self.seed_generation_floor(seed, floor=int(self._generation_floor()))
            self._last_fingerprint = _fingerprint(seed)

    def refresh(self) -> Optional[Publication]:
        """Publish the live projection when it changed since the last read."""

        bundle = self._projection_source()
        if bundle is None:
            return None
        with self._refresh_mutex:
            fingerprint = _fingerprint(bundle)
            if fingerprint == self._last_fingerprint:
                return super().current_publication()
            publication = self.publish(bundle)
            self._last_fingerprint = fingerprint
            return publication

    def current_publication(self) -> Optional[Publication]:
        self.refresh()
        return super().current_publication()

    def subscribe(self, *, last_event_id: Optional[int] = None):
        self.refresh()
        return super().subscribe(last_event_id=last_event_id)


@dataclass(frozen=True)
class LiveProjectionGatewayRuntime:
    """The live stack's Observatory gateway: projection, publisher, ASGI surface."""

    publisher: LiveProjectionPublisher
    app: ObservatoryASGIApplication

    def close(self) -> None:
        """No owned threads or descriptors: publications are durable on write."""


def build_live_projection_gateway(
    *,
    state_path: Union[str, Path],
    projection_source: Callable[[], Optional[Mapping[str, Any]]],
    generation_floor: Callable[[], int],
    read_policy: Optional[ReadPolicy] = None,
    heartbeat_interval: float = 15.0,
    poll_interval: float = 0.05,
    **bounds: Any,
) -> LiveProjectionGatewayRuntime:
    """Build the live gateway in one call (deny-by-default read policy)."""

    publisher = LiveProjectionPublisher(
        state_path,
        projection_source=projection_source,
        generation_floor=generation_floor,
        **bounds,
    )
    app = ObservatoryASGIApplication(
        publisher,
        read_policy=read_policy,
        heartbeat_interval=heartbeat_interval,
        poll_interval=poll_interval,
    )
    return LiveProjectionGatewayRuntime(publisher=publisher, app=app)


__all__ = [
    "LiveProjectionGatewayRuntime",
    "LiveProjectionPublisher",
    "build_live_projection_gateway",
    "loopback_upstream_read_policy",
]
