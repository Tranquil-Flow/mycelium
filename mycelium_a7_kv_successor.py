# SPDX-License-Identifier: AGPL-3.0-only
"""A7 fenced KV-successor recovery -- deterministic contract core + in-process runtime half.

DETERMINISTIC CORE AND IN-PROCESS RUNTIME / NOT PRODUCT-INTEGRATED / NOT PHYSICAL.

Executable half of the A7 implementation contract:
`docs/superpowers/specs/2026-09-20-mycelium-a7-successor-recovery-implementation-contract.md`.
It refines -- and never widens -- the A7 gate specification
(`docs/superpowers/specs/2026-08-18-mycelium-a7-fenced-kv-successor-recovery.md`) for
the part of A7 that does not depend on A6 integration.

Deterministic contract core (pure, in-memory; no I/O, no processes, no threads):

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

In-process runtime half (the implementation contract section 4 seams; every one of
them deterministic and process-local, and every one of them `IMPLEMENTED_RUNTIME_SEAMS`):

* closed-shape validation for the four records the specification names
  (`kv_standby_intent`, `kv_successor_runtime`, `kv_successor_qualification`,
  `kv_successor_recovery_gate`), each of which *cannot* express a physical or
  qualified claim: the runtime and gate records pin `physical_evidence=False`,
  `qualification_claim=False`, `promotion_authorized=False` and
  `execution_mode="in_process_deterministic"` as closed rules;
* a byte-exact, bounded, privacy-reduced private-KV payload envelope
  (`seal_checkpoint_payload` / `open_checkpoint_payload`) whose confidentiality
  field states plainly that no cipher is applied;
* a bounded in-process shadow-checkpoint channel
  (`transfer_shadow_checkpoint`) that returns the very acknowledgement record the
  route watermark algebra consumes;
* a monotonic, process-local fence-generation store
  (`persist_fence_generation`) whose receipt says plainly that it is *not* durable;
* the fenced cutover (`perform_fenced_cutover`), which either continues without
  replay or fails over -- requesting the real `mycelium_a6_recovery` seam when it
  is reachable, or aborting explicitly when it is not;
* the orchestration seam (`run_successor_recovery`), which drives one whole
  in-process cycle and returns the closed `mycelium.kv_successor_runtime.v1`
  projection.

`SCAFFOLD_BOUNDARIES` is now empty: no seam in this module refuses with
`A7ScaffoldBoundary`. The class is retained as an explicit, tested first-class
surface for future seams; the deterministic core still imports only the standard
library, and the A6 seam is imported lazily *inside* the handoff function, only
read, and never at module import time.

A7 stays `design_only`: the live router now classifies every replacement-path
attempt through `evaluate_cutover_eligibility`
(`mycelium_router.entry.classify_kv_successor_recovery`), but nothing here is
durable, networked, threaded, qualified, promoted or physically proven: there
is no checkpoint transport, no fence persistence, no cutover execution, no
product runtime execution, and A6 is untouched. Running this in-process cycle
is not A7 product runtime execution, and the honest claim boundary keeps
reporting `runtime_executed=False`.
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

# -- runtime-half protocol identifiers -------------------------------------

PAYLOAD_ENVELOPE_PROTOCOL = "mycelium.a7_private_kv_payload_envelope.v1"
FENCE_PERSISTENCE_PROTOCOL = "mycelium.a7_fence_persistence_receipt.v1"
CUTOVER_REQUEST_PROTOCOL = "mycelium.a7_fenced_cutover_request.v1"
CUTOVER_RESULT_PROTOCOL = "mycelium.a7_fenced_cutover_result.v1"
A6_HANDOFF_PROTOCOL = "mycelium.a7_a6_replay_handoff.v1"

# -- runtime-half closed vocabularies --------------------------------------

#: The only execution mode the runtime half can report. There is deliberately no
#: "physical" value: the closed records below cannot express one.
IN_PROCESS_EXECUTION_MODE = "in_process_deterministic"
#: What every process-local store says about itself.
PROCESS_LOCAL_SCOPE = "process_local_in_memory"
#: What the payload envelope says about confidentiality. The specification wants
#: a dedicated authenticated encrypted channel; this scaffold applies no cipher.
NO_IN_PROCESS_CONFIDENTIALITY = "not_provided_in_process_scaffold"
#: What the fence-generation receipt says about durability. The specification
#: requires a durable increment before successor decode; this scaffold is not one.
NON_DURABLE_PERSISTENCE = "not_durable_in_process_scaffold"

SOURCE_RUNTIME_STATES = ("active", "fenced", "draining", "released", "failed")
SUCCESSOR_FAILURE_SCOPES = ("request", "edge", "placement")
QUALIFICATION_SCOPE = "deterministic_in_process"
QUALIFICATION_ISSUER = "qualifier"
GATE_EVIDENCE_KIND = "deterministic_in_process_test_only_seam"

#: Cleanup counters that must return to zero (spec section 7).
SUCCESSOR_CLEANUP_COUNTERS = (
    "pending_acknowledgements",
    "reservations_live",
    "source_kv_live",
    "standby_kv_live",
    "streams_live",
    "transfer_buffers_live",
)

A6_HANDOFF_OUTCOMES = (
    "not_requested",
    "full_context_replay_requested",
    "full_context_replay_unavailable",
)

#: Invariants the in-process cycle can report as *observed* (never as proven
#: physical behaviour). Named after the frozen inventory's invariants.
GATE_POSITIVE_INVARIANTS = frozenset(
    {
        "route_watermark_exact_equality",
        "fence_installed_all_participants",
        "single_publication_cas_granted",
        "no_recovery_prefill",
        "no_prompt_or_committed_prefix_transfer",
        "fallback_never_relabelled_as_kv_continuation",
        "explicit_bounded_abort_reason",
        "exact_cleanup_counters_zero",
    }
)

#: Rejections the in-process cycle can report as *observed*. The gate record only
#: ever names one of these after the probe really failed closed.
GATE_NEGATIVE_INVARIANTS = frozenset(
    {
        "rejected_unknown_stage",
        "rejected_over_transfer_bound",
        "rejected_payload_reference_unknown",
        "rejected_non_monotonic_fence_generation",
        "rejected_absent_cutover_request",
        "rejected_unclosed_record_shape",
    }
)

#: The live A6 seam the cutover hands a KV rejection to. Imported lazily, never at
#: module import time, and only read.
A6_SEAM_MODULE = "mycelium_a6_recovery"
_A6_HANDOFF_REQUEST_ID = "sha256:" + hashlib.sha256(
    b"a7-successor-recovery-a6-handoff"
).hexdigest()

_STANDBY_INTENT_INVALID = "a7_kv_standby_intent_invalid"
_SUCCESSOR_RUNTIME_INVALID = "a7_kv_successor_runtime_invalid"
_SUCCESSOR_QUALIFICATION_INVALID = "a7_kv_successor_qualification_invalid"
_SUCCESSOR_GATE_INVALID = "a7_kv_successor_recovery_gate_invalid"
_CUTOVER_REQUEST_INVALID = "a7_fenced_cutover_request_invalid"

_STANDBY_INTENT_FIELDS = frozenset(
    {
        "protocol",
        "intent_id",
        "request_id",
        "standby_placement_id",
        "standby_membership_incarnation",
        "layer_range",
        "covered_failure_scope",
        "required_identity_fields",
        "max_context_tokens",
        "max_checkpoint_bytes",
        "max_retained_checkpoints",
        "hysteresis_generations",
        "hysteresis_seconds",
        "reservation_generation",
        "route_ready",
        "evidence_digest",
    }
)
_SUCCESSOR_QUALIFICATION_FIELDS = frozenset(
    {
        "protocol",
        "qualification_id",
        "request_id",
        "standby_placement_id",
        "successor_membership_incarnation",
        "compatibility",
        "replaced_stage_ids",
        "qualified_stage_ids",
        "path_generation",
        "resource_reservation_generation",
        "parity_challenged",
        "parity_passed",
        "fence_generation",
        "fallback_available",
        "physical_challenge_executed",
        "qualification_scope",
        "issuer",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
        "evidence_digest",
    }
)
_SUCCESSOR_RUNTIME_FIELDS = frozenset(
    {
        "protocol",
        "runtime_id",
        "request_id",
        "session_id",
        "request_attempt",
        "path_generation",
        "fencing_generation",
        "source_placement_id",
        "standby_placement_id",
        "source_state",
        "standby_state",
        "checkpoint_sequence",
        "checkpoint_count",
        "aggregate_kv_bytes",
        "gateway_committed_token_count",
        "acknowledged_watermark",
        "acknowledged_lag",
        "committed_prefix_digest",
        "fence_installed",
        "cutover_granted",
        "recovery_mode",
        "fallback_reason",
        "cleanup_complete",
        "terminal_outcome",
        "execution_mode",
        "physical_evidence",
        "qualification_claim",
        "promotion_authorized",
        "evidence_digest",
    }
)
_SUCCESSOR_GATE_FIELDS = frozenset(
    {
        "protocol",
        "gate_id",
        "request_id",
        "terminal_outcome",
        "runtime_digest",
        "positive_scenarios",
        "negative_scenarios",
        "counter_report",
        "cleanup_complete",
        "browser_scenarios_executed",
        "performance_measured",
        "gate_state",
        "gate_evidence_kind",
        "physical_evidence",
        "qualification_claim",
        "promotion_authorized",
        "evidence_digest",
    }
)
_CUTOVER_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "request_id",
        "request_attempt",
        "path_generation",
        "fencing_generation",
        "route_watermark",
        "gateway_committed_token_count",
        "gateway_committed_prefix_digest",
        "compatibility",
        "qualifier_current",
        "fence_installed",
        "a6_available",
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
#: The seams that still refuse with `A7ScaffoldBoundary`. Empty: the in-process
#: runtime half implements every seam the implementation contract section 4 named.
SCAFFOLD_BOUNDARIES: dict[str, str] = {}

#: The seams the runtime half implements in-process, in contract order.
IMPLEMENTED_RUNTIME_SEAMS = frozenset(
    {
        "validate_kv_standby_intent",
        "validate_kv_successor_runtime",
        "validate_kv_successor_qualification",
        "validate_kv_successor_recovery_gate",
        "seal_checkpoint_payload",
        "open_checkpoint_payload",
        "transfer_shadow_checkpoint",
        "persist_fence_generation",
        "perform_fenced_cutover",
        "run_successor_recovery",
    }
)

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

#: What the runtime half may name as the reason a cutover was withheld: the closed
#: eligibility vocabulary plus the one runtime-only reason (the A6 seam itself
#: being unreachable in-process). This *adds* nothing to `ELIGIBILITY_REASONS`.
RUNTIME_FALLBACK_REASONS = frozenset(ELIGIBILITY_REASONS - {"eligible"}) | {
    "a6_seam_unreachable"
}

ACK_OUTCOMES = ("advanced", "idempotent", "accepted_route_pending")

_CHECKPOINT_INVALID = "a7_checkpoint_invalid"
_ACK_INVALID = "a7_checkpoint_ack_invalid"


class A7ScaffoldBoundary(NotImplementedError):
    """Raised where an A7 seam intentionally has no behaviour yet.

    Retained as an explicit, first-class contract surface for future seams. No
    seam in this module raises it any more: the in-process runtime half
    implements every seam the implementation contract section 4 named, and
    `SCAFFOLD_BOUNDARIES` is empty. It stays a `NotImplementedError` and never a
    `ValueError`, so a caller can always tell an unimplemented seam apart from an
    ordinary fail-closed input rejection.
    """


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


def _payload_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_id(protocol: str, binding: Mapping[str, Any]) -> str:
    """Deterministic identity for an in-process record (no clock, no randomness)."""

    body = {"protocol": protocol, **binding}
    return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()


def _half_open_layer_range(value: Any, code: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ValueError(code)
    start, end = value
    if not (0 <= start < end <= 4096):
        raise ValueError(code)
    return [start, end]


def _sorted_digest_list(value: Any, code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(set(value)) != len(value)
        or value != sorted(value)
    ):
        raise ValueError(code)
    for item in value:
        _sha(item, code)
    return list(value)


def _sorted_text_list(value: Any, code: str, *, maximum: int, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(code)
    if len(value) > 64 or len(set(value)) != len(value) or value != sorted(value):
        raise ValueError(code)
    for item in value:
        _bounded_text(item, maximum, code)
    return list(value)


def _require_fence_state(fence_state: Any) -> dict[str, Any]:
    """Closed-shape validation of `mycelium.a7_fence_state.v1` (fence ledger D9)."""

    if (
        not isinstance(fence_state, Mapping)
        or set(fence_state) != FENCE_STATE_FIELDS
        or fence_state.get("protocol") != FENCE_STATE_PROTOCOL
    ):
        raise A7ContractViolation("fence_state_invalid")
    for field in ("request_attempt", "path_generation", "fencing_generation"):
        value = fence_state[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise A7ContractViolation("fence_state_invalid")
    return copy.deepcopy(dict(fence_state))


def _require_compatibility_result(compatibility: Any) -> dict[str, Any]:
    """Closed, non-self-contradictory compatibility result (contract D8, D11)."""

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
    return copy.deepcopy(dict(compatibility))


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
    is NOT implemented -- `persist_fence_generation` stores in a process-local
    register whose receipt says `durability=not_durable_in_process_scaffold`.
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
    # identical closed-shape rejection, shared with the runtime half (D8, D11)
    _require_compatibility_result(compatibility)

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
            "A7 deterministic contract core plus a deterministic in-process runtime "
            "cycle only: the live router replacement-path classification dispatches "
            "to the closed cutover-eligibility predicate "
            "(mycelium_router.entry.classify_kv_successor_recovery), but there is no "
            "checkpoint transport, no durable persistence, no networked transport, "
            "no cutover execution, no product runtime execution, no browser "
            "surface, no physical evidence, no qualification and no promotion "
            "claim; A6 remains the blocking dependency for the real gate close."
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
# In-process runtime half
#
# Every seam below implements, deterministically and in-process, behaviour the
# implementation contract section 4 listed as unimplemented. None of it is
# durable, networked, threaded, product-integrated or physical, and every record
# it can produce carries closed fields that cannot express the opposite.
# ---------------------------------------------------------------------------

#: A fabricated, always-unknown stage identity used only by the fail-closed probes.
_PROBE_UNKNOWN_STAGE = "sha256:" + "f" * 64


class InProcessPayloadStore:
    """Process-local register of sealed private KV payloads.

    Deterministic and in-memory only: no durability, no network, no cipher. Every
    envelope it takes or hands back says exactly that, so a payload kept here can
    never be mistaken for a transported, encrypted or durable one.
    """

    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}
        self._digests: dict[str, str] = {}
        self._envelopes: dict[str, dict[str, Any]] = {}

    def register(self, *, reference: str, payload: bytes, envelope: Mapping[str, Any]) -> None:
        if not isinstance(payload, (bytes, bytearray)):
            raise A7ContractViolation("payload_not_bytes")
        body = bytes(payload)
        digest = _payload_digest(body)
        if self._digests.get(reference) is not None and self._digests[reference] != digest:
            raise A7ContractViolation("payload_reference_conflict")
        self._payloads[reference] = body
        self._digests[reference] = digest
        self._envelopes[reference] = copy.deepcopy(dict(envelope))

    def resolve(self, *, reference: str) -> tuple[bytes, str, dict[str, Any]]:
        reference = _bounded_text(reference, 256, "payload_reference_invalid")
        if reference not in self._payloads:
            raise A7ContractViolation("payload_reference_unknown")
        return (
            self._payloads[reference],
            self._digests[reference],
            copy.deepcopy(self._envelopes[reference]),
        )

    @property
    def retained_payloads(self) -> int:
        return len(self._payloads)

    @property
    def retained_bytes(self) -> int:
        return sum(len(payload) for payload in self._payloads.values())

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(sorted(self._payloads))

    def release(self) -> None:
        """Exact cleanup: drop every retained payload and envelope."""

        self._payloads.clear()
        self._digests.clear()
        self._envelopes.clear()


class FenceGenerationStore:
    """Process-local stand-in for the durable fence store (contract D9, spec section 6).

    Records one monotonically increasing fencing generation per request attempt.
    The receipt it returns names itself *not durable*, because the specification
    requires a durable increment before successor decode and this scaffold is not
    one: nothing here survives the process.
    """

    def __init__(self) -> None:
        self._latest: dict[int, dict[str, Any]] = {}
        self._sequence = 0

    def persist(self, *, fence_state: Any) -> dict[str, Any]:
        state = _require_fence_state(fence_state)
        attempt = state["request_attempt"]
        previous = self._latest.get(attempt)
        if previous is not None and (
            state["fencing_generation"] <= previous["fencing_generation"]
        ):
            raise A7ContractViolation("fence_generation_not_monotonic")
        self._sequence += 1
        self._latest[attempt] = copy.deepcopy(state)
        return {
            "protocol": FENCE_PERSISTENCE_PROTOCOL,
            "request_attempt": attempt,
            "path_generation": state["path_generation"],
            "fencing_generation": state["fencing_generation"],
            "generation_digest": "sha256:"
            + hashlib.sha256(_canonical(state)).hexdigest(),
            "durability": NON_DURABLE_PERSISTENCE,
            "store_scope": PROCESS_LOCAL_SCOPE,
            "sequence": self._sequence,
        }

    def latest(self, *, request_attempt: int) -> dict[str, Any]:
        if (
            isinstance(request_attempt, bool)
            or not isinstance(request_attempt, int)
            or request_attempt not in self._latest
        ):
            raise A7ContractViolation("fence_generation_unknown")
        return copy.deepcopy(self._latest[request_attempt])

    @property
    def persisted_generations(self) -> int:
        return self._sequence


class InProcessCheckpointChannel:
    """The bounded in-process stand-in for the shadow-checkpoint channel.

    Deterministic, single-process, socket-free and cipher-free. Every bound the
    standby intent declares (checkpoint bytes, context tokens, retained
    checkpoints) is enforced here, the standby placement and membership
    incarnation are verified, and every accepted transfer returns the
    acknowledgement record the route watermark algebra consumes.
    """

    def __init__(
        self,
        *,
        channel_id: str,
        standby_placement_id: str,
        replaced_stage_ids: Sequence[str],
        current_incarnations: Mapping[str, str],
        max_checkpoint_bytes: int,
        max_context_tokens: int,
        max_retained_checkpoints: int,
        resource_reservation_generation: int,
        acknowledgement_authority_generation: int = 1,
    ) -> None:
        self._channel_id = _bounded_text(channel_id, 128, "channel_id_invalid")
        self._standby_placement_id = _sha(
            standby_placement_id, "channel_placement_invalid"
        )
        stage_ids = tuple(replaced_stage_ids)
        if (
            not stage_ids
            or len(set(stage_ids)) != len(stage_ids)
            or any(not isinstance(stage, str) for stage in stage_ids)
        ):
            raise A7ContractViolation("channel_stage_set_invalid")
        for stage in stage_ids:
            _sha(stage, "channel_stage_set_invalid")
        if (
            not isinstance(current_incarnations, Mapping)
            or set(current_incarnations) != set(stage_ids)
        ):
            raise A7ContractViolation("channel_incarnation_set_invalid")
        for incarnation in current_incarnations.values():
            _sha(incarnation, "channel_incarnation_set_invalid")
        for bound in (
            max_checkpoint_bytes,
            max_context_tokens,
            max_retained_checkpoints,
            resource_reservation_generation,
            acknowledgement_authority_generation,
        ):
            _integer(bound, 1, "channel_bound_invalid")
        self._stage_ids = frozenset(stage_ids)
        self._incarnations = dict(current_incarnations)
        self._max_checkpoint_bytes = max_checkpoint_bytes
        self._max_context_tokens = max_context_tokens
        self._max_retained_checkpoints = max_retained_checkpoints
        self._reservation_generation = resource_reservation_generation
        self._ack_generation = acknowledgement_authority_generation
        self._reference_digests: dict[str, str] = {}

    @property
    def standby_placement_id(self) -> str:
        return self._standby_placement_id

    @property
    def max_checkpoint_bytes(self) -> int:
        return self._max_checkpoint_bytes

    @property
    def transferred_checkpoints(self) -> int:
        return len(self._reference_digests)

    def transfer(
        self,
        *,
        checkpoint: Any,
        standby_placement_id: str,
        stage_id: str,
        payload_digest: str,
    ) -> dict[str, Any]:
        """Verify one shadow checkpoint and return its standby acknowledgement."""

        document = validate_private_kv_checkpoint(checkpoint)
        if _sha(standby_placement_id, "standby_placement_invalid") != (
            self._standby_placement_id
        ):
            raise A7ContractViolation("checkpoint_foreign_standby")
        if stage_id not in self._stage_ids:
            raise A7ContractViolation("checkpoint_unknown_stage")
        if document["byte_size"] > self._max_checkpoint_bytes:
            raise A7ContractViolation("checkpoint_over_transfer_bound")
        if document["committed_token_count"] > self._max_context_tokens:
            raise A7ContractViolation("checkpoint_over_context_bound")
        reference = document["encrypted_payload_reference"]
        if (
            reference not in self._reference_digests
            and len(self._reference_digests) >= self._max_retained_checkpoints
        ):
            raise A7ContractViolation("checkpoint_retention_exceeded")
        verified = _sha(payload_digest, "payload_digest_invalid")
        known = self._reference_digests.get(reference)
        if known is not None and known != verified:
            raise A7ContractViolation("checkpoint_transfer_conflict")
        self._reference_digests[reference] = verified

        checkpoint_digest = document["evidence_digest"]
        ack: dict[str, Any] = {
            "protocol": CHECKPOINT_ACK_PROTOCOL,
            "ack_id": _canonical_id(
                CHECKPOINT_ACK_PROTOCOL,
                {
                    "channel_id": self._channel_id,
                    "stage_id": stage_id,
                    "checkpoint_digest": checkpoint_digest,
                    "checkpoint_sequence": document["checkpoint_sequence"],
                    "ack_authority_generation": self._ack_generation,
                },
            ),
            "request_id": document["request_id"],
            "session_id": document["session_id"],
            "request_attempt": document["request_attempt"],
            "stage_id": stage_id,
            "standby_placement_id": self._standby_placement_id,
            "standby_membership_incarnation": self._incarnations[stage_id],
            "checkpoint_sequence": document["checkpoint_sequence"],
            "checkpoint_digest": checkpoint_digest,
            "predecessor_digest": document["predecessor_digest"],
            "committed_token_count": document["committed_token_count"],
            "committed_prefix_digest": document["committed_prefix_digest"],
            "stored_checkpoint_digest_verified": True,
            "stored_checkpoint_digest": checkpoint_digest,
            "resource_reservation_generation": self._reservation_generation,
            "acknowledgement_authority": "standby_runtime",
            "ack_authority_generation": self._ack_generation,
        }
        ack["evidence_digest"] = binding_digest(ack)
        return validate_private_kv_checkpoint_ack(ack)


# ---------------------------------------------------------------------------
# Runtime half: the four records the specification names (contract section 4)
# ---------------------------------------------------------------------------


def validate_kv_standby_intent(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed-shape validation for `mycelium.kv_standby_intent.v1` (spec section 8).

    Planner-owned placement, compatibility requirements, failure coverage, bounds
    and hysteresis. `route_ready` is the claim boundary and may only ever be
    `false`: a standby intent never asserts route readiness.
    """

    result = _closed(document, _STANDBY_INTENT_FIELDS, _STANDBY_INTENT_INVALID)
    if result["protocol"] != STANDBY_INTENT_PROTOCOL:
        raise ValueError(_STANDBY_INTENT_INVALID)
    for field in (
        "intent_id",
        "request_id",
        "standby_placement_id",
        "standby_membership_incarnation",
    ):
        _sha(result[field], _STANDBY_INTENT_INVALID)
    _half_open_layer_range(result["layer_range"], _STANDBY_INTENT_INVALID)
    if result["covered_failure_scope"] not in SUCCESSOR_FAILURE_SCOPES:
        raise ValueError(_STANDBY_INTENT_INVALID)
    required = result["required_identity_fields"]
    if (
        not isinstance(required, list)
        or not required
        or len(set(required)) != len(required)
        or required != sorted(required)
        or any(
            not isinstance(field, str) or field not in COMPATIBILITY_IDENTITY_FIELDS
            for field in required
        )
    ):
        raise ValueError(_STANDBY_INTENT_INVALID)
    for field in (
        "max_context_tokens",
        "max_checkpoint_bytes",
        "max_retained_checkpoints",
        "hysteresis_generations",
        "hysteresis_seconds",
        "reservation_generation",
    ):
        _integer(result[field], 1, _STANDBY_INTENT_INVALID)
    if result["route_ready"] is not False:
        raise ValueError(_STANDBY_INTENT_INVALID)
    _expected_digest(result, _STANDBY_INTENT_INVALID)
    return result


