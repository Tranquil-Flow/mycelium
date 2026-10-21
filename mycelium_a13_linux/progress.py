# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.onboarding_progress.v1`` -- the privacy-reduced A13 ladder.

Authority-derived and monotone: a rung may only be satisfied once every earlier
rung is satisfied, a failure is immutable and stops the ladder, a satisfied rung
cannot regress, and no amount of ladder progress ever sets ``eligibility_granted``
or ``route_ready``.  The public projection carries the rung, its state and its
bounded blocker -- no seed origin, identity, path, or endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import re

PROTOCOL = "mycelium.onboarding_progress.v1"

#: The frozen A13 acceptance inventory's ``onboarding_ladder``, in order.
LADDER: tuple[str, ...] = (
    "package_verification",
    "target_identity",
    "pairing_request",
    "owner_consent_and_invite_authorization",
    "encrypted_invitation_handoff",
    "seed_pin_and_atomic_join",
    "signed_membership",
    "lease",
    "software_update",
    "capability_probe",
    "class_qualification",
    "directed_link_evidence",
    "assignment",
    "artifact_acquisition",
    "load",
    "startup_challenge",
    "deployment_qualification",
    "selection",
)

STATES: tuple[str, ...] = (
    "not_started",
    "in_progress",
    "blocked",
    "satisfied",
    "failed",
)

#: Bounded blocker vocabulary: a rung is blocked by one named, publishable fact.
BLOCKERS: frozenset[str] = frozenset(
    {
        "package_verification_failed",
        "identity_missing",
        "pairing_pending",
        "owner_authorization_pending",
        "handoff_expired",
        "handoff_invalid",
        "seed_pin_mismatch",
        "membership_unreachable",
        "lease_expired",
        "update_unavailable",
        "capability_probe_failed",
        "requalification_required",
        "assignment_absent",
        "artifact_unavailable",
        "load_failed",
        "startup_challenge_failed",
        "qualification_absent",
        "selection_absent",
        "drain_pending",
        "uninstall_pending",
        "network_unreachable",
        "storage_low",
        "revoked_membership",
    }
)

AUTHORITY_PROVENANCE: frozenset[str] = frozenset(
    {"seed_authority_response", "local_join", "installed_agent_health"}
)

_DOCUMENT_FIELDS = frozenset(
    {
        "protocol",
        "member_generation",
        "authority_provenance",
        "rungs",
        "current_rung",
        "eligibility_granted",
        "route_ready",
        "updated_at",
    }
)
_RUNG_FIELDS = frozenset({"rung", "state", "blocker", "updated_at"})
_PROVENANCE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ProgressError(ValueError):
    """Fail-closed onboarding-progress error carrying a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _time(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ProgressError("onboarding_time_invalid")
    return float(value)


def _copy(document: Mapping[str, object]) -> dict:
    return {
        **document,
        "rungs": [dict(entry) for entry in document["rungs"]],  # type: ignore[arg-type]
    }


def _validate(document: object) -> dict:
    if not isinstance(document, Mapping) or set(document) != _DOCUMENT_FIELDS:
        raise ProgressError("onboarding_progress_invalid")
    if document["protocol"] != PROTOCOL:
        raise ProgressError("onboarding_progress_invalid")
    generation = document["member_generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ProgressError("onboarding_member_generation_invalid")
    provenance = document["authority_provenance"]
    if not isinstance(provenance, str) or provenance not in AUTHORITY_PROVENANCE:
        raise ProgressError("onboarding_authority_provenance_invalid")
    if document["eligibility_granted"] is not False or document["route_ready"] is not False:
        raise ProgressError("onboarding_progress_invalid")
    rungs = document["rungs"]
    if not isinstance(rungs, list) or [
        entry.get("rung") if isinstance(entry, Mapping) else None for entry in rungs
    ] != list(LADDER):
        raise ProgressError("onboarding_progress_invalid")
    for entry in rungs:
        if not isinstance(entry, Mapping) or set(entry) != _RUNG_FIELDS:
            raise ProgressError("onboarding_progress_invalid")
        if entry["state"] not in STATES:
            raise ProgressError("onboarding_progress_invalid")
        blocker = entry["blocker"]
        if entry["state"] == "blocked":
            if blocker not in BLOCKERS:
                raise ProgressError("onboarding_blocker_invalid")
        elif blocker is not None:
            raise ProgressError("onboarding_blocker_invalid")
        _time(entry["updated_at"])
    if document["current_rung"] not in LADDER:
        raise ProgressError("onboarding_progress_invalid")
    _time(document["updated_at"])
    return _copy(document)


def _current(document: Mapping[str, object]) -> str:
    for entry in document["rungs"]:  # type: ignore[index]
        if entry["state"] != "satisfied":
            return entry["rung"]
    return LADDER[-1]


def new_progress(
    *,
    member_generation: int,
    authority_provenance: str,
    now: float,
) -> dict:
    """Start the ladder at the first rung with nothing claimed."""

    instant = _time(now)
    document = {
        "protocol": PROTOCOL,
        "member_generation": member_generation,
        "authority_provenance": authority_provenance,
        "rungs": [
            {"rung": rung, "state": "not_started", "blocker": None, "updated_at": instant}
            for rung in LADDER
        ],
        "current_rung": LADDER[0],
        "eligibility_granted": False,
        "route_ready": False,
        "updated_at": instant,
    }
    _validate(document)
    return document


def advance(
    progress: object,
    *,
    rung: str,
    state: str,
    blocker: str | None = None,
    now: float,
) -> dict:
    """Move one rung; refuse anything that would shortcut the ladder."""

    document = _validate(progress)
    if rung not in LADDER:
        raise ProgressError("onboarding_rung_unknown")
    if state not in STATES:
        raise ProgressError("onboarding_rung_state_invalid")
    if state == "blocked":
        if blocker not in BLOCKERS:
            raise ProgressError("onboarding_blocker_invalid")
    elif blocker is not None:
        raise ProgressError("onboarding_blocker_invalid")
    index = LADDER.index(rung)
    for entry in document["rungs"]:
        if entry["state"] == "failed":
            code = (
                "onboarding_failure_immutable"
                if entry["rung"] == rung
                else "onboarding_rung_after_failure"
            )
            raise ProgressError(code)
    for earlier in document["rungs"][:index]:
        if earlier["state"] != "satisfied":
            raise ProgressError("onboarding_rung_out_of_order")
    if document["rungs"][index]["state"] == "satisfied" and state != "satisfied":
        raise ProgressError("onboarding_rung_regression")
    instant = _time(now)
    if instant < float(document["updated_at"]):
        raise ProgressError("onboarding_time_invalid")
    document["rungs"][index] = {
        "rung": rung,
        "state": state,
        "blocker": blocker if state == "blocked" else None,
        "updated_at": instant,
    }
    document["updated_at"] = instant
    document["current_rung"] = _current(document)
    return document


def public_projection(progress: object) -> dict:
    """The publishable view: rung, state and bounded blocker only."""

    document = _validate(progress)
    return {
        "protocol": PROTOCOL,
        "member_generation": document["member_generation"],
        "current_rung": document["current_rung"],
        "rungs": [
            {"rung": entry["rung"], "state": entry["state"], "blocker": entry["blocker"]}
            for entry in document["rungs"]
        ],
        "eligibility_granted": False,
        "route_ready": False,
    }


__all__ = [
    "AUTHORITY_PROVENANCE",
    "BLOCKERS",
    "LADDER",
    "PROTOCOL",
    "STATES",
    "ProgressError",
    "advance",
    "new_progress",
    "public_projection",
]
