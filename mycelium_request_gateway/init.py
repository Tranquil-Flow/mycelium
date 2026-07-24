"""Composition root for the isolated authenticated request gateway."""
from __future__ import annotations

from collections.abc import Callable
import time
import uuid

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
) -> RequestGatewayASGIApplication:
    """Build production adapters around a qualifier-owned current-record source.

    The source must return an already validated RouteQualificationV1 object from
    the qualifier authority. This module deliberately does not deserialize or
    promote route_ready records on the authority's behalf.
    """
    backend = RouterSessionBackend(
        router=router,
        codec=codec,
        clock=clock,
        qualification_source=qualification_source,
    )
    service = RequestGatewayService(
        qualification_source=qualification_source,
        backend=backend,
        request_id_source=request_id_source or (lambda: str(uuid.uuid4())),
        max_buffered_events=max_buffered_events,
        max_sessions=max_sessions,
    )
    return RequestGatewayASGIApplication(
        service,
        authenticator=StaticBearerAuthenticator(bearer_token),
    )
