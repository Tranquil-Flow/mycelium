"""Host-local product composition over the existing production gateways."""
from __future__ import annotations

import hmac
from typing import Any

from mycelium_request_gateway import create_request_gateway_application
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application


_AUTHENTICATED_USER_HEADER = b"x-mycelium-authenticated-user"
_PROXY_CAPABILITY_HEADER = b"x-mycelium-proxy-capability"


def _trusted_proxy_access_policy(capability: bytes) -> Any:
    """Require proxy identity plus an owner-private loopback capability."""

    def authorize(scope: Any) -> bool:
        headers = scope.get("headers", ()) if isinstance(scope, dict) else ()
        if not isinstance(headers, (list, tuple)):
            return False
        identities: list[bytes] = []
        capabilities: list[bytes] = []
        for item in headers:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not isinstance(item[0], bytes)
                or not isinstance(item[1], bytes)
            ):
                return False
            name = item[0].lower()
            if name == _AUTHENTICATED_USER_HEADER:
                identities.append(item[1])
            elif name == _PROXY_CAPABILITY_HEADER:
                capabilities.append(item[1])
        return (
            len(identities) == 1
            and identities[0] == b"owner"
            and len(capabilities) == 1
            and hmac.compare_digest(capabilities[0], capability)
        )

    return authorize


def build_loopback_product_stack(
    *,
    qualification_source: Any,
    router: Any,
    codec: Any,
    observatory_app: Any,
    product_app: Any | None = None,
    swarm_coordinator: Any,
    request_bearer_token: str,
    public_origin: str | None = None,
    trusted_https_proxy: bool = False,
    trusted_proxy_capability: bytes | None = None,
) -> Any:
    """Compose the browser gateway on loopback, optionally behind an explicit HTTPS proxy."""
    if trusted_https_proxy:
        if (
            not isinstance(trusted_proxy_capability, bytes)
            or len(trusted_proxy_capability) != 64
            or any(value not in b"0123456789abcdef" for value in trusted_proxy_capability)
        ):
            raise ValueError("invalid_trusted_proxy_capability")
    elif trusted_proxy_capability is not None:
        raise ValueError("trusted_proxy_capability_requires_trusted_https_proxy")
    request_gateway_app = create_request_gateway_application(
        qualification_source=qualification_source,
        router=router,
        codec=codec,
        bearer_token=request_bearer_token,
    )
    return create_product_gateway_application(
        config=GatewayConfig(
            bind_host="127.0.0.1",
            tls_enabled=trusted_https_proxy,
            public_origin=public_origin,
            trusted_https_proxy=trusted_https_proxy,
        ),
        observatory_app=observatory_app,
        product_app=product_app,
        request_gateway_app=request_gateway_app,
        swarm_coordinator=swarm_coordinator,
        request_gateway_bearer_token=request_bearer_token,
        access_policy=(
            _trusted_proxy_access_policy(trusted_proxy_capability)
            if trusted_https_proxy and trusted_proxy_capability is not None
            else None
        ),
    )


__all__ = ["build_loopback_product_stack"]
