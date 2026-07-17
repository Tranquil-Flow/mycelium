"""Explicit construction seam for the read-only Observatory gateway."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional, Union

from .asgi import ObservatoryASGIApplication, ReadPolicy
from .observatory import CoherentSnapshotPublisher


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
