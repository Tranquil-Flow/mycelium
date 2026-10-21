# SPDX-License-Identifier: AGPL-3.0-only
"""Real local gateway/router/controller composition; route injection is simulated."""

import json
import threading
import time

import pytest

from mycelium_a6_cleanup_observation_v2 import collect_local_authorities
from mycelium_request_gateway.service import _GatewayEmission
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_qualification.replica_track_backend import (
    ReplicaTrackDispatcher, ReplicaTrackSessionBackend,
)
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
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


class UnprovenSuccessorCleanupRoute(MissingCleanupReceiptAfterCutoverRoute):
    """The successor generates, then its completion cleanup cannot be proven.

    Mirrors the physical route when the successor's peers are stalled at the
    completion-cleanup authorization: ``infer`` raises the route's
    ``request_terminal_blocked_cleanup_unproven`` and no receipt exists for
    the successor attempt. Simulated; never physical cleanup evidence.
    """

    def __init__(self, *, hold_observed=None) -> None:
        super().__init__()
        self.hold_observed = hold_observed

    def infer(self, prompt_token_ids, *, sink, command_identity, publish_cleanup_receipt,
              authorize_cleanup, **kwargs):
        if command_identity["request_attempt"] == 1:
            self.source_command = dict(command_identity)
            return super().infer(prompt_token_ids, sink=sink,
                                 command_identity=command_identity, **kwargs)
        self.successor_command = dict(command_identity)
        self.late_publisher = publish_cleanup_receipt
        sink.emit(0, 103)
        sink.emit(1, 104)
        authorize_cleanup(time.monotonic() + 1.0)
        if self.hold_observed is not None:
            self.hold_observed()
        raise RuntimeError("request_terminal_blocked_cleanup_unproven")


def test_unproven_successor_cleanup_aborts_before_cutover_with_one_blocked_notice(tmp_path):
    # The product's real behaviour for case 15 (owner ruling 2026-10-06).
    route = UnprovenSuccessorCleanupRoute()
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        # Only the source's prefix was ever published; the successor's
        # buffered suffix (<103>, <104>) is discarded, nothing twice.
        assert [e.text for e in events if e.kind == "token"] == ["<101>", "<102>"]
        assert [(e.kind, e.code) for e in events if e.kind in {
            "completed", "failed", "cancelled"}] == [("failed", "request_cleanup_unproven")]
        assert service.terminal_event_count(request_id) == 1
        # No cutover: the coordinator keeps the source's route identity.
        assert coordinator.route_identity(request_id)["request_attempt"] == 1
        view = router.a6_request_view(request_id)
        assert router.request_status(request_id) == "TERMINAL_BLOCKED"
        assert view["terminal_blocked_reason"] == "command_cleanup_receipt_missing"
        assert view["terminal_status"] is None
        assert view["request_attempt"] == 2 and view["recovery_attempts"] == 1
        assert view["token_count"] == view["cursor"] == 2
        assert view["recovery_output_count"] == 2
        # No failover: the refused successor cannot be retired without proof.
        assert len([c for c in route.calls
                    if c["command_identity"]["request_attempt"] > 1]) == 0
        # The pre-cutover abort released the reservations and ended the
        # request failed; the successor command's terminal stays uncommitted.
        projected = next(x for x in coordinator.status()["requests"]
                         if x["request_id"] == request_id)
        assert projected["terminal_state"] == "failed"
        assert projected["reservation_count"] == 0
        assert router.a6_command_authority(request_id) == {
            "request_id": request_id, "request_attempt": 2,
            "registered_command_count": 1, "terminal_cas_count": 0}
        assert service.cleanup_notice_provenance(request_id)[1] == 2
        # A late successor proof cannot rewrite the refused authority.
        identity = dict(route.successor_command)
        with pytest.raises(RuntimeError, match="command_cleanup_authority_changed"):
            route.late_publisher({**identity, "cleanup_owner_id":
                                  f"physical-live-route:{identity['deployment_id']}",
                                  "node_ids": []})
        assert router.a6_command_authority(request_id)["terminal_cas_count"] == 0
        assert service.terminal_event_count(request_id) == 1
    finally:
        service.close()


