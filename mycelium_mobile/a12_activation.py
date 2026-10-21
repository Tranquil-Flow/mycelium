# SPDX-License-Identifier: AGPL-3.0-only
"""A12 gate: deterministic mobile rungs, evidence records and honest refusals.

This module is the A12 gate machine source named by the implementation contract
(``docs/superpowers/specs/2026-09-21-mycelium-a12-implementation-contract.md``).

IMPLEMENTED (deterministic half, no device executes anything here)

- the four separately authored eligibility/readiness rungs of specification
  section 4, one authoring surface per authority: rung 1 ``signed_membership``
  (``a9_membership_authority``), rung 2 ``capability_eligibility``
  (``a9_role_class_qualifier``), rung 3 ``artifact_readiness`` (``a2_provisioner``)
  and rung 4 ``operation_qualification`` (``mobile_role_operation_qualifier``);
- the closed rung-proof record (``RUNG_PROOF_PROTOCOL``) each of them authors,
  carrying its authority, its single grant, the later rungs it may never grant,
  its blockers and its generations;
- member-signed control-plane envelopes: a member-signed v2 peer profile and a
  member-signed capability report, both verified against the A9 envelope and
  Ed25519 evidence-signing machinery the A9 gate already ships;
- the fail-closed rung arithmetic: a rung that cannot be authored records
  ``withheld``/``unknown``/``revoked`` plus the exact missing input, and missing
  evidence is ``None``/``unknown`` — never zero, healthy or inferred support.

DECIDED FROM EXECUTED EVIDENCE (Android only; Apple mobile deferred by ruling R2)

- ``observe_mobile_runtime`` accepts a runtime-owned observation only inside the
  member-signed envelope the native core seals (``native/mobile_core``), bound to the
  enrolled member key, native, fresh and for the exact platform and build;
- ``qualify_mobile_operation`` re-verifies those envelopes and decides the exact
  native Android role from them plus the owner-private executed physical gate
  bundle, emitting one closed ``mycelium.mobile_role_qualification.v1`` record.

REFUSED (still RED by design)

Called without executed evidence, the observation and operation seams refuse with
``not_implemented:<code>``: there is nothing to accept or qualify.  The optional A11
draft binding, the all-eight product projection and the owner-private physical gate
are NOT implemented here; each validates its closed input shape and then raises
``A12NotImplementedError``; none returns a value and none records success.  The
deterministic ``author_operation_qualification`` still never authors rung 4: the
physical evidence path is ``qualify_mobile_operation``.

State: ``design_only``.  ``EARNED_CLOSURE_REQUIREMENTS`` is empty by construction:
this gate earns none of the thirteen live closure requirements of
``scripts/mycelium_completion_audit.py``, and no record authored here is physical,
device, browser or completion evidence.  The spec, the acceptance inventory and the
scenario manifest remain the only frozen inputs, and their digests are pinned below.

The shared mobile evidence contract that validates these records lives in
``mycelium_mobile/a12_evidence.py``; this module owns the vocabulary, the guards and
the rung arithmetic it validates.  This gate deliberately does not modify
``mycelium_mobile/__init__.py``, ``contracts/``, any manifest or the completion
checklist: exporting these names and registering the declared protocols are
owner-reviewed steps of the implementation lane.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, Mapping, NoReturn, Sequence

from mycelium_membership.contracts import SIGNED_MESSAGE_PROTOCOL
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    SIGNATURE_FIELDS,
    Ed25519EvidenceSigner,
    build_ed25519_verifier,
)

A12_GATE_ID = "A12"
SCAFFOLD_PROTOCOL = "mycelium.a12_scaffold.v1"
SCAFFOLD_STATE = "design_only"
SPECIFICATION = (
    "docs/superpowers/specs/"
    "2026-08-18-mycelium-a12-generic-mobile-activation.md"
)
IMPLEMENTATION_CONTRACT = (
    "docs/superpowers/specs/2026-09-21-mycelium-a12-implementation-contract.md"
)
ACCEPTANCE_INVENTORY = "tests/a12_acceptance/inventory.v1.json"
ACCEPTANCE_SCENARIOS = "tests/a12_acceptance/scenarios.v1.json"

# Frozen-input digests of the exact files this scaffold binds (sha256 of the raw
# bytes of the current frozen inputs).  The boundary suite recomputes them, so they
# are re-pinned in place whenever an input is amended: both were re-pinned on
# 2026-09-27 with the owner's ``two_android_family_proof`` narrowing for the initial
# build (status/decisions.log) — the amendment is a claim narrowing, never a digest
# chase, and it keeps ``one_phone_generic_android_claim`` forbidden.  All three were
# re-pinned on 2026-09-29 with the owner's rulings R1 (A9 dependency narrowed to its
# named non-Windows parts) and R2 (Apple mobile deferred; A12 is Android-only).
SPECIFICATION_DIGEST = (
    "sha256:b3c23c040991a88a6f1556cf19b27c33ad454d5f42b9ceebdfc409131bd6a18f"
)
ACCEPTANCE_INVENTORY_DIGEST = (
    "sha256:045f38bf66c73849f8cfe474f674204cfa2bfb80aee2558f2d6b1b974e705eec"
)
ACCEPTANCE_SCENARIOS_DIGEST = (
    "sha256:45b9253d9c04be3f45a88919a0dd08677c88e0bcc21300f4786f3249af2f7bbf"
)

CLAIM_BOUNDARY = (
    "deterministic mobile rungs only: no runtime observation production, no "
    "optional draft binding, no product projection, no physical gate and no "
    "completion claim; no closure requirement earned; every remaining execution seam "
    "refuses with a coded error and never returns success"
)
IMPLEMENTED_CLAIM = (
    "the four separately authored deterministic rungs, closed rung-proof records, "
    "member-signed control-plane profile and capability-report verification, and the "
    "source-kind/family/product-path/rung honesty guards only"
)

# The capability-named A12 records of specification section 5.  They are owned by
# this gate; registering them in a wire contract or the contract manifest is an
# implementation-lane step.
RUNTIME_OBSERVATION_PROTOCOL = "mycelium.mobile_runtime_observation.v1"
ROLE_QUALIFICATION_PROTOCOL = "mycelium.mobile_role_qualification.v1"
ACTIVATION_QUALIFICATION_PROTOCOL = "mycelium.mobile_activation_qualification.v1"
DECLARED_CONTRACT_PROTOCOLS = (
    RUNTIME_OBSERVATION_PROTOCOL,
    ROLE_QUALIFICATION_PROTOCOL,
    ACTIVATION_QUALIFICATION_PROTOCOL,
)

# The closed rung-proof record authored by each rung's own authority.
RUNG_PROOF_PROTOCOL = "mycelium.mobile_rung_proof.v1"

# The member-signed control-plane statements this gate verifies.  The envelope
# shape is the A9 signed-message envelope; the profile and report names are the A9
# v2 contract names of the A9 specification section 4.
PEER_PROFILE_PROTOCOL = "mycelium.membership.peer_profile.v2"
CAPABILITY_REPORT_PROTOCOL = "mycelium.membership.capability_report.v2"
MEMBER_ENVELOPE_PROTOCOL = SIGNED_MESSAGE_PROTOCOL

# Bounds for the control-plane records this gate authors.  They are closed
# control-plane ceilings, not qualified physical limits.
MAX_PROFILE_TTL_MS = 3_600_000.0
MAX_CLOCK_SKEW_MS = 30_000.0
DEFAULT_RUNG_VALIDITY_MS = 900_000.0
MAX_RUNG_VALIDITY_MS = 3_600_000.0

# Verbatim from the frozen acceptance inventory (tests/a12_acceptance/inventory.v1.json).
RUNG_IDS = (
    "signed_membership",
    "capability_eligibility",
    "artifact_readiness",
    "operation_qualification",
)
RUNG_AUTHORITIES = {
    "signed_membership": "a9_membership_authority",
    "capability_eligibility": "a9_role_class_qualifier",
    "artifact_readiness": "a2_provisioner",
    "operation_qualification": "mobile_role_operation_qualifier",
}
RUNG_GRANTS = {
    "signed_membership": "current_signed_membership",
    "capability_eligibility": "exact_role_capability_eligibility",
    "artifact_readiness": "assignment_local_artifact_ready",
    "operation_qualification": (
        "exact_platform_runtime_build_and_role_operation_qualification"
    ),
}
RUNG_CANNOT_GRANT: dict[str, tuple[str, ...]] = {
    "signed_membership": (
        "capability_eligibility",
        "artifact_access",
        "artifact_readiness",
        "operation_qualification",
        "planner_placement",
        "deployment_selection",
    ),
    "capability_eligibility": (
        "artifact_grant",
        "artifact_readiness",
        "operation_qualification",
        "planner_placement",
        "route_readiness",
        "deployment_selection",
    ),
    "artifact_readiness": (
        "capability_eligibility",
        "operation_qualification",
        "runtime_load_proof",
        "planner_placement",
        "route_readiness",
        "deployment_selection",
    ),
    "operation_qualification": (
        "another_role_qualification",
        "another_platform_qualification",
        "planner_placement",
        "provisioner_grant",
        "deployment_qualification",
        "registry_selection",
    ),
}
RUNG_REQUIRES: dict[str, tuple[str, ...]] = {
    "signed_membership": (
        "durable_device_owned_key",
        "signed_a9_v2_profile",
        "current_lease_incarnation_and_generation",
        "revocation_checks",
        "consent",
        "authenticated_reconnect",
    ),
    "capability_eligibility": (
        "current_signed_membership",
        "registered_platform_runtime_transport_and_build",
        "fresh_native_observations",
        "role_specific_constraints",
        "sustainable_resource_and_lifecycle_evidence",
    ),
    "artifact_readiness": (
        "assignment_scoped_grant",
        "exact_artifact_model_and_representation",
        "digest_and_component_integrity",
        "supported_inert_format",
        "bounded_resumable_acquisition",
        "quota_and_sustainable_headroom",
        "thermal_power_storage_and_cancellation_policy",
        "atomic_promotion",
    ),
    "operation_qualification": (
        "current_applicable_rung_proofs",
        "exact_native_runtime_build_and_operation",
        "physical_positive_and_negative_evidence",
        "thermal_power_memory_lifecycle_network_gates",
        "artifact_integrity_when_applicable",
        "native_parity_when_applicable",
        "cancellation_cleanup_and_load_proof",
    ),
}
MOBILE_ROLES = (
    "probe_contributor",
    "speculative_draft_worker",
    "qualified_model_stage",
)
ANDROID_ARCHITECTURE = "generic_registered_native_android"
# Minimum pseudonymous device families for a *general* Android claim.  This
# number does not move under the initial-build narrowing below: any caller that
# asserts a general or cross-vendor Android role still needs two families.
MINIMUM_ANDROID_FAMILIES = 2
# [2026-09-27 OWNER RULING] ``two_android_family_proof`` NARROWED FOR THE INITIAL
# BUILD, not deleted (status/decisions.log; packet
# a12-claim-narrowing-single-family-20260927).  The initial-build claim is bound to
# the single named family below, so the two-independent-family runtime requirement
# is a DEFERRED obligation with a named reactivation trigger, not a present
# precondition.  A generic claim made on one family is still refused
# (``android_general_claim_requires_two_families``) and every brand-rejection guard
# stays in force: behaviour remains registry/capability-driven and never brand- or
# model-driven (``brand_or_model_specific_branching_allowed: false``).
ANDROID_GENERAL_CLAIM_SCOPE = "general"
ANDROID_FAMILY_BOUND_CLAIM_SCOPE = "family_bound"
ANDROID_CLAIM_SCOPES = (ANDROID_GENERAL_CLAIM_SCOPE, ANDROID_FAMILY_BOUND_CLAIM_SCOPE)
#: The one device family the narrowed initial-build claim is bound to.
INITIAL_BUILD_ANDROID_FAMILY = "Google Pixel 8 Pro (husky), SDK 36, Android 16"
#: Named reactivation trigger for the deferred two-independent-family obligation.
TWO_FAMILY_REQUIREMENT_REACTIVATION_TRIGGER = (
    "before_any_general_or_cross_vendor_android_claim_including_public_or_swarm_scope"
)
APPLE_PLATFORM_FAMILIES = ("ios", "ipados")
MOBILE_PLATFORM_FAMILIES = ("android", *APPLE_PLATFORM_FAMILIES)
APPLE_INITIAL_ELIGIBILITY = "foreground_active_only"
EXCLUDED_NORMAL_PRODUCT_PATHS = ("termux", "adb", "ssh")
REQUIRED_GATE_FAMILIES = (
    "thermal",
    "power",
    "memory",
    "lifecycle",
    "network",
    "artifact",
    "parity",
)
# Every capability family carries its own source kind; a missing family is a
# blocker, never an inherited sibling kind (A9 ``native_synthetic_evidence_separation``).
CAPABILITY_FAMILY_UNKNOWN_BLOCKER = "capability_family_observation_unknown"
CAPABILITY_FAMILY_SOURCE_KIND_MISSING = "capability_family_source_kind_missing"
CAPABILITY_FAMILY_UNREGISTERED = "capability_family_unregistered"
CAPABILITY_FAMILY_SYNTHETIC_BLOCKER = "capability_family_synthetic_evidence_cannot_qualify"
# One platform/runtime is registered but unobserved; the blocker is explicit.
REGISTERED_UNOBSERVED_BLOCKER = "native_observation_absent"
DECLARATION_BLOCKER = "declaration_is_not_observation"
SYNTHETIC_NATIVE_BLOCKER = "synthetic_conformance_cannot_qualify_native"
PHYSICAL_EXECUTION_BLOCKER = "physical_positive_and_negative_evidence"
PLATFORM_UNKNOWN_BLOCKER = "platform_family_unknown"

# Requirement states for the deterministic rung arithmetic.  ``satisfied`` is the
# only state that can advance a rung; every other state blocks with the requirement's
# own name (or the member-revoked blocker) and is never rewritten into success.
REQUIREMENT_STATES = ("satisfied", "withheld", "unknown", "revoked")

# Closed vocabularies for the artifact-readiness rung policies (specification 4.3).
ARTIFACT_FORMATS = ("inert_assignment_local",)
ARTIFACT_INTEGRITY_EVIDENCE = ("digest_verified",)
ARTIFACT_QUOTA_HEADROOM = ("sustainable",)
ARTIFACT_CANCELLATION_POLICIES = ("bounded",)
ARTIFACT_ACQUISITION_POLICIES = ("bounded_resumable",)

# Closed shapes of the member-signed control-plane statements.
PEER_PROFILE_FIELDS = frozenset(
    {
        "protocol",
        "member_ref",
        "member_endpoint_id",
        "member_key_digest",
        "platform_family",
        "runtime_build_digest",
        "profile_generation",
        "membership_generation",
        "incarnation",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
    }
)
MEMBER_ENVELOPE_FIELDS = frozenset(
    {"protocol", "message", "signature", "verification_key"}
)

# Closed shape of one rung-proof record: the common declaration every rung carries
# plus the rung-specific fields, all present on every record so the shape stays
# closed rather than per-rung variable.
RUNG_PROOF_FIELDS = frozenset(
    {
        "protocol",
        "rung_id",
        "authority",
        "decision",
        "member_ref",
        "platform_family",
        "role",
        "source_kind",
        "grants",
        "cannot_grant",
        "blockers",
        "route_ready",
        "profile_generation",
        "membership_generation",
        "evidence_generation",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
        "prior_rungs",
        "metrics",
        "physical_execution",
        "role_constraints",
        "capability_family_sources",
        "assignment_ref",
        "artifact_digest",
        "acquisition_authority",
        "artifact_format",
        "integrity_evidence",
        "quota_headroom",
        "cancellation_policy",
        "acquisition_policy",
        "cache_presence_used_as_proof",
        "artifact_rung",
    }
)
WORKSPACES = (
    "inference",
    "device_lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
)

# Source kinds.  Every mobile metric records its source kind (specification
# section 5); only a native device observation may reach a physical rung.
NATIVE_SOURCE_KIND = "native_device_observation"
SOURCE_KINDS = (
    NATIVE_SOURCE_KIND,
    "synthetic_conformance",
    "termux_conformance",
    "operator_declared",
    "unknown",
)
NON_NATIVE_SOURCE_KINDS = tuple(
    kind for kind in SOURCE_KINDS if kind != NATIVE_SOURCE_KIND
)

# Closed per-rung decisions.  Missing or denied evidence is ``unknown`` and blocks;
# it is never zero, healthy or inferred support.
RUNG_DECISIONS = (
    "eligible",
    "ineligible",
    "not_applicable",
    "withheld",
    "unknown",
    "stale",
    "revoked",
)
RUNG_CURRENT_DECISIONS = ("eligible", "not_applicable")

# The optional speculative-draft claim is bound to an exact physically closed A11
# target-authoritative binding (specification section 4.4 and the inventory's
# dependency policy).  When that binding is absent the claim fails closed and no
# other rung, role or gate state changes.
A11_CLAIM_ID = "speculative_draft_worker"
A11_REQUIRED_DEPENDENCY_STATE = "physically_closed"
A11_FAILURE_MODE = "claim_rejected_gate_closure_unchanged"
A11_ABSENT_BINDING_REASON = "a11_target_authoritative_binding_absent"

# Mobile enrollment starts with route_ready=false and joining alters no Router state.
ROUTE_READY_AT_ENROLLMENT = False

EARNED_CLOSURE_REQUIREMENTS: frozenset[str] = frozenset()

IMPLEMENTED_BEHAVIOUR = (
    "validate_mobile_runtime_observation",
    "require_native_observation",
    "require_android_family_evidence",
    "require_apple_platform_isolation",
    "require_undivided_product_path",
    "require_rung_isolation",
    "require_artifact_rung_decision",
    "require_route_ready_is_false",
    "require_capability_family_sources",
    "capability_family_blockers",
    "sign_member_profile",
    "verify_member_signed_profile",
    "sign_member_report",
    "verify_member_signed_report",
    "evaluate_optional_draft_claim",
    "enroll_mobile_member",
    "qualify_capability_eligibility",
    "promote_assignment_artifact",
    "author_operation_qualification",
    "scaffold_status",
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FAMILY_REF = re.compile(r"(?:sha256:[0-9a-f]{64}|family:[0-9a-f]{16,64})\Z")
_METRIC_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_UNKNOWN_METRIC = "unknown"

# Scaffold-level sanity ceilings only.  They are NOT qualified bounds: the
# qualified limits are policy inputs owned by the exact platform qualifier.
SCAFFOLD_METRIC_COUNT_CEILING = 256

# Closed shape of one runtime observation document
# (mycelium.mobile_runtime_observation.v1).
OBSERVATION_FIELDS = frozenset(
    {
        "protocol",
        "platform_family",
        "runtime_build_digest",
        "profile_generation",
        "evidence_generation",
        "source_kind",
        "observed_at_unix_ms",
        "measurement_duration_ms",
        "sample_count",
        "authority",
        "metrics",
    }
)


# ---------------------------------------------------------------------------
# Errors: every refusal in this scaffold is explicit and coded
# ---------------------------------------------------------------------------


class A12ScaffoldError(ValueError):
    """Base class for every coded A12 scaffold refusal."""

    code = ""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class A12ContractError(A12ScaffoldError):
    """A closed-shape, bound, digest or authority rejection (specification section 5)."""


class A12NotImplementedError(A12ScaffoldError):
    """An A12 entry point that is declared but not implemented.

    The message is always ``not_implemented:<code>``.  This exception is never a
    success path and never stands in for real behaviour.
    """

    def __init__(self, code: str):
        super().__init__(f"not_implemented:{code}")


def _reject(code: str) -> NoReturn:
    raise A12ContractError(code)


def _refuse(code: str) -> NoReturn:
    raise A12NotImplementedError(code)


def _text(value: object, *, code: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _reject(code)
    return value


def _digest(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.match(value):
        _reject(code)
    return value


def _finite(value: object, *, code: str, minimum: float | None = None) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float)) or not math.isfinite(float(value))
    ):
        _reject(code)
    number = float(value)
    if minimum is not None and number < minimum:
        _reject(code)
    return number


def _counter(value: object, *, code: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _reject(code)
    return value


def _closed(
    document: object, *, fields: frozenset[str], code: str
) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != set(fields):
        _reject(code)
    return dict(document)


def _member_index(rung_id: object) -> int:
    if not isinstance(rung_id, str) or rung_id not in RUNG_IDS:
        _reject("rung_id_invalid")
    return RUNG_IDS.index(rung_id)


# ---------------------------------------------------------------------------
# Implemented behaviour: the honesty guards
# ---------------------------------------------------------------------------


def require_native_observation(source_kind: object) -> str:
    """A physical rung proof may only be built from a native device observation.

    Synthetic, Termux, operator-declared and unknown evidence keeps its own
    separate label and can never satisfy a native rung (specification sections 4,
    5, 6 and 10).  Missing or denied evidence is ``unknown`` and blocks; it is
    never treated as zero, healthy or inferred support.
    """

    if source_kind == NATIVE_SOURCE_KIND:
        return NATIVE_SOURCE_KIND
    if source_kind == "unknown":
        _reject("source_kind_unknown_blocks_qualification")
    if source_kind in NON_NATIVE_SOURCE_KINDS:
        _reject("non_native_source_cannot_qualify")
    _reject("source_kind_invalid")


def require_android_family_evidence(
    family_refs: object, *, claim_scope: object = ANDROID_GENERAL_CLAIM_SCOPE
) -> tuple[str, ...]:
    """The Android family arithmetic of the claim being asserted.

    A *general* Android claim needs two distinct pseudonymous device families:
    one family is insufficient (``android_general_claim_requires_two_families``),
    a repeated family is not diversity, and the ordinary product contracts must be
    the same for both (inventory decision ``two_android_family_proof``).

    [2026-09-27 OWNER RULING] That decision is NARROWED for the initial build,
    not deleted (``status/decisions.log``; packet
    ``a12-claim-narrowing-single-family-20260927``).  The initial-build claim is
    family-bound to ``INITIAL_BUILD_ANDROID_FAMILY``, so a caller that says so —
    ``claim_scope=ANDROID_FAMILY_BOUND_CLAIM_SCOPE`` — may stand on one family.
    The generic branch is untouched: an unqualified (default) caller is treated as
    a general claim, so one family is still refused, and the deferred obligation
    reactivates at ``TWO_FAMILY_REQUIREMENT_REACTIVATION_TRIGGER`` before any
    general or cross-vendor Android claim is made.
    """

    if claim_scope not in ANDROID_CLAIM_SCOPES:
        _reject("android_claim_scope_invalid")
    if (
        isinstance(family_refs, (str, bytes))
        or not isinstance(family_refs, Sequence)
        or not family_refs
    ):
        _reject("android_family_evidence_invalid")
    families: list[str] = []
    for reference in family_refs:
        if not isinstance(reference, str) or not _FAMILY_REF.match(reference):
            _reject("android_family_reference_invalid")
        families.append(reference)
    if len(set(families)) != len(families):
        _reject("android_family_evidence_duplicated")
    if (
        claim_scope != ANDROID_FAMILY_BOUND_CLAIM_SCOPE
        and len(families) < MINIMUM_ANDROID_FAMILIES
    ):
        _reject("android_general_claim_requires_two_families")
    return tuple(families)


def require_apple_platform_isolation(
    platform_family: object, other_family_state: object = None
) -> str:
    """iOS and iPadOS never qualify each other (inventory ``apple_foreground_initial``).

    ``other_family_state`` is the sibling Apple platform's current state; only an
    absent or unqualified sibling keeps this platform's own qualification valid.
    """

    if platform_family not in APPLE_PLATFORM_FAMILIES:
        _reject("apple_platform_family_invalid")
    if other_family_state not in (None, "absent", "ineligible", "not_qualified"):
        _reject("apple_platform_carryover_forbidden")
    return str(platform_family)


def require_undivided_product_path(path_kind: object) -> str:
    """The signed native application is the ordinary product path (decision
    ``native_product_path_only``).  Termux, ADB, SSH and manual shell or copy paths
    are development/conformance tools and cannot qualify anything.
    """

    if path_kind in EXCLUDED_NORMAL_PRODUCT_PATHS:
        _reject("excluded_development_path_cannot_qualify")
    if path_kind not in ("signed_native_application", "embedded_iroh_transport"):
        _reject("product_path_kind_invalid")
    return str(path_kind)


def require_rung_isolation(
    rung_id: object, prior_rung_decisions: object
) -> str:
    """Every rung is separately authored; a later rung consumes prior proofs only.

    The caller supplies the prior-rung decisions this rung consumes.  Every supplied
    decision must name an earlier rung, must use the closed decision vocabulary, and
    must be current (``eligible`` or ``not_applicable``); a non-current supplied proof
    blocks the rung instead of being rewritten.  Current signed membership is
    required for every rung, and qualifying an operation additionally requires the
    exact role's current capability eligibility.  Artifact readiness stays optional
    here because artifact-free roles record it as ``not_applicable``.
    """

    index = _member_index(rung_id)
    if not isinstance(prior_rung_decisions, Mapping):
        _reject("prior_rung_decisions_invalid")
    for name, decision in prior_rung_decisions.items():
        if name not in RUNG_IDS:
            _reject("prior_rung_id_invalid")
        if decision not in RUNG_DECISIONS:
            _reject("rung_decision_invalid")
        if RUNG_IDS.index(name) >= index:
            _reject("later_rung_cannot_author_earlier")
        if decision not in RUNG_CURRENT_DECISIONS:
            _reject("prior_rung_not_current")
    required = ("signed_membership",)
    if rung_id == "operation_qualification":
        required = ("signed_membership", "capability_eligibility")
    for earlier in required:
        if prior_rung_decisions.get(earlier) not in RUNG_CURRENT_DECISIONS:
            _reject("prior_rung_not_current")
    return str(rung_id)


def require_artifact_rung_decision(
    role: object, decision: object, reason: object = None
) -> dict[str, Any]:
    """Artifact-free roles record ``not_applicable`` with a closed reason.

    A probe/evidence role never fabricates artifact success, and a role with an
    artifact need never records ``not_applicable`` (inventory invariant
    ``artifact_free_role_uses_closed_not_applicable_reason``).
    """

    if role not in MOBILE_ROLES:
        _reject("mobile_role_invalid")
    if decision not in RUNG_DECISIONS:
        _reject("rung_decision_invalid")
    if role == "probe_contributor":
        if decision != "not_applicable":
            _reject("artifact_free_probe_must_record_closed_not_applicable")
        _text(reason, code="artifact_not_applicable_reason_missing", maximum=128)
    elif decision == "not_applicable":
        _reject("artifact_required_role_cannot_record_not_applicable")
    return {"rung_id": "artifact_readiness", "decision": decision, "reason": reason}


def require_route_ready_is_false(value: object) -> bool:
    """Mobile enrollment starts with ``route_ready=false`` (specification section 3)."""

    if value is not False:
        _reject("route_ready_must_start_false")
    return ROUTE_READY_AT_ENROLLMENT


def evaluate_optional_draft_claim(
    *, a11_binding_state: object, a11_binding_digest: object = None
) -> dict[str, Any]:
    """The optional draft claim fails closed unless A11 is physically closed.

    This is real policy arithmetic, not a qualification: absent, stale, disabled or
    foreign A11 bindings return an ``ineligible`` decision with the exact blocker,
    and the other mobile roles keep their independently earned state.  Even a
    complete A11 binding cannot be qualified here — that operation is a declared
    A12 seam and refuses with ``draft_worker_qualification_absent``.
    """

    _text(a11_binding_state, code="a11_binding_state_invalid", maximum=128)
    if (
        a11_binding_state == A11_REQUIRED_DEPENDENCY_STATE
        and isinstance(a11_binding_digest, str)
        and _SHA256.match(a11_binding_digest)
    ):
        _refuse("draft_worker_qualification_absent")
    return {
        "claim_id": A11_CLAIM_ID,
        "decision": "ineligible",
        "reason": A11_ABSENT_BINDING_REASON,
        "failure_mode": A11_FAILURE_MODE,
        "other_roles_unchanged": True,
    }


def validate_mobile_runtime_observation(document: object) -> dict[str, Any]:
    """Validate one closed ``mycelium.mobile_runtime_observation.v1`` document.

    Shape validation only: it proves a record is well formed, never that any
    observation is true, fresh, native or sufficient for a rung.
    """

    record = _closed(
        document, fields=OBSERVATION_FIELDS, code="mobile_observation_fields_invalid"
    )
    if record["protocol"] != "mycelium.mobile_runtime_observation.v1":
        _reject("mobile_observation_protocol_invalid")
    if record["platform_family"] not in ("android", *APPLE_PLATFORM_FAMILIES):
        _reject("mobile_observation_platform_invalid")
    _digest(record["runtime_build_digest"], code="mobile_observation_build_invalid")
    _counter(record["profile_generation"], code="mobile_observation_generation_invalid")
    _counter(record["evidence_generation"], code="mobile_observation_generation_invalid")
    if record["source_kind"] not in SOURCE_KINDS:
        _reject("mobile_observation_source_kind_invalid")
    _finite(
        record["observed_at_unix_ms"], code="mobile_observation_measurement_invalid",
        minimum=0.0,
    )
    _finite(
        record["measurement_duration_ms"],
        code="mobile_observation_measurement_invalid",
        minimum=0.0,
    )
    if float(record["measurement_duration_ms"]) <= 0.0:
        _reject("mobile_observation_measurement_invalid")
    _counter(record["sample_count"], code="mobile_observation_measurement_invalid")
    _text(record["authority"], code="mobile_observation_authority_invalid", maximum=128)
    metrics = record["metrics"]
    if not isinstance(metrics, Mapping) or len(metrics) > SCAFFOLD_METRIC_COUNT_CEILING:
        _reject("mobile_observation_metric_invalid")
    for name, value in metrics.items():
        if not isinstance(name, str) or not _METRIC_NAME.match(name):
            _reject("mobile_observation_metric_invalid")
        if value == _UNKNOWN_METRIC:
            continue
        _finite(value, code="mobile_observation_metric_invalid")
    return dict(record)


# ---------------------------------------------------------------------------
# Deterministic rung arithmetic: vocabulary helpers
# ---------------------------------------------------------------------------


def _now_ms(value: object) -> float:
    if value is None:
        return time.time() * 1000.0
    return _finite(value, code="rung_time_invalid", minimum=0.0)


def _validity_ms(value: object) -> float:
    if value is None:
        return DEFAULT_RUNG_VALIDITY_MS
    number = _finite(value, code="rung_validity_invalid", minimum=0.0)
    if number <= 0.0 or number > MAX_RUNG_VALIDITY_MS:
        _reject("rung_validity_invalid")
    return number


def _require_mobile_platform(platform_family: object, *, code: str) -> str:
    if platform_family not in MOBILE_PLATFORM_FAMILIES:
        _reject(code)
    return str(platform_family)


def _require_role(role: object) -> str:
    if role not in MOBILE_ROLES:
        _reject("mobile_role_invalid")
    return str(role)


def _decision_rank(decision: object) -> int:
    if decision not in RUNG_DECISIONS:
        _reject("rung_decision_invalid")
    return RUNG_DECISIONS.index(str(decision))


def _rung_record(
    *,
    rung_id: str,
    decision: object,
    member_ref: str,
    platform_family: str | None,
    role: str | None,
    source_kind: str,
    blockers: Sequence[str],
    now_unix_ms: float,
    validity_ms: float,
    prior_rungs: Mapping[str, str],
    metrics: Mapping[str, object],
    **rung_fields: object,
) -> dict[str, Any]:
    """Build one closed rung-proof record and refuse to emit anything else."""

    _decision_rank(decision)
    record: dict[str, Any] = {
        "protocol": RUNG_PROOF_PROTOCOL,
        "rung_id": rung_id,
        "authority": RUNG_AUTHORITIES[rung_id],
        "decision": decision,
        "member_ref": member_ref,
        "platform_family": platform_family,
        "role": role,
        "source_kind": source_kind,
        "grants": [RUNG_GRANTS[rung_id]],
        "cannot_grant": list(RUNG_CANNOT_GRANT[rung_id]),
        "blockers": list(blockers),
        "route_ready": ROUTE_READY_AT_ENROLLMENT,
        "profile_generation": None,
        "membership_generation": None,
        "evidence_generation": None,
        "issued_at_unix_ms": now_unix_ms,
        "expires_at_unix_ms": now_unix_ms + validity_ms,
        "prior_rungs": dict(prior_rungs),
        "metrics": dict(metrics),
        "physical_execution": False,
        "role_constraints": None,
        "capability_family_sources": {},
        "assignment_ref": None,
        "artifact_digest": None,
        "acquisition_authority": None,
        "artifact_format": None,
        "integrity_evidence": None,
        "quota_headroom": None,
        "cancellation_policy": None,
        "acquisition_policy": None,
        "cache_presence_used_as_proof": False,
        "artifact_rung": None,
    }
    record.update(rung_fields)
    return _closed(record, fields=RUNG_PROOF_FIELDS, code="mobile_rung_proof_fields_invalid")


def validate_rung_proof(record: object) -> dict[str, Any]:
    """Validate one closed ``mycelium.mobile_rung_proof.v1`` record.

    Shape and declaration validation only: it proves the record is well formed and
    that a rung never lists its own grant as something it cannot grant.  It never
    proves that any rung was physically earned.
    """

    validated = _closed(
        record, fields=RUNG_PROOF_FIELDS, code="mobile_rung_proof_fields_invalid"
    )
    if validated["protocol"] != RUNG_PROOF_PROTOCOL:
        _reject("mobile_rung_proof_protocol_invalid")
    rung_id = validated["rung_id"]
    if rung_id not in RUNG_IDS:
        _reject("rung_id_invalid")
    if validated["authority"] != RUNG_AUTHORITIES[rung_id]:
        _reject("mobile_rung_proof_authority_invalid")
    _decision_rank(validated["decision"])
    if tuple(validated["grants"]) != (RUNG_GRANTS[rung_id],):
        _reject("mobile_rung_proof_grant_invalid")
    if tuple(validated["cannot_grant"]) != RUNG_CANNOT_GRANT[rung_id]:
        _reject("mobile_rung_proof_cannot_grant_invalid")
    if RUNG_GRANTS[rung_id] in validated["cannot_grant"]:
        _reject("mobile_rung_proof_self_grant_forbidden")
    if validated["route_ready"] is not False:
        _reject("route_ready_must_start_false")
    if validated["physical_execution"] is not False:
        _reject("mobile_rung_proof_physical_execution_forbidden")
    for name in ("issued_at_unix_ms", "expires_at_unix_ms"):
        _finite(validated[name], code="mobile_rung_proof_validity_invalid")
    if float(validated["expires_at_unix_ms"]) <= float(validated["issued_at_unix_ms"]):
        _reject("mobile_rung_proof_validity_invalid")
    return validated


def require_capability_family_sources(
    sources: object, *, required_families: object = None
) -> dict[str, str]:
    """Every capability family carries its own source kind; nothing is inherited.

    A family that is missing entirely is refused — never filled in from a sibling
    family's kind (A9 ``source_kind_per_capability_family``, forbidden shortcut
    ``source_kind_inheritance``).  A family observed as ``unknown`` is legal input:
    it stays visible and blocks the rung instead of reading as zero or healthy.
    """

    families = tuple(REQUIRED_GATE_FAMILIES if required_families is None else required_families)
    if not isinstance(sources, Mapping):
        _reject("capability_family_sources_invalid")
    normalized: dict[str, str] = {}
    for family, kind in sources.items():
        if family not in families:
            _reject(CAPABILITY_FAMILY_UNREGISTERED)
        if kind not in SOURCE_KINDS:
            _reject("capability_family_source_kind_invalid")
        normalized[str(family)] = str(kind)
    for family in families:
        if family not in normalized:
            _reject(CAPABILITY_FAMILY_SOURCE_KIND_MISSING)
    return normalized


def capability_family_blockers(
    sources: object, *, required_families: object = None
) -> dict[str, str]:
    """Name the blocker of every family whose evidence is not native, in order."""

    normalized = require_capability_family_sources(
        sources, required_families=required_families
    )
    families = tuple(REQUIRED_GATE_FAMILIES if required_families is None else required_families)
    blockers: dict[str, str] = {}
    for family in families:
        kind = normalized[family]
        if kind == NATIVE_SOURCE_KIND:
            continue
        if kind == "unknown":
            blockers[family] = CAPABILITY_FAMILY_UNKNOWN_BLOCKER
        else:
            blockers[family] = CAPABILITY_FAMILY_SYNTHETIC_BLOCKER
    return blockers


# ---------------------------------------------------------------------------
# Member-signed control-plane statements (A9 signed_capability_binding)
# ---------------------------------------------------------------------------


def _validate_peer_profile(profile: object) -> dict[str, Any]:
    record = _closed(profile, fields=PEER_PROFILE_FIELDS, code="member_profile_fields_invalid")
    if record["protocol"] != PEER_PROFILE_PROTOCOL:
        _reject("member_profile_protocol_invalid")
    _text(record["member_ref"], code="member_profile_member_invalid", maximum=128)
    _text(record["member_endpoint_id"], code="member_profile_member_invalid", maximum=256)
    _digest(record["member_key_digest"], code="member_profile_key_binding_invalid")
    _require_mobile_platform(record["platform_family"], code="member_profile_platform_invalid")
    _digest(record["runtime_build_digest"], code="member_profile_build_invalid")
    _counter(record["profile_generation"], code="member_profile_generation_invalid")
    _counter(record["membership_generation"], code="member_profile_generation_invalid")
    if int(record["membership_generation"]) > int(record["profile_generation"]):
        _reject("member_profile_generation_invalid")
    _text(record["incarnation"], code="member_profile_incarnation_invalid", maximum=128)
    issued = _finite(
        record["issued_at_unix_ms"], code="member_profile_validity_invalid", minimum=0.0
    )
    expires = _finite(
        record["expires_at_unix_ms"], code="member_profile_validity_invalid", minimum=0.0
    )
    if expires <= issued or expires - issued > MAX_PROFILE_TTL_MS:
        _reject("member_profile_validity_invalid")
    return record


def _verify_envelope_signature(
    message: Mapping[str, Any], signature: object, verification_key: object
) -> dict[str, Any]:
    record = _closed(signature, fields=SIGNATURE_FIELDS, code="member_profile_signature_invalid")
    if record["algorithm"] != "ed25519":
        _reject("member_profile_signature_invalid")
    try:
        verifier = build_ed25519_verifier([verification_key])
    except Exception as exc:  # noqa: BLE001 - fail closed on any key-record problem
        raise A12ContractError("member_profile_key_record_invalid") from exc
    try:
        verified = bool(verifier(canonical_json_bytes(dict(message)), dict(record)))
    except Exception:  # noqa: BLE001 - canonicalisation faults fail closed
        verified = False
    if not verified:
        _reject("member_profile_signature_invalid")
    return record


def sign_member_profile(
    *, signer: Ed25519EvidenceSigner, profile: object
) -> dict[str, Any]:
    """Sign one validated v2 peer profile with the member's durable key."""

    record = _validate_peer_profile(profile)
    if signer.endpoint_id != record["member_endpoint_id"]:
        _reject("member_profile_signer_mismatch")
    if signer.verification_key_digest != record["member_key_digest"]:
        _reject("member_profile_key_binding_invalid")
    return {
        "protocol": MEMBER_ENVELOPE_PROTOCOL,
        "message": record,
        "signature": signer.sign(record),
        "verification_key": signer.public_key_record(),
    }


