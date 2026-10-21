# SPDX-License-Identifier: AGPL-3.0-only
"""Case15 count-layer truth: zero command CAS blocked vs a real delayed proof.

The frozen v1 `cleanup_authority_failure` reader requires a zero count and the
owner-approved v2 case requires complete cleanup with zero stale reservations
*and* -- per the owner ruling of 2026-09-27, "A6 CASE15 COUNT-LAYER CLAUSE
AMENDED" (status/decisions.log) -- exactly one terminal CAS for the releasing
attempt.  The amended clause, quoted:

    FROM:  terminal_cas_count == 0  AND  process_cleanup_complete is True
           AND  stale_reservation_count == 0
    TO:    terminal_cas_count == 1  AND  process_cleanup_complete is True
           AND  stale_reservation_count == 0

Releasing the reservations IS the committed command terminal, so requiring zero
made the clause unsatisfiable on any real path.  This is a CLAUSE change, not an
assertion weakened to reach green: the committed command is still required, the
cardinality is exact, and a cleanup claim with *zero* committed command terminal
CAS is still refused rather than reported.  The reconciliation below is
measurement only: it never supplies a release authority, never zeroes an
unavailable count, and never reports a blocked clause as satisfied.
"""
from __future__ import annotations

import pytest

from mycelium_a6_cleanup_observation_v2 import (
    BLOCKED_CLAUSE_CLEANUP_COMPLETENESS,
    BLOCKED_CLAUSE_EXACTLY_ONE_COMMAND_TERMINAL_CAS,
    COUNT_LAYER_PROTOCOL,
    LAYER_BLOCKED_UNPROVEN_CLEANUP,
    LAYER_PROVEN_LATE_CLEANUP,
    OPEN_OWNER_DECISION,
    collect_local_authorities,
    reconcile_count_layers,
)
from tests.live.test_a6_cleanup_observation_v2 import (
    DelayedSuccessorProofRoute,
    MissingCleanupReceiptAfterCutoverRoute,
)
from tests.live.test_a6_live_router_recovery import _runtime, _submit_and_drain


def authorities(*, attempt=2, registered=1, cas=0, notices=1, stale=2, complete=False):
    command = {"request_id": "request-1", "request_attempt": attempt,
               "registered_command_count": registered, "terminal_cas_count": cas}
    gateway = {"request_id": "request-1", "request_attempt": attempt,
               "terminal_event_count": notices, "terminal_kind": "failed",
               "terminal_code": "request_cleanup_unproven"}
    cleanup = {"request_id": "request-1", "request_attempt": attempt,
               "stale_reservation_count": stale, "process_cleanup_complete": complete}
    return command, gateway, cleanup


def test_blocked_zero_cas_retains_reservations_is_truthful_and_clause_stays_blocked():
    # Row 1 of the ruling's behaviour table: UNCHANGED by the 2026-09-27 clause
    # amendment.  The retained-reservation state stays blocked and unauthorized.
    command, gateway, cleanup = authorities()
    truth = reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                                   cleanup_authority=cleanup)
    assert truth["protocol"] == COUNT_LAYER_PROTOCOL
    assert truth["layer_state"] == LAYER_BLOCKED_UNPROVEN_CLEANUP
    assert truth["command_terminal_cas_count"] == 0
    assert truth["gateway_failure_notice_count"] == 1
    assert truth["stale_reservation_count"] == 2
    assert truth["process_cleanup_complete"] is False
    assert truth["v2_envelope_authorized"] is False
    assert truth["blocked_clause"] == BLOCKED_CLAUSE_CLEANUP_COMPLETENESS
    assert truth["open_owner_decision"] == OPEN_OWNER_DECISION
    assert truth["v1_gate_record"] == "failed_unqualified_unchanged"
    assert truth["physical_qualification"] is False


