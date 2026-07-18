from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from .compiler import EVIDENCE_SCOPE, CapacityProfile


_STATUS_PROTOCOL = "mycelium.device_status.v1"
_PROFILE_REF_PROTOCOL = "mycelium.capacity_profile_ref.v1"


def _profile_summary(profile: CapacityProfile) -> dict[str, Any]:
    return {
        "protocol": _PROFILE_REF_PROTOCOL,
        "profile_digest": profile.profile_digest,
        "max_safe_concurrency": profile.max_safe_concurrency,
        "interactive_concurrency_limit": profile.interactive_concurrency_limit,
        "batch_concurrency_limit": profile.batch_concurrency_limit,
        "evidence_scope": EVIDENCE_SCOPE,
        "route_ready": False,
    }


def status_with_capacity_profile(
    status: Mapping[str, Any],
    profile: CapacityProfile,
    *,
    allow_concurrency_limit_update: bool = False,
) -> dict[str, Any]:
    if allow_concurrency_limit_update is not True:
        raise ValueError("capacity profile status promotion requires explicit authorization")
    if status.get("protocol") != _STATUS_PROTOCOL:
        raise ValueError("status protocol must be mycelium.device_status.v1")
    extensions_value = status.get("extensions", {})
    if not isinstance(extensions_value, Mapping):
        raise ValueError("status extensions must be an object")

    summary = _profile_summary(profile)
    existing = extensions_value.get("capacity_profile")
    if existing is not None and existing != summary:
        raise ValueError("conflicting capacity profile already attached")

    adapted = copy.deepcopy(dict(status))
    extensions = copy.deepcopy(dict(extensions_value))
    extensions["capacity_profile"] = summary
    adapted["extensions"] = extensions
    adapted["concurrency_limit"] = profile.interactive_concurrency_limit
    return adapted