def verify_member_signed_profile(
    envelope: object,
    *,
    now_unix_ms: object = None,
    expected_member_key_digest: object = None,
    expected_member_ref: object = None,
    expected_platform_family: object = None,
) -> dict[str, Any]:
    """Verify one member-signed profile: the member's key signed the member's profile.

    An operator- or seed-authored claim cannot satisfy this: the signature must be
    produced by the exact key the profile itself binds, and the caller may pin that
    key digest as well.  Stale, expired and future-dated profiles fail closed
    (A9 ``profile_signed_by_member``, ``report_binds_generation_and_expiry``).
    """

    checked = _closed(
        envelope, fields=MEMBER_ENVELOPE_FIELDS, code="member_profile_envelope_invalid"
    )
    if checked["protocol"] != MEMBER_ENVELOPE_PROTOCOL:
        _reject("member_profile_envelope_invalid")
    profile = _validate_peer_profile(checked["message"])
    signature = checked["signature"]
    if not isinstance(signature, Mapping) or signature.get("signer_endpoint_id") != profile["member_endpoint_id"]:
        _reject("member_profile_signer_mismatch")
    if signature.get("verification_key_digest") != profile["member_key_digest"]:
        _reject("member_profile_signature_invalid")
    if (
        expected_member_key_digest is not None
        and expected_member_key_digest != profile["member_key_digest"]
    ):
        _reject("member_profile_key_mismatch")
    _verify_envelope_signature(profile, signature, checked["verification_key"])
    now = _now_ms(now_unix_ms)
    if float(profile["expires_at_unix_ms"]) <= now:
        _reject("member_profile_expired")
    if float(profile["issued_at_unix_ms"]) > now + MAX_CLOCK_SKEW_MS:
        _reject("member_profile_time_invalid")
    if expected_member_ref is not None and expected_member_ref != profile["member_ref"]:
        _reject("member_profile_member_mismatch")
    if (
        expected_platform_family is not None
        and expected_platform_family != profile["platform_family"]
    ):
        _reject("member_profile_platform_mismatch")
    return dict(profile)


