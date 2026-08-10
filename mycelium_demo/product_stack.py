"""Host-local product composition over the existing production gateways."""
from __future__ import annotations

from typing import Any

from mycelium_request_gateway import create_request_gateway_application
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


def build_loopback_product_stack(
    *,
    qualification_source: Any,
    router: Any,
    codec: Any,
    observatory_app: Any,
    product_app: Any | None = None,
    swarm_coordinator: Any,
    request_bearer_token: str,
) -> Any:
    """Compose the browser gateway and request gateway on loopback only."""
    request_gateway_app = create_request_gateway_application(
        qualification_source=qualification_source,
        router=router,
        codec=codec,
        bearer_token=request_bearer_token,
    )
    return create_product_gateway_application(
        config=GatewayConfig(bind_host="127.0.0.1"),
        observatory_app=observatory_app,
        product_app=product_app,
        request_gateway_app=request_gateway_app,
        swarm_coordinator=swarm_coordinator,
        request_gateway_bearer_token=request_bearer_token,
    )


__all__ = ["build_loopback_product_stack"]
