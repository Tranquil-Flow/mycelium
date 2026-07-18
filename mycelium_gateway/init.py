"""Explicit construction seam for the read-only Observatory gateway."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from typing import Callable, Optional, Union

from .asgi import ObservatoryASGIApplication, ReadPolicy
from .observatory import CoherentSnapshotPublisher
from .semantic import ObservatoryPublicationOwner


@dataclass(frozen=True)
class ObservatoryGatewayRuntime:
    """Publisher plus ASGI consumer surface; no control-plane capabilities."""

    publisher: CoherentSnapshotPublisher
    app: ObservatoryASGIApplication


def build_observatory_gateway(
    *,
    state_path: Union[str, os.PathLike[str]],
    read_policy: Optional[ReadPolicy] = None,
    max_payload_bytes: int = 2 * 1024 * 1024,
    max_nesting: int = 32,
    replay_capacity: int = 32,
    max_subscribers: int = 64,
    subscriber_queue_size: int = 8,
    heartbeat_interval: float = 15.0,
    poll_interval: float = 0.05,
) -> ObservatoryGatewayRuntime:
    """Build a deny-by-default gateway without inference or mutation surfaces."""
    publisher = CoherentSnapshotPublisher(
        Path(state_path),
        max_payload_bytes=max_payload_bytes,
        max_nesting=max_nesting,
        replay_capacity=replay_capacity,
        max_subscribers=max_subscribers,
        subscriber_queue_size=subscriber_queue_size,
    )
    app = ObservatoryASGIApplication(
        publisher,
        read_policy=read_policy,
        heartbeat_interval=heartbeat_interval,
        poll_interval=poll_interval,
    )
    return ObservatoryGatewayRuntime(publisher=publisher, app=app)


@dataclass(frozen=True)
class SemanticObservatoryGatewayRuntime:
    """One semantic publication/SSE owner plus its read-only ASGI surface."""

    owner: ObservatoryPublicationOwner
    app: ObservatoryASGIApplication

    def close(self) -> None:
        self.owner.close()

    def __enter__(self) -> "SemanticObservatoryGatewayRuntime":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def build_semantic_observatory_gateway(
    *,
    state_path: Union[str, os.PathLike[str]],
    read_policy: Optional[ReadPolicy] = None,
    now: Optional[Callable[[], datetime]] = None,
    max_payload_bytes: int = 2 * 1024 * 1024,
    max_nesting: int = 32,
    replay_capacity: int = 32,
    max_subscribers: int = 64,
    subscriber_queue_size: int = 8,
    heartbeat_interval: float = 15.0,
    poll_interval: float = 0.05,
) -> SemanticObservatoryGatewayRuntime:
    """Build the fail-closed single-owner semantic gateway."""
    owner = ObservatoryPublicationOwner(
        Path(state_path),
        now=now,
        max_payload_bytes=max_payload_bytes,
        max_nesting=max_nesting,
        replay_capacity=replay_capacity,
        max_subscribers=max_subscribers,
        subscriber_queue_size=subscriber_queue_size,
    )
    try:
        app = ObservatoryASGIApplication(
            owner,
            read_policy=read_policy,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
        )
    except Exception:
        owner.close()
        raise
    return SemanticObservatoryGatewayRuntime(owner=owner, app=app)
