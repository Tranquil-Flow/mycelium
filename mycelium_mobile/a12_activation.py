"""A12 scaffold: generic mobile activation rungs, evidence records and honest refusals.

SCAFFOLD ONLY.  This module implements no mobile enrollment, no eligibility
qualification, no artifact readiness, no operation qualification, no physical gate
and no product projection.  It declares the A12 entry points, validates their closed
input shapes, applies the five freeze-framed honesty guards, and then refuses every
execution seam with an explicit coded exception (``A12NotImplementedError``).  It
never returns a value that could be mistaken for an earned mobile rung, role or
qualification.

Why it exists: the A12 specification
(``docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md``)
is ``design_only`` and leaves several implementation seams ambiguous.  The companion
implementation contract
(``docs/superpowers/specs/2026-09-21-mycelium-a12-implementation-contract.md``)
resolves those seams and names the records, guards and coded refusals below.  A
future implementation lane replaces the refusals with real behaviour and flips the
strong-RED suite in ``tests/a12_scaffold/``.

State: ``design_only``.  ``EARNED_CLOSURE_REQUIREMENTS`` is empty by construction:
this scaffold earns none of the thirteen live closure requirements of
``scripts/mycelium_completion_audit.py``.  The spec, the acceptance inventory and the
scenario manifest remain the only frozen inputs, and their digests are pinned below.

What is genuinely implemented here (and claimed as nothing more): closed-shape
validation of a runtime observation, the rung/family/source-kind/product-path
honesty guards, the fail-closed optional-draft policy, and the explicit refusal
boundary.  Shape validation and policy arithmetic are not execution evidence.

The scaffold deliberately does not modify ``mycelium_mobile/__init__.py``,
``contracts/``, any manifest or the completion checklist: exporting these names and
registering the three declared protocols are owner-reviewed steps of the
implementation lane.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, NoReturn, Sequence

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
# bytes at the acceptance base c573e0f).  The boundary suite recomputes them.
SPECIFICATION_DIGEST = (
    "sha256:bd825c5e3de166be43b730d647a164e692b978be378a23469e96184e7f6c5df8"
)
ACCEPTANCE_INVENTORY_DIGEST = (
    "sha256:a65834dc0b78fedb949b38342220e453dd6caff6708d1e5c791daac5794c7c16"
)
ACCEPTANCE_SCENARIOS_DIGEST = (
    "sha256:66d82d9683fc1132051dbd2d8a6fdb6d356a08d6b09cccb7172b25652b2fc9b2"
)

CLAIM_BOUNDARY = (
    "scaffold only: no mobile enrollment, capability eligibility, artifact "
    "readiness, operation qualification, physical gate, product projection or "
    "completion claim; no closure requirement earned; every execution seam refuses "
    "with a coded error and never returns success"
)
IMPLEMENTED_CLAIM = (
    "closed-shape validation, source-kind/family/product-path/rung honesty guards, "
    "and the fail-closed optional draft policy only"
)

# The capability-named A12 records of specification section 5.  They are declared
# here and owned by this gate while the gate is a scaffold; registering them in a
# wire contract or the contract manifest is an implementation-lane step.
DECLARED_CONTRACT_PROTOCOLS = (
    "mycelium.mobile_runtime_observation.v1",
    "mycelium.mobile_role_qualification.v1",
    "mycelium.mobile_activation_qualification.v1",
)

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
MOBILE_ROLES = (
    "probe_contributor",
    "speculative_draft_worker",
    "qualified_model_stage",
)
ANDROID_ARCHITECTURE = "generic_registered_native_android"
MINIMUM_ANDROID_FAMILIES = 2
APPLE_PLATFORM_FAMILIES = ("ios", "ipados")
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
    "evaluate_optional_draft_claim",
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


def require_android_family_evidence(family_refs: object) -> tuple[str, ...]:
    """A general Android claim needs two distinct pseudonymous device families.

    One family is insufficient, a repeated family is not diversity, and the
    ordinary product contracts must be the same for both (inventory decision
    ``two_android_family_proof``).
    """

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
    if len(families) < MINIMUM_ANDROID_FAMILIES:
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
# Declared but not implemented: every execution seam refuses with a coded error
# ---------------------------------------------------------------------------


def enroll_mobile_member(
    *,
    member_ref: object,
    platform_family: object,
    source_kind: object,
    route_ready: object,
) -> dict[str, Any]:
    """Declared A12 rung 1 (signed membership).  Not implemented."""

    _text(member_ref, code="mobile_enrollment_member_invalid")
    if platform_family not in ("android", *APPLE_PLATFORM_FAMILIES):
        _reject("mobile_enrollment_platform_invalid")
    require_native_observation(source_kind)
    require_route_ready_is_false(route_ready)
    _refuse("mobile_enrollment_absent")


def qualify_capability_eligibility(
    *,
    member_ref: object,
    platform_family: object,
    role: object,
    observation: object,
) -> dict[str, Any]:
    """Declared A12 rung 2 (capability eligibility).  Not implemented."""

    _text(member_ref, code="capability_eligibility_member_invalid")
    if platform_family not in ("android", *APPLE_PLATFORM_FAMILIES):
        _reject("capability_eligibility_platform_invalid")
    if role not in MOBILE_ROLES:
        _reject("mobile_role_invalid")
    validate_mobile_runtime_observation(observation)
    _refuse("capability_eligibility_absent")


def promote_assignment_artifact(
    *,
    member_ref: object,
    assignment_ref: object,
    artifact_digest: object,
    promotion: object = "atomic",
) -> dict[str, Any]:
    """Declared A12 rung 3 (artifact readiness).  Not implemented."""

    _text(member_ref, code="artifact_readiness_member_invalid")
    _text(assignment_ref, code="artifact_readiness_assignment_invalid")
    _digest(artifact_digest, code="artifact_readiness_digest_invalid")
    if promotion != "atomic":
        _reject("artifact_promotion_must_be_atomic")
    _refuse("artifact_readiness_absent")


def qualify_mobile_operation(
    *,
    member_ref: object,
    role: object,
    platform_family: object,
    runtime_build_digest: object,
    prior_rung_decisions: object,
) -> dict[str, Any]:
    """Declared A12 rung 4 (operation qualification).  Not implemented."""

    _text(member_ref, code="operation_qualification_member_invalid")
    if role not in MOBILE_ROLES:
        _reject("mobile_role_invalid")
    if platform_family not in ("android", *APPLE_PLATFORM_FAMILIES):
        _reject("operation_qualification_platform_invalid")
    _digest(runtime_build_digest, code="operation_qualification_build_invalid")
    require_rung_isolation("operation_qualification", prior_rung_decisions)
    _refuse("operation_qualification_absent")


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
) -> dict[str, Any]:
    """Declared runtime-owned observation seam.  Not implemented."""

    _text(member_ref, code="mobile_runtime_observation_member_invalid")
    if platform_family not in ("android", *APPLE_PLATFORM_FAMILIES):
        _reject("mobile_runtime_observation_platform_invalid")
    _digest(runtime_build_digest, code="mobile_runtime_observation_build_invalid")
    _refuse("mobile_runtime_observation_absent")


def project_mobile_rungs(*, workspace: object, member_ref: object) -> dict[str, Any]:
    """Declared privacy-reduced all-eight projection seam.  Not implemented."""

    if workspace not in WORKSPACES:
        _reject("mobile_projection_workspace_invalid")
    _text(member_ref, code="mobile_projection_member_invalid")
    _refuse("mobile_projection_absent")


def run_mobile_physical_gate(
    *, platform_family: object, roles: object
) -> dict[str, Any]:
    """Declared owner-private physical/browser gate seam.  Not implemented."""

    if platform_family not in ("android", *APPLE_PLATFORM_FAMILIES):
        _reject("mobile_physical_gate_platform_invalid")
    if (
        isinstance(roles, (str, bytes))
        or not isinstance(roles, Sequence)
        or not roles
        or any(role not in MOBILE_ROLES for role in roles)
    ):
        _reject("mobile_physical_gate_role_invalid")
    _refuse("mobile_physical_gate_absent")


# name -> refusal code, in the order the specification's rungs are ordered.
BOUNDARIES: tuple[tuple[str, str], ...] = (
    ("enroll_mobile_member", "mobile_enrollment_absent"),
    ("qualify_capability_eligibility", "capability_eligibility_absent"),
    ("promote_assignment_artifact", "artifact_readiness_absent"),
    ("qualify_mobile_operation", "operation_qualification_absent"),
    ("bind_speculative_draft_worker", "a11_draft_binding_absent"),
    ("observe_mobile_runtime", "mobile_runtime_observation_absent"),
    ("project_mobile_rungs", "mobile_projection_absent"),
    ("run_mobile_physical_gate", "mobile_physical_gate_absent"),
)

BOUNDARY_CODES: frozenset[str] = frozenset(code for _, code in BOUNDARIES)


def scaffold_status() -> dict[str, Any]:
    """Report exactly what this scaffold is: a refusal boundary, nothing more."""

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
    "ANDROID_ARCHITECTURE",
    "APPLE_INITIAL_ELIGIBILITY",
    "APPLE_PLATFORM_FAMILIES",
    "BOUNDARIES",
    "BOUNDARY_CODES",
    "CLAIM_BOUNDARY",
    "DECLARED_CONTRACT_PROTOCOLS",
    "EARNED_CLOSURE_REQUIREMENTS",
    "EXCLUDED_NORMAL_PRODUCT_PATHS",
    "IMPLEMENTATION_CONTRACT",
    "IMPLEMENTED_BEHAVIOUR",
    "IMPLEMENTED_CLAIM",
    "MINIMUM_ANDROID_FAMILIES",
    "MOBILE_ROLES",
    "NATIVE_SOURCE_KIND",
    "NON_NATIVE_SOURCE_KINDS",
    "OBSERVATION_FIELDS",
    "REQUIRED_GATE_FAMILIES",
    "ROUTE_READY_AT_ENROLLMENT",
    "RUNG_AUTHORITIES",
    "RUNG_CURRENT_DECISIONS",
    "RUNG_DECISIONS",
    "RUNG_GRANTS",
    "RUNG_IDS",
    "SCAFFOLD_PROTOCOL",
    "SCAFFOLD_STATE",
    "SOURCE_KINDS",
    "SPECIFICATION",
    "SPECIFICATION_DIGEST",
    "WORKSPACES",
    "bind_speculative_draft_worker",
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
    "require_native_observation",
    "require_route_ready_is_false",
    "require_rung_isolation",
    "require_undivided_product_path",
    "run_mobile_physical_gate",
    "scaffold_status",
    "validate_mobile_runtime_observation",
]