def sign_member_report(
    *, signer: Ed25519EvidenceSigner, statement: object
) -> dict[str, Any]:
    """Sign one capability report statement with the member's durable key."""

    if not isinstance(statement, Mapping) or not statement:
        _reject("member_report_message_invalid")
    message = dict(statement)
    canonical_json_bytes(message)
    if message.get("protocol") != CAPABILITY_REPORT_PROTOCOL:
        _reject("member_report_protocol_invalid")
    return {
        "protocol": MEMBER_ENVELOPE_PROTOCOL,
        "message": message,
        "signature": signer.sign(message),
        "verification_key": signer.public_key_record(),
    }


def verify_member_signed_report(
    envelope: object,
    *,
    now_unix_ms: object = None,
    expected_member_key_digest: object = None,
    expected_member_ref: object = None,
) -> dict[str, Any]:
    """Verify one member-signed capability report envelope."""

    checked = _closed(
        envelope, fields=MEMBER_ENVELOPE_FIELDS, code="member_report_envelope_invalid"
    )
    if checked["protocol"] != MEMBER_ENVELOPE_PROTOCOL:
        _reject("member_report_envelope_invalid")
    message = checked["message"]
    if not isinstance(message, Mapping) or not message:
        _reject("member_report_message_invalid")
    record = dict(message)
    signature = _verify_envelope_signature(record, checked["signature"], checked["verification_key"])
    if (
        expected_member_key_digest is not None
        and expected_member_key_digest != signature["verification_key_digest"]
    ):
        _reject("member_profile_key_mismatch")
    issued = record.get("issued_at_unix_ms")
    expires = record.get("expires_at_unix_ms")
    if issued is not None and expires is not None:
        start = _finite(issued, code="member_report_validity_invalid", minimum=0.0)
        end = _finite(expires, code="member_report_validity_invalid", minimum=0.0)
        if end <= start or end - start > MAX_PROFILE_TTL_MS:
            _reject("member_report_validity_invalid")
        if end <= _now_ms(now_unix_ms):
            _reject("member_report_expired")
    if (
        expected_member_ref is not None
        and record.get("member_ref") is not None
        and expected_member_ref != record["member_ref"]
    ):
        _reject("member_profile_member_mismatch")
    return record


