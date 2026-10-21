# SPDX-License-Identifier: AGPL-3.0-only
"""Browser-safe product gateway composition package."""

from .app import ProductGatewayASGIApplication, create_product_gateway_application
from .config import GatewayConfig
from .coordinator import CoordinatorError, SwarmCoordinator

__all__ = [
    "CoordinatorError",
    "GatewayConfig",
    "ProductGatewayASGIApplication",
    "SwarmCoordinator",
    "create_product_gateway_application",
]
