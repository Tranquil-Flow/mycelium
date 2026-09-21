"""A7 fenced KV-successor recovery -- deterministic contract scaffold.

DETERMINISTIC CORE / NO RUNTIME / NOT PRODUCT-INTEGRATED.

Executable half of the A7 implementation contract:
`docs/superpowers/specs/2026-09-20-mycelium-a7-successor-recovery-implementation-contract.md`.
It refines -- and never widens -- the A7 gate specification
(`docs/superpowers/specs/2026-08-18-mycelium-a7-fenced-kv-successor-recovery.md`) for
the part of A7 that does not depend on A6 integration.

Implemented here (pure, in-memory, deterministic; no I/O, no processes, no threads):

* closed-shape validation for the two pre-integration A7 records
  (`mycelium.private_kv_checkpoint.v1`, `mycelium.private_kv_checkpoint_ack.v1`);
* the route-wide acknowledged-watermark algebra over the frozen `watermark_contract`
  in `tests/a7_acceptance/inventory.v1.json`;
* the exact compatibility-identity comparison over the frozen identity groups;
* the monotonic fence ledger, fence-install gating and the single-publication CAS;
* the closed cutover-eligibility predicate that maps every rejection to A6
  full-context replay or an honest abort;
* the closed standby lifecycle transition table;
* the honest claim boundary (`scaffold_claim_boundary`).

Deliberately NOT implemented (raises `A7ScaffoldBoundary`): every runtime,
transport, private-payload, durable-persistence, cutover-execution and orchestration
seam (`SCAFFOLD_BOUNDARIES`), and the four record shapes whose fields the
specification names but does not freeze.

A7 remains `design_only`: nothing here executes, integrates, qualifies, promotes, or
physically proves anything, and A6 is untouched.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

CHECKPOINT_PROTOCOL = "mycelium.private_kv_checkpoint.v1"
CHECKPOINT_ACK_PROTOCOL = "mycelium.private_kv_checkpoint_ack.v1"
STANDBY_INTENT_PROTOCOL = "mycelium.kv_standby_intent.v1"
SUCCESSOR_RUNTIME_PROTOCOL = "mycelium.kv_successor_runtime.v1"
SUCCESSOR_QUALIFICATION_PROTOCOL = "mycelium.kv_successor_qualification.v1"
RECOVERY_GATE_PROTOCOL = "mycelium.kv_successor_recovery_gate.v1"
ROUTE_WATERMARK_PROTOCOL = "mycelium.a7_route_watermark.v1"
COMPATIBILITY_RESULT_PROTOCOL = "mycelium.a7_kv_compatibility_result.v1"
FENCE_STATE_PROTOCOL = "mycelium.a7_fence_state.v1"
CUTOVER_ELIGIBILITY_PROTOCOL = "mycelium.a7_cutover_eligibility.v1"
CLAIM_BOUNDARY_PROTOCOL = "mycelium.a7_scaffold_claim_boundary.v1"

SCAFFOLD_STATE = "scaffold_unintegrated"

OUTCOMES = ("fenced_kv_successor", "full_context_replay", "aborted")

FENCE_INSTALLERS = (
    "request_gateway",
    "router",
    "surviving_stage_runtimes",
    "transport_ingress",
    "standby_runtime",
)

WATERMARK_ACTIONS = {
    "partial_acknowledgement": "report_lag_without_cutover_authority",
    "ahead_of_gateway": "reject_kv_successor",
    "behind_gateway": "require_a6_fallback_or_abort",
    "exact_repeat": "idempotent_without_authority_advance",
    "skipped_regressed_or_conflicting": "reject_kv_successor",
}

COMPATIBILITY_IDENTITY_GROUPS: dict[str, list[str]] = {
    "model": [
        "model_id",
        "immutable_revision",
        "representation_manifest_digest",
        "architecture",
        "tokenizer_semantics",
        "checkpoint_schema_version",
    ],
    "layer_assignment": [
        "half_open_layer_range",
        "component_roles",
        "placement_assignment_id",
        "complete_model_operation_path",
    ],
    "runtime": [
        "backend",
        "runtime_family",
        "runtime_build",
        "decode_mode",
        "device_layout_encoding",
        "byte_order",
    ],
    "cache": [
        "cache_implementation",
        "attention_layout",
        "query_head_count",
        "kv_head_count",
        "head_dimension",
        "batch_shape",
        "dtype",
        "quantization_effect",
        "rope_configuration_and_scaling",
        "position_semantics",
        "sequence_semantics",
        "context_policy",
    ],
    "authority_and_freshness": [
        "session_id",
        "request_id",
        "source_attempt",
        "source_path_generation",
        "source_membership_incarnation",
        "successor_membership_incarnation",
        "checkpoint_sequence_and_digest",
        "source_load_and_qualification_generation",
        "successor_load_and_qualification_generation",
        "directed_path_evidence_generation",
        "resource_reservation_generation",
    ],
}

COMPATIBILITY_IDENTITY_FIELDS = tuple(
    field for group in COMPATIBILITY_IDENTITY_GROUPS.values() for field in group
)

COMPATIBILITY_RESULT_FIELDS = frozenset(
    {"protocol", "compatible", "mismatches", "unknown_fields", "compared_fields"}
)
CUTOVER_ELIGIBILITY_FIELDS = frozenset(
    {"protocol", "outcome", "kv_successor_eligible", "reason", "a6_available"}
)
FENCE_STATE_FIELDS = frozenset(
    {"protocol", "request_attempt", "path_generation", "fencing_generation"}
)
ROUTE_WATERMARK_FIELDS = frozenset(
    {
        "protocol",
        "request_attempt",
        "committed_token_count",
        "committed_prefix_digest",
        "stage_ids",
    }
)
CLAIM_BOUNDARY_FIELDS = frozenset(
    {
        "protocol",
        "gate_state",
        "scaffold_state",
        "claim_boundary",
        "unearned_closure_requirements",
        "unearned_count",
        "runtime_executed",
        "integrated",
        "physical_evidence",
        "qualification_claim",
        "promotion_authorized",
        "a6_dependency_integrated",
    }
)

_CHECKPOINT_FIELDS = frozenset(
    {
        "protocol",
        "checkpoint_id",
        "request_id",
        "session_id",
        "request_attempt",
        "source_placement_id",
        "source_membership_incarnation",
        "layer_range",
        "checkpoint_sequence",
        "predecessor_digest",
        "committed_token_count",
        "committed_prefix_digest",
        "kv_position",
        "byte_size",
        "encrypted_payload_reference",
        "expires_at_unix_ms",
        "source_authority_generation",
        "evidence_digest",
    }
)
_ACK_FIELDS = frozenset(
    {
        "protocol",
        "ack_id",
        "request_id",
        "session_id",
        "request_attempt",
        "stage_id",
        "standby_placement_id",
        "standby_membership_incarnation",
        "checkpoint_sequence",
        "checkpoint_digest",
        "predecessor_digest",
        "committed_token_count",
        "committed_prefix_digest",
        "stored_checkpoint_digest_verified",
        "stored_checkpoint_digest",
        "resource_reservation_generation",
        "acknowledgement_authority",
        "ack_authority_generation",
        "evidence_digest",
    }
)

STANDBY_CHAIN = (
    "proposed",
    "acquiring",
    "loaded",
    "challenged",
    "qualified",
    "shadowing",
    "cutover_pending",
    "active_successor",
    "draining",
    "released",
)
STANDBY_STATES = STANDBY_CHAIN + ("rejected", "failed")


def _standby_transitions() -> dict[str, frozenset[str]]:
    transitions: dict[str, frozenset[str]] = {}
    for index, state in enumerate(STANDBY_CHAIN):
        targets = set() if state == "released" else {"rejected", "failed"}
        if index + 1 < len(STANDBY_CHAIN):
            targets.add(STANDBY_CHAIN[index + 1])
        transitions[state] = frozenset(targets)
    return transitions


STANDBY_TRANSITIONS = _standby_transitions()

UNEARNED_CLOSURE_REQUIREMENTS = frozenset(
    {
        "implementation",
        "product_integration",
        "deterministic_positive",
        "deterministic_negative",
        "physical_positive",
        "physical_negative",
        "ui_live_all_eight",
        "browser_navigation_reconnect",
        "browser_second_session",
        "full_regressions_audits",
        "architecture_handover",
        "atomic_feature_commit",
    }
)

SCAFFOLD_BOUNDARY_REASONS = frozenset(
    {
        "record_shape_not_frozen",
        "private_kv_payload_not_implemented",
        "checkpoint_transport_not_implemented",
        "durable_fence_persistence_not_implemented",
        "cutover_execution_not_implemented",
        "recovery_orchestration_not_implemented",
    }
)
SCAFFOLD_BOUNDARIES = {
    "validate_kv_standby_intent": "record_shape_not_frozen",
    "validate_kv_successor_runtime": "record_shape_not_frozen",
    "validate_kv_successor_qualification": "record_shape_not_frozen",
    "validate_kv_successor_recovery_gate": "record_shape_not_frozen",
    "seal_checkpoint_payload": "private_kv_payload_not_implemented",
    "open_checkpoint_payload": "private_kv_payload_not_implemented",
    "transfer_shadow_checkpoint": "checkpoint_transport_not_implemented",
    "persist_fence_generation": "durable_fence_persistence_not_implemented",
    "perform_fenced_cutover": "cutover_execution_not_implemented",
    "run_successor_recovery": "recovery_orchestration_not_implemented",
}

LEDGER_REJECTION_REASONS = frozenset(
    {
        "ack_shape_invalid",
        "ack_unknown_stage",
        "stale_attempt",
        "stale_incarnation",
        "conflicting_repeat",
        "regressed_position",
        "skipped_predecessor",
        "predecessor_mismatch",
        "conflicting_stage_agreement",
    }
)
FENCE_REASONS = frozenset(
    {
        "fence_state_invalid",
        "stale_generation",
        "duplicate_publisher",
        "unknown_participant",
    }
)
ELIGIBILITY_REASONS = frozenset(
    {
        "eligible",
        "route_watermark_unavailable",
        "watermark_ahead_of_gateway",
        "watermark_behind_gateway",
        "prefix_digest_mismatch",
        "compatibility_incompatible",
        "qualification_not_current",
        "fence_not_installed",
    }
)

ACK_OUTCOMES = ("advanced", "idempotent", "accepted_route_pending")

_CHECKPOINT_INVALID = "a7_checkpoint_invalid"
_ACK_INVALID = "a7_checkpoint_ack_invalid"


class A7ScaffoldBoundary(NotImplementedError):
    """Raised where the A7 scaffold intentionally has no behaviour yet."""


class A7ContractViolation(ValueError):
    """Fail-closed A7 contract rejection carrying a closed reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def binding_digest(document: Mapping[str, Any]) -> str:
    """Self-binding canonical digest of a record without its evidence_digest."""

    unsigned = {
        key: value for key, value in document.items() if key != "evidence_digest"
    }
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _closed(document: Any, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != fields:
        raise ValueError(code)
    return copy.deepcopy(dict(document))


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(code)
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    if any(char not in "0123456789abcdef" for char in value[7:]):
        raise ValueError(code)
    return value


def _integer(value: Any, minimum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(code)
    return value


def _bounded_text(value: Any, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(code)
    return value


def _expected_digest(document: dict[str, Any], code: str) -> None:
    if document.get("evidence_digest") != binding_digest(document):
        raise ValueError(code)


# ---------------------------------------------------------------------------
# Closed records
# ---------------------------------------------------------------------------


def validate_private_kv_checkpoint(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed-shape validation for `mycelium.private_kv_checkpoint.v1`."""

    result = _closed(document, _CHECKPOINT_FIELDS, _CHECKPOINT_INVALID)
    if result["protocol"] != CHECKPOINT_PROTOCOL:
        raise ValueError(_CHECKPOINT_INVALID)
    for field in (
        "checkpoint_id",
        "request_id",
        "session_id",
        "source_placement_id",
        "source_membership_incarnation",
        "committed_prefix_digest",
    ):
        _sha(result[field], _CHECKPOINT_INVALID)
    _integer(result["request_attempt"], 1, _CHECKPOINT_INVALID)
    _integer(result["checkpoint_sequence"], 0, _CHECKPOINT_INVALID)
    sequence = result["checkpoint_sequence"]
    predecessor = result["predecessor_digest"]
    if sequence == 0:
        if predecessor is not None:
            raise ValueError(_CHECKPOINT_INVALID)
    else:
        _sha(predecessor, _CHECKPOINT_INVALID)
    committed = _integer(result["committed_token_count"], 0, _CHECKPOINT_INVALID)
    position = _integer(result["kv_position"], 0, _CHECKPOINT_INVALID)
    if position != committed:
        raise ValueError(_CHECKPOINT_INVALID)
    layer_range = result["layer_range"]
    if (
        not isinstance(layer_range, list)
        or len(layer_range) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in layer_range)
    ):
        raise ValueError(_CHECKPOINT_INVALID)
    start, end = layer_range
    if not (0 <= start < end <= 4096):
        raise ValueError(_CHECKPOINT_INVALID)
    _integer(result["byte_size"], 1, _CHECKPOINT_INVALID)
    _bounded_text(result["encrypted_payload_reference"], 256, _CHECKPOINT_INVALID)
    _integer(result["expires_at_unix_ms"], 1, _CHECKPOINT_INVALID)
    _integer(result["source_authority_generation"], 1, _CHECKPOINT_INVALID)
    _expected_digest(result, _CHECKPOINT_INVALID)
    return result


def validate_private_kv_checkpoint_ack(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed-shape validation for `mycelium.private_kv_checkpoint_ack.v1`."""

    result = _closed(document, _ACK_FIELDS, _ACK_INVALID)
    if result["protocol"] != CHECKPOINT_ACK_PROTOCOL:
        raise ValueError(_ACK_INVALID)
    for field in (
        "ack_id",
        "request_id",
        "session_id",
        "stage_id",
        "standby_placement_id",
        "standby_membership_incarnation",
        "checkpoint_digest",
        "committed_prefix_digest",
        "stored_checkpoint_digest",
    ):
        _sha(result[field], _ACK_INVALID)
    _integer(result["request_attempt"], 1, _ACK_INVALID)
    _integer(result["checkpoint_sequence"], 0, _ACK_INVALID)
    sequence = result["checkpoint_sequence"]
    predecessor = result["predecessor_digest"]
    if sequence == 0:
        if predecessor is not None:
            raise ValueError(_ACK_INVALID)
    else:
        _sha(predecessor, _ACK_INVALID)
    _integer(result["committed_token_count"], 0, _ACK_INVALID)
    if result["stored_checkpoint_digest_verified"] is not True:
        raise ValueError(_ACK_INVALID)
    if result["stored_checkpoint_digest"] != result["checkpoint_digest"]:
        raise ValueError(_ACK_INVALID)
    _integer(result["resource_reservation_generation"], 1, _ACK_INVALID)
    if result["acknowledgement_authority"] != "standby_runtime":
        raise ValueError(_ACK_INVALID)
    _integer(result["ack_authority_generation"], 1, _ACK_INVALID)
    _expected_digest(result, _ACK_INVALID)
    return result


# ---------------------------------------------------------------------------
# Route-wide acknowledged watermark (implementation contract D1, D2, D6)
# ---------------------------------------------------------------------------


class RouteAckLedger:
    """Route-wide acknowledged KV watermark over the frozen watermark_contract.

    In-memory only: this is the deterministic ledger algebra, not durable
    persistence, network receipt, or any runtime integration.
    """

    def __init__(
        self,
        *,
        replaced_stage_ids: Sequence[str],
        request_attempt: int,
        current_incarnations: Mapping[str, str],
    ) -> None:
        stage_ids = tuple(replaced_stage_ids)
        if (
            not stage_ids
            or any(not isinstance(stage, str) or not stage for stage in stage_ids)
            or len(set(stage_ids)) != len(stage_ids)
        ):
            raise A7ContractViolation("ledger_stage_set_invalid")
        if (
            isinstance(request_attempt, bool)
            or not isinstance(request_attempt, int)
            or request_attempt < 1
        ):
            raise A7ContractViolation("ledger_attempt_invalid")
        if (
            not isinstance(current_incarnations, Mapping)
            or set(current_incarnations) != set(stage_ids)
            or any(
                not isinstance(value, str) or not value
                for value in current_incarnations.values()
            )
        ):
            raise A7ContractViolation("ledger_incarnation_set_invalid")
        self._replaced = frozenset(stage_ids)
        self._attempt = request_attempt
        self._incarnations = dict(current_incarnations)
        self._last: dict[str, dict[str, Any]] = {}
        self._watermark: dict[str, Any] | None = None

    def record(self, ack: Mapping[str, Any]) -> str:
        """Accept, repeat, or fail closed; returns an `ACK_OUTCOMES` value."""

        try:
            document = validate_private_kv_checkpoint_ack(ack)
        except (TypeError, ValueError) as exc:
            raise A7ContractViolation("ack_shape_invalid") from exc

        stage = document["stage_id"]
        if stage not in self._replaced:
            raise A7ContractViolation("ack_unknown_stage")
        if document["request_attempt"] != self._attempt:
            raise A7ContractViolation("stale_attempt")
        if document["standby_membership_incarnation"] != self._incarnations[stage]:
            raise A7ContractViolation("stale_incarnation")

        previous = self._last.get(stage)
        if previous is not None:
            if document["checkpoint_sequence"] == previous["checkpoint_sequence"]:
                if (
                    document["checkpoint_digest"] == previous["checkpoint_digest"]
                    and document["committed_token_count"]
                    == previous["committed_token_count"]
                    and document["committed_prefix_digest"]
                    == previous["committed_prefix_digest"]
                ):
                    return "idempotent"
                raise A7ContractViolation("conflicting_repeat")
            if document["checkpoint_sequence"] < previous["checkpoint_sequence"]:
                raise A7ContractViolation("regressed_position")
            if document["checkpoint_sequence"] != previous["checkpoint_sequence"] + 1:
                raise A7ContractViolation("skipped_predecessor")
            if document["predecessor_digest"] != previous["checkpoint_digest"]:
                raise A7ContractViolation("predecessor_mismatch")
            if document["committed_token_count"] < previous["committed_token_count"]:
                raise A7ContractViolation("regressed_position")
        else:
            if document["checkpoint_sequence"] != 0:
                raise A7ContractViolation("skipped_predecessor")
            if document["predecessor_digest"] is not None:
                raise A7ContractViolation("predecessor_mismatch")

        entry = {
            "checkpoint_sequence": document["checkpoint_sequence"],
            "checkpoint_digest": document["checkpoint_digest"],
            "committed_token_count": document["committed_token_count"],
            "committed_prefix_digest": document["committed_prefix_digest"],
        }
        candidate = dict(self._last)
        candidate[stage] = entry
        advanced = self._refresh_watermark(candidate)
        self._last = candidate
        return "advanced" if advanced else "accepted_route_pending"

    def _refresh_watermark(self, candidate: Mapping[str, dict[str, Any]]) -> bool:
        if len(candidate) != len(self._replaced):
            return False
        counts = {entry["committed_token_count"] for entry in candidate.values()}
        digests = {entry["committed_prefix_digest"] for entry in candidate.values()}
        if len(counts) != 1 or len(digests) != 1:
            self._reject_watermark_conflict(candidate)
            return False
        count = next(iter(counts))
        digest = next(iter(digests))
        if self._watermark is not None:
            current = self._watermark["committed_token_count"]
            if count < current:
                raise A7ContractViolation("conflicting_stage_agreement")
            if count == current:
                if digest != self._watermark["committed_prefix_digest"]:
                    raise A7ContractViolation("conflicting_stage_agreement")
                return False
        self._watermark = {
            "protocol": ROUTE_WATERMARK_PROTOCOL,
            "request_attempt": self._attempt,
            "committed_token_count": count,
            "committed_prefix_digest": digest,
            "stage_ids": sorted(self._replaced),
        }
        return True

    def _reject_watermark_conflict(
        self, candidate: Mapping[str, dict[str, Any]]
    ) -> None:
        if self._watermark is None:
            return
        current = self._watermark["committed_token_count"]
        for entry in candidate.values():
            if entry["committed_token_count"] < current or (
                entry["committed_token_count"] == current
                and entry["committed_prefix_digest"]
                != self._watermark["committed_prefix_digest"]
            ):
                raise A7ContractViolation("conflicting_stage_agreement")

    @property
    def route_watermark(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._watermark)

    @property
    def pending_stage_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._replaced - set(self._last)))

    @property
    def stage_positions(self) -> dict[str, int | None]:
        return {
            stage: (
                self._last[stage]["committed_token_count"]
                if stage in self._last
                else None
            )
            for stage in sorted(self._replaced)
        }

    def lag_against(self, committed_token_count: int) -> int | None:
        """Gateway count minus acknowledged count; None while unknown."""

        if (
            isinstance(committed_token_count, bool)
            or not isinstance(committed_token_count, int)
            or committed_token_count < 0
        ):
            raise A7ContractViolation("lag_input_invalid")
        if self._watermark is None:
            return None
        return committed_token_count - self._watermark["committed_token_count"]


# ---------------------------------------------------------------------------
# Compatibility identity (implementation contract D8)
# ---------------------------------------------------------------------------


def compare_compatibility(
    source: Mapping[str, Any], successor: Mapping[str, Any]
) -> dict[str, Any]:
    """Exact compatibility comparison; unknown or differing = incompatible."""

    def _closed_identity(value: Mapping[str, Any]) -> None:
        # Absent fields are unknown compatibility (incompatible, never assumed);
        # extra keys are unknown identity data and fail closed (contract D8).
        if not isinstance(value, Mapping) or not set(value) <= set(
            COMPATIBILITY_IDENTITY_FIELDS
        ):
            raise A7ContractViolation("compatibility_identity_invalid")
        for identity in value.values():
            if identity is not None and (
                not isinstance(identity, str) or not identity or len(identity) > 256
            ):
                raise A7ContractViolation("compatibility_identity_invalid")

    _closed_identity(source)
    _closed_identity(successor)

    mismatches: list[str] = []
    unknown: list[str] = []
    for field in COMPATIBILITY_IDENTITY_FIELDS:
        left = source.get(field)
        right = successor.get(field)
        if left is None or right is None:
            unknown.append(field)
        elif left != right:
            mismatches.append(field)
    return {
        "protocol": COMPATIBILITY_RESULT_PROTOCOL,
        "compatible": not mismatches and not unknown,
        "mismatches": sorted(mismatches),
        "unknown_fields": sorted(unknown),
        "compared_fields": len(COMPATIBILITY_IDENTITY_FIELDS),
    }


# ---------------------------------------------------------------------------
# Fencing (implementation contract D9, D10)
# ---------------------------------------------------------------------------


class FenceLedger:
    """Monotonic fence generations, all-installer receipts, publication CAS.

    In-memory only: durable persistence of the generation before successor decode
    is NOT implemented (`persist_fence_generation` raises `A7ScaffoldBoundary`).
    """

    def __init__(
        self,
        *,
        request_attempt: int,
        path_generation: int,
        fencing_generation: int,
    ) -> None:
        for value in (request_attempt, path_generation, fencing_generation):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise A7ContractViolation("fence_state_invalid")
        self._attempt = request_attempt
        self._path = path_generation
        self._fence = fencing_generation
        self._installed: set[str] = set()
        self._published_for: int | None = None

    @property
    def fencing_generation(self) -> int:
        return self._fence

    @property
    def state(self) -> dict[str, Any]:
        return {
            "protocol": FENCE_STATE_PROTOCOL,
            "request_attempt": self._attempt,
            "path_generation": self._path,
            "fencing_generation": self._fence,
        }

    def advance(self) -> dict[str, Any]:
        """In-memory advance of attempt/path/fence; voids prior receipts."""

        self._attempt += 1
        self._path += 1
        self._fence += 1
        self._installed = set()
        self._published_for = None
        return self.state

    def accepts_generation(self, generation: int) -> bool:
        if isinstance(generation, bool) or not isinstance(generation, int):
            return False
        return generation == self._fence

    def install(self, participant: str) -> None:
        if participant not in FENCE_INSTALLERS:
            raise A7ContractViolation("unknown_participant")
        self._installed.add(participant)

    @property
    def installed_participants(self) -> tuple[str, ...]:
        return tuple(
            participant
            for participant in FENCE_INSTALLERS
            if participant in self._installed
        )

    @property
    def fence_installed(self) -> bool:
        return len(self._installed) == len(FENCE_INSTALLERS)

    def grant_publication(self, generation: int) -> None:
        """Compare-and-swap publication grant: once per current generation."""

        if not self.accepts_generation(generation):
            raise A7ContractViolation("stale_generation")
        if self._published_for == generation:
            raise A7ContractViolation("duplicate_publisher")
        self._published_for = generation


# ---------------------------------------------------------------------------
# Cutover eligibility (implementation contract D7, D11)
# ---------------------------------------------------------------------------


def _require_watermark(route_watermark: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(route_watermark, Mapping)
        or set(route_watermark) != ROUTE_WATERMARK_FIELDS
        or route_watermark.get("protocol") != ROUTE_WATERMARK_PROTOCOL
    ):
        raise A7ContractViolation("route_watermark_invalid")
    try:
        _integer(route_watermark["request_attempt"], 1, "route_watermark_invalid")
        _integer(
            route_watermark["committed_token_count"], 0, "route_watermark_invalid"
        )
        _sha(route_watermark["committed_prefix_digest"], "route_watermark_invalid")
    except ValueError as exc:
        raise A7ContractViolation("route_watermark_invalid") from exc
    stage_ids = route_watermark["stage_ids"]
    if (
        not isinstance(stage_ids, list)
        or not stage_ids
        or any(not isinstance(stage, str) or not stage for stage in stage_ids)
        or stage_ids != sorted(stage_ids)
        or len(set(stage_ids)) != len(stage_ids)
    ):
        raise A7ContractViolation("route_watermark_invalid")
    return copy.deepcopy(dict(route_watermark))


def evaluate_cutover_eligibility(
    *,
    route_watermark: Mapping[str, Any] | None,
    gateway_committed_token_count: int,
    gateway_committed_prefix_digest: str,
    compatibility: Mapping[str, Any],
    qualifier_current: bool,
    fence_installed: bool,
    a6_available: bool,
) -> dict[str, Any]:
    """Closed cutover-eligibility predicate mapping rejections to A6 or abort."""

    try:
        gateway_count = _integer(
            gateway_committed_token_count, 0, "eligibility_input_invalid"
        )
        gateway_digest = _sha(
            gateway_committed_prefix_digest, "eligibility_input_invalid"
        )
    except ValueError as exc:
        raise A7ContractViolation("eligibility_input_invalid") from exc
    if (
        type(qualifier_current) is not bool
        or type(fence_installed) is not bool
        or type(a6_available) is not bool
    ):
        raise A7ContractViolation("eligibility_input_invalid")
    if (
        not isinstance(compatibility, Mapping)
        or set(compatibility) != COMPATIBILITY_RESULT_FIELDS
        or compatibility.get("protocol") != COMPATIBILITY_RESULT_PROTOCOL
        or type(compatibility.get("compatible")) is not bool
        or not isinstance(compatibility.get("mismatches"), list)
        or not isinstance(compatibility.get("unknown_fields"), list)
        or type(compatibility.get("compared_fields")) is not int
        or compatibility["compared_fields"] != len(COMPATIBILITY_IDENTITY_FIELDS)
        or (
            compatibility["compatible"]
            and (compatibility["mismatches"] or compatibility["unknown_fields"])
        )
    ):
        raise A7ContractViolation("compatibility_result_invalid")

    reason = "eligible"
    if route_watermark is None:
        reason = "route_watermark_unavailable"
    else:
        watermark = _require_watermark(route_watermark)
        watermark_count = watermark["committed_token_count"]
        if watermark_count > gateway_count:
            reason = "watermark_ahead_of_gateway"
        elif watermark_count < gateway_count:
            reason = "watermark_behind_gateway"
        elif watermark["committed_prefix_digest"] != gateway_digest:
            reason = "prefix_digest_mismatch"
    if reason == "eligible" and compatibility["compatible"] is not True:
        reason = "compatibility_incompatible"
    if reason == "eligible" and qualifier_current is not True:
        reason = "qualification_not_current"
    if reason == "eligible" and fence_installed is not True:
        reason = "fence_not_installed"

    eligible = reason == "eligible"
    outcome = (
        "fenced_kv_successor"
        if eligible
        else ("full_context_replay" if a6_available else "aborted")
    )
    return {
        "protocol": CUTOVER_ELIGIBILITY_PROTOCOL,
        "outcome": outcome,
        "kv_successor_eligible": eligible,
        "reason": reason,
        "a6_available": a6_available,
    }


# ---------------------------------------------------------------------------
# Standby lifecycle (implementation contract D12)
# ---------------------------------------------------------------------------


def transition_standby(current: str, target: str) -> str:
    """Closed standby lifecycle transition; illegal targets fail closed."""

    if current not in STANDBY_TRANSITIONS or target not in STANDBY_STATES:
        raise A7ContractViolation("illegal_standby_transition")
    if target not in STANDBY_TRANSITIONS[current]:
        raise A7ContractViolation("illegal_standby_transition")
    return target


# ---------------------------------------------------------------------------
# Claim boundary (implementation contract D13)
# ---------------------------------------------------------------------------


def scaffold_claim_boundary() -> dict[str, Any]:
    """Honest A7 scaffold claim boundary; never promotes or qualifies."""

    return {
        "protocol": CLAIM_BOUNDARY_PROTOCOL,
        "gate_state": "design_only",
        "scaffold_state": SCAFFOLD_STATE,
        "claim_boundary": (
            "A7 scaffold only: deterministic contract surface, no runtime, transport, "
            "cutover, product integration, physical evidence, qualification, or "
            "promotion claim; A6 untouched and still the blocking dependency."
        ),
        "unearned_closure_requirements": sorted(UNEARNED_CLOSURE_REQUIREMENTS),
        "unearned_count": len(UNEARNED_CLOSURE_REQUIREMENTS),
        "runtime_executed": False,
        "integrated": False,
        "physical_evidence": False,
        "qualification_claim": False,
        "promotion_authorized": False,
        "a6_dependency_integrated": False,
    }


# ---------------------------------------------------------------------------
# Explicit scaffold boundaries (implementation contract D14, section 4)
# ---------------------------------------------------------------------------


def _boundary(name: str) -> A7ScaffoldBoundary:
    return A7ScaffoldBoundary(
        f"a7_scaffold_boundary:{name}:{SCAFFOLD_BOUNDARIES[name]}"
    )


def validate_kv_standby_intent(document: Mapping[str, Any]) -> dict[str, Any]:
    """Deferred: `mycelium.kv_standby_intent.v1` shape is not frozen yet."""

    raise _boundary("validate_kv_standby_intent")


def validate_kv_successor_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    """Deferred: `mycelium.kv_successor_runtime.v1` shape is not frozen yet."""

    raise _boundary("validate_kv_successor_runtime")


def validate_kv_successor_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
    """Deferred: `mycelium.kv_successor_qualification.v1` shape is not frozen."""

    raise _boundary("validate_kv_successor_qualification")


def validate_kv_successor_recovery_gate(document: Mapping[str, Any]) -> dict[str, Any]:
    """Deferred: `mycelium.kv_successor_recovery_gate.v1` shape is not frozen."""

    raise _boundary("validate_kv_successor_recovery_gate")


def seal_checkpoint_payload(
    *, checkpoint: Mapping[str, Any], kv_payload: object
) -> dict[str, Any]:
    """Deferred: private stage-local KV payload sealing is not implemented."""

    raise _boundary("seal_checkpoint_payload")


def open_checkpoint_payload(*, reference: str) -> dict[str, Any]:
    """Deferred: private stage-local KV payload opening is not implemented."""

    raise _boundary("open_checkpoint_payload")


def transfer_shadow_checkpoint(
    *, checkpoint: Mapping[str, Any], standby_placement_id: str
) -> None:
    """Deferred: the bounded authenticated checkpoint channel is not implemented."""

    raise _boundary("transfer_shadow_checkpoint")


def persist_fence_generation(*, fence_state: Mapping[str, Any]) -> None:
    """Deferred: durable fence persistence before successor decode is not implemented."""

    raise _boundary("persist_fence_generation")


def perform_fenced_cutover(
    *, fence_state: Mapping[str, Any], checkpoint: Mapping[str, Any]
) -> dict[str, Any]:
    """Deferred: atomic cutover execution is not implemented."""

    raise _boundary("perform_fenced_cutover")


def run_successor_recovery(*, request_id: str) -> dict[str, Any]:
    """Deferred: runtime recovery orchestration is not implemented."""

    raise _boundary("run_successor_recovery")
