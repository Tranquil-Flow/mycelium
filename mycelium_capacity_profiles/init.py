from __future__ import annotations

from .catalog import CapacityProfileCatalog, CapacityProfileCatalogPolicy


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


__all__ = ["initialize_capacity_profile_catalog"]