# ---------------------------------------------------------------------------
# Deterministic rung authoring: one surface per rung, one authority each
# ---------------------------------------------------------------------------


def _resolve_membership_requirements(
    *, member_ref: str, profile: Mapping[str, Any] | None, requirements: object, now: float
) -> tuple[str, list[str]]:
    """Return the rung-1 decision and its blockers, in the inventory's order."""

    supplied: dict[str, Mapping[str, Any]] = {}
    if requirements is not None:
        if not isinstance(requirements, Mapping):
            _reject("member_requirements_invalid")
        for name, spec in requirements.items():
            if name == "signed_a9_v2_profile":
                _reject("member_requirement_override_forbidden")
            if name not in RUNG_REQUIRES["signed_membership"]:
                _reject("member_requirement_invalid")
            if not isinstance(spec, Mapping) or spec.get("state") not in REQUIREMENT_STATES:
                _reject("member_requirement_state_invalid")
            supplied[str(name)] = spec

    blockers: list[str] = []
    decisions: list[str] = []
    for name in RUNG_REQUIRES["signed_membership"]:
        if name == "signed_a9_v2_profile":
            if profile is None:
                blockers.append(name)
                decisions.append("withheld")
            continue
        spec = supplied.get(name)
        if spec is None:
            blockers.append(name)
            decisions.append("withheld")
            continue
        state = str(spec["state"])
        if state == "satisfied":
            if name == "current_lease_incarnation_and_generation":
                lease = spec.get("lease_expires_at_unix_ms")
                if lease is None:
                    blockers.append(name)
                    decisions.append("withheld")
                    continue
                if _finite(lease, code="member_requirement_invalid", minimum=0.0) <= now:
                    blockers.append(name)
                    decisions.append("withheld")
            if name == "revocation_checks":
                _finite(
                    spec.get("checked_at_unix_ms"),
                    code="member_requirement_invalid",
                    minimum=0.0,
                )
            if name == "consent" and spec.get("granted") is not True:
                blockers.append(name)
                decisions.append("withheld")
            continue
        if state == "revoked":
            return "revoked", ["member_revoked"]
        blockers.append(name)
        decisions.append(state)

    if not blockers:
        return "eligible", []
    if "unknown" in decisions:
        return "unknown", blockers
    return "withheld", blockers