def test_armed_cleanup_hold_holds_the_successor_until_released(tmp_path):
    held = threading.Event()
    release_seen = []
    route = UnprovenSuccessorCleanupRoute(hold_observed=lambda: release_seen.append(True))
    coordinator, router, service, qualification = _runtime(tmp_path, route)
    real_authorize = router._authorize_recovery_cleanup

    def observed(pending, deadline):
        held.set()
        return real_authorize(pending, deadline)

    router._authorize_recovery_cleanup = observed
    armed = threading.Event()
    real_infer = route.infer

    def arm_then_infer(*args, command_identity, **kwargs):
        if command_identity["request_attempt"] == 1 and not armed.is_set():
            assert router.a6_arm_completion_cleanup_hold("live-a6-001") is True
            assert router.a6_arm_completion_cleanup_hold("live-a6-001") is False
            armed.set()
        return real_infer(*args, command_identity=command_identity, **kwargs)

    route.infer = arm_then_infer
    try:
        request_id = service.submit(InferenceSubmission(
            prompt="live recovery prompt", max_new_tokens=4,
            qualification=qualification_binding(qualification)))
        assert held.wait(timeout=2)
        deadline = time.monotonic() + 2
        while (router.a6_request_view(request_id).get("recovery_cleanup_held") is not True
               and time.monotonic() < deadline):
            time.sleep(0.005)
        view = router.a6_request_view(request_id)
        assert view["recovery_cleanup_held"] is True
        assert view["recovery_output_finished"] is True
        # Held: the successor cannot continue to its cleanup proof.
        time.sleep(0.2)
        assert release_seen == []
        assert router.a6_release_completion_cleanup_hold(request_id) is True
        deadline = time.monotonic() + 2
        while router.request_status(request_id) != "TERMINAL_BLOCKED" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert release_seen == [True]
        assert router.a6_request_view(request_id)["recovery_cleanup_held"] is False
        assert coordinator.route_identity(request_id)["request_attempt"] == 1
    finally:
        router.a6_release_completion_cleanup_hold("live-a6-001")
        service.close()


def test_unarmed_cleanup_hold_never_waits(tmp_path):
    # The production default: no hold is armed, the authorization proceeds.
    route = UnprovenSuccessorCleanupRoute()
    _coordinator, router, service, qualification = _runtime(tmp_path, route)
    try:
        started = time.monotonic()
        request_id, _events = _submit_and_drain(service, qualification)
        assert time.monotonic() - started < 2.0
        assert router.a6_request_view(request_id)["recovery_cleanup_held"] is False
        assert router.a6_release_completion_cleanup_hold(request_id) is False
        # A finished or unknown request can never be armed.
        assert router.a6_arm_completion_cleanup_hold(request_id) is False
        assert router.a6_arm_completion_cleanup_hold("unknown-request") is False
    finally:
        service.close()


def test_cleanup_hold_is_reachable_only_from_the_a6_fault_seam():
    # The hold is armed only by the fault seam; no product path arms it. Product paths are the
    # files git tracks or would track: ignored build output (a package build copies sources into
    # build/) is not one.
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
        capture_output=True, check=True).stdout.decode().split("\0")
    arming = sorted(
        name for name in listed
        if name and (root / name).is_file()
        and not any(part in {"tests", "runs", ".venv", "node_modules", ".git", "__pycache__"}
                    for part in Path(name).parts)
        and "a6_arm_completion_cleanup_hold" in (root / name).read_text(errors="ignore"))
    assert arming == ["mycelium_a6_product_fault_seams.py", "mycelium_live/router_port.py"]


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
