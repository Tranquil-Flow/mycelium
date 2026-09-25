# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real local gateway/router/controller composition; route injection is simulated."""

import json
import pytest

from mycelium_a6_cleanup_observation_v2 import collect_local_authorities
from mycelium_request_gateway.service import _GatewayEmission
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_qualification.replica_track_backend import (
    ReplicaTrackDispatcher, ReplicaTrackSessionBackend,
)
from tests.live.test_a6_live_router_recovery import (
    MissingCleanupReceiptAfterCutoverRoute, FailingThenRecoveringRoute,
    _runtime, _submit_and_drain,
)

class DelayedSuccessorProofRoute(MissingCleanupReceiptAfterCutoverRoute):
    """Simulated delayed route-owner proof, never physical cleanup evidence."""

    def infer(self, *args, publish_cleanup_receipt, command_identity, **kwargs):
        if command_identity["request_attempt"] == 2:
            self.late_publisher = publish_cleanup_receipt
            self.successor_command = dict(command_identity)
        else:
            self.source_publisher = publish_cleanup_receipt
            self.source_command = dict(command_identity)
        return super().infer(*args, command_identity=command_identity, **kwargs)

    def prove_successor_cleanup(self, *, foreign=False):
        identity = dict(self.successor_command)
        if foreign:
            identity["path_digest"] = "sha256:" + "f" * 64
        self.late_publisher({**identity, "cleanup_owner_id":
                             f"physical-live-route:{identity['deployment_id']}", "node_ids": []})

    def deliver_stale_source_proof(self):
        identity = dict(self.source_command)
        self.source_publisher({**identity, "cleanup_owner_id":
                               f"physical-live-route:{identity['deployment_id']}", "node_ids": []})


def test_late_exact_route_proof_finalizes_retained_reservations_without_rewriting_gateway(tmp_path):
    route = DelayedSuccessorProofRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    original_cas = router._commands.terminal_compare_and_swap
    accepted = []
    def observed_cas(*args, **kwargs):
        result = original_cas(*args, **kwargs)
        if result.accepted:
            accepted.append(args[0].identity.request_attempt)
        return result
    router._commands.terminal_compare_and_swap = observed_cas
    try:
        request_id, events = _submit_and_drain(service, qualification)
        receipt = service.cleanup_notice_provenance(request_id)
        assert [(e.kind, e.code) for e in events if e.kind == "failed"] == [
            ("failed", "request_cleanup_unproven")]
        assert router.request_status(request_id) == "TERMINAL_BLOCKED"
        assert next(x for x in coordinator.status()["requests"] if x["request_id"] == request_id)["reservation_count"] == 2
        route.deliver_stale_source_proof()
        assert next(x for x in coordinator.status()["requests"] if x["request_id"] == request_id)["reservation_count"] == 2
        with pytest.raises(RuntimeError, match="command_cleanup_receipt_identity_mismatch"):
            route.prove_successor_cleanup(foreign=True)
        assert next(x for x in coordinator.status()["requests"] if x["request_id"] == request_id)["reservation_count"] == 2
        assert accepted == [1]
        assert service.cleanup_notice_provenance(request_id) == receipt
        route.prove_successor_cleanup()
        projected = next(x for x in coordinator.status()["requests"] if x["request_id"] == request_id)
        assert projected["reservation_count"] == 0
        assert projected["terminal_state"] == "failed"
        assert service.terminal_event_count(request_id) == 1
        assert service.cleanup_notice_provenance(request_id) == receipt
        # Late authoritative completion necessarily records a command terminal CAS;
        # it cannot satisfy v2's zero-CAS terminal-blocked case retrospectively.
        assert accepted == [1, 2]
        with pytest.raises(ValueError, match="command_authority_unavailable"):
            collect_local_authorities(router._commands, service, request_id, 2, router)
    finally:
        service.close()