def enroll_mobile_member(
    *,
    member_ref: object,
    platform_family: object,
    source_kind: object,
    route_ready: object,
    profile_envelope: object = None,
    requirements: object = None,
    expected_member_key_digest: object = None,
    now_unix_ms: object = None,
    validity_ms: object = None,
) -> dict[str, Any]:
    """Rung 1 (``a9_membership_authority``): author the membership rung.

    Signed membership is granted only when the member-signed v2 profile verifies
    against the member's own durable key and every other required input of the frozen
    inventory is satisfied.  A missing or denied input records ``withheld`` (or
    ``unknown``/``revoked``) with the exact blocker; it is never rewritten into a
    grant, and joining still starts ``route_ready=false``.
    """

    member = _text(member_ref, code="mobile_enrollment_member_invalid")
    platform = _require_mobile_platform(
        platform_family, code="mobile_enrollment_platform_invalid"
    )
    require_native_observation(source_kind)
    require_route_ready_is_false(route_ready)

    now = _now_ms(now_unix_ms)
    validity = _validity_ms(validity_ms)
    profile: Mapping[str, Any] | None = None
    if profile_envelope is not None:
        profile = verify_member_signed_profile(
            profile_envelope,
            now_unix_ms=now,
            expected_member_key_digest=expected_member_key_digest,
            expected_member_ref=member,
            expected_platform_family=platform,
        )

    decision, blockers = _resolve_membership_requirements(
        member_ref=member, profile=profile, requirements=requirements, now=now
    )
    profile_generation = None if profile is None else int(profile["profile_generation"])
    record = _rung_record(
        rung_id="signed_membership",
        decision=decision,
        member_ref=member,
        platform_family=platform,
        role=None,
        source_kind=str(source_kind),
        blockers=blockers,
        now_unix_ms=now,
        validity_ms=validity,
        prior_rungs={},
        metrics={},
        profile_generation=profile_generation,
        membership_generation=None if profile is None else int(profile["membership_generation"]),
        evidence_generation=profile_generation,
    )
    return validate_rung_proof(record)


def qualify_capability_eligibility(
    *,
    member_ref: object,
    platform_family: object,
    role: object,
    observation: object,
    prior_rung_decisions: object = None,
    role_constraints: object = None,
    capability_sources: object = None,
    now_unix_ms: object = None,
    validity_ms: object = None,
) -> dict[str, Any]:
    """Rung 2 (``a9_role_class_qualifier``): qualify exactly one role.

    The rung consumes the caller-supplied current prior-rung proofs (the frozen
    inventory requires ``current_signed_membership``), the caller-supplied role
    constraints, and fresh *native* per-family observations.  Anything missing is a
    named blocker; any non-native source kind is refused, and a metric the device
    could not observe stays ``unknown`` inside the record instead of becoming zero.
    This rung grants eligibility for one exact role and nothing else.
    """

    member = _text(member_ref, code="capability_eligibility_member_invalid")
    platform = _require_mobile_platform(
        platform_family, code="capability_eligibility_platform_invalid"
    )
    exact_role = _require_role(role)
    validated = validate_mobile_runtime_observation(observation)
    require_native_observation(validated["source_kind"])
    if validated["platform_family"] != platform:
        _reject("capability_eligibility_platform_evidence_mismatch")

    blockers: list[str] = []
    prior: dict[str, str] = {}
    if prior_rung_decisions is None:
        blockers.append("prior_rung_proof_not_supplied")
    else:
        require_rung_isolation("capability_eligibility", prior_rung_decisions)
        prior = {str(name): str(value) for name, value in prior_rung_decisions.items()}

    constraints: dict[str, Any] | None = None
    if role_constraints is None:
        blockers.append("role_specific_constraints_missing")
    else:
        if not isinstance(role_constraints, Mapping) or not role_constraints:
            _reject("capability_eligibility_constraints_invalid")
        constraints = {}
        for name, value in role_constraints.items():
            if not isinstance(name, str) or not _METRIC_NAME.match(name):
                _reject("capability_eligibility_constraints_invalid")
            if isinstance(value, bool) or isinstance(value, str):
                constraints[name] = value
            else:
                _finite(value, code="capability_eligibility_constraints_invalid")
                constraints[name] = float(value)

    families: dict[str, str] = {}
    if capability_sources is None:
        blockers.append("capability_family_observations_missing")
        families = {family: "unknown" for family in REQUIRED_GATE_FAMILIES}
    else:
        families = require_capability_family_sources(capability_sources)
        family_blockers = capability_family_blockers(capability_sources)
        blockers.extend(family_blockers.values())

    if not blockers:
        decision = "eligible"
    elif any(code.endswith("_cannot_qualify") or code == CAPABILITY_FAMILY_UNKNOWN_BLOCKER for code in blockers):
        decision = "ineligible"
    else:
        decision = "withheld"

    now = _now_ms(now_unix_ms)
    record = _rung_record(
        rung_id="capability_eligibility",
        decision=decision,
        member_ref=member,
        platform_family=platform,
        role=exact_role,
        source_kind=str(validated["source_kind"]),
        blockers=blockers,
        now_unix_ms=now,
        validity_ms=_validity_ms(validity_ms),
        prior_rungs=prior,
        metrics=validated["metrics"],
        profile_generation=int(validated["profile_generation"]),
        evidence_generation=int(validated["evidence_generation"]),
        role_constraints=constraints,
        capability_family_sources=families,
    )
    return validate_rung_proof(record)


