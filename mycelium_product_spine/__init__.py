# SPDX-License-Identifier: AGPL-3.0-only
"""Closed read-only M12 product snapshot and event contracts."""

from .contracts import (
    ENTITY_KINDS,
    ProductContractError,
    validate_product_event,
    validate_product_snapshot,
)
from .projector import ProductProjector
from .app import ProductEvidenceApplication, ProductSourceUnavailable
from .state import ProductEvidenceStateError, ProductEvidenceStateStore

__all__ = [
    "ENTITY_KINDS",
    "ProductContractError",
    "ProductEvidenceApplication",
    "ProductEvidenceStateError",
    "ProductEvidenceStateStore",
    "ProductProjector",
    "ProductSourceUnavailable",
    "validate_product_event",
    "validate_product_snapshot",
]