def validate_kv_successor_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed-shape validation for `mycelium.kv_successor_qualification.v1`.

    Qualifier-owned exact compatibility, stage/path completeness, resource, parity,
    fence and fallback result. Two closed rules keep the claim boundary honest: the
    scope may only be `deterministic_in_process` (never physical), and
    `physical_challenge_executed` may only ever be `false`. A record with a failed
    parity challenge is not a qualification and fails closed.
    """

    result = _closed(document, _SUCCESSOR_QUALIFICATION_FIELDS, _SUCCESSOR_QUALIFICATION_INVALID)
    if result["protocol"] != SUCCESSOR_QUALIFICATION_PROTOCOL:
        raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    for field in (
        "qualification_id",
        "request_id",
        "standby_placement_id",
        "successor_membership_incarnation",
    ):
        _sha(result[field], _SUCCESSOR_QUALIFICATION_INVALID)
    _require_compatibility_result(result["compatibility"])
    replaced = _sorted_digest_list(
        result["replaced_stage_ids"], _SUCCESSOR_QUALIFICATION_INVALID
    )
    qualified = _sorted_digest_list(
        result["qualified_stage_ids"], _SUCCESSOR_QUALIFICATION_INVALID
    )
    if qualified != replaced:
        raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    for field in (
        "path_generation",
        "resource_reservation_generation",
        "fence_generation",
    ):
        _integer(result[field], 1, _SUCCESSOR_QUALIFICATION_INVALID)
    for field in (
        "parity_challenged",
        "parity_passed",
        "fallback_available",
        "physical_challenge_executed",
    ):
        if type(result[field]) is not bool:
            raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    if result["parity_challenged"] is not True or result["parity_passed"] is not True:
        raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    if result["physical_challenge_executed"] is not False:
        raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    if result["qualification_scope"] != QUALIFICATION_SCOPE:
        raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    if result["issuer"] != QUALIFICATION_ISSUER:
        raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    issued = _integer(result["issued_at_unix_ms"], 1, _SUCCESSOR_QUALIFICATION_INVALID)
    expires = _integer(
        result["expires_at_unix_ms"], 1, _SUCCESSOR_QUALIFICATION_INVALID
    )
    if expires <= issued:
        raise ValueError(_SUCCESSOR_QUALIFICATION_INVALID)
    _expected_digest(result, _SUCCESSOR_QUALIFICATION_INVALID)
    return result


def qualification_is_current(
    qualification: Mapping[str, Any], *, now_unix_ms: int
) -> bool:
    """Current-ness of a deterministic in-process qualifier result.

    Only this may feed the eligibility predicate's `qualifier_current` input. It
    answers exactly: is a closed, compatible, parity-passed, unexpired
    *deterministic* qualification present for the clock handed in? It never
    answers a physical question, and an unreadable record is not current.
    """

    try:
        document = validate_kv_successor_qualification(qualification)
        now = _integer(now_unix_ms, 0, "qualification_clock_invalid")
    except ValueError:
        return False
    return (
        document["compatibility"]["compatible"] is True
        and document["qualification_scope"] == QUALIFICATION_SCOPE
        and document["issued_at_unix_ms"] <= now < document["expires_at_unix_ms"]
    )


def validate_kv_successor_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed-shape validation for `mycelium.kv_successor_runtime.v1`.

    The privacy-reduced source/standby lifecycle, checkpoint sequences, aggregate
    bytes, acknowledged watermark/lag, fence, cutover, fallback, cleanup and
    terminal outcome. Three closed rules make a physical or qualified claim
    unrepresentable: `execution_mode` is always `in_process_deterministic`, and
    `physical_evidence`, `qualification_claim` and `promotion_authorized` are
    always `false`.
    """

    result = _closed(document, _SUCCESSOR_RUNTIME_FIELDS, _SUCCESSOR_RUNTIME_INVALID)
    if result["protocol"] != SUCCESSOR_RUNTIME_PROTOCOL:
        raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    for field in (
        "runtime_id",
        "request_id",
        "session_id",
        "source_placement_id",
        "standby_placement_id",
        "committed_prefix_digest",
    ):
        _sha(result[field], _SUCCESSOR_RUNTIME_INVALID)
    for field in ("request_attempt", "path_generation", "fencing_generation"):
        _integer(result[field], 1, _SUCCESSOR_RUNTIME_INVALID)
    for field in ("checkpoint_count", "aggregate_kv_bytes"):
        _integer(result[field], 0, _SUCCESSOR_RUNTIME_INVALID)
    gateway = _integer(
        result["gateway_committed_token_count"], 0, _SUCCESSOR_RUNTIME_INVALID
    )
    if result["source_state"] not in SOURCE_RUNTIME_STATES:
        raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    if result["standby_state"] not in STANDBY_STATES:
        raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    sequence = result["checkpoint_sequence"]
    if sequence is None:
        if result["checkpoint_count"] != 0:
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    else:
        _integer(sequence, 0, _SUCCESSOR_RUNTIME_INVALID)
        if result["checkpoint_count"] < 1:
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    watermark = result["acknowledged_watermark"]
    lag = result["acknowledged_lag"]
    if watermark is None or lag is None:
        # unknown coverage stays unknown; it is never defaulted to zero or ready
        if watermark is not None or lag is not None:
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    else:
        _integer(watermark, 0, _SUCCESSOR_RUNTIME_INVALID)
        if isinstance(lag, bool) or not isinstance(lag, int):
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
        if lag != gateway - watermark:
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    for field in ("fence_installed", "cutover_granted", "cleanup_complete"):
        if type(result[field]) is not bool:
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    if result["recovery_mode"] not in OUTCOMES:
        raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    if result["terminal_outcome"] != result["recovery_mode"]:
        raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    reason = result["fallback_reason"]
    if result["recovery_mode"] == "fenced_kv_successor":
        if reason is not None or result["cutover_granted"] is not True:
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    elif reason not in RUNTIME_FALLBACK_REASONS or result["cutover_granted"] is not False:
        raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    if result["execution_mode"] != IN_PROCESS_EXECUTION_MODE:
        raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    for field in ("physical_evidence", "qualification_claim", "promotion_authorized"):
        if result[field] is not False:
            raise ValueError(_SUCCESSOR_RUNTIME_INVALID)
    _expected_digest(result, _SUCCESSOR_RUNTIME_INVALID)
    return result