def promote_assignment_artifact(
    *,
    member_ref: object,
    assignment_ref: object,
    artifact_digest: object,
    promotion: object = "atomic",
    artifact_format: object = "inert_assignment_local",
    integrity: object = "digest_verified",
    quota_headroom: object = "sustainable",
    cancellation_policy: object = "bounded",
    acquisition: object = "bounded_resumable",
    now_unix_ms: object = None,
    validity_ms: object = None,
) -> dict[str, Any]:
    """Rung 3 (``a2_provisioner``): author the assignment-local artifact rung.

    The rung records an assignment-scoped, digest-bound, atomically promoted artifact
    in a supported inert format with bounded resumable acquisition, sustainable
    headroom and a bounded cancellation policy.  Cache presence, membership and
    capability eligibility are not inputs and can never satisfy it, and the record
    states that the A2 client — not this gate — owns the acquisition and integrity
    check.
    """

    member = _text(member_ref, code="artifact_readiness_member_invalid")
    assignment = _text(assignment_ref, code="artifact_readiness_assignment_invalid")
    digest = _digest(artifact_digest, code="artifact_readiness_digest_invalid")
    if promotion != "atomic":
        _reject("artifact_promotion_must_be_atomic")
    if artifact_format not in ARTIFACT_FORMATS:
        _reject("artifact_format_unsupported")
    if integrity not in ARTIFACT_INTEGRITY_EVIDENCE:
        _reject("artifact_integrity_evidence_invalid")
    if quota_headroom not in ARTIFACT_QUOTA_HEADROOM:
        _reject("artifact_quota_headroom_invalid")
    if cancellation_policy not in ARTIFACT_CANCELLATION_POLICIES:
        _reject("artifact_cancellation_policy_invalid")
    if acquisition not in ARTIFACT_ACQUISITION_POLICIES:
        _reject("artifact_acquisition_policy_invalid")

    now = _now_ms(now_unix_ms)
    record = _rung_record(
        rung_id="artifact_readiness",
        decision="eligible",
        member_ref=member,
        platform_family=None,
        role=None,
        source_kind="unknown",
        blockers=[],
        now_unix_ms=now,
        validity_ms=_validity_ms(validity_ms),
        prior_rungs={},
        metrics={},
        assignment_ref=assignment,
        artifact_digest=digest,
        acquisition_authority=RUNG_AUTHORITIES["artifact_readiness"],
        artifact_format=str(artifact_format),
        integrity_evidence=str(integrity),
        quota_headroom=str(quota_headroom),
        cancellation_policy=str(cancellation_policy),
        acquisition_policy=str(acquisition),
        cache_presence_used_as_proof=False,
    )
    return validate_rung_proof(record)


def author_operation_qualification(
    *,
    member_ref: object,
    role: object,
    platform_family: object,
    runtime_build_digest: object,
    prior_rung_decisions: object,
    physical_execution: object = None,
    now_unix_ms: object = None,
    validity_ms: object = None,
) -> dict[str, Any]:
    """Rung 4 (``mobile_role_operation_qualifier``): the deterministic half only.

    This surface consumes the current applicable rung proofs and records the exact
    role's artifact decision.  It **cannot** author an eligible operation
    qualification: specification section 4.4 requires physically executed positive and
    negative native evidence, which only the owner-private physical gate produces.
    Without that evidence the record is ``withheld`` with the exact blocker, and a
    caller-supplied execution bundle can never stand in for it.
    """

    member = _text(member_ref, code="operation_qualification_member_invalid")
    exact_role = _require_role(role)
    platform = _require_mobile_platform(
        platform_family, code="operation_qualification_platform_invalid"
    )
    _digest(runtime_build_digest, code="operation_qualification_build_invalid")
    if not isinstance(prior_rung_decisions, Mapping):
        _reject("prior_rung_decisions_invalid")
    require_rung_isolation("operation_qualification", prior_rung_decisions)
    prior = {str(name): str(value) for name, value in prior_rung_decisions.items()}

    blockers: list[str] = []
    if exact_role == "probe_contributor":
        artifact_rung = require_artifact_rung_decision(
            exact_role,
            prior.get("artifact_readiness") or "not_applicable",
            "artifact_free_probe_role"
            if (prior.get("artifact_readiness") or "not_applicable") == "not_applicable"
            else None,
        )
    else:
        artifact_decision = prior.get("artifact_readiness")
        artifact_rung = {
            "rung_id": "artifact_readiness",
            "decision": artifact_decision,
            "reason": None,
        }
        if artifact_decision is None:
            blockers.append("artifact_readiness_rung_not_supplied")

    source_kind = "unknown"
    if physical_execution is not None:
        if not isinstance(physical_execution, Mapping):
            _reject("physical_execution_bundle_invalid")
        source_kind = str(physical_execution.get("source_kind"))
        require_native_observation(source_kind)
        _reject("physical_execution_requires_owner_private_gate")
    blockers = [PHYSICAL_EXECUTION_BLOCKER, *blockers]

    now = _now_ms(now_unix_ms)
    record = _rung_record(
        rung_id="operation_qualification",
        decision="withheld",
        member_ref=member,
        platform_family=platform,
        role=exact_role,
        source_kind=source_kind,
        blockers=blockers,
        now_unix_ms=now,
        validity_ms=_validity_ms(validity_ms),
        prior_rungs=prior,
        metrics={},
        artifact_rung=artifact_rung,
    )
    return validate_rung_proof(record)


# ---------------------------------------------------------------------------
# Declared but not implemented: every remaining seam refuses with a coded error
# ---------------------------------------------------------------------------
#
# The deterministic rung seams (enrollment, capability eligibility and artifact
# readiness) are implemented above.  What remains here is the execution half A12
# cannot reach without native hardware: physical operation qualification, the
# optional A11 draft binding, runtime-owned observation, the all-eight product
# projection and the owner-private physical gate.  Each refuses with a closed
# coded error and never returns success.


def qualify_mobile_operation(
    *,
    member_ref: object,
    role: object,
    platform_family: object,
    runtime_build_digest: object,
    prior_rung_decisions: object,
    observation_envelopes: object = None,
    physical_gate: object = None,
    member_key_digest: object = None,
    family_ref: object = None,
    now_unix_ms: object = None,
    validity_ms: object = None,
) -> dict[str, Any]:
    """Rung 4: qualify one exact native Android operation from runtime evidence.

    Decided only from (a) member-signed runtime observations the native app sealed
    itself (``observe_mobile_runtime`` re-verifies every envelope here; a caller
    summary is never accepted) and (b) the owner-private executed physical gate
    bundle of ``run_mobile_physical_gate``.  Without that executed evidence there is
    nothing to qualify and the seam refuses with ``operation_qualification_absent``.

    The result is one closed ``mycelium.mobile_role_qualification.v1`` record for
    exactly this member, platform, runtime build and role.  It is ``eligible`` only
    when every blocker is clear; otherwise ``withheld`` with the exact blockers.
    Apple mobile is deferred (ruling R2) and the optional draft role still needs a
    physically closed A11 binding, so both refuse.
    """

    member = _text(member_ref, code="operation_qualification_member_invalid")
    if role not in MOBILE_ROLES:
        _reject("mobile_role_invalid")
    if platform_family not in MOBILE_PLATFORM_FAMILIES:
        _reject("operation_qualification_platform_invalid")
    build = _digest(runtime_build_digest, code="operation_qualification_build_invalid")
    require_rung_isolation("operation_qualification", prior_rung_decisions)
    if observation_envelopes is None and physical_gate is None:
        _refuse("operation_qualification_absent")
    if platform_family in APPLE_PLATFORM_FAMILIES:
        _reject(APPLE_DEFERRED_CODE)
    if role == "speculative_draft_worker":
        _refuse("draft_worker_qualification_absent")
    key_digest = _digest(member_key_digest, code="mobile_observation_member_key_unbound")
    family = _text(family_ref, code="android_family_ref_invalid")
    if not _FAMILY_REF.fullmatch(family):
        _reject("android_family_ref_invalid")
    now = _now_ms(now_unix_ms)
    if (
        isinstance(observation_envelopes, (str, bytes))
        or not isinstance(observation_envelopes, Sequence)
        or not observation_envelopes
    ):
        _reject("mobile_observations_absent")
    observations = [
        observe_mobile_runtime(
            member_ref=member,
            platform_family=platform_family,
            runtime_build_digest=build,
            observation_envelope=envelope,
            expected_member_key_digest=key_digest,
            now_unix_ms=now,
            maximum_age_ms=MAX_QUALIFYING_OBSERVATION_AGE_MS,
        )
        for envelope in observation_envelopes
    ]
    prior = {str(name): str(value) for name, value in dict(prior_rung_decisions).items()}
    blockers: list[str] = []
    for rung in ("signed_membership", "capability_eligibility"):
        if prior.get(rung) != "eligible":
            blockers.append(f"{rung}_not_current")
    artifact = prior.get("artifact_readiness")
    if role == "qualified_model_stage" and artifact != "eligible":
        blockers.append("artifact_readiness_not_current")
    if role == "probe_contributor" and artifact not in (None, "not_applicable"):
        blockers.append("artifact_rung_must_be_not_applicable_for_probe")

    generations = [item["observation"]["evidence_generation"] for item in observations]
    if generations != sorted(set(generations)):
        blockers.append("observation_generations_not_monotonic")
    covered = sum(float(item["observation"]["measurement_duration_ms"]) for item in observations)
    if covered < SUSTAINED_OBSERVATION_WINDOW_MS:
        blockers.append("sustained_observation_window_not_covered")
    families = OPERATION_GATE_FAMILIES[role]
    for family_name in families:
        for metric in GATE_FAMILY_METRICS[family_name]:
            values = [item["observation"]["metrics"].get(metric, _UNKNOWN_METRIC) for item in observations]
            if any(value == _UNKNOWN_METRIC for value in values):
                blockers.append(f"gate_family_unknown:{family_name}:{metric}")

    gate = validate_mobile_physical_gate(physical_gate)
    if gate["platform_family"] != platform_family or role not in gate["roles"]:
        blockers.append("physical_gate_not_for_this_platform_and_role")
    if gate["member_key_digest"] != key_digest or gate["runtime_build_digest"] != build:
        blockers.append("physical_gate_bound_to_another_member_or_build")
    if gate["family_ref"] != family:
        blockers.append("physical_gate_bound_to_another_family")
    for case_id in REQUIRED_MOBILE_POSITIVES[role]:
        if gate["positives"].get(case_id, {}).get("outcome") != "passed":
            blockers.append(f"physical_positive_not_passed:{case_id}")
    for case_id in REQUIRED_MOBILE_NEGATIVES:
        if gate["negatives"].get(case_id, {}).get("outcome") != "failed_closed":
            blockers.append(f"physical_negative_not_passed:{case_id}")
    if gate["adb_used_during_gate"] is not False:
        blockers.append("adb_used_during_gate")
    if gate["provenance"] != "live":
        blockers.append("non_live_provenance_cannot_qualify")
    observation_digests = [item["observation_digest"] for item in observations]
    if not set(observation_digests) <= set(gate["observation_digests"]):
        blockers.append("observation_not_bound_to_physical_gate")

    decision = "eligible" if not blockers else "withheld"
    return {
        "protocol": ROLE_QUALIFICATION_PROTOCOL,
        "rung_id": "operation_qualification",
        "authority": RUNG_AUTHORITIES["operation_qualification"],
        "decision": decision,
        "member_ref": member,
        "platform_family": platform_family,
        "role": role,
        "runtime_build_digest": build,
        "member_key_digest": key_digest,
        "claim_scope": ANDROID_FAMILY_BOUND_CLAIM_SCOPE,
        "family_ref": family,
        "grants": [RUNG_GRANTS["operation_qualification"]] if decision == "eligible" else [],
        "cannot_grant": list(RUNG_CANNOT_GRANT["operation_qualification"]),
        "blockers": sorted(set(blockers)),
        "route_ready": ROUTE_READY_AT_ENROLLMENT,
        "source_kind": NATIVE_SOURCE_KIND,
        "observation_digests": observation_digests,
        "observed_window_ms": covered,
        "physical_gate_digest": gate["gate_digest"],
        "issued_at_unix_ms": now,
        "expires_at_unix_ms": now + _validity_ms(validity_ms),
    }


