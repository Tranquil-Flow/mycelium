# SPDX-License-Identifier: AGPL-3.0-only
"""Local interactive browser-swarm qualification surface.

This package never establishes production route readiness. All public evidence
is local-only and carries ``route_ready=false``.
"""

from .swarm import (
    BrowserStageResult,
    Invitation,
    JoinGrant,
    SwarmCoordinator,
    SwarmError,
    normalize_public_origin,
)

__all__ = [
    "BrowserStageResult",
    "Invitation",
    "JoinGrant",
    "SwarmCoordinator",
    "SwarmError",
    "normalize_public_origin",
]
