# SPDX-License-Identifier: AGPL-3.0-or-later
"""Case15 count-layer truth: zero command CAS blocked vs a real delayed proof.

The frozen v1 `cleanup_authority_failure` reader requires a zero count and the
owner-approved v2 case requires zero command terminal CAS *and* complete cleanup
with zero stale reservations.  Those two requirements were never measured on the
same path.  The reconciliation below is measurement only: it never supplies a
release authority, never zeroes an unavailable count, and never reports the
blocked clause as satisfied.
"""
from __future__ import annotations

import pytest

from mycelium_a6_cleanup_observation_v2 import (
    BLOCKED_CLAUSE_CLEANUP_COMPLETENESS,
    BLOCKED_CLAUSE_ZERO_COMMAND_CAS,
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


def test_real_delayed_proof_releases_with_cas_and_cannot_retrofit_the_v2_clause():
    command, gateway, cleanup = authorities(cas=1, stale=0, complete=True)
    truth = reconcile_count_layers(command_authority=command, gateway_authority=gateway,
                                   cleanup_authority=cleanup)
    assert truth["layer_state"] == LAYER_PROVEN_LATE_CLEANUP
    assert truth["v2_envelope_authorized"] is False
    assert truth["blocked_clause"] == BLOCKED_CLAUSE_ZERO_COMMAND_CAS
    assert truth["open_owner_decision"] == OPEN_OWNER_DECISION


def test_claimed_complete_cleanup_with_zero_command_cas_is_refused_not_reported():
    # The two co-required v2 clauses cannot both be measured on any real path:
    # releasing the reservations *is* the committed command terminal.
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
        assert truth["v2_envelope_authorized"] is False
    finally:
        service.close()
