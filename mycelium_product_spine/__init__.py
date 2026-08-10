"""Closed read-only M12 product snapshot and event contracts."""

from .contracts import (
    ENTITY_KINDS,
    ProductContractError,
    validate_product_event,
    validate_product_snapshot,
)
from .projector import ProductProjector
from .app import ProductEvidenceApplication
from .state import ProductEvidenceStateError, ProductEvidenceStateStore

__all__ = [
    "ENTITY_KINDS",
    "ProductContractError",
    "ProductEvidenceApplication",
    "ProductEvidenceStateError",
    "ProductEvidenceStateStore",
    "ProductProjector",
    "validate_product_event",
    "validate_product_snapshot",
]