def validate_mobile_physical_gate(bundle: object) -> dict[str, Any]:
    """Validate one owner-private executed physical gate bundle (closed shape)."""

    record = _closed(bundle, fields=PHYSICAL_GATE_FIELDS, code="mobile_physical_gate_fields_invalid")
    if record["protocol"] != ACTIVATION_QUALIFICATION_PROTOCOL:
        _reject("mobile_physical_gate_protocol_invalid")
    if record["platform_family"] not in MOBILE_PLATFORM_FAMILIES:
        _reject("mobile_physical_gate_platform_invalid")
    if (
        not isinstance(record["roles"], list)
        or not record["roles"]
        or any(role not in MOBILE_ROLES for role in record["roles"])
    ):
        _reject("mobile_physical_gate_role_invalid")
    _digest(record["member_key_digest"], code="mobile_physical_gate_binding_invalid")
    _digest(record["runtime_build_digest"], code="mobile_physical_gate_binding_invalid")
    if not _FAMILY_REF.fullmatch(str(record["family_ref"])):
        _reject("android_family_ref_invalid")
    require_undivided_product_path(record["execution_path"])
    for field in ("positives", "negatives"):
        cases = record[field]
        if not isinstance(cases, Mapping):
            _reject("mobile_physical_gate_cases_invalid")
        for case_id, result in cases.items():
            if (
                not isinstance(case_id, str)
                or not isinstance(result, Mapping)
                or result.get("outcome") not in PHYSICAL_CASE_OUTCOMES
            ):
                _reject("mobile_physical_gate_cases_invalid")
    if not isinstance(record["adb_used_during_gate"], bool):
        _reject("mobile_physical_gate_adb_invalid")
    if record["provenance"] not in ("live", "fixture", "replay", "historical"):
        _reject("mobile_physical_gate_provenance_invalid")
    if not isinstance(record["observation_digests"], list) or any(
        _SHA256.fullmatch(str(item)) is None for item in record["observation_digests"]
    ):
        _reject("mobile_physical_gate_observations_invalid")
    body = {key: value for key, value in record.items() if key != "gate_digest"}
    if record["gate_digest"] != _canonical_digest(body):
        _reject("mobile_physical_gate_digest_mismatch")
    return record


def _canonical_digest(value: object) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def bind_speculative_draft_worker(
    *,
    member_ref: object,
    a11_binding_state: object,
    a11_binding_digest: object = None,
) -> dict[str, Any]:
    """Declared optional A12 draft role.  Not implemented; fails closed first."""

    _text(member_ref, code="draft_worker_member_invalid")
    evaluate_optional_draft_claim(
        a11_binding_state=a11_binding_state, a11_binding_digest=a11_binding_digest
    )
    _refuse("a11_draft_binding_absent")


def observe_mobile_runtime(
    *,
    member_ref: object,
    platform_family: object,
    runtime_build_digest: object,
    observation_envelope: object = None,
    expected_member_key_digest: object = None,
    now_unix_ms: object = None,
    maximum_age_ms: object = None,
) -> dict[str, Any]:
    """Accept one runtime-owned observation sealed by the native node app.

    The observation must arrive inside the member-signed envelope the native core
    seals (``native/mobile_core`` ``evidence::sign_observation``): the exact
    enrolled member key signed a closed ``mycelium.mobile_runtime_observation.v1``
    with native source kind and the node-app authority, for this platform and build,
    and it is fresh.  The caller never authors an observation: without a sealed
    envelope the seam refuses with ``mobile_runtime_observation_absent``.
    """

    member = _text(member_ref, code="mobile_runtime_observation_member_invalid")
    if platform_family not in MOBILE_PLATFORM_FAMILIES:
        _reject("mobile_runtime_observation_platform_invalid")
    build = _digest(runtime_build_digest, code="mobile_runtime_observation_build_invalid")
    if observation_envelope is None:
        _refuse("mobile_runtime_observation_absent")
    if platform_family in APPLE_PLATFORM_FAMILIES:
        _reject(APPLE_DEFERRED_CODE)
    key_digest = _digest(expected_member_key_digest, code="mobile_observation_member_key_unbound")
    envelope = _closed(
        observation_envelope, fields=MEMBER_ENVELOPE_FIELDS, code="mobile_observation_envelope_invalid"
    )
    if envelope["protocol"] != MEMBER_ENVELOPE_PROTOCOL:
        _reject("mobile_observation_envelope_invalid")
    observation = validate_mobile_runtime_observation(envelope["message"])
    signature = envelope["signature"]
    if not isinstance(signature, Mapping) or signature.get("verification_key_digest") != key_digest:
        _reject("mobile_observation_signer_not_member")
    _verify_envelope_signature(observation, signature, envelope["verification_key"])
    require_native_observation(observation["source_kind"])
    if observation["authority"] != NATIVE_OBSERVATION_AUTHORITY:
        _reject("mobile_observation_not_runtime_owned")
    if observation["platform_family"] != platform_family:
        _reject("mobile_observation_platform_mismatch")
    if observation["runtime_build_digest"] != build:
        _reject("mobile_observation_build_mismatch")
    now = _now_ms(now_unix_ms)
    age_limit = (
        MAX_QUALIFYING_OBSERVATION_AGE_MS
        if maximum_age_ms is None
        else _finite(maximum_age_ms, code="mobile_observation_age_invalid", minimum=0.0)
    )
    observed_at = float(observation["observed_at_unix_ms"])
    if observed_at > now + MAX_CLOCK_SKEW_MS:
        _reject("mobile_observation_time_invalid")
    if now - observed_at > age_limit:
        _reject("mobile_observation_stale")
    return {
        "protocol": OBSERVATION_RECORD_PROTOCOL,
        "member_ref": member,
        "member_key_digest": key_digest,
        "observation": observation,
        "observation_digest": _canonical_digest(observation),
    }


def project_mobile_rungs(
    *, workspace: object, member_ref: object, source: object = None
) -> dict[str, Any]:
    """Project one member's four rungs and roles into one of the eight workspaces.

    ``source`` is one public generation of the rung ledger:
    ``{"generation": int, "freshness": str, "members": [{"member_ref",
    "platform_family", "rungs": {rung_id: rung record}, "roles": {role: role
    qualification record}}]}``.  Each rung renders only from its own record (a
    missing rung is ``unknown`` with a blocker, never inferred from another), only
    native evidence can show an eligible operation, Apple members stay deferred
    (ruling R2), and the projection carries decisions, blockers and source kinds
    only -- no endpoint, key, address, device or payload material.  Without a
    source generation there is nothing to project and the seam refuses.
    """

    if workspace not in WORKSPACES:
        _reject("mobile_projection_workspace_invalid")
    member = _text(member_ref, code="mobile_projection_member_invalid")
    if source is None:
        _refuse("mobile_projection_absent")
    if not isinstance(source, Mapping):
        _reject("mobile_projection_source_invalid")
    generation = _counter(source.get("generation"), code="mobile_projection_generation_invalid")
    freshness = source.get("freshness")
    if freshness not in PROJECTION_FRESHNESS:
        _reject("mobile_projection_freshness_invalid")
    members = source.get("members")
    if isinstance(members, (str, bytes)) or not isinstance(members, Sequence):
        _reject("mobile_projection_source_invalid")
    entry = next(
        (item for item in members if isinstance(item, Mapping) and item.get("member_ref") == member),
        None,
    )
    if entry is None:
        _reject("mobile_projection_member_absent")
    platform = entry.get("platform_family")
    if platform not in (*MOBILE_PLATFORM_FAMILIES, "unknown"):
        _reject("mobile_projection_platform_invalid")
    deferred = platform in APPLE_PLATFORM_FAMILIES

    def reduced(record: object, *, operation: bool) -> dict[str, Any]:
        if deferred:
            return {"decision": "withheld", "blockers": [APPLE_DEFERRED_CODE], "source_kind": "unknown"}
        if record is None:
            return {"decision": "unknown", "blockers": ["rung_record_absent"], "source_kind": "unknown"}
        if not isinstance(record, Mapping):
            _reject("mobile_projection_record_invalid")
        decision = record.get("decision")
        _decision_rank(decision)
        source_kind = record.get("source_kind", "unknown")
        if source_kind not in SOURCE_KINDS:
            _reject("mobile_projection_record_invalid")
        blockers = [str(item) for item in record.get("blockers", ())]
        if operation and decision == "eligible" and source_kind != NATIVE_SOURCE_KIND:
            return {
                "decision": "withheld",
                "blockers": [*blockers, "non_native_source_cannot_qualify"],
                "source_kind": source_kind,
            }
        return {"decision": decision, "blockers": blockers, "source_kind": source_kind}

    rungs_source = entry.get("rungs") or {}
    roles_source = entry.get("roles") or {}
    if not isinstance(rungs_source, Mapping) or not isinstance(roles_source, Mapping):
        _reject("mobile_projection_record_invalid")
    rungs = {
        rung: reduced(rungs_source.get(rung), operation=rung == "operation_qualification")
        for rung in RUNG_IDS
    }
    roles = {role: reduced(roles_source.get(role), operation=True) for role in MOBILE_ROLES}
    return {
        "protocol": MOBILE_PROJECTION_PROTOCOL,
        "workspace": workspace,
        "generation": generation,
        "freshness": freshness,
        "member_ref": member,
        "platform_family": platform,
        "platform_state": "deferred" if deferred else "active",
        "rungs": rungs,
        "roles": roles,
        "route_ready": ROUTE_READY_AT_ENROLLMENT,
    }