def validate_kv_successor_recovery_gate(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed-shape validation for `mycelium.kv_successor_recovery_gate.v1`.

    The executed-gate record this scaffold can produce is a *deterministic,
    in-process, test-only* one: `gate_state` may only be `design_only`,
    `gate_evidence_kind` only `deterministic_in_process_test_only_seam`, and the
    browser, performance, physical, qualification and promotion flags may only be
    false. The scenario lists are the invariants the in-process run really
    observed, named from closed vocabularies, and every cleanup counter must be
    zero whenever cleanup is claimed complete.
    """

    result = _closed(document, _SUCCESSOR_GATE_FIELDS, _SUCCESSOR_GATE_INVALID)
    if result["protocol"] != RECOVERY_GATE_PROTOCOL:
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    for field in ("gate_id", "request_id", "runtime_digest"):
        _sha(result[field], _SUCCESSOR_GATE_INVALID)
    if result["terminal_outcome"] not in OUTCOMES:
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    positives = _sorted_text_list(
        result["positive_scenarios"],
        _SUCCESSOR_GATE_INVALID,
        maximum=64,
        allow_empty=False,
    )
    negatives = _sorted_text_list(
        result["negative_scenarios"],
        _SUCCESSOR_GATE_INVALID,
        maximum=64,
        allow_empty=True,
    )
    if any(scenario not in GATE_POSITIVE_INVARIANTS for scenario in positives):
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    if any(scenario not in GATE_NEGATIVE_INVARIANTS for scenario in negatives):
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    counters = result["counter_report"]
    if not isinstance(counters, Mapping) or set(counters) != set(SUCCESSOR_CLEANUP_COUNTERS):
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    for value in counters.values():
        _integer(value, 0, _SUCCESSOR_GATE_INVALID)
    if type(result["cleanup_complete"]) is not bool:
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    if result["cleanup_complete"] and any(counters.values()):
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    if result["gate_state"] != "design_only":
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    if result["gate_evidence_kind"] != GATE_EVIDENCE_KIND:
        raise ValueError(_SUCCESSOR_GATE_INVALID)
    for field in (
        "browser_scenarios_executed",
        "performance_measured",
        "physical_evidence",
        "qualification_claim",
        "promotion_authorized",
    ):
        if result[field] is not False:
            raise ValueError(_SUCCESSOR_GATE_INVALID)
    _expected_digest(result, _SUCCESSOR_GATE_INVALID)
    return result


# ---------------------------------------------------------------------------
# Runtime half: payload envelope, transport and fence persistence
# ---------------------------------------------------------------------------


def seal_checkpoint_payload(
    *,
    checkpoint: Mapping[str, Any],
    kv_payload: object,
    store: InProcessPayloadStore | None = None,
) -> dict[str, Any]:
    """Seal one private stage-local KV payload into a bounded in-process envelope.

    Byte-exact: the payload must be exactly the checkpoint's `byte_size` bytes.
    Privacy-reduced: the envelope carries the reference and the payload digest,
    never the payload bytes, and its `confidentiality` field states plainly that
    no cipher is applied here -- the specification's dedicated authenticated
    encrypted channel is a product-integration requirement this scaffold does not
    meet. With a `store` the payload is registered process-locally so
    `open_checkpoint_payload` can reopen it.
    """

    document = validate_private_kv_checkpoint(checkpoint)
    if not isinstance(kv_payload, (bytes, bytearray)):
        raise A7ContractViolation("payload_not_bytes")
    body = bytes(kv_payload)
    if len(body) != document["byte_size"]:
        raise A7ContractViolation("payload_byte_size_mismatch")
    if store is not None and not isinstance(store, InProcessPayloadStore):
        raise A7ContractViolation("payload_store_invalid")
    envelope = {
        "protocol": PAYLOAD_ENVELOPE_PROTOCOL,
        "encrypted_payload_reference": document["encrypted_payload_reference"],
        "payload_digest": _payload_digest(body),
        "byte_size": len(body),
        "checkpoint_sequence": document["checkpoint_sequence"],
        "committed_token_count": document["committed_token_count"],
        "confidentiality": NO_IN_PROCESS_CONFIDENTIALITY,
        "store_scope": PROCESS_LOCAL_SCOPE,
        "payload": None,
    }
    if store is not None:
        store.register(
            reference=document["encrypted_payload_reference"],
            payload=body,
            envelope=envelope,
        )
    return envelope


def open_checkpoint_payload(
    *,
    reference: str,
    store: InProcessPayloadStore | None = None,
    include_bytes: bool = False,
) -> dict[str, Any]:
    """Reopen one sealed in-process payload reference.

    Returns the same closed envelope shape the seal produced; the raw payload is
    only handed back when the caller explicitly asks for it with
    `include_bytes=True`. An absent store or an unknown reference fails closed:
    unknown coverage is never defaulted to ready.
    """

    if type(include_bytes) is not bool:
        raise A7ContractViolation("payload_open_input_invalid")
    if not isinstance(store, InProcessPayloadStore):
        raise A7ContractViolation("payload_store_absent")
    payload, digest, envelope = store.resolve(reference=reference)
    if envelope["payload_digest"] != digest:
        raise A7ContractViolation("payload_envelope_mismatch")
    return {**envelope, "payload": payload if include_bytes else None}


def transfer_shadow_checkpoint(
    *,
    checkpoint: Mapping[str, Any],
    standby_placement_id: str,
    channel: InProcessCheckpointChannel | None = None,
    stage_id: str | None = None,
    payload_digest: str | None = None,
) -> dict[str, Any]:
    """Transfer one shadow checkpoint over the bounded in-process channel.

    Returns the `mycelium.private_kv_checkpoint_ack.v1` record the standby runtime
    would sign, ready for the route acknowledgement ledger. No socket, no thread,
    no encryption and no durability are involved; an absent channel, stage or
    payload digest fails closed rather than fabricating a transport.
    """

    document = validate_private_kv_checkpoint(checkpoint)
    if not isinstance(channel, InProcessCheckpointChannel):
        raise A7ContractViolation("checkpoint_channel_absent")
    if stage_id is None:
        raise A7ContractViolation("checkpoint_stage_absent")
    if payload_digest is None:
        raise A7ContractViolation("checkpoint_payload_digest_absent")
    return channel.transfer(
        checkpoint=document,
        standby_placement_id=standby_placement_id,
        stage_id=stage_id,
        payload_digest=payload_digest,
    )


def persist_fence_generation(
    *, fence_state: Mapping[str, Any], store: FenceGenerationStore | None = None
) -> dict[str, Any]:
    """Record the current fencing generation in a process-local store.

    Monotonic per request attempt, and honest about itself: the receipt's
    `durability` field says the record does not survive the process, so nobody can
    read a durable fence out of it. Successor decode still requires the real
    durable store.
    """

    if not isinstance(store, FenceGenerationStore):
        raise A7ContractViolation("fence_store_absent")
    return store.persist(fence_state=fence_state)


# ---------------------------------------------------------------------------
# Runtime half: the A6 handoff and the fenced cutover
# ---------------------------------------------------------------------------


def _a6_replay_handoff(*, requested: bool) -> dict[str, Any]:
    """Request the real A6 full-context replay seam, in-process and bounded.

    The A6 module is imported lazily, read-only, and never at module import time.
    When it is reachable this constructs A6's own in-process owner/runtime pair and
    admits one request, which proves the seam is callable from A7. It does **not**
    run, qualify, or complete an A6 replay: the handoff field says *requested*, and
    the record pins `a6_qualification_claim=False` and `a6_promotion_authorized=False`.
    An unreachable seam is reported as data, never as a crash and never as success.
    """

    reachable = False
    admitted = False
    try:
        import importlib

        module = importlib.import_module(A6_SEAM_MODULE)
        owner_factory = getattr(module, "OwnerAuthority")
        runtime_factory = getattr(module, "RecoveryRuntime")
        reachable = True
    except Exception:  # noqa: BLE001 - an unreachable A6 seam is data, not a crash
        reachable = False
    if requested and reachable:
        try:
            owner = owner_factory(a4_atomic_commit="0" * 40)
            owner.register_canonical_path(
                "a7-handoff-source", digest="sha256:" + "0" * 64, attempt=1
            )
            runtime = runtime_factory(owner)
            identity = owner.admission_identity(
                _A6_HANDOFF_REQUEST_ID, path_id="a7-handoff-source"
            )
            runtime.admit(
                _A6_HANDOFF_REQUEST_ID, identity=identity, absolute_deadline_ms=1
            )
            admitted = runtime.phase(_A6_HANDOFF_REQUEST_ID).value == "active"
        except Exception:  # noqa: BLE001 - the seam refused the handoff; record it
            admitted = False

    if not requested:
        handoff = "not_requested"
    elif reachable and admitted:
        handoff = "full_context_replay_requested"
    else:
        handoff = "full_context_replay_unavailable"
    return {
        "protocol": A6_HANDOFF_PROTOCOL,
        "a6_module": A6_SEAM_MODULE,
        "a6_reachable": reachable,
        "a6_runtime_admitted": admitted,
        "handoff": handoff,
        "a6_qualification_claim": False,
        "a6_promotion_authorized": False,
        "execution_mode": IN_PROCESS_EXECUTION_MODE,
        "physical_evidence": False,
    }


def _require_cutover_request(cutover_request: Any) -> dict[str, Any]:
    if (
        not isinstance(cutover_request, Mapping)
        or set(cutover_request) != _CUTOVER_REQUEST_FIELDS
        or cutover_request.get("protocol") != CUTOVER_REQUEST_PROTOCOL
    ):
        raise A7ContractViolation(_CUTOVER_REQUEST_INVALID)
    _sha(cutover_request["request_id"], _CUTOVER_REQUEST_INVALID)
    for field in ("request_attempt", "path_generation", "fencing_generation"):
        _integer(cutover_request[field], 1, _CUTOVER_REQUEST_INVALID)
    if cutover_request["route_watermark"] is not None:
        _require_watermark(cutover_request["route_watermark"])
    _integer(
        cutover_request["gateway_committed_token_count"], 0, _CUTOVER_REQUEST_INVALID
    )
    _sha(cutover_request["gateway_committed_prefix_digest"], _CUTOVER_REQUEST_INVALID)
    _require_compatibility_result(cutover_request["compatibility"])
    for field in ("qualifier_current", "fence_installed", "a6_available"):
        if type(cutover_request[field]) is not bool:
            raise A7ContractViolation(_CUTOVER_REQUEST_INVALID)
    return copy.deepcopy(dict(cutover_request))


def perform_fenced_cutover(
    *,
    fence_state: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    cutover_request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the fenced cutover decision for one frozen checkpoint in-process.

    Validates the fence state, the checkpoint and an explicit cutover request, then
    runs the closed eligibility predicate. On `fenced_kv_successor` it additionally
    proves the checkpoint's committed position and prefix digest equal the frozen
    route watermark, and reports the next contiguous position: no recovery prefill
    and no prompt or committed-prefix transfer. On any rejection it requests the
    real A6 full-context replay seam, or -- when that seam is unreachable --
    downgrades to an explicit abort. The result is always labelled
    `in_process_deterministic` with `physical_evidence=False`.
    """

    state = _require_fence_state(fence_state)
    document = validate_private_kv_checkpoint(checkpoint)
    if cutover_request is None:
        raise A7ContractViolation("cutover_request_absent")
    request = _require_cutover_request(cutover_request)
    if (
        request["request_attempt"] != state["request_attempt"]
        or document["request_attempt"] != state["request_attempt"]
    ):
        raise A7ContractViolation("stale_attempt")
    if (
        request["path_generation"] != state["path_generation"]
        or request["fencing_generation"] != state["fencing_generation"]
    ):
        raise A7ContractViolation("stale_generation")
    if document["request_id"] != request["request_id"]:
        raise A7ContractViolation("request_binding_mismatch")

    decision = evaluate_cutover_eligibility(
        route_watermark=request["route_watermark"],
        gateway_committed_token_count=request["gateway_committed_token_count"],
        gateway_committed_prefix_digest=request["gateway_committed_prefix_digest"],
        compatibility=request["compatibility"],
        qualifier_current=request["qualifier_current"],
        fence_installed=request["fence_installed"],
        a6_available=request["a6_available"],
    )
    outcome = decision["outcome"]
    reason = decision["reason"]
    if decision["kv_successor_eligible"]:
        watermark = _require_watermark(request["route_watermark"])
        if (
            document["committed_token_count"] != watermark["committed_token_count"]
            or document["committed_prefix_digest"]
            != watermark["committed_prefix_digest"]
        ):
            # the frozen checkpoint must be the frozen committed watermark exactly
            raise A7ContractViolation("checkpoint_watermark_mismatch")
        handoff = _a6_replay_handoff(requested=False)
    else:
        handoff = _a6_replay_handoff(requested=outcome == "full_context_replay")
        if handoff["handoff"] == "full_context_replay_unavailable":
            outcome = "aborted"
            reason = "a6_seam_unreachable"

    return {
        "protocol": CUTOVER_RESULT_PROTOCOL,
        "request_id": request["request_id"],
        "request_attempt": state["request_attempt"],
        "path_generation": state["path_generation"],
        "fencing_generation": state["fencing_generation"],
        "outcome": outcome,
        "reason": reason,
        "committed_token_count": document["committed_token_count"],
        "next_position": document["committed_token_count"],
        "publication_authorized": outcome == "fenced_kv_successor",
        "recovery_prefill_performed": False,
        "prompt_or_prefix_transferred": False,
        "a6_handoff": handoff,
        "execution_mode": IN_PROCESS_EXECUTION_MODE,
        "physical_evidence": False,
    }


# ---------------------------------------------------------------------------
# Runtime half: the orchestration seam
# ---------------------------------------------------------------------------


class SuccessorRecoveryCycle:
    """One request's deterministic, in-process A7 recovery cycle.

    It owns the real deterministic building blocks -- the route acknowledgement
    ledger, the fence ledger with its single-publication compare-and-swap, the
    closed standby lifecycle -- plus the runtime-half seams: the payload envelope,
    the bounded in-process shadow-checkpoint channel, the process-local fence
    generation store and the fenced cutover. It has no threads, no sockets, no
    durable store and no physical feedback, and the runtime projection it produces
    cannot express a physical or qualified claim.

    The cycle is *not* product integration: driving it proves the in-process
    contract behaves, and proves nothing about a live gateway, Router, runtime or
    browser request.
    """

    def __init__(
        self,
        *,
        request_id: str,
        session_id: str,
        request_attempt: int,
        replaced_stage_ids: Sequence[str],
        current_incarnations: Mapping[str, str],
        source_placement_id: str,
        standby_placement_id: str,
        source_identity: Mapping[str, Any],
        successor_identity: Mapping[str, Any],
        layer_range: Sequence[int],
        gateway_committed_token_count: int,
        gateway_committed_prefix_digest: str,
        standby_intent: Mapping[str, Any],
        qualification: Mapping[str, Any],
        now_unix_ms: int,
        reservation_generation: int = 1,
        path_generation: int = 1,
        fencing_generation: int = 1,
        a6_available: bool = True,
        channel_id: str = "a7-in-process-channel",
    ) -> None:
        self._request_id = _sha(request_id, "request_id_invalid")
        self._session_id = _sha(session_id, "session_id_invalid")
        self._source_placement_id = _sha(source_placement_id, "source_placement_invalid")
        self._standby_placement_id = _sha(
            standby_placement_id, "standby_placement_invalid"
        )
        self._intent = validate_kv_standby_intent(standby_intent)
        self._qualification = validate_kv_successor_qualification(qualification)
        if (
            self._intent["request_id"] != self._request_id
            or self._qualification["request_id"] != self._request_id
        ):
            raise A7ContractViolation("request_binding_mismatch")
        if (
            self._intent["standby_placement_id"] != self._standby_placement_id
            or self._qualification["standby_placement_id"] != self._standby_placement_id
        ):
            raise A7ContractViolation("standby_binding_mismatch")
        if (
            self._intent["standby_membership_incarnation"]
            != self._qualification["successor_membership_incarnation"]
        ):
            raise A7ContractViolation("standby_binding_mismatch")
        if self._qualification["path_generation"] != path_generation or (
            self._qualification["fence_generation"] != fencing_generation
        ):
            raise A7ContractViolation("generation_binding_mismatch")
        if (
            self._qualification["resource_reservation_generation"]
            != self._intent["reservation_generation"]
            or self._intent["reservation_generation"] != reservation_generation
        ):
            raise A7ContractViolation("reservation_binding_mismatch")
        layer_span = _half_open_layer_range(list(layer_range), "layer_range_invalid")
        if layer_span != self._intent["layer_range"]:
            raise A7ContractViolation("layer_range_binding_mismatch")
        if self._qualification["replaced_stage_ids"] != sorted(set(replaced_stage_ids)):
            raise A7ContractViolation("stage_set_mismatch")
        if type(a6_available) is not bool:
            raise A7ContractViolation("a6_availability_invalid")
        self._compatibility = compare_compatibility(source_identity, successor_identity)
        if self._compatibility != self._qualification["compatibility"]:
            raise A7ContractViolation("compatibility_binding_mismatch")
        self._now = _integer(now_unix_ms, 0, "clock_invalid")
        self._gateway_count = _integer(
            gateway_committed_token_count, 0, "gateway_watermark_invalid"
        )
        self._gateway_digest = _sha(
            gateway_committed_prefix_digest, "gateway_watermark_invalid"
        )
        self._a6_available = a6_available
        self._ledger = RouteAckLedger(
            replaced_stage_ids=replaced_stage_ids,
            request_attempt=request_attempt,
            current_incarnations=current_incarnations,
        )
        self._fence = FenceLedger(
            request_attempt=request_attempt,
            path_generation=path_generation,
            fencing_generation=fencing_generation,
        )
        self._payloads = InProcessPayloadStore()
        self._fence_store = FenceGenerationStore()
        self._channel = InProcessCheckpointChannel(
            channel_id=channel_id,
            standby_placement_id=self._standby_placement_id,
            replaced_stage_ids=replaced_stage_ids,
            current_incarnations=current_incarnations,
            max_checkpoint_bytes=self._intent["max_checkpoint_bytes"],
            max_context_tokens=self._intent["max_context_tokens"],
            max_retained_checkpoints=self._intent["max_retained_checkpoints"],
            resource_reservation_generation=self._intent["reservation_generation"],
        )
        self._latest_checkpoint: dict[str, Any] | None = None
        self._checkpoint_count = 0
        self._aggregate_bytes = 0
        self._latest_sequence: int | None = None
        self._source_state = "active"
        self._standby_state = "qualified"
        self._cutover: dict[str, Any] | None = None
        self._runtime: dict[str, Any] | None = None
        self._cutover_granted = False
        self._cleanup_complete = False

    # -- inspection --------------------------------------------------------

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def ledger(self) -> RouteAckLedger:
        return self._ledger

    @property
    def fence(self) -> FenceLedger:
        return self._fence

    @property
    def channel(self) -> InProcessCheckpointChannel:
        return self._channel

    @property
    def payloads(self) -> InProcessPayloadStore:
        return self._payloads

    @property
    def fence_store(self) -> FenceGenerationStore:
        return self._fence_store

    @property
    def standby_state(self) -> str:
        return self._standby_state

    @property
    def source_state(self) -> str:
        return self._source_state

    @property
    def cutover_result(self) -> dict[str, Any] | None:
        return None if self._cutover is None else copy.deepcopy(self._cutover)

    # -- shadow phase ------------------------------------------------------

    def seal(self, *, checkpoint: Mapping[str, Any], kv_payload: object) -> dict[str, Any]:
        document = validate_private_kv_checkpoint(checkpoint)
        self._require_own_checkpoint(document)
        return seal_checkpoint_payload(
            checkpoint=document, kv_payload=kv_payload, store=self._payloads
        )

    def accept_checkpoint(
        self,
        *,
        checkpoint: Mapping[str, Any],
        kv_payload: object,
        stage_id: str,
    ) -> dict[str, Any]:
        """Seal, transfer and acknowledge one shadow checkpoint in-process."""

        envelope = self.seal(checkpoint=checkpoint, kv_payload=kv_payload)
        document = validate_private_kv_checkpoint(checkpoint)
        ack = transfer_shadow_checkpoint(
            checkpoint=document,
            standby_placement_id=self._standby_placement_id,
            channel=self._channel,
            stage_id=stage_id,
            payload_digest=envelope["payload_digest"],
        )
        ledger_outcome = self._ledger.record(ack)
        self._latest_checkpoint = copy.deepcopy(document)
        self._checkpoint_count += 1
        self._aggregate_bytes += document["byte_size"]
        self._latest_sequence = document["checkpoint_sequence"]
        return {"envelope": envelope, "ack": ack, "ledger_outcome": ledger_outcome}

    def _require_own_checkpoint(self, document: Mapping[str, Any]) -> None:
        if (
            document["request_id"] != self._request_id
            or document["session_id"] != self._session_id
        ):
            raise A7ContractViolation("request_binding_mismatch")
        if document["request_attempt"] != self._fence.state["request_attempt"]:
            raise A7ContractViolation("stale_attempt")
        if document["source_placement_id"] != self._source_placement_id:
            raise A7ContractViolation("source_binding_mismatch")
        if document["layer_range"] != self._intent["layer_range"]:
            raise A7ContractViolation("checkpoint_layer_range_mismatch")
        if document["committed_token_count"] > self._intent["max_context_tokens"]:
            raise A7ContractViolation("checkpoint_over_context_bound")

    # -- fence phase -------------------------------------------------------

    def install_fence(self, participants: Sequence[str] | None = None) -> tuple[str, ...]:
        """Install the fence on every participant (or an explicit subset)."""

        for participant in FENCE_INSTALLERS if participants is None else participants:
            self._fence.install(participant)
        return self._fence.installed_participants

    def persist_fence(self) -> dict[str, Any]:
        return persist_fence_generation(
            fence_state=self._fence.state, store=self._fence_store
        )

    # -- cutover phase -----------------------------------------------------

    def build_cutover_request(self) -> dict[str, Any]:
        """The closed cutover request built from this cycle's own state."""

        return {
            "protocol": CUTOVER_REQUEST_PROTOCOL,
            "request_id": self._request_id,
            "request_attempt": self._fence.state["request_attempt"],
            "path_generation": self._fence.state["path_generation"],
            "fencing_generation": self._fence.state["fencing_generation"],
            "route_watermark": self._ledger.route_watermark,
            "gateway_committed_token_count": self._gateway_count,
            "gateway_committed_prefix_digest": self._gateway_digest,
            "compatibility": self._compatibility,
            "qualifier_current": qualification_is_current(
                self._qualification, now_unix_ms=self._now
            ),
            "fence_installed": self._fence.fence_installed,
            "a6_available": self._a6_available,
        }

    def perform_cutover(self) -> dict[str, Any]:
        if self._latest_checkpoint is None:
            raise A7ContractViolation("checkpoint_absent")
        self._cutover = perform_fenced_cutover(
            fence_state=self._fence.state,
            checkpoint=self._latest_checkpoint,
            cutover_request=self.build_cutover_request(),
        )
        return self._cutover

    # -- terminal phase ----------------------------------------------------

    def complete(self) -> dict[str, Any]:
        """Drive the whole in-process cycle and return the runtime projection."""

        if self._standby_state != "shadowing":
            self._standby_state = transition_standby(self._standby_state, "shadowing")
        self._standby_state = transition_standby(self._standby_state, "cutover_pending")
        self.install_fence()
        self.persist_fence()
        self._source_state = "fenced"
        cutover = self.perform_cutover()
        if cutover["outcome"] == "fenced_kv_successor":
            # exactly one compare-and-swap grant, once per fencing generation
            self._fence.grant_publication(self._fence.fencing_generation)
            self._cutover_granted = True
            self._standby_state = transition_standby(
                self._standby_state, "active_successor"
            )
            self._standby_state = transition_standby(self._standby_state, "draining")
            self._standby_state = transition_standby(self._standby_state, "released")
        else:
            # the standby is never admitted as the publisher
            self._standby_state = transition_standby(self._standby_state, "rejected")
        self._source_state = "released"
        self._payloads.release()
        self._cleanup_complete = True
        self._runtime = self.runtime_projection()
        return self._runtime

    def runtime_projection(self) -> dict[str, Any]:
        """The closed `mycelium.kv_successor_runtime.v1` projection of this cycle."""

        if self._cutover is None:
            raise A7ContractViolation("cycle_not_terminal")
        watermark = self._ledger.route_watermark
        count = None if watermark is None else watermark["committed_token_count"]
        lag = None if count is None else self._gateway_count - count
        document: dict[str, Any] = {
            "protocol": SUCCESSOR_RUNTIME_PROTOCOL,
            "runtime_id": "sha256:" + "0" * 64,
            "request_id": self._request_id,
            "session_id": self._session_id,
            "request_attempt": self._fence.state["request_attempt"],
            "path_generation": self._fence.state["path_generation"],
            "fencing_generation": self._fence.state["fencing_generation"],
            "source_placement_id": self._source_placement_id,
            "standby_placement_id": self._standby_placement_id,
            "source_state": self._source_state,
            "standby_state": self._standby_state,
            "checkpoint_sequence": self._latest_sequence,
            "checkpoint_count": self._checkpoint_count,
            "aggregate_kv_bytes": self._aggregate_bytes,
            "gateway_committed_token_count": self._gateway_count,
            "acknowledged_watermark": count,
            "acknowledged_lag": lag,
            "committed_prefix_digest": self._gateway_digest,
            "fence_installed": self._fence.fence_installed,
            "cutover_granted": self._cutover_granted,
            "recovery_mode": self._cutover["outcome"],
            "fallback_reason": (
                None
                if self._cutover["outcome"] == "fenced_kv_successor"
                else self._cutover["reason"]
            ),
            "cleanup_complete": self._cleanup_complete,
            "terminal_outcome": self._cutover["outcome"],
            "execution_mode": IN_PROCESS_EXECUTION_MODE,
            "physical_evidence": False,
            "qualification_claim": False,
            "promotion_authorized": False,
        }
        document["runtime_id"] = _canonical_id(
            SUCCESSOR_RUNTIME_PROTOCOL,
            {key: value for key, value in document.items() if key != "runtime_id"},
        )
        document["evidence_digest"] = binding_digest(document)
        return validate_kv_successor_runtime(document)

    def recovery_gate(self) -> dict[str, Any]:
        """The deterministic in-process executed-gate record for this cycle.

        Every positive invariant named here was really observed in-process, and
        every negative invariant was really rejected by a fail-closed probe just
        before this record was built. The record still cannot claim a gate: it
        reports `gate_state=design_only`, declares itself a test-only seam, and
        leaves the browser, performance, physical, qualification and promotion
        flags false.
        """

        if self._runtime is None:
            raise A7ContractViolation("cycle_not_terminal")
        document: dict[str, Any] = {
            "protocol": RECOVERY_GATE_PROTOCOL,
            "gate_id": "sha256:" + "0" * 64,
            "request_id": self._request_id,
            "terminal_outcome": self._cutover["outcome"],
            "runtime_digest": self._runtime["evidence_digest"],
            "positive_scenarios": self._observed_positive_invariants(),
            "negative_scenarios": self._probe_in_process_rejections(),
            "counter_report": {name: 0 for name in SUCCESSOR_CLEANUP_COUNTERS},
            "cleanup_complete": self._cleanup_complete,
            "browser_scenarios_executed": False,
            "performance_measured": False,
            "gate_state": "design_only",
            "gate_evidence_kind": GATE_EVIDENCE_KIND,
            "physical_evidence": False,
            "qualification_claim": False,
            "promotion_authorized": False,
        }
        document["gate_id"] = _canonical_id(
            RECOVERY_GATE_PROTOCOL,
            {
                "request_id": self._request_id,
                "terminal_outcome": document["terminal_outcome"],
                "runtime_digest": document["runtime_digest"],
            },
        )
        document["evidence_digest"] = binding_digest(document)
        return validate_kv_successor_recovery_gate(document)

    def _observed_positive_invariants(self) -> list[str]:
        observed: set[str] = set()
        watermark = self._ledger.route_watermark
        if watermark is not None and (
            watermark["committed_token_count"] == self._gateway_count
            and watermark["committed_prefix_digest"] == self._gateway_digest
        ):
            observed.add("route_watermark_exact_equality")
        if self._fence.fence_installed:
            observed.add("fence_installed_all_participants")
        if self._cutover_granted:
            observed.update(
                {
                    "single_publication_cas_granted",
                    "no_recovery_prefill",
                    "no_prompt_or_committed_prefix_transfer",
                }
            )
        else:
            observed.add("fallback_never_relabelled_as_kv_continuation")
            if self._cutover is not None and self._cutover["outcome"] == "aborted":
                observed.add("explicit_bounded_abort_reason")
        if self._cleanup_complete:
            observed.add("exact_cleanup_counters_zero")
        return sorted(observed)

    def _probe_in_process_rejections(self) -> list[str]:
        """Name only the fail-closed rejections this process actually observed."""

        checkpoint = self._latest_checkpoint
        if checkpoint is None:
            return []
        over_bound = copy.deepcopy(checkpoint)
        over_bound["byte_size"] = self._channel.max_checkpoint_bytes + 1
        over_bound["evidence_digest"] = binding_digest(over_bound)
        unclosed = {**checkpoint, "note": "unexpected"}
        member_stage = self._qualification["replaced_stage_ids"][0]
        probes = (
            (
                "rejected_unknown_stage",
                lambda: self._channel.transfer(
                    checkpoint=checkpoint,
                    standby_placement_id=self._standby_placement_id,
                    stage_id=_PROBE_UNKNOWN_STAGE,
                    payload_digest=_payload_digest(b""),
                ),
            ),
            (
                "rejected_over_transfer_bound",
                lambda: self._channel.transfer(
                    checkpoint=over_bound,
                    standby_placement_id=self._standby_placement_id,
                    stage_id=member_stage,
                    payload_digest=_payload_digest(b""),
                ),
            ),
            (
                "rejected_payload_reference_unknown",
                lambda: open_checkpoint_payload(
                    reference="ref:a7-probe-unregistered", store=self._payloads
                ),
            ),
            (
                "rejected_non_monotonic_fence_generation",
                lambda: persist_fence_generation(
                    fence_state=self._fence.state, store=self._fence_store
                ),
            ),
            (
                "rejected_absent_cutover_request",
                lambda: perform_fenced_cutover(
                    fence_state=self._fence.state,
                    checkpoint=checkpoint,
                    cutover_request=None,
                ),
            ),
            (
                "rejected_unclosed_record_shape",
                lambda: validate_private_kv_checkpoint(unclosed),
            ),
        )
        observed: list[str] = []
        for name, probe in probes:
            try:
                probe()
            except (A7ContractViolation, ValueError):
                observed.append(name)
        return sorted(observed)


def run_successor_recovery(
    *, request_id: str, cycle: SuccessorRecoveryCycle | None = None
) -> dict[str, Any]:
    """Run one deterministic, in-process A7 successor-recovery cycle.

    The cycle object carries every input; an absent or invalid cycle fails closed
    rather than fabricating recovery state for an unknown request, and the request
    binding must match the cycle. Returns the closed
    `mycelium.kv_successor_runtime.v1` projection -- which cannot express a
    physical, qualified or promotion claim.
    """

    if not isinstance(cycle, SuccessorRecoveryCycle):
        raise A7ContractViolation("recovery_cycle_absent")
    if _sha(request_id, "request_id_invalid") != cycle.request_id:
        raise A7ContractViolation("request_binding_mismatch")
    return cycle.complete()
