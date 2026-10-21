# SPDX-License-Identifier: AGPL-3.0-only
"""Prospective A6 cleanup-authority-failure v2, separate from immutable case v1.

A validated envelope is a closed shape, not proof its producer ran. The local
collector below reads command snapshots and gateway events independently; neither
an unavailable ledger nor an empty snapshot can be counted as zero.

OWNER RULING 2026-10-06 ("A6 CASE15: THE TEST MATCHES THE PRODUCT"): the case
no longer requires a committed cutover. The product refuses an unproven
successor completion cleanup before cutover: the recovery aborts pre-cutover
(the coordinator releases the reservations and ends the request failed), the
successor's buffered suffix is discarded unpublished, the successor command's
terminal is never committed (its cleanup was never proven), and the gateway
publishes exactly one ``request_cleanup_unproven`` failure notice
(TERMINAL_BLOCKED). The envelope protocol moved to ``.v3`` for this shape.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

PROTOCOL = "mycelium.a6_cleanup_authority_observation.v3"
AUTHORITY = "command_controller_and_request_gateway"
CASE = "cleanup_authority_failure"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z")
DOCUMENT_FIELDS = {"protocol", "authority", "case_id", "bindings", "observation", "observation_digest"}
BINDING_FIELDS = {"request_id", "request_attempt", "source_digest", "candidate_digest",
                  "spec_digest", "contract_digest", "operator_plan_digest", "run_id",
                  "deployment_id", "deployment_epoch", "source_path_id", "source_attempt",
                  "source_path_attempt", "source_path_generation", "successor_path_id",
                  "successor_attempt", "successor_path_attempt", "successor_path_generation"}
COMMAND_FIELDS = {"request_id", "request_attempt", "registered_command_count", "terminal_cas_count"}
GATEWAY_FIELDS = {"request_id", "request_attempt", "terminal_event_count", "terminal_kind", "terminal_code"}
OBSERVATION_FIELDS = {"case_id", "executed", "ordinary_product_path", "private_material_exposed",
                      "rejection_observed", "fail_closed", "terminal_state", "unpublishable_suffix_discarded",
                      "process_cleanup_complete", "stale_reservation_count", "command_authority", "gateway_authority",
                      "cutover_committed", "committed_route_attempt", "coordinator_terminal",
                      "duplicated_token_count"}
PRIVATE_MARKERS = ("encoded_prompt_token_ids", "committed_prefix_tokens", "checkpoint_path", "node_secret",
                   "ssh_identity", "private_key")


def _exact(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("cleanup_v2_closed_shape_invalid")
    return dict(value)


def _int(value: object, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def validate_observation(document: Mapping[str, Any]) -> dict[str, Any]:
    doc = _exact(document, DOCUMENT_FIELDS)
    if doc["protocol"] != PROTOCOL or doc["authority"] != AUTHORITY or doc["case_id"] != CASE:
        raise ValueError("cleanup_v2_protocol_invalid")
    if not isinstance(doc["observation_digest"], str) or _digest({k: v for k, v in doc.items() if k != "observation_digest"}) != doc["observation_digest"]:
        raise ValueError("cleanup_v2_digest_invalid")
    if any(marker in json.dumps(doc, sort_keys=True) for marker in PRIVATE_MARKERS):
        raise ValueError("cleanup_v2_private_material")
    binding = _exact(doc["bindings"], BINDING_FIELDS)
    if (any(not isinstance(binding[key], str) or not IDENTIFIER.fullmatch(binding[key])
            for key in ("request_id", "run_id", "deployment_id", "source_path_id", "successor_path_id"))
        or any(not _int(binding[key], 1) for key in ("request_attempt", "deployment_epoch", "source_attempt",
                                                       "source_path_generation", "successor_attempt", "successor_path_generation"))
        or not _int(binding["source_path_attempt"], 0)
        or not _int(binding["successor_path_attempt"], 0)
        or binding["request_attempt"] != binding["successor_attempt"]
        or binding["successor_attempt"] <= binding["source_attempt"]
        or binding["successor_path_generation"] <= binding["source_path_generation"]
        or any(not isinstance(binding[key], str) or not DIGEST.fullmatch(binding[key])
               for key in ("source_digest", "candidate_digest", "spec_digest", "contract_digest",
                           "operator_plan_digest"))):
        raise ValueError("cleanup_v2_binding_invalid")
    value = _exact(doc["observation"], OBSERVATION_FIELDS)
    command = _exact(value["command_authority"], COMMAND_FIELDS)
    gateway = _exact(value["gateway_authority"], GATEWAY_FIELDS)
    if (value["case_id"] != CASE or value["executed"] is not True
        or value["ordinary_product_path"] is not True or value["private_material_exposed"] is not False
        or value["rejection_observed"] is not True or value["fail_closed"] is not True
        or value["terminal_state"] != "TERMINAL_BLOCKED"
        or value["unpublishable_suffix_discarded"] is not True
        or value["process_cleanup_complete"] is not True
        or not _int(value["stale_reservation_count"], 0) or value["stale_reservation_count"] != 0
        or not _int(value["duplicated_token_count"], 0) or value["duplicated_token_count"] != 0
        or value["coordinator_terminal"] != "failed"):
        raise ValueError("cleanup_v2_case_invalid")
    # Owner ruling 2026-10-06: the cutover must NOT be committed; the request
    # keeps the source's route identity, never the refused successor's.
    if (value["cutover_committed"] is not False
            or not _int(value["committed_route_attempt"], 1)
            or value["committed_route_attempt"] != binding["source_attempt"]):
        raise ValueError("cleanup_v2_cutover_committed")
    for authority in (command, gateway):
        if (authority["request_id"] != binding["request_id"]
            or authority["request_attempt"] != binding["request_attempt"]):
            raise ValueError("cleanup_v2_authority_binding_mismatch")
    # OWNER RULING 2026-10-06 (supersedes the 2026-09-27 count clause for this
    # shape): the refused successor's command terminal is never committed, so
    # its terminal CAS count is exactly zero. The pre-cutover abort, not that
    # command, released the reservations (checked as the coordinator terminal
    # and zero stale reservations above).
    if (not _int(command["registered_command_count"], 1)
        or not _int(command["terminal_cas_count"], 0)
        or command["terminal_cas_count"] != 0):
        raise ValueError("cleanup_v2_command_terminal_invalid")
    if (not _int(gateway["terminal_event_count"], 0)
        or gateway["terminal_event_count"] != 1
        or gateway["terminal_kind"] != "failed"
        or gateway["terminal_code"] != "request_cleanup_unproven"):
        raise ValueError("cleanup_v2_gateway_notice_invalid")
    return value


def collect_local_authorities(controller: Any, service: Any, request_id: str,
                              request_attempt: int, router: Any = None) -> dict[str, dict[str, Any]]:
    """Local only. Router identity joins the independently queried authorities.

    This private in-process seam cannot establish physical durability or survive
    a retired command controller; the caller must never substitute an empty tuple.
    """
    if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id) or not _int(request_attempt, 1):
        raise ValueError("request_attempt_binding_invalid")
    try:
        pending = router._pending[request_id]
        identity = pending.command_identity
        if identity is None or identity.request_id != request_id or identity.request_attempt != request_attempt:
            raise ValueError("command_authority_binding_mismatch")
        snapshots = controller.snapshot(request_id, request_attempt=request_attempt)
        if not snapshots:
            raise ValueError("command_authority_unavailable")
        if any(s.identity.request_id != request_id or s.identity.request_attempt != request_attempt
               or s.identity.path_digest != identity.path_digest for s in snapshots):
            raise ValueError("command_authority_binding_mismatch")
        command = {"request_id": request_id, "request_attempt": request_attempt,
                   "registered_command_count": len(snapshots),
                   "terminal_cas_count": sum(s.terminal_revision for s in snapshots)}
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("command_authority_unavailable") from exc
    try:
        session = service._get_session(request_id)
        with session.condition:
            terminal = session.terminal_event
            count = session.terminal_count
            if terminal is None or terminal.request_id != request_id:
                raise ValueError("gateway_authority_unavailable")
            # This is the gateway's receipt written at the actual notice
            # publication, not a later read of router's current attempt.
            receipt = session.terminal_provenance
            if (not isinstance(receipt, tuple) or len(receipt) != 4
                or receipt != (identity.request_id, identity.request_attempt,
                               identity.path_digest, identity.command_id)
                or count != 1 or terminal.kind != "failed"
                or terminal.code != "request_cleanup_unproven"):
                raise ValueError("gateway_authority_unavailable")
            gateway = {"request_id": receipt[0], "request_attempt": receipt[1],
                       "terminal_event_count": count, "terminal_kind": terminal.kind,
                       "terminal_code": terminal.code}
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("gateway_authority_unavailable") from exc
    return {"command_authority": command, "gateway_authority": gateway}


# ---------------------------------------------------------------------------
# case15 count-layer truth
#
# Frozen v1 `cleanup_authority_failure` requires a zero count; the owner
# approved a v2 case requiring *exactly one terminal CAS for the releasing
# attempt* together with *complete cleanup and zero stale reservations*.
#
# OWNER RULING 2026-09-27 -- "A6 CASE15 COUNT-LAYER CLAUSE AMENDED"
# (mycelium-core-program/status/decisions.log).  The amended clause, quoted:
#
#     FROM:  terminal_cas_count == 0  AND  process_cleanup_complete is True
#            AND  stale_reservation_count == 0
#     TO:    terminal_cas_count == 1  AND  process_cleanup_complete is True
#            AND  stale_reservation_count == 0
#
# Releasing the reservations IS the committed command terminal, so the original
# co-required clauses were mutually exclusive on every real path and the zero
# cardinality made the clause unsatisfiable -- a specification defect, not a
# control.  The amendment preserves the authority: a committed command is still
# required to prove the release; only the cardinality becomes achievable and
# exact (exactly 1, never 0).  The rejected alternative -- dropping the CAS
# requirement -- stays refused: a cleanup claim with no committed command
# terminal behind it is never reported as a satisfiable envelope.
#
# The reconciliation below records what the three real authorities actually
# measured.  It never supplies a release authority, never turns an unavailable
# measurement into zero, and never reports a blocked clause as satisfied.
# ---------------------------------------------------------------------------

COUNT_LAYER_PROTOCOL = "mycelium.a6_cleanup_count_layer_truth.v2"
LAYER_BLOCKED_UNPROVEN_CLEANUP = "blocked_unproven_cleanup_retains_reservations"
LAYER_PROVEN_LATE_CLEANUP = "proven_late_cleanup_commits_terminal_cas"
LAYER_PRE_CUTOVER_ABORT = "pre_cutover_abort_releases_reservations_successor_terminal_uncommitted"
BLOCKED_CLAUSE_CLEANUP_COMPLETENESS = "process_cleanup_complete_and_zero_stale_reservations"
# Amended 2026-09-27 by the owner ruling "A6 CASE15 COUNT-LAYER CLAUSE AMENDED":
# the cardinality clause is now *exactly one* terminal CAS for the releasing
# attempt (it authorizes the proven-late-cleanup state) and blocks that state
# only when the releasing attempt committed more than one terminal CAS.
BLOCKED_CLAUSE_EXACTLY_ONE_COMMAND_TERMINAL_CAS = "exactly_one_command_terminal_cas_for_the_releasing_attempt"
CLEANUP_FIELDS = {"request_id", "request_attempt", "stale_reservation_count",
                  "process_cleanup_complete"}
OPEN_OWNER_DECISION = (
    "OWNER RULING RECORDED 2026-09-27 (status/decisions.log, 'A6 CASE15 COUNT-LAYER CLAUSE "
    "AMENDED'): the v2 clause is amended FROM 'zero command terminal CAS AND complete cleanup with "
    "zero stale reservations' TO 'exactly one terminal CAS for the releasing attempt AND complete "
    "cleanup with zero stale reservations' -- terminal_cas_count == 1 AND process_cleanup_complete "
    "is True AND stale_reservation_count == 0. Releasing the reservations IS the committed command "
    "terminal, so the original co-required clauses were mutually exclusive on every real path and "
    "the zero-CAS cardinality made this clause unsatisfiable (a specification defect, not a "
    "control). The authority is preserved: a committed command is still required to prove the "
    "release. The rejected alternative -- dropping the CAS requirement and accepting "
    "'process_cleanup_complete=true with stale_reservation_count=0' on its own -- stays refused: a "
    "cleanup claim with terminal_cas_count == 0 has no committed command terminal behind it and is "
    "never reported as a satisfiable envelope (cleanup_v2_count_layer_conflict_requires_owner_"
    "decision). The blocked retained-reservation state (blocked_unproven_cleanup_retains_"
    "reservations) is unchanged by the ruling and stays BLOCKED: no v2 envelope, gate record, or "
    "physical claim may be emitted from it. OWNER RULING 2026-10-06 ('A6 CASE15: THE TEST MATCHES "
    "THE PRODUCT'): the case proves a refusal BEFORE cutover; with no committed cutover the only "
    "authorized state is pre_cutover_abort_releases_reservations_successor_terminal_uncommitted -- "
    "terminal_cas_count == 0 for the refused successor attempt AND complete coordinator cleanup "
    "with zero stale reservations."
)


def reconcile_count_layers(*, command_authority: Mapping[str, Any],
                           gateway_authority: Mapping[str, Any],
                           cleanup_authority: Mapping[str, Any],
                           cutover_committed: bool = True) -> dict[str, Any]:
    """Reconcile the three measured count layers for `cleanup_authority_failure`.

    CommandController terminal CAS, the RequestGateway failure notice, and the
    coordinator's retained request reservations are independent authorities.
    The result states which measured state the request is in and whether the v2
    envelope may be emitted at all.  The frozen v1 predicate is not reinterpreted
    here and stays failed/unqualified.

    The v2 acceptance clause was AMENDED by the owner on 2026-09-27 (ruling
    "A6 CASE15 COUNT-LAYER CLAUSE AMENDED", status/decisions.log), quoted:

        FROM:  terminal_cas_count == 0  AND  process_cleanup_complete is True
               AND  stale_reservation_count == 0
        TO:    terminal_cas_count == 1  AND  process_cleanup_complete is True
               AND  stale_reservation_count == 0

    Releasing the reservations IS the committed command terminal, so a real
    release implies exactly one terminal CAS and the original zero-CAS
    requirement was unsatisfiable on every real path.  The amendment keeps the
    authority -- a committed command is still required to prove the release --
    and only makes the cardinality achievable and exact.  A declared complete
    cleanup with zero committed command terminal CAS therefore stays refused
    after a committed cutover.

    OWNER RULING 2026-10-06: with ``cutover_committed=False`` the successor was
    refused before cutover. The recovery's pre-cutover abort released the
    reservations and ended the coordinator's request, and the successor's
    command terminal stays uncommitted because its cleanup was never proven:
    exactly zero terminal CAS with complete coordinator cleanup and zero stale
    reservations is that state, and the only one authorized for it.
    """
    command = _exact(command_authority, COMMAND_FIELDS)
    gateway = _exact(gateway_authority, GATEWAY_FIELDS)
    cleanup = _exact(cleanup_authority, CLEANUP_FIELDS)
    if not (isinstance(command["request_id"], str) and command["request_id"]
            and _int(command["request_attempt"], 1)):
        raise ValueError("cleanup_v2_count_layer_binding_mismatch")
    if any(authority["request_id"] != command["request_id"]
           or authority["request_attempt"] != command["request_attempt"]
           for authority in (gateway, cleanup)):
        raise ValueError("cleanup_v2_count_layer_binding_mismatch")
    if (not _int(command["registered_command_count"], 1)
            or not _int(command["terminal_cas_count"], 0)):
        raise ValueError("cleanup_v2_count_layer_command_terminal_invalid")
    if (not _int(gateway["terminal_event_count"], 0)
            or gateway["terminal_event_count"] != 1
            or gateway["terminal_kind"] != "failed"
            or gateway["terminal_code"] != "request_cleanup_unproven"):
        raise ValueError("cleanup_v2_count_layer_gateway_notice_invalid")
    if (not _int(cleanup["stale_reservation_count"], 0)
            or cleanup["process_cleanup_complete"] not in (True, False)):
        raise ValueError("cleanup_v2_count_layer_cleanup_authority_invalid")
    cas = command["terminal_cas_count"]
    stale = cleanup["stale_reservation_count"]
    complete = cleanup["process_cleanup_complete"]
    if cutover_committed not in (True, False):
        raise ValueError("cleanup_v2_count_layer_cutover_unmeasured")
    if cutover_committed is False:
        if cas != 0 or complete is not True or stale != 0:
            raise ValueError("cleanup_v2_count_layer_inconsistent")
        state, clause, authorized = LAYER_PRE_CUTOVER_ABORT, None, True
    elif cas == 0:
        if complete is True and stale == 0:
            # Still refused after the 2026-09-27 amendment -- it is not weakened to
            # reach green.  The amended clause permits EXACTLY ONE terminal CAS for
            # the releasing attempt, never zero: a claimed complete cleanup with no
            # committed command terminal behind it is not an observable product
            # state and has no authority to release the reservations.
            raise ValueError("cleanup_v2_count_layer_conflict_requires_owner_decision")
        if complete is True or stale == 0:
            raise ValueError("cleanup_v2_count_layer_inconsistent")
        state, clause, authorized = (
            LAYER_BLOCKED_UNPROVEN_CLEANUP, BLOCKED_CLAUSE_CLEANUP_COMPLETENESS, False)
    elif cas == 1:
        if complete is not True or stale != 0:
            raise ValueError("cleanup_v2_count_layer_inconsistent")
        # Amended clause satisfied (owner ruling 2026-09-27): exactly one terminal
        # CAS for the releasing attempt AND complete cleanup with zero stale
        # reservations.  The committed command terminal is what proved the release,
        # so nothing here is blocked and no clause is reported as blocked.
        state, clause, authorized = LAYER_PROVEN_LATE_CLEANUP, None, True
    else:
        if complete is not True or stale != 0:
            raise ValueError("cleanup_v2_count_layer_inconsistent")
        # The amended cardinality is exact: more than one terminal CAS for the
        # releasing attempt is not the authorized clause and stays blocked.
        state, clause, authorized = (
            LAYER_PROVEN_LATE_CLEANUP, BLOCKED_CLAUSE_EXACTLY_ONE_COMMAND_TERMINAL_CAS, False)
    return {
        "protocol": COUNT_LAYER_PROTOCOL,
        "request_id": command["request_id"],
        "request_attempt": command["request_attempt"],
        "layer_state": state,
        "command_terminal_cas_count": cas,
        "gateway_failure_notice_count": gateway["terminal_event_count"],
        "gateway_failure_code": gateway["terminal_code"],
        "stale_reservation_count": stale,
        "process_cleanup_complete": complete,
        "cutover_committed": cutover_committed,
        "v2_envelope_authorized": authorized,
        "blocked_clause": clause,
        "open_owner_decision": OPEN_OWNER_DECISION,
        "v1_gate_record": "failed_unqualified_unchanged",
        "product_notice_preserved": True,
        "physical_qualification": False,
        "qualification_claim": False,
    }


__all__ = [
    "AUTHORITY",
    "BLOCKED_CLAUSE_CLEANUP_COMPLETENESS",
    "BLOCKED_CLAUSE_EXACTLY_ONE_COMMAND_TERMINAL_CAS",
    "CASE",
    "CLEANUP_FIELDS",
    "COUNT_LAYER_PROTOCOL",
    "LAYER_BLOCKED_UNPROVEN_CLEANUP",
    "LAYER_PRE_CUTOVER_ABORT",
    "LAYER_PROVEN_LATE_CLEANUP",
    "OPEN_OWNER_DECISION",
    "PROTOCOL",
    "collect_local_authorities",
    "reconcile_count_layers",
    "validate_observation",
]