def run_mobile_physical_gate(
    *, platform_family: object, roles: object, execution: object = None
) -> dict[str, Any]:
    """Seal one owner-private executed physical gate bundle.

    ``execution`` is what the physical gate driver
    (``scripts/run_a12_mobile_physical_gate.py``) observed on hardware: the
    seed-recorded member key, the runtime build, the family reference, the product
    path, every positive and negative case outcome, whether ADB was used during the
    gate, provenance, and the digests of the member-signed observations it verified.
    The bundle is closed and digest-sealed (``mycelium.mobile_activation_qualification.v1``)
    so ``qualify_mobile_operation`` can detect any later edit.  Sealing records what
    ran; it decides nothing -- the role decision stays with
    ``qualify_mobile_operation``.  Without executed evidence the seam refuses; Apple
    mobile is deferred (ruling R2).
    """

    if platform_family not in MOBILE_PLATFORM_FAMILIES:
        _reject("mobile_physical_gate_platform_invalid")
    if (
        isinstance(roles, (str, bytes))
        or not isinstance(roles, Sequence)
        or not roles
        or any(role not in MOBILE_ROLES for role in roles)
    ):
        _reject("mobile_physical_gate_role_invalid")
    if execution is None:
        _refuse("mobile_physical_gate_absent")
    if platform_family in APPLE_PLATFORM_FAMILIES:
        _reject(APPLE_DEFERRED_CODE)
    executed = _closed(
        execution,
        fields=PHYSICAL_GATE_FIELDS - {"protocol", "platform_family", "roles", "gate_digest"},
        code="mobile_physical_gate_fields_invalid",
    )
    body = {
        "protocol": ACTIVATION_QUALIFICATION_PROTOCOL,
        "platform_family": platform_family,
        "roles": list(roles),
        **executed,
    }
    return validate_mobile_physical_gate({**body, "gate_digest": _canonical_digest(body)})


# name -> refusal code, in the order the specification's rungs are ordered.  The
# deterministic rungs are absent by construction: they are implemented above, and
# ``BOUNDARY_CODES`` stays exactly the codes this module can still refuse with.
BOUNDARIES: tuple[tuple[str, str], ...] = (
    ("qualify_mobile_operation", "operation_qualification_absent"),
    ("bind_speculative_draft_worker", "a11_draft_binding_absent"),
    ("observe_mobile_runtime", "mobile_runtime_observation_absent"),
    ("project_mobile_rungs", "mobile_projection_absent"),
    ("run_mobile_physical_gate", "mobile_physical_gate_absent"),
)

BOUNDARY_CODES: frozenset[str] = frozenset(code for _, code in BOUNDARIES)

# ---------------------------------------------------------------------------
# Native Android operation qualification vocabulary (implementation-lane owned,
# contract resolution 11): the metric names the native core
# (``native/mobile_core/src/evidence.rs``, ``node.rs``) seals, per gate family.
# ---------------------------------------------------------------------------

#: [2026-09-29 OWNER RULING R2] Apple mobile is deferred; A12 is Android-only.
APPLE_DEFERRED_CODE = "apple_mobile_deferred_by_owner_ruling_r2"
NATIVE_OBSERVATION_AUTHORITY = "mycelium_node_app"
OBSERVATION_RECORD_PROTOCOL = "mycelium.mobile_runtime_observation_record.v1"
MAX_QUALIFYING_OBSERVATION_AGE_MS = 6 * 3_600_000.0
#: The frozen sustained window the observations must jointly cover (10 minutes).
SUSTAINED_OBSERVATION_WINDOW_MS = 600_000.0
GATE_FAMILY_METRICS: dict[str, tuple[str, ...]] = {
    "thermal": ("thermal_max_celsius",),
    "power": ("battery_level_percent", "charging"),
    "memory": ("memory_available_bytes",),
    "lifecycle": ("lifecycle_member",),
    "network": ("relay_online",),
    "artifact": ("stage_loaded",),
    "parity": ("frames_executed_window",),
}
OPERATION_GATE_FAMILIES: dict[str, tuple[str, ...]] = {
    "probe_contributor": ("thermal", "power", "memory", "lifecycle", "network"),
    "qualified_model_stage": REQUIRED_GATE_FAMILIES,
    "speculative_draft_worker": REQUIRED_GATE_FAMILIES,
}
REQUIRED_MOBILE_POSITIVES: dict[str, tuple[str, ...]] = {
    "probe_contributor": ("off_tailnet_join", "reconnect", "native_evidence_published"),
    "qualified_model_stage": (
        "off_tailnet_join",
        "reconnect",
        "native_evidence_published",
        "router_frames_executed",
        "native_parity",
        "browser_request_completed",
    ),
    "speculative_draft_worker": (),
}
#: Section 11 Android negatives this lane's physical gate driver executes.
REQUIRED_MOBILE_NEGATIVES = (
    "background",
    "network_loss",
    "revocation",
    "process_death",
    "thermal_window",
)
PHYSICAL_CASE_OUTCOMES = ("passed", "failed_closed", "violated", "not_executed")
MOBILE_PROJECTION_PROTOCOL = "mycelium.mobile_rung_projection.v1"
PROJECTION_FRESHNESS = ("live", "stale", "historical", "fixture", "unknown")
PHYSICAL_GATE_FIELDS = frozenset(
    {
        "protocol",
        "platform_family",
        "roles",
        "member_key_digest",
        "runtime_build_digest",
        "family_ref",
        "execution_path",
        "positives",
        "negatives",
        "adb_used_during_gate",
        "provenance",
        "observation_digests",
        "gate_digest",
    }
)


def scaffold_status() -> dict[str, Any]:
    """Report exactly what this gate is: deterministic rungs plus a refusal edge."""

    return {
        "protocol": SCAFFOLD_PROTOCOL,
        "gate_id": A12_GATE_ID,
        "state": SCAFFOLD_STATE,
        "claim_boundary": CLAIM_BOUNDARY,
        "implemented_claim": IMPLEMENTED_CLAIM,
        "specification": SPECIFICATION,
        "specification_digest": SPECIFICATION_DIGEST,
        "implementation_contract": IMPLEMENTATION_CONTRACT,
        "acceptance_inventory": ACCEPTANCE_INVENTORY,
        "acceptance_inventory_digest": ACCEPTANCE_INVENTORY_DIGEST,
        "acceptance_scenarios": ACCEPTANCE_SCENARIOS,
        "acceptance_scenarios_digest": ACCEPTANCE_SCENARIOS_DIGEST,
        "declared_contract_protocols": list(DECLARED_CONTRACT_PROTOCOLS),
        "rung_ids": list(RUNG_IDS),
        "mobile_roles": list(MOBILE_ROLES),
        "earned_closure_requirements": sorted(EARNED_CLOSURE_REQUIREMENTS),
        "implemented_behaviour": list(IMPLEMENTED_BEHAVIOUR),
        "boundaries": [{"entry_point": name, "code": code} for name, code in BOUNDARIES],
    }


__all__ = [
    "A11_ABSENT_BINDING_REASON",
    "A11_CLAIM_ID",
    "A11_FAILURE_MODE",
    "A11_REQUIRED_DEPENDENCY_STATE",
    "A12ContractError",
    "A12NotImplementedError",
    "A12ScaffoldError",
    "A12_GATE_ID",
    "ACCEPTANCE_INVENTORY",
    "ACCEPTANCE_INVENTORY_DIGEST",
    "ACCEPTANCE_SCENARIOS",
    "ACCEPTANCE_SCENARIOS_DIGEST",
    "ACTIVATION_QUALIFICATION_PROTOCOL",
    "ANDROID_ARCHITECTURE",
    "ANDROID_CLAIM_SCOPES",
    "ANDROID_FAMILY_BOUND_CLAIM_SCOPE",
    "ANDROID_GENERAL_CLAIM_SCOPE",
    "APPLE_INITIAL_ELIGIBILITY",
    "APPLE_PLATFORM_FAMILIES",
    "BOUNDARIES",
    "BOUNDARY_CODES",
    "CAPABILITY_REPORT_PROTOCOL",
    "CLAIM_BOUNDARY",
    "DECLARED_CONTRACT_PROTOCOLS",
    "EARNED_CLOSURE_REQUIREMENTS",
    "EXCLUDED_NORMAL_PRODUCT_PATHS",
    "IMPLEMENTATION_CONTRACT",
    "IMPLEMENTED_BEHAVIOUR",
    "IMPLEMENTED_CLAIM",
    "INITIAL_BUILD_ANDROID_FAMILY",
    "MINIMUM_ANDROID_FAMILIES",
    "MOBILE_PLATFORM_FAMILIES",
    "MOBILE_ROLES",
    "NATIVE_SOURCE_KIND",
    "NON_NATIVE_SOURCE_KINDS",
    "OBSERVATION_FIELDS",
    "PEER_PROFILE_PROTOCOL",
    "REQUIRED_GATE_FAMILIES",
    "ROLE_QUALIFICATION_PROTOCOL",
    "ROUTE_READY_AT_ENROLLMENT",
    "RUNG_AUTHORITIES",
    "RUNG_CANNOT_GRANT",
    "RUNG_CURRENT_DECISIONS",
    "RUNG_DECISIONS",
    "RUNG_GRANTS",
    "RUNG_IDS",
    "RUNG_PROOF_PROTOCOL",
    "RUNG_REQUIRES",
    "RUNTIME_OBSERVATION_PROTOCOL",
    "SCAFFOLD_PROTOCOL",
    "SCAFFOLD_STATE",
    "SOURCE_KINDS",
    "SPECIFICATION",
    "SPECIFICATION_DIGEST",
    "TWO_FAMILY_REQUIREMENT_REACTIVATION_TRIGGER",
    "WORKSPACES",
    "author_operation_qualification",
    "bind_speculative_draft_worker",
    "capability_family_blockers",
    "enroll_mobile_member",
    "evaluate_optional_draft_claim",
    "observe_mobile_runtime",
    "project_mobile_rungs",
    "promote_assignment_artifact",
    "qualify_capability_eligibility",
    "qualify_mobile_operation",
    "require_android_family_evidence",
    "require_apple_platform_isolation",
    "require_artifact_rung_decision",
    "require_capability_family_sources",
    "require_native_observation",
    "require_route_ready_is_false",
    "require_rung_isolation",
    "require_undivided_product_path",
    "run_mobile_physical_gate",
    "scaffold_status",
    "sign_member_profile",
    "sign_member_report",
    "validate_mobile_runtime_observation",
    "validate_rung_proof",
    "verify_member_signed_profile",
    "verify_member_signed_report",
]