def test_blocked_cleanup_counts_distinct_real_product_authorities(tmp_path):
    coordinator, router, service, qualification = _runtime(tmp_path, MissingCleanupReceiptAfterCutoverRoute())
    try:
        request_id, events = _submit_and_drain(service, qualification)
        attempt = coordinator.route_identity(request_id)["request_attempt"]
        measured = collect_local_authorities(router._commands, service, request_id, attempt, router)
        assert measured["command_authority"]["terminal_cas_count"] == 0
        assert measured["command_authority"]["registered_command_count"] >= 1
        assert measured["gateway_authority"]["terminal_event_count"] == 1
        assert measured["gateway_authority"]["request_attempt"] == attempt
        from mycelium_a6_cleanup_observation_v2 import validate_observation
        from tests.a6_acceptance.test_cleanup_observation_v2 import sample, resign
        doc = sample()
        doc["bindings"]["request_id"] = request_id
        doc["observation"].update(measured)
        projected = next(item for item in coordinator.status()["requests"]
                         if item["request_id"] == request_id)
        public_requests = service.recovery_public_projection()["requests"]
        assert isinstance(public_requests, list)
        public = next(item for item in public_requests
                      if item["request_id"] == request_id)
        assert router.request_status(request_id) == "TERMINAL_BLOCKED"
        assert projected["reservation_count"] == 2
        assert public["cleanup_complete"] is False
        assert public["attempt"] == attempt
        print("A6_LOCAL_OBSERVATION=" + json.dumps({
            "request_id": request_id, "request_attempt": attempt,
            "route_status": router.request_status(request_id),
            "command_authority": measured["command_authority"],
            "gateway_authority": measured["gateway_authority"],
            "cleanup_complete": public["cleanup_complete"],
            "reservation_count": projected["reservation_count"],
            "physical_qualified": False,
        }, sort_keys=True))
        doc["observation"]["process_cleanup_complete"] = public["cleanup_complete"]
        doc["observation"]["stale_reservation_count"] = projected["reservation_count"]
        with pytest.raises(ValueError, match="cleanup_v2_case_invalid"):
            validate_observation(resign(doc))
        assert measured["gateway_authority"]["terminal_kind"] == "failed"
        assert measured["gateway_authority"]["terminal_code"] == "request_cleanup_unproven"
        session = service._get_session(request_id)
        original_receipt = service.cleanup_notice_provenance(request_id)
        assert original_receipt is not None and original_receipt[1] == attempt
        _GatewayEmission(service, session).bind_terminal_blocked_identity(
            (request_id, attempt + 1, original_receipt[2], original_receipt[3]))
        service._append_cleanup_unproven_terminal(session)  # duplicate notice loses
        assert service.cleanup_notice_provenance(request_id) == original_receipt
        assert service.terminal_event_count(request_id) == 1
        assert [(e.kind, e.code) for e in events if e.kind in {"completed", "failed", "cancelled"}] == [
            ("failed", "request_cleanup_unproven")]
        with pytest.raises(ValueError, match="command_authority_binding_mismatch|command_authority_unavailable"):
            collect_local_authorities(router._commands, service, request_id, attempt + 1, router)
        with pytest.raises(ValueError, match="command_authority_unavailable"):
            collect_local_authorities(router._commands, service, "missing-request", attempt, router)
    finally:
        service.close()


@pytest.mark.parametrize("corruption", ["missing", "wrong_attempt", "foreign", "raises"])
def test_invalid_blocked_identity_never_suppresses_notice_or_mints_v2(tmp_path, corruption):
    coordinator, router, service, qualification = _runtime(tmp_path, MissingCleanupReceiptAfterCutoverRoute())
    original = router.terminal_blocked_identity
    def corrupted(request_id):
        if corruption == "raises":
            raise RuntimeError("diagnostic_unavailable")
        identity = original(request_id)
        if corruption == "missing":
            return None
        assert identity is not None
        if corruption == "foreign":
            return ("another-request", *identity[1:])
        return (identity[0], identity[1] + 1, *identity[2:])
    router.terminal_blocked_identity = corrupted
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [(e.kind, e.code) for e in events if e.kind in {"completed", "failed", "cancelled"}] == [
            ("failed", "request_cleanup_unproven")]
        assert service.terminal_event_count(request_id) == 1
        attempt = coordinator.route_identity(request_id)["request_attempt"]
        with pytest.raises(ValueError, match="gateway_authority_unavailable"):
            collect_local_authorities(router._commands, service, request_id, attempt, router)
    finally:
        service.close()


def test_ordinary_success_keeps_one_gateway_terminal(tmp_path):
    coordinator, router, service, qualification = _runtime(tmp_path, FailingThenRecoveringRoute())
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [(e.kind, e.code) for e in events if e.kind in {"completed", "failed", "cancelled"}] == [
            ("completed", None)]
        assert service.terminal_event_count(request_id) == 1
        assert service.cleanup_notice_provenance(request_id) is None
    finally:
        service.close()


def test_replica_wrapper_plain_path_forwards_private_emitter(tmp_path):
    coordinator, router, service, qualification = _runtime(tmp_path, MissingCleanupReceiptAfterCutoverRoute())
    plain = service._backend
    assert isinstance(plain, RouterSessionBackend)
    dispatcher = ReplicaTrackDispatcher(
        router=router, codec=plain._codec, clock=plain._clock,
        qualification_source=plain._qualification_source,
        replica_qualifications_factory=lambda: (),
        replica_loss_placement_ids_factory=frozenset,
    )
    service._backend = ReplicaTrackSessionBackend(
        dispatcher=dispatcher, now_unix_ms=lambda: 1_200,
        plain_backend=plain,
    )
    try:
        request_id, events = _submit_and_drain(service, qualification)
        attempt = coordinator.route_identity(request_id)["request_attempt"]
        measured = collect_local_authorities(router._commands, service, request_id, attempt, router)
        assert measured["gateway_authority"]["request_attempt"] == attempt
        assert measured["command_authority"]["terminal_cas_count"] == 0
        assert [(e.kind, e.code) for e in events if e.kind == "failed"] == [
            ("failed", "request_cleanup_unproven")]
    finally:
        service.close()
