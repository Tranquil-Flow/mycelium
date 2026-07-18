from .compiler import CapacityProfile, compile_capacity_profile
from .catalog import (
    CapacityProfileCatalog,
    CapacityProfileCatalogPolicy,
    CapacityProfileCatalogState,
    CapacityProfileSlot,
    CatalogInsertAction,
    CatalogInsertResult,
    CatalogLookup,
)
from .contracts import (
    CapacityObservation,
    CapacityProfileKey,
    CapacityProfilePolicy,
    EvaluatedCapacityObservation,
    canonical_json_bytes,
)
from .document import MAX_PROFILE_DOCUMENT_BYTES, parse_capacity_profile_bytes
from .status import status_with_capacity_profile


def initialize_capacity_profile_catalog(
    *,
    max_entries: int,
    max_ttl: float,
) -> CapacityProfileCatalog:
    """Create one isolated process-local catalog from explicit immutable policy."""

    return CapacityProfileCatalog(
        CapacityProfileCatalogPolicy(
            max_entries=max_entries,
            max_ttl=max_ttl,
        )
    )

__all__ = [
    "CapacityObservation",
    "CapacityProfile",
    "CapacityProfileCatalog",
    "CapacityProfileCatalogPolicy",
    "CapacityProfileCatalogState",
    "CapacityProfileKey",
    "CapacityProfilePolicy",
    "CapacityProfileSlot",
    "CatalogInsertAction",
    "CatalogInsertResult",
    "CatalogLookup",
    "EvaluatedCapacityObservation",
    "MAX_PROFILE_DOCUMENT_BYTES",
    "canonical_json_bytes",
    "compile_capacity_profile",
    "initialize_capacity_profile_catalog",
    "parse_capacity_profile_bytes",
    "status_with_capacity_profile",
]