def test_real_delayed_proof_releases_with_exactly_one_cas_and_satisfies_the_amended_clause():
    # Row 2 of the ruling's behaviour table -- the assertion that flips.
    # OWNER RULING 2026-09-27, "A6 CASE15 COUNT-LAYER CLAUSE AMENDED"
    # (status/decisions.log).  The v2 clause changed from "zero command terminal
    # CAS AND complete cleanup with zero stale reservations" to:
    #
    #     terminal_cas_count == 1  AND  process_cleanup_complete is True
    #     AND stale_reservation_count == 0
    #
    # This real delayed proof releases the reservations by committing the command
    # terminal, so it now satisfies the clause: v2_envelope_authorized flips
    # False -> True *because the clause changed*, not because an assertion was
    # weakened.  The authority is intact -- the committed CAS is still required
    # (zero would be refused, see below), the v1 record stays failed/unqualified,
    # and no physical qualification is claimed.
    command, gateway, cleanup = authorities(cas=1, stale=0, complete=True)
    truth = reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                                   cleanup_authority=cleanup)
    assert truth["layer_state"] == LAYER_PROVEN_LATE_CLEANUP
    assert truth["command_terminal_cas_count"] == 1
    assert truth["v2_envelope_authorized"] is True
    assert truth["blocked_clause"] is None
    assert truth["open_owner_decision"] == OPEN_OWNER_DECISION
    assert truth["v1_gate_record"] == "failed_unqualified_unchanged"
    assert truth["physical_qualification"] is False
    assert truth["qualification_claim"] is False


def test_more_than_one_terminal_cas_for_the_releasing_attempt_is_not_authorized():
    # The amended cardinality is exact ("exactly one terminal CAS for the
    # releasing attempt", owner ruling 2026-09-27).  A measured state with more
    # than one committed terminal CAS is reported as blocked, never authorized.
    command, gateway, cleanup = authorities(cas=2, stale=0, complete=True)
    truth = reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                                   cleanup_authority=cleanup)
    assert truth["layer_state"] == LAYER_PROVEN_LATE_CLEANUP
    assert truth["command_terminal_cas_count"] == 2
    assert truth["v2_envelope_authorized"] is False
    assert truth["blocked_clause"] == BLOCKED_CLAUSE_EXACTLY_ONE_COMMAND_TERMINAL_CAS


def test_claimed_complete_cleanup_with_zero_command_cas_is_refused_not_reported():
    # Row 3 of the ruling's behaviour table -- still REFUSED, not reported.
    # The 2026-09-27 amendment permits EXACTLY ONE terminal CAS for the releasing
    # attempt, never zero: a claimed complete cleanup with no committed command
    # terminal behind it is the fabricated-green failure mode the ruling's
    # rejected alternative would have allowed.  Releasing the reservations *is*
    # the committed command terminal, so this input is contradictory.
    command, gateway, cleanup = authorities(cas=0, stale=0, complete=True)
    with pytest.raises(ValueError,
                       match="cleanup_v2_count_layer_conflict_requires_owner_decision"):
        reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                               cleanup_authority=cleanup)


