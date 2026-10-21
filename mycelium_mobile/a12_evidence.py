# SPDX-License-Identifier: AGPL-3.0-only
"""A12 shared mobile evidence contract: closed records, separated authority.

This module is the deterministic half of the shared mobile evidence contract of
``docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md``
section 5, specializing the A9 separated-authority invariants the frozen A9
acceptance inventory names.

Two things live here:

1. The four evidence eligibility levels a platform/runtime can earn, authored as
   typed, closed records: ``registered_with_blocker`` (known but unobserved — an
   explicit blocker, never zero and never "supported"), ``declared`` (a declaration
   is not an observation), ``synthetic_conformance`` (labelled conformance, which can
   never close a native gate) and ``native_observed`` (the only level that may).
   ``evaluate_evidence_level`` dispatches on the evidence it is given.
2. The closed validators for the capability-named records of the specification —
   the per-role qualification record and the owner-private activation qualification
   record — plus the member-signed envelope helpers the gate owns.  Validated
   invariants: the profile and the capability report are signed by the member's own
   key; a report binds the current profile, its generations and its expiry; every
   capability family carries its own source kind, never an inherited one; a
   non-native source kind can never satisfy a native gate; missing observations stay
   ``unknown`` or absent instead of becoming zero; and no owner-private projection
   may carry private device, session or payload material.

What this module cannot claim: nothing here executes on a device.  Every record it
authors carries ``physical_execution`` false, and every validator that requires
physical evidence fails closed on anything this lane can produce.  The physical
positive and negative gates of specification section 11 stay RED.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Any, Mapping, Sequence

import mycelium_mobile.a12_activation as _gate

# The gate owns the vocabulary; this contract re-exports it rather than restating it.
A12ContractError = _gate.A12ContractError
SOURCE_KINDS = _gate.SOURCE_KINDS
NATIVE_SOURCE_KIND = _gate.NATIVE_SOURCE_KIND
NON_NATIVE_SOURCE_KINDS = _gate.NON_NATIVE_SOURCE_KINDS
MOBILE_PLATFORM_FAMILIES = _gate.MOBILE_PLATFORM_FAMILIES
APPLE_PLATFORM_FAMILIES = _gate.APPLE_PLATFORM_FAMILIES
MOBILE_ROLES = _gate.MOBILE_ROLES
REQUIRED_GATE_FAMILIES = _gate.REQUIRED_GATE_FAMILIES
MOBILE_CAPABILITY_FAMILIES = _gate.REQUIRED_GATE_FAMILIES
ROLE_QUALIFICATION_PROTOCOL = _gate.ROLE_QUALIFICATION_PROTOCOL
ACTIVATION_QUALIFICATION_PROTOCOL = _gate.ACTIVATION_QUALIFICATION_PROTOCOL
RUNTIME_OBSERVATION_PROTOCOL = _gate.RUNTIME_OBSERVATION_PROTOCOL
CAPABILITY_REPORT_PROTOCOL = _gate.CAPABILITY_REPORT_PROTOCOL
ANDROID_GENERAL_CLAIM_SCOPE = _gate.ANDROID_GENERAL_CLAIM_SCOPE
ANDROID_FAMILY_BOUND_CLAIM_SCOPE = _gate.ANDROID_FAMILY_BOUND_CLAIM_SCOPE
ANDROID_CLAIM_SCOPES = _gate.ANDROID_CLAIM_SCOPES
#: [2026-09-27 OWNER RULING] the only family reference a ``family_bound`` claim
#: may stand on: the pseudonymous digest of ``INITIAL_BUILD_ANDROID_FAMILY``.
INITIAL_BUILD_ANDROID_FAMILY_REF = (
    "sha256:"
    + hashlib.sha256(_gate.INITIAL_BUILD_ANDROID_FAMILY.encode("utf-8")).hexdigest()
)
ROLE_CLASS_QUALIFIER = _gate.RUNG_AUTHORITIES["capability_eligibility"]

sign_member_profile = _gate.sign_member_profile
verify_member_signed_profile = _gate.verify_member_signed_profile
sign_member_report = _gate.sign_member_report
verify_member_signed_report = _gate.verify_member_signed_report
require_source_kind_per_capability_family = _gate.require_capability_family_sources
capability_family_blockers = _gate.capability_family_blockers
validate_runtime_observation = _gate.validate_mobile_runtime_observation

EVIDENCE_LEVEL_PROTOCOL = "mycelium.mobile_evidence_level.v1"
LEVEL_REGISTERED_WITH_BLOCKER = "registered_with_blocker"
LEVEL_DECLARED = "declared"
LEVEL_SYNTHETIC_CONFORMANCE = "synthetic_conformance"
LEVEL_NATIVE_OBSERVED = "native_observed"
MOBILE_EVIDENCE_LEVELS = (
    LEVEL_REGISTERED_WITH_BLOCKER,
    LEVEL_DECLARED,
    LEVEL_SYNTHETIC_CONFORMANCE,
    LEVEL_NATIVE_OBSERVED,
)
CONFORMANCE_SOURCE_KINDS = ("synthetic_conformance", "termux_conformance")
# The exact source kind(s) each evidence level may be authored from.
LEVEL_SOURCE_KINDS: dict[str, tuple[str, ...]] = {
    LEVEL_REGISTERED_WITH_BLOCKER: ("unknown",),
    LEVEL_DECLARED: ("operator_declared",),
    LEVEL_SYNTHETIC_CONFORMANCE: CONFORMANCE_SOURCE_KINDS,
    LEVEL_NATIVE_OBSERVED: (NATIVE_SOURCE_KIND,),
}
EVIDENCE_LEVEL_FIELDS = frozenset(
    {
        "protocol",
        "level",
        "decision",
        "role",
        "platform_family",
        "source_kind",
        "blockers",
        "metrics",
        "grants",
        "native_evidence_required",
        "cannot_qualify_native_rung",
        "physical_execution",
    }
)

ROLE_QUALIFICATION_FIELDS = frozenset(
    {
        "protocol",
        "authority",
        "role",
        "platform_family",
        "runtime_build_digest",
        "profile_digest",
        "profile_generation",
        "membership_generation",
        "evidence_generation",
        "source_kind",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
        "positive_evidence_digests",
        "negative_evidence_digests",
        "constraints",
        "capability_family_sources",
        "blockers",
    }
)

ACTIVATION_QUALIFICATION_FIELDS = frozenset(
    {
        "protocol",
        "platform_family",
        "roles",
        "native_application_digest",
        "transport_digest",
        "runtime_digest",
        "native_source_kind",
        "membership_generation",
        "profile_generation",
        "evidence_generation",
        "physical_family_refs",
        "executed_positive_cases",
        "executed_negative_cases",
        "privacy_projection",
        "role_carryover_from",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
    }
)
#: Optional closed field of the activation qualification record.  Absent means
#: ``general``: an unscoped record is a general claim (two families on Android).
ACTIVATION_QUALIFICATION_OPTIONAL_FIELDS = frozenset({"claim_scope"})

# Material the privacy-reduced projection must never carry (specification section 5).
PRIVATE_PROJECTION_KEYS = frozenset(
    {
        "endpoint_id",
        "endpoint_addr",
        "invite_nonce",
        "invite_secret",
        "signing_key",
        "private_key",
        "advertising_id",
        "phone_number",
        "username",
        "account_id",
        "account_identifier",
        "serial_number",
        "precise_location",
        "latitude",
        "longitude",
        "ssid",
        "private_path",
        "hostname",
        "ip_address",
        "mac_address",
        "imei",
        "prompt",
        "prompts",
        "output",
        "outputs",
        "token_ids",
        "tensors",
        "activations",
        "kv",
        "kv_content",
        "logs",
    }
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FAMILY_REFERENCE = re.compile(r"(?:sha256:[0-9a-f]{64}|family:[0-9a-f]{16,64})\Z")
_KEY_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
DECLARATION_MAX_FIELDS = 16


def _reject(code: str) -> None:
    raise A12ContractError(code)


def _finite(value: object, *, code: str, minimum: float | None = None) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        _reject(code)
    number = float(value)
    if minimum is not None and number < minimum:
        _reject(code)
    return number


def _now_ms(value: object) -> float:
    if value is None:
        return time.time() * 1000.0
    return _finite(value, code="mobile_evidence_time_invalid", minimum=0.0)


def _text(value: object, *, code: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _reject(code)
    return value


def _digest(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _DIGEST.match(value):
        _reject(code)
    return value


def _counter(value: object, *, code: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _reject(code)
    return value


def _closed(document: object, *, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != set(fields):
        _reject(code)
    return dict(document)


def _closed_projection(value: object, *, code: str) -> None:
    """Refuse any private material, at any depth, in an owner-private projection."""

    if isinstance(value, Mapping):
        for name, nested in value.items():
            if not isinstance(name, str) or name.lower() in PRIVATE_PROJECTION_KEYS:
                _reject(code)
            _closed_projection(nested, code=code)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            _closed_projection(nested, code=code)
    elif value is None or isinstance(value, (bool, int, float, str)):
        return
    else:
        _reject(code)


# ---------------------------------------------------------------------------
# The four evidence eligibility levels
# ---------------------------------------------------------------------------


def _level_record(
    *,
    level: str,
    decision: str,
    role: str,
    platform_family: str,
    source_kind: str,
    blockers: Sequence[str],
    metrics: Mapping[str, object] | None,
    cannot_qualify_native_rung: bool,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "protocol": EVIDENCE_LEVEL_PROTOCOL,
        "level": level,
        "decision": decision,
        "role": role,
        "platform_family": platform_family,
        "source_kind": source_kind,
        "blockers": list(blockers),
        "metrics": dict(metrics or {}),
        "grants": [],
        "native_evidence_required": True,
        "cannot_qualify_native_rung": cannot_qualify_native_rung,
        "physical_execution": False,
    }
    if level not in LEVEL_SOURCE_KINDS or source_kind not in LEVEL_SOURCE_KINDS[level]:
        _reject("mobile_evidence_level_source_kind_invalid")
    claims_native_closure = cannot_qualify_native_rung is False
    if claims_native_closure != (level == LEVEL_NATIVE_OBSERVED):
        _reject("mobile_evidence_level_claim_invalid")
    return _closed(record, fields=EVIDENCE_LEVEL_FIELDS, code="mobile_evidence_level_fields_invalid")


def _require_role(role: object) -> str:
    if role not in MOBILE_ROLES:
        _reject("mobile_evidence_role_invalid")
    return str(role)


def _require_declared_platform(platform_family: object) -> str:
    if platform_family not in MOBILE_PLATFORM_FAMILIES:
        _reject("mobile_evidence_platform_invalid")
    return str(platform_family)


def author_registered_with_blocker_level(
    *, platform_family: object, role: object, blocker: object = None
) -> dict[str, Any]:
    """A registered platform/runtime with no observation: visible, ineligible.

    An unregistered platform value is refused rather than converted; the registered
    ``unknown`` sentinel stays visible with an explicit blocker
    (A9 ``unknown_visible_with_blocker``, ``unknown_activation_ineligible``).
    """

    exact_role = _require_role(role)
    if platform_family == "unknown":
        platform = "unknown"
        blockers = [_gate.PLATFORM_UNKNOWN_BLOCKER]
    else:
        platform = _require_declared_platform(platform_family)
        blockers = []
    blockers.append(_gate.REGISTERED_UNOBSERVED_BLOCKER)
    if blocker is not None:
        blockers.append(_text(blocker, code="mobile_evidence_blocker_invalid", maximum=128))
    return _level_record(
        level=LEVEL_REGISTERED_WITH_BLOCKER,
        decision="ineligible",
        role=exact_role,
        platform_family=platform,
        source_kind="unknown",
        blockers=blockers,
        metrics=None,
        cannot_qualify_native_rung=True,
    )


def author_declared_level(
    *, platform_family: object, role: object, declaration: object
) -> dict[str, Any]:
    """A declaration is not an observation and cannot advance anything."""

    exact_role = _require_role(role)
    platform = _require_declared_platform(platform_family)
    if not isinstance(declaration, Mapping) or not declaration:
        _reject("mobile_evidence_declaration_invalid")
    if len(declaration) > DECLARATION_MAX_FIELDS:
        _reject("mobile_evidence_declaration_invalid")
    for name in declaration:
        if not isinstance(name, str) or not _KEY_NAME.match(name):
            _reject("mobile_evidence_declaration_invalid")
    return _level_record(
        level=LEVEL_DECLARED,
        decision="ineligible",
        role=exact_role,
        platform_family=platform,
        source_kind="operator_declared",
        blockers=[_gate.DECLARATION_BLOCKER, _gate.REGISTERED_UNOBSERVED_BLOCKER],
        metrics=None,
        cannot_qualify_native_rung=True,
    )


def author_synthetic_conformance_level(
    *, platform_family: object, role: object, observation: object
) -> dict[str, Any]:
    """Synthetic or Termux conformance stays labelled and never becomes native."""

    exact_role = _require_role(role)
    platform = _require_declared_platform(platform_family)
    validated = validate_runtime_observation(observation)
    source_kind = str(validated["source_kind"])
    if source_kind not in CONFORMANCE_SOURCE_KINDS:
        _reject("evidence_level_requires_its_own_source_kind")
    if validated["platform_family"] != platform:
        _reject("mobile_evidence_platform_mismatch")
    return _level_record(
        level=LEVEL_SYNTHETIC_CONFORMANCE,
        decision="ineligible",
        role=exact_role,
        platform_family=platform,
        source_kind=source_kind,
        blockers=[_gate.SYNTHETIC_NATIVE_BLOCKER],
        metrics=validated["metrics"],
        cannot_qualify_native_rung=True,
    )


def author_native_observed_level(
    *, platform_family: object, role: object, observation: object
) -> dict[str, Any]:
    """The only level that may close a native gate — earned by native observation.

    ``physical_execution`` stays false: this record is authored from a native
    observation, not by executing an operation in this process.
    """

    exact_role = _require_role(role)
    platform = _require_declared_platform(platform_family)
    validated = validate_runtime_observation(observation)
    _gate.require_native_observation(validated["source_kind"])
    if validated["platform_family"] != platform:
        _reject("mobile_evidence_platform_mismatch")
    return _level_record(
        level=LEVEL_NATIVE_OBSERVED,
        decision="eligible",
        role=exact_role,
        platform_family=platform,
        source_kind=str(validated["source_kind"]),
        blockers=[],
        metrics=validated["metrics"],
        cannot_qualify_native_rung=False,
    )


def evaluate_evidence_level(
    *,
    platform_family: object,
    role: object,
    declaration: object = None,
    observation: object = None,
) -> dict[str, Any]:
    """Author the exact evidence level the supplied evidence earns, and no more."""

    if observation is None:
        if declaration is None:
            return author_registered_with_blocker_level(
                platform_family=platform_family, role=role
            )
        return author_declared_level(
            platform_family=platform_family, role=role, declaration=declaration
        )
    validated = validate_runtime_observation(observation)
    source_kind = str(validated["source_kind"])
    if source_kind == NATIVE_SOURCE_KIND:
        return author_native_observed_level(
            platform_family=platform_family, role=role, observation=validated
        )
    if source_kind in CONFORMANCE_SOURCE_KINDS:
        return author_synthetic_conformance_level(
            platform_family=platform_family, role=role, observation=validated
        )
    if source_kind == "operator_declared":
        return author_declared_level(
            platform_family=platform_family,
            role=role,
            declaration={"member_ref": "declared", "declared_source_kind": source_kind},
        )
    return author_registered_with_blocker_level(
        platform_family="unknown" if platform_family not in MOBILE_PLATFORM_FAMILIES else platform_family,
        role=role,
        blocker="evidence_source_kind_unknown",
    )


# ---------------------------------------------------------------------------
# Per-role qualification record (mycelium.mobile_role_qualification.v1)
# ---------------------------------------------------------------------------


def validate_mobile_role_qualification(
    record: object, *, now_unix_ms: object = None
) -> dict[str, Any]:
    """Validate one closed per-role qualification record.

    It binds the current profile digest and generations, carries both positive and
    negative evidence digests, and keeps every capability family's own source kind.
    A non-native source kind is refused outright, and an expired record is refused
    instead of being read as current.
    """

    validated = _closed(
        record,
        fields=ROLE_QUALIFICATION_FIELDS,
        code="mobile_role_qualification_fields_invalid",
    )
    if validated["protocol"] != ROLE_QUALIFICATION_PROTOCOL:
        _reject("mobile_role_qualification_protocol_invalid")
    if validated["authority"] != ROLE_CLASS_QUALIFIER:
        _reject("mobile_role_qualification_authority_invalid")
    if validated["role"] not in MOBILE_ROLES:
        _reject("mobile_role_qualification_role_invalid")
    if validated["platform_family"] not in MOBILE_PLATFORM_FAMILIES:
        _reject("mobile_role_qualification_platform_invalid")
    _digest(validated["runtime_build_digest"], code="mobile_role_qualification_build_invalid")
    _digest(
        validated["profile_digest"], code="mobile_role_qualification_profile_binding_invalid"
    )
    profile_generation = _counter(
        validated["profile_generation"], code="mobile_role_qualification_generation_invalid"
    )
    membership_generation = _counter(
        validated["membership_generation"], code="mobile_role_qualification_generation_invalid"
    )
    _counter(
        validated["evidence_generation"], code="mobile_role_qualification_generation_invalid"
    )
    if profile_generation != membership_generation:
        _reject("mobile_role_qualification_generation_invalid")
    if validated["source_kind"] != NATIVE_SOURCE_KIND:
        if validated["source_kind"] in SOURCE_KINDS:
            _reject("non_native_source_cannot_qualify")
        _reject("mobile_role_qualification_source_kind_invalid")
    for name in ("positive_evidence_digests", "negative_evidence_digests"):
        digests = validated[name]
        if (
            isinstance(digests, (str, bytes))
            or not isinstance(digests, Sequence)
            or not digests
        ):
            _reject("mobile_role_qualification_evidence_digest_invalid")
        for digest in digests:
            _digest(digest, code="mobile_role_qualification_evidence_digest_invalid")
    constraints = validated["constraints"]
    if not isinstance(constraints, Mapping) or not constraints:
        _reject("mobile_role_qualification_constraints_invalid")
    for name, value in constraints.items():
        if not isinstance(name, str) or not _KEY_NAME.match(name):
            _reject("mobile_role_qualification_constraints_invalid")
        if value is not None and not isinstance(value, (bool, int, float, str)):
            _reject("mobile_role_qualification_constraints_invalid")
        if isinstance(value, float):
            _finite(value, code="mobile_role_qualification_constraints_invalid")
    require_source_kind_per_capability_family(validated["capability_family_sources"])
    blockers = validated["blockers"]
    if isinstance(blockers, (str, bytes)) or not isinstance(blockers, Sequence):
        _reject("mobile_role_qualification_blockers_invalid")
    for blocker in blockers:
        _text(blocker, code="mobile_role_qualification_blockers_invalid", maximum=128)
    issued = _finite(
        validated["issued_at_unix_ms"],
        code="mobile_role_qualification_validity_invalid",
        minimum=0.0,
    )
    expires = _finite(
        validated["expires_at_unix_ms"],
        code="mobile_role_qualification_validity_invalid",
        minimum=0.0,
    )
    if expires <= issued:
        _reject("mobile_role_qualification_validity_invalid")
    if expires <= _now_ms(now_unix_ms):
        _reject("mobile_role_qualification_expired")
    return validated


# ---------------------------------------------------------------------------
# Owner-private activation qualification record
# (mycelium.mobile_activation_qualification.v1)
# ---------------------------------------------------------------------------


def validate_mobile_activation_qualification(
    record: object, *, now_unix_ms: object = None
) -> dict[str, Any]:
    """Validate one owner-private activation qualification bundle.

    A bundle with zero executed cases is not physical evidence; a synthetic source
    kind can never satisfy the physical gate; every device family is a pseudonymous
    reference and a general Android claim needs two independent ones; an Apple
    platform never inherits its sibling's qualification; and the privacy-reduced
    projection may not carry private material at any depth.

    [2026-09-27 OWNER RULING] The optional closed ``claim_scope`` field carries
    the claim being asserted.  Absent or ``general`` keeps the two-family Android
    requirement.  ``family_bound`` is Android-only and stands on exactly one
    family reference, ``INITIAL_BUILD_ANDROID_FAMILY_REF``; any other or any
    additional reference is refused, so no second family (Termux, simulator or
    another phone) can be relabelled into a family-bound claim.
    """

    fields = ACTIVATION_QUALIFICATION_FIELDS
    if isinstance(record, Mapping) and "claim_scope" in record:
        fields = fields | ACTIVATION_QUALIFICATION_OPTIONAL_FIELDS
    validated = _closed(
        record,
        fields=fields,
        code="mobile_activation_qualification_fields_invalid",
    )
    if validated["protocol"] != ACTIVATION_QUALIFICATION_PROTOCOL:
        _reject("mobile_activation_qualification_protocol_invalid")
    claim_scope = validated.get("claim_scope", ANDROID_GENERAL_CLAIM_SCOPE)
    if not isinstance(claim_scope, str) or claim_scope not in ANDROID_CLAIM_SCOPES:
        _reject("mobile_activation_claim_scope_invalid")
    platform = validated["platform_family"]
    if platform not in MOBILE_PLATFORM_FAMILIES:
        _reject("mobile_activation_qualification_platform_invalid")
    roles = validated["roles"]
    if isinstance(roles, (str, bytes)) or not isinstance(roles, Sequence) or not roles:
        _reject("mobile_activation_qualification_role_invalid")
    for role in roles:
        if role not in MOBILE_ROLES:
            _reject("mobile_activation_qualification_role_invalid")
    for name in (
        "native_application_digest",
        "transport_digest",
        "runtime_digest",
    ):
        _digest(validated[name], code="mobile_activation_qualification_digest_invalid")
    if validated["native_source_kind"] != NATIVE_SOURCE_KIND:
        if validated["native_source_kind"] in SOURCE_KINDS:
            _reject("synthetic_conformance_cannot_satisfy_physical_gate")
        _reject("mobile_activation_qualification_source_kind_invalid")
    _counter(
        validated["membership_generation"],
        code="mobile_activation_qualification_generation_invalid",
    )
    profile_generation = _counter(
        validated["profile_generation"],
        code="mobile_activation_qualification_generation_invalid",
    )
    _counter(
        validated["evidence_generation"],
        code="mobile_activation_qualification_generation_invalid",
    )
    if profile_generation != validated["membership_generation"]:
        _reject("mobile_activation_qualification_generation_invalid")
    positive = _counter(
        validated["executed_positive_cases"], code="mobile_activation_cases_required"
    )
    negative = _counter(
        validated["executed_negative_cases"], code="mobile_activation_cases_required"
    )
    if positive < 1 or negative < 1:
        _reject("mobile_activation_cases_required")

    references = validated["physical_family_refs"]
    if (
        isinstance(references, (str, bytes))
        or not isinstance(references, Sequence)
        or not references
    ):
        _reject("mobile_activation_family_reference_invalid")
    for reference in references:
        if not isinstance(reference, str) or not _FAMILY_REFERENCE.match(reference):
            _reject("mobile_activation_family_reference_invalid")
    if claim_scope == ANDROID_FAMILY_BOUND_CLAIM_SCOPE and platform != "android":
        # The family-bound narrowing is an Android initial-build ruling only.
        _reject("mobile_activation_claim_scope_invalid")
    if platform == "android":
        if claim_scope == ANDROID_FAMILY_BOUND_CLAIM_SCOPE and list(references) != [
            INITIAL_BUILD_ANDROID_FAMILY_REF
        ]:
            _reject("android_family_bound_claim_family_mismatch")
        _gate.require_android_family_evidence(references, claim_scope=claim_scope)
    else:
        carryover = validated["role_carryover_from"]
        if carryover is not None:
            if carryover not in APPLE_PLATFORM_FAMILIES or carryover == platform:
                _reject("apple_platform_family_invalid")
            _gate.require_apple_platform_isolation(platform, "qualified")

    _closed_projection(
        validated["privacy_projection"], code="mobile_activation_private_material_present"
    )
    issued = _finite(
        validated["issued_at_unix_ms"],
        code="mobile_activation_qualification_validity_invalid",
        minimum=0.0,
    )
    expires = _finite(
        validated["expires_at_unix_ms"],
        code="mobile_activation_qualification_validity_invalid",
        minimum=0.0,
    )
    if expires <= issued:
        _reject("mobile_activation_qualification_validity_invalid")
    if expires <= _now_ms(now_unix_ms):
        _reject("mobile_activation_qualification_expired")
    return validated


__all__ = [
    "ACTIVATION_QUALIFICATION_FIELDS",
    "ACTIVATION_QUALIFICATION_OPTIONAL_FIELDS",
    "ACTIVATION_QUALIFICATION_PROTOCOL",
    "ANDROID_CLAIM_SCOPES",
    "ANDROID_FAMILY_BOUND_CLAIM_SCOPE",
    "ANDROID_GENERAL_CLAIM_SCOPE",
    "A12ContractError",
    "APPLE_PLATFORM_FAMILIES",
    "CAPABILITY_REPORT_PROTOCOL",
    "CONFORMANCE_SOURCE_KINDS",
    "EVIDENCE_LEVEL_FIELDS",
    "EVIDENCE_LEVEL_PROTOCOL",
    "INITIAL_BUILD_ANDROID_FAMILY_REF",
    "LEVEL_DECLARED",
    "LEVEL_NATIVE_OBSERVED",
    "LEVEL_REGISTERED_WITH_BLOCKER",
    "LEVEL_SOURCE_KINDS",
    "LEVEL_SYNTHETIC_CONFORMANCE",
    "MOBILE_CAPABILITY_FAMILIES",
    "MOBILE_EVIDENCE_LEVELS",
    "MOBILE_PLATFORM_FAMILIES",
    "MOBILE_ROLES",
    "NATIVE_SOURCE_KIND",
    "NON_NATIVE_SOURCE_KINDS",
    "PRIVATE_PROJECTION_KEYS",
    "REQUIRED_GATE_FAMILIES",
    "ROLE_QUALIFICATION_FIELDS",
    "ROLE_QUALIFICATION_PROTOCOL",
    "RUNTIME_OBSERVATION_PROTOCOL",
    "SOURCE_KINDS",
    "author_declared_level",
    "author_native_observed_level",
    "author_registered_with_blocker_level",
    "author_synthetic_conformance_level",
    "capability_family_blockers",
    "evaluate_evidence_level",
    "require_source_kind_per_capability_family",
    "sign_member_profile",
    "sign_member_report",
    "validate_mobile_activation_qualification",
    "validate_mobile_role_qualification",
    "validate_runtime_observation",
    "verify_member_signed_profile",
    "verify_member_signed_report",
]
