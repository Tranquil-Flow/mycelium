# SPDX-License-Identifier: AGPL-3.0-only
"""The frozen ``mycelium.onboarding_progress.v1`` ladder for the Windows tranche.

The ladder is platform-neutral: it is one frozen protocol shared by the macOS
slice, the landed Linux slice, and this Windows slice.  The Windows tranche
therefore *reuses* the landed implementation instead of forking a second copy of
a frozen acceptance contract; ``mycelium_a13_linux.progress`` is read-only from
here and is never edited.  Windows adds no rung, no blocker, and no way to grant
eligibility: ``eligibility_granted`` and ``route_ready`` stay false forever.
"""

from __future__ import annotations

from mycelium_a13_linux.progress import (
    AUTHORITY_PROVENANCE,
    BLOCKERS,
    LADDER,
    PROTOCOL,
    STATES,
    ProgressError,
    advance,
    new_progress,
    public_projection,
)

#: Which platform tranche supplies the shared implementation.  Recorded so the
#: reuse is visible rather than implicit.
PROGRESS_IMPLEMENTATION_OWNER = "mycelium_a13_linux.progress"

__all__ = [
    "AUTHORITY_PROVENANCE",
    "BLOCKERS",
    "LADDER",
    "PROGRESS_IMPLEMENTATION_OWNER",
    "PROTOCOL",
    "STATES",
    "ProgressError",
    "advance",
    "new_progress",
    "public_projection",
]