@pytest.mark.parametrize("mutate,message", [
    ("foreign_request", "count_layer_binding_mismatch"),
    ("foreign_attempt", "count_layer_binding_mismatch"),
    ("no_registered_command", "command_terminal_invalid"),
    ("negative_cas", "command_terminal_invalid"),
    ("cas_unknown", "command_terminal_invalid"),
    ("missing_notice", "gateway_notice_invalid"),
    ("duplicate_notice", "gateway_notice_invalid"),
    ("wrong_notice_code", "gateway_notice_invalid"),
    ("wrong_notice_kind", "gateway_notice_invalid"),
    ("reservations_unknown", "cleanup_authority_invalid"),
    ("reservations_negative", "cleanup_authority_invalid"),
    ("cleanup_unknown", "cleanup_authority_invalid"),
    ("extra_field", "closed_shape_invalid"),
])
def test_unmeasured_or_mismatched_layers_never_become_zero(mutate, message):
    command, gateway, cleanup = authorities()
    if mutate == "foreign_request":
        gateway["request_id"] = "foreign"
    elif mutate == "foreign_attempt":
        cleanup["request_attempt"] = 3
    elif mutate == "no_registered_command":
        command["registered_command_count"] = 0
    elif mutate == "negative_cas":
        command["terminal_cas_count"] = -1
    elif mutate == "cas_unknown":
        command["terminal_cas_count"] = None
    elif mutate == "missing_notice":
        gateway["terminal_event_count"] = 0
    elif mutate == "duplicate_notice":
        gateway["terminal_event_count"] = 2
    elif mutate == "wrong_notice_code":
        gateway["terminal_code"] = "request_completed"
    elif mutate == "wrong_notice_kind":
        gateway["terminal_kind"] = "completed"
    elif mutate == "reservations_unknown":
        cleanup["stale_reservation_count"] = None
    elif mutate == "reservations_negative":
        cleanup["stale_reservation_count"] = -1
    elif mutate == "cleanup_unknown":
        cleanup["process_cleanup_complete"] = None
    else:
        command["receipt"] = {"secret": "x"}
    with pytest.raises(ValueError, match=message):
        reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                               cleanup_authority=cleanup)


def test_measured_product_authorities_reconcile_the_blocked_state(tmp_path):
    coordinator, router, service, qualification = _runtime(
        tmp_path, MissingCleanupReceiptAfterCutoverRoute())
    try:
        request_id, _events = _submit_and_drain(service, qualification)
        attempt = coordinator.route_identity(request_id)["request_attempt"]
        measured = collect_local_authorities(router._commands, service, request_id, attempt, router)
        projected = next(item for item in coordinator.status()["requests"]
                         if item["request_id"] == request_id)
        public = next(item for item in service.recovery_public_projection()["requests"]
                      if item["request_id"] == request_id)
        truth = reconcile_count_layers(
            command_authority=measured["command_authority"],
            gateway_authority=measured["gateway_authority"],
            cleanup_authority={"request_id": request_id, "request_attempt": attempt,
                               "stale_reservation_count": projected["reservation_count"],
                               "process_cleanup_complete": public["cleanup_complete"]})
        assert measured["command_authority"]["terminal_cas_count"] == 0
        assert measured["gateway_authority"]["terminal_event_count"] == 1
        assert truth["layer_state"] == LAYER_BLOCKED_UNPROVEN_CLEANUP
        assert truth["stale_reservation_count"] == projected["reservation_count"] > 0
        assert truth["v2_envelope_authorized"] is False
    finally:
        service.close()


def test_measured_product_authorities_reconcile_the_real_delayed_proof(tmp_path):
    route = DelayedSuccessorProofRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    original = router._commands.terminal_compare_and_swap
    accepted = []

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.accepted:
            accepted.append(args[0].identity.request_attempt)
        return result

    router._commands.terminal_compare_and_swap = observed
    try:
        request_id, _events = _submit_and_drain(service, qualification)
        attempt = coordinator.route_identity(request_id)["request_attempt"]
        blocked = next(item for item in coordinator.status()["requests"]
                       if item["request_id"] == request_id)
        assert blocked["reservation_count"] == 2 and accepted == [1]
        route.prove_successor_cleanup()
        proven = next(item for item in coordinator.status()["requests"]
                      if item["request_id"] == request_id)
        public = next(item for item in service.recovery_public_projection()["requests"]
                      if item["request_id"] == request_id)
        assert proven["reservation_count"] == 0
        assert accepted == [1, 2]
        assert service.terminal_event_count(request_id) == 1
        truth = reconcile_count_layers(
            command_authority={"request_id": request_id, "request_attempt": attempt,
                               "registered_command_count": len(accepted),
                               "terminal_cas_count": len(accepted) - 1},
            gateway_authority={"request_id": request_id, "request_attempt": attempt,
                               "terminal_event_count": service.terminal_event_count(request_id),
                               "terminal_kind": "failed", "terminal_code": "request_cleanup_unproven"},
            cleanup_authority={"request_id": request_id, "request_attempt": attempt,
                               "stale_reservation_count": proven["reservation_count"],
                               "process_cleanup_complete": public["cleanup_complete"]})
        assert truth["layer_state"] == LAYER_PROVEN_LATE_CLEANUP
        # Flipped False -> True by the owner ruling of 2026-09-27 (row 2): the
        # measured release commits exactly one terminal CAS for the releasing
        # attempt (accepted == [1, 2] above; the source attempt is not the
        # releasing one), so the amended clause is satisfied on this real path
        # rather than blocked on the unachievable zero-CAS requirement.
        assert truth["command_terminal_cas_count"] == 1
        assert truth["v2_envelope_authorized"] is True
        assert truth["blocked_clause"] is None
    finally:
        service.close()


