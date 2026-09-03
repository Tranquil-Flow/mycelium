"""Composition root for the isolated authenticated request gateway."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import time
import uuid
from typing import Any

from .asgi import RequestGatewayASGIApplication
from .auth import StaticBearerAuthenticator
from .backend import PromptCodec, RouterPort, RouterSessionBackend
from .qualification import QualificationSource
from .service import RequestGatewayService


def create_request_gateway_application(
    *,
    qualification_source: QualificationSource,
    router: RouterPort,
    codec: PromptCodec,
    bearer_token: str,
    clock: Callable[[], float] = time.monotonic,
    request_id_source: Callable[[], str] | None = None,
    max_buffered_events: int = 64,
    max_sessions: int = 1_024,
    replica_qualifications_factory: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    replica_loss_placement_ids_factory: Callable[[], frozenset[str]] | None = None,
    replica_now_unix_ms: Callable[[], int] | None = None,
) -> RequestGatewayASGIApplication:
    """Build production adapters around a qualifier-owned current-record source.

    The source must return an already validated RouteQualificationV1 object from
    the qualifier authority. This module deliberately does not deserialize or
    promote route_ready records on the authority's behalf.

    A5 may optionally provide replica qualification and replica-loss factories.
    When present, the request backend remains A4's RouterSessionBackend under
    the hood: A5 only selects an already-qualified track and passes the
    resulting selected/excluded placements into A4's normal admission path.
    """
    plain_backend = RouterSessionBackend(
        router=router,
        codec=codec,
        clock=clock,
        qualification_source=qualification_source,
    )
    if replica_qualifications_factory is not None:
        # Lazy import: this module is imported from mycelium_qualification
        # in production composition paths, so a top-level import would be
        # circular (replica_track_backend imports the request gateway backend).
        from mycelium_qualification.replica_track_backend import (
            ReplicaTrackDispatcher,
            ReplicaTrackSessionBackend,
        )

        if replica_loss_placement_ids_factory is None:
            replica_loss_placement_ids_factory = frozenset
        dispatcher = ReplicaTrackDispatcher(
            router=router,
            codec=codec,
            clock=clock,
            qualification_source=qualification_source,
            replica_qualifications_factory=replica_qualifications_factory,
            replica_loss_placement_ids_factory=replica_loss_placement_ids_factory,
        )
        backend = ReplicaTrackSessionBackend(
            dispatcher=dispatcher,
            now_unix_ms=replica_now_unix_ms
            or (lambda: int(time.time() * 1_000)),
            plain_backend=plain_backend,
        )
    else:
        backend = plain_backend
    service = RequestGatewayService(
        qualification_source=qualification_source,
        backend=backend,
        request_id_source=request_id_source or (lambda: str(uuid.uuid4())),
        max_buffered_events=max_buffered_events,
        max_sessions=max_sessions,
        clock=clock,
    )
    return RequestGatewayASGIApplication(
        service,
        authenticator=StaticBearerAuthenticator(bearer_token),
    )