# Owner ruling 2026-10-06 ("A6 CASE15: THE TEST MATCHES THE PRODUCT"): the case
# proves a refusal before cutover. Its only authorized state is the pre-cutover
# abort: zero terminal CAS for the refused successor attempt, with the
# coordinator's request ended and no stale reservation.

def test_pre_cutover_refusal_is_the_authorized_state():
    from mycelium_a6_cleanup_observation_v2 import LAYER_PRE_CUTOVER_ABORT

    command, gateway, cleanup = authorities(cas=0, stale=0, complete=True)
    truth = reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                                   cleanup_authority=cleanup, cutover_committed=False)
    assert truth["layer_state"] == LAYER_PRE_CUTOVER_ABORT
    assert truth["cutover_committed"] is False
    assert truth["v2_envelope_authorized"] is True
    assert truth["blocked_clause"] is None


@pytest.mark.parametrize("cas,stale,complete", [
    (1, 0, True),   # a committed successor terminal is not the refused shape
    (2, 0, True),
    (0, 2, False),  # retained reservations are not consistent cleanup
    (0, 0, False),
    (0, 1, True),
])
def test_pre_cutover_refusal_counts_are_exact(cas, stale, complete):
    command, gateway, cleanup = authorities(cas=cas, stale=stale, complete=complete)
    with pytest.raises(ValueError, match="cleanup_v2_count_layer_inconsistent"):
        reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                               cleanup_authority=cleanup, cutover_committed=False)


def test_unmeasured_cutover_is_never_assumed():
    command, gateway, cleanup = authorities(cas=0, stale=0, complete=True)
    with pytest.raises(ValueError, match="cleanup_v2_count_layer_cutover_unmeasured"):
        reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                               cleanup_authority=cleanup, cutover_committed=None)


def test_measured_product_authorities_reconcile_the_pre_cutover_refusal(tmp_path):
    from mycelium_a6_cleanup_observation_v2 import LAYER_PRE_CUTOVER_ABORT
    from tests.live.test_a6_cleanup_observation_v2 import UnprovenSuccessorCleanupRoute

    route = UnprovenSuccessorCleanupRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, _events = _submit_and_drain(service, qualification)
        measured = collect_local_authorities(router._commands, service, request_id, 2, router)
        projected = next(item for item in coordinator.status()["requests"]
                         if item["request_id"] == request_id)
        truth = reconcile_count_layers(
            **measured,
            cleanup_authority={
                "request_id": request_id, "request_attempt": 2,
                "stale_reservation_count": projected["reservation_count"],
                "process_cleanup_complete": (projected["terminal_state"] is not None
                                             and projected["reservation_count"] == 0)},
            cutover_committed=coordinator.route_identity(request_id)["request_attempt"] != 1)
        assert truth["layer_state"] == LAYER_PRE_CUTOVER_ABORT
        assert truth["v2_envelope_authorized"] is True
    finally:
        service.close()
