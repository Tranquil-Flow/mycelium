from __future__ import annotations

from collections import deque
import threading

import pytest

from mycelium_live.command_controller import (
    CleanupResult,
    CleanupStatus,
    CommandController,
    CommandEnvelope,
    CommandIdentity,
    CommandKind,
    TerminalResult,
    TerminalStatus,
)
from mycelium_live.liveness import (
    IncidentSource,
    LivenessPolicy,
    LivenessState,
    LivenessSubject,
    ObservationSource,
    SubjectKind,
    TrafficAwareLivenessDetector,
)
from mycelium_live.route import AffectedPeerQuarantined, PhysicalLiveRoute


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _subject(
    subject_id: str = "node-a->node-b",
    *,
    kind: SubjectKind = SubjectKind.EDGE,
    generation: int = 4,
) -> LivenessSubject:
    return LivenessSubject(
        subject_id=subject_id,
        kind=kind,
        membership_generation=generation,
    )


def _register(
    detector: TrafficAwareLivenessDetector,
    subject: LivenessSubject,
    *,
    observed_at_ms: int = 1_000,
) -> None:
    result = detector.register_subject(subject, observed_at_ms=observed_at_ms)
    assert result.accepted is True


def _command() -> CommandEnvelope:
    return CommandEnvelope(
        identity=CommandIdentity(
            deployment_id="deployment-a",
            deployment_epoch=1,
            qualification_digest=DIGEST_B,
            request_id="request-a",
            request_attempt=1,
            path_id="path-a",
            path_attempt=0,
            path_digest=DIGEST_A,
            topology_generation=1,
            command_id="decode-a",
            publisher_generation=1,
            absolute_deadline_ms=20_000,
        ),
        stage_id="stage-a",
        placement_id="placement-a",
        assignment_id="assignment-a",
        kind=CommandKind.DECODE,
        issued_at_ms=1_000,
        idempotency_digest=DIGEST_C,
        cleanup_owner_id="placement-owner-a",
        maximum_request_bytes=4_096,
        maximum_response_bytes=4_096,
    )


def test_traffic_receipt_suppresses_otherwise_due_keepalive() -> None:
    detector = TrafficAwareLivenessDetector()
    edge = _subject()
    _register(detector, edge)
    before_receipt = detector.keepalive_due(edge, observed_at_ms=5_999)
    receipt = detector.observe_receipt(
        edge,
        observed_at_ms=5_900,
        source=ObservationSource.APPLICATION_RECEIPT,
        signed=True,
    )
    after_receipt = detector.keepalive_due(edge, observed_at_ms=6_000)

    assert before_receipt.accepted is True and before_receipt.due is False
    assert receipt.accepted is True
    assert after_receipt.accepted is True and after_receipt.due is False
    assert after_receipt.reason == "traffic_fresh"
    assert after_receipt.next_keepalive_due_ms == 10_900


def test_one_missed_receipt_is_suspect_only() -> None:
    detector = TrafficAwareLivenessDetector()
    edge = _subject()
    _register(detector, edge)

    missed = detector.record_keepalive_miss(edge, observed_at_ms=6_000)

    assert missed.accepted is True
    assert missed.snapshot is not None
    assert missed.snapshot.state is LivenessState.SUSPECT
    assert missed.snapshot.consecutive_misses == 1
    assert detector.deployment_fatal_reason is None
    assert detector.incidents()[-1].source is IncidentSource.IDLE_KEEPALIVE
    assert detector.incidents()[-1].scope == "edge"


def test_idle_subject_quarantines_only_after_frozen_threshold() -> None:
    detector = TrafficAwareLivenessDetector()
    edge = _subject()
    _register(detector, edge)

    first = detector.record_keepalive_miss(edge, observed_at_ms=6_000)
    second = detector.record_keepalive_miss(edge, observed_at_ms=11_000)
    too_early = detector.record_keepalive_miss(edge, observed_at_ms=15_999)
    third = detector.record_keepalive_miss(edge, observed_at_ms=16_000)

    assert first.snapshot is not None and first.snapshot.state is LivenessState.SUSPECT
    assert second.snapshot is not None and second.snapshot.state is LivenessState.SUSPECT
    assert too_early.accepted is False and too_early.reason == "keepalive_not_due"
    assert third.snapshot is not None
    assert third.snapshot.state is LivenessState.QUARANTINED
    assert third.snapshot.consecutive_misses == 3
    assert third.incident is not None
    assert third.incident.source is IncidentSource.IDLE_KEEPALIVE


def test_stale_incarnation_receipt_cannot_refresh_subject() -> None:
    detector = TrafficAwareLivenessDetector()
    current = _subject(generation=5)
    stale = _subject(generation=4)
    unrelated = _subject("node-c->node-d", generation=2)
    _register(detector, current)
    _register(detector, unrelated, observed_at_ms=2_000)
    detector.record_keepalive_miss(current, observed_at_ms=6_000)
    before = detector.subject_snapshot(current)
    unrelated_before = detector.subject_snapshot(unrelated)

    rejected = detector.observe_receipt(
        stale,
        observed_at_ms=6_100,
        source=ObservationSource.ACTIVATION_RECEIPT,
        signed=True,
    )

    assert rejected.accepted is False and rejected.reason == "stale_generation"
    assert detector.subject_snapshot(current) == before
    assert detector.subject_snapshot(unrelated) == unrelated_before


def test_new_incarnation_starts_fresh_without_inheriting_old_state() -> None:
    detector = TrafficAwareLivenessDetector()
    old = _subject(generation=4)
    new = _subject(generation=5)
    _register(detector, old)
    detector.record_keepalive_miss(old, observed_at_ms=6_000)
    detector.record_keepalive_miss(old, observed_at_ms=11_000)

    replaced = detector.register_subject(new, observed_at_ms=12_000)

    assert replaced.accepted is True
    assert replaced.snapshot is not None
    assert replaced.snapshot.identity == new
    assert replaced.snapshot.state is LivenessState.FRESH
    assert replaced.snapshot.consecutive_misses == 0
    assert detector.subject_snapshot(old) is None


def test_recovery_requires_two_current_generation_signed_observations() -> None:
    detector = TrafficAwareLivenessDetector()
    edge = _subject()
    _register(detector, edge)
    detector.record_keepalive_miss(edge, observed_at_ms=6_000)

    unsigned = detector.observe_receipt(
        edge,
        observed_at_ms=6_100,
        source=ObservationSource.APPLICATION_RECEIPT,
        signed=False,
    )
    first = detector.observe_keepalive(edge, observed_at_ms=6_200, signed=True)
    second = detector.observe_keepalive(edge, observed_at_ms=6_300, signed=True)

    assert unsigned.snapshot is not None
    assert unsigned.snapshot.state is LivenessState.SUSPECT
    assert first.snapshot is not None and first.snapshot.state is LivenessState.SUSPECT
    assert second.snapshot is not None
    assert second.snapshot.state is LivenessState.RECOVERED


def test_command_deadline_is_suspect_not_active_failure() -> None:
    detector = TrafficAwareLivenessDetector()
    edge = _subject()
    _register(detector, edge)

    result = detector.record_command_deadline(
        edge,
        observed_at_ms=2_000,
        affected_track_ids=("track-a",),
    )

    assert result.snapshot is not None
    assert result.snapshot.state is LivenessState.SUSPECT
    assert result.incident is not None
    assert result.incident.source is IncidentSource.COMMAND_DEADLINE
    assert result.incident.source is not IncidentSource.ACTIVE_TRANSPORT_FAILURE


def test_active_disconnect_interrupts_owned_command_within_budget() -> None:
    """Compose the two leaves with modeled time; no physical process is touched."""

    detector = TrafficAwareLivenessDetector()
    edge = _subject()
    _register(detector, edge)
    controller = CommandController()
    command = _command()
    controller.register(command)

    failure = detector.record_active_failure(
        edge,
        failure_started_at_ms=2_000,
        observed_at_ms=2_400,
        scope="edge",
        affected_track_ids=("track-a",),
        verified=True,
    )
    cancelled = controller.cancel(
        command.identity,
        new_cancellation_generation=1,
        observed_at_ms=2_400,
        idempotency_digest=DIGEST_C,
    )
    assert cancelled.snapshot is not None
    current = cancelled.snapshot.identity
    cleanup = controller.record_cleanup(
        current,
        owner_id="placement-owner-a",
        result=CleanupResult(
            status=CleanupStatus.COMPLETED,
            released_resource_count=2,
            result_digest=DIGEST_A,
        ),
        observed_at_ms=4_300,
    )
    terminal = controller.terminal_compare_and_swap(
        TerminalResult(
            identity=current,
            status=TerminalStatus.CANCELLED,
            observed_at_ms=4_300,
            result_digest=DIGEST_B,
        ),
        expected_terminal_revision=0,
    )

    assert failure.snapshot is not None
    assert failure.snapshot.state is LivenessState.FAILED
    assert failure.incident is not None
    assert failure.incident.source is IncidentSource.ACTIVE_TRANSPORT_FAILURE
    assert failure.incident.detection_latency_ms == 400
    assert failure.incident.within_detection_budget is True
    assert terminal.accepted is True
    assert cleanup.snapshot is not None
    assert cleanup.snapshot.cleanup_within_interruption_budget is True


def test_unverified_or_late_active_failure_is_truthful() -> None:
    detector = TrafficAwareLivenessDetector()
    edge = _subject()
    _register(detector, edge)
    unverified = detector.record_active_failure(
        edge,
        failure_started_at_ms=2_000,
        observed_at_ms=2_100,
        scope="edge",
        affected_track_ids=(),
        verified=False,
    )
    late = detector.record_active_failure(
        edge,
        failure_started_at_ms=2_000,
        observed_at_ms=4_001,
        scope="edge",
        affected_track_ids=(),
        verified=True,
    )

    assert unverified.accepted is False
    assert detector.subject_snapshot(edge) == late.snapshot
    assert late.incident is not None
    assert late.incident.within_detection_budget is False


def test_nonparticipating_peer_exit_does_not_mutate_active_route() -> None:
    detector = TrafficAwareLivenessDetector()
    peer = _subject("peer-c", kind=SubjectKind.PEER)
    active_edge = _subject("node-a->node-b")
    _register(detector, peer)
    _register(detector, active_edge)
    edge_before = detector.subject_snapshot(active_edge)

    exited = detector.record_nonparticipating_peer_exit(
        peer,
        observed_at_ms=2_000,
    )

    assert exited.snapshot is not None
    assert exited.snapshot.state is LivenessState.FAILED
    assert exited.incident is not None
    assert exited.incident.affected_track_ids == ()
    assert exited.incident.action == "membership_evidence_only"
    assert detector.subject_snapshot(active_edge) == edge_before
    assert detector.deployment_fatal_reason is None


def test_unknown_worker_exception_cannot_latch_deployment_fatal() -> None:
    detector = TrafficAwareLivenessDetector()
    deployment = _subject("deployment-a", kind=SubjectKind.DEPLOYMENT)
    _register(detector, deployment)
    request_incident = detector.record_worker_exception(
        deployment,
        request_id="request-a",
        observed_at_ms=2_000,
    )
    rejected = detector.request_deployment_fatal(
        deployment,
        reason="unexpected_worker_exception",
        observed_at_ms=2_001,
        verified=True,
    )

    assert request_incident.scope == "request"
    assert request_incident.source is IncidentSource.WORKER_EXCEPTION
    assert rejected.accepted is False
    assert rejected.reason == "fatal_reason_not_allowlisted"
    assert detector.deployment_fatal_reason is None


def test_physical_route_ingests_exact_scoped_iroh_failure_without_global_fatal() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._liveness = TrafficAwareLivenessDetector()
    route._liveness_edge_subjects = {}
    route._last_transport_event_sequence = {}
    route._scoped_runtime_incidents = deque(maxlen=64)
    route._scoped_runtime_incident_sequence = 0
    route._active_route_requests = {
        "request-a": {
            "deployment_id": "deployment-a",
            "deployment_epoch": 4,
            "qualification_digest": DIGEST_A,
            "request_attempt": 3,
            "path_id": "path-a",
            "path_attempt": 2,
            "path_digest": DIGEST_B,
            "topology_generation": 8,
            "command_id": "command-a",
            "cancellation_generation": 1,
            "publisher_generation": 2,
        }
    }
    observation = {
        "details": {
            "transport": {
                "scoped_events": [
                    {
                        "sequence": 1,
                        "event": "failure",
                        "request_id": "request-a",
                        "path_id": "path-a",
                        "path_attempt": 2,
                        "peer_node_id": "node-b",
                        "peer_generation": 7,
                        "code": "delivery_not_confirmed",
                    }
                ]
            }
        }
    }

    route._ingest_transport_scoped_events("node-a", observation)
    route._ingest_transport_scoped_events("node-a", observation)

    edge = LivenessSubject("node-a->node-b", SubjectKind.EDGE, 7)
    snapshot = route._liveness.subject_snapshot(edge)
    assert snapshot is not None and snapshot.state is LivenessState.FAILED
    incidents = route._liveness.incidents()
    assert len(incidents) == 1
    assert incidents[0].scope == "edge"
    assert incidents[0].affected_track_ids == ("request-a",)
    assert route._liveness.deployment_fatal_reason is None
    scoped = route.a4_scoped_runtime_incidents()
    assert len(scoped) == 1
    assert scoped[0]["protocol"] == "mycelium.scoped_runtime_incident.v1"
    assert scoped[0]["request_attempt"] == 3
    assert scoped[0]["path_id"] == "path-a"
    assert scoped[0]["path_attempt"] == 2
    assert scoped[0]["path_digest"] == DIGEST_B
    assert scoped[0]["command_id"] == "command-a"
    assert scoped[0]["cancellation_generation"] == 1
    assert scoped[0]["publisher_generation"] == 2
    assert scoped[0]["fatal_requested"] is False
    assert scoped[0]["fatal_accepted"] is False


def test_all_qualified_tracks_must_be_lost_before_fatal_latch() -> None:
    detector = TrafficAwareLivenessDetector()
    deployment = _subject("deployment-a", kind=SubjectKind.DEPLOYMENT)
    _register(detector, deployment)
    surviving = detector.request_deployment_fatal(
        deployment,
        reason="all_qualified_tracks_lost",
        observed_at_ms=2_000,
        verified=True,
        current_qualified_track_ids=("track-a", "track-b"),
        lost_track_ids=("track-a",),
    )
    accepted = detector.request_deployment_fatal(
        deployment,
        reason="all_qualified_tracks_lost",
        observed_at_ms=2_100,
        verified=True,
        current_qualified_track_ids=("track-a", "track-b"),
        lost_track_ids=("track-b", "track-a"),
    )

    assert surviving.accepted is False
    assert surviving.reason == "surviving_qualified_track"
    assert accepted.accepted is True
    assert detector.deployment_fatal_reason == "all_qualified_tracks_lost"


def test_subject_and_incident_collections_are_bounded() -> None:
    detector = TrafficAwareLivenessDetector(
        policy=LivenessPolicy(maximum_subjects=1, maximum_incidents=1)
    )
    edge = _subject()
    _register(detector, edge)
    rejected = detector.register_subject(
        _subject("node-c->node-d"),
        observed_at_ms=1_000,
    )
    detector.record_command_deadline(edge, observed_at_ms=2_000)
    detector.record_worker_exception(
        edge,
        request_id="request-a",
        observed_at_ms=2_100,
    )

    assert rejected.accepted is False and rejected.reason == "subject_limit"
    assert len(detector.snapshots()) == 1
    assert len(detector.incidents()) == 1
    assert detector.incidents()[0].source is IncidentSource.WORKER_EXCEPTION


# --- Live-route idle-keepalive monitor wiring (PhysicalLiveRoute) -----------


class _FakeProbeSession:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def send_before(
        self,
        *,
        command_id: str,
        command: str,
        payload: dict,
        deadline_monotonic_s: float,
    ) -> dict:
        self.calls += 1
        if self.fail:
            raise TimeoutError("command_deadline_exceeded")
        return {"protocol": "mycelium.node_observation.v1", "details": {}}


def _monitor_route(
    detector: TrafficAwareLivenessDetector,
    subject: LivenessSubject,
    *,
    clock: dict,
    session: _FakeProbeSession,
    refresh_on_probe: bool,
) -> PhysicalLiveRoute:
    route = PhysicalLiveRoute.__new__(PhysicalLiveRoute)
    route._liveness = detector
    route._liveness_subjects = {"node-2": subject}
    route._open = True
    route._closed = False
    route._lock = threading.RLock()
    route._sessions = {"node-2": session}
    route._active_route_requests = {}
    route._last_health = {}
    route._command_id = lambda node_id, operation: f"{node_id}:{operation}"
    route._monotonic_ms = lambda: clock["t"]

    def _fake_verify_observation(node_id, response, *, event, require_known_key=True):
        if refresh_on_probe:
            detector.observe_receipt(
                subject,
                observed_at_ms=clock["t"],
                source=ObservationSource.APPLICATION_RECEIPT,
                signed=True,
            )
        return {"verified": True, "details": {"transport": {}}}

    route._verify_observation = _fake_verify_observation
    return route


def test_idle_keepalive_probe_keeps_fresh_peer_fresh() -> None:
    detector = TrafficAwareLivenessDetector()
    subject = _subject("node-2", kind=SubjectKind.PEER)
    _register(detector, subject, observed_at_ms=1_000)
    clock = {"t": 6_000}
    session = _FakeProbeSession()
    route = _monitor_route(
        detector,
        subject,
        clock=clock,
        session=session,
        refresh_on_probe=True,
    )

    route._monitor_idle_keepalives_once()

    assert session.calls == 1
    snapshot = detector.subject_snapshot(subject)
    assert snapshot is not None
    assert snapshot.state is LivenessState.FRESH
    assert snapshot.consecutive_misses == 0
    assert snapshot.next_keepalive_due_ms == 11_000
    # Not due yet: no probe is sent, and no miss is recorded.
    clock["t"] = 10_999
    route._monitor_idle_keepalives_once()
    assert session.calls == 1
    after = detector.subject_snapshot(subject)
    assert after is not None and after.consecutive_misses == 0


def test_idle_keepalive_probe_miss_suspects_then_quarantines() -> None:
    detector = TrafficAwareLivenessDetector()
    subject = _subject("node-2", kind=SubjectKind.PEER)
    _register(detector, subject, observed_at_ms=1_000)
    clock = {"t": 6_000}
    route = _monitor_route(
        detector,
        subject,
        clock=clock,
        session=_FakeProbeSession(fail=True),
        refresh_on_probe=False,
    )

    route._monitor_idle_keepalives_once()
    first = detector.subject_snapshot(subject)
    assert first is not None
    assert first.state is LivenessState.SUSPECT
    assert first.consecutive_misses == 1

    clock["t"] = 11_000
    route._monitor_idle_keepalives_once()
    second = detector.subject_snapshot(subject)
    assert second is not None
    assert second.state is LivenessState.SUSPECT
    assert second.consecutive_misses == 2

    clock["t"] = 16_000
    route._monitor_idle_keepalives_once()
    third = detector.subject_snapshot(subject)
    assert third is not None
    assert third.state is LivenessState.QUARANTINED
    assert third.consecutive_misses == 3

    incidents = detector.incidents()
    assert any(
        incident.source is IncidentSource.IDLE_KEEPALIVE
        and incident.action == "remove_from_affected_admission"
        for incident in incidents
    )


@pytest.mark.parametrize("cancellation_requested", (False, True))
def test_idle_keepalive_does_not_probe_peer_owned_by_nonterminal_request(
    cancellation_requested: bool,
) -> None:
    detector = TrafficAwareLivenessDetector()
    subject = _subject("node-2", kind=SubjectKind.PEER)
    _register(detector, subject, observed_at_ms=1_000)
    session = _FakeProbeSession(fail=True)
    route = _monitor_route(
        detector,
        subject,
        clock={"t": 16_000},
        session=session,
        refresh_on_probe=False,
    )
    route._active_route_requests = {
        "request-active": {
            "participating_node_ids": frozenset({"node-2"}),
            "cancellation_requested": cancellation_requested,
            "terminal": False,
        }
    }

    route._monitor_idle_keepalives_once()

    assert session.calls == 0
    snapshot = detector.subject_snapshot(subject)
    assert snapshot is not None
    assert snapshot.state is LivenessState.FRESH
    assert snapshot.consecutive_misses == 0
    assert detector.incidents() == ()


def test_quarantined_peer_admission_fails_closed() -> None:
    detector = TrafficAwareLivenessDetector()
    subject = _subject("node-2", kind=SubjectKind.PEER)
    _register(detector, subject, observed_at_ms=1_000)
    clock = {"t": 6_000}
    route = _monitor_route(
        detector,
        subject,
        clock=clock,
        session=_FakeProbeSession(fail=True),
        refresh_on_probe=False,
    )
    for t in (6_000, 11_000, 16_000):
        clock["t"] = t
        route._monitor_idle_keepalives_once()
    quarantined_snapshot = detector.subject_snapshot(subject)
    assert quarantined_snapshot is not None
    assert quarantined_snapshot.state is LivenessState.QUARANTINED

    with pytest.raises(AffectedPeerQuarantined, match="affected_peer_quarantined"):
        route._reject_if_affected_peer_quarantined(frozenset({"node-2"}))
    # Unaffected participation is untouched.
    route._reject_if_affected_peer_quarantined(frozenset({"node-0"}))
    # Unknown nodes have no subject and admit normally.
    assert route._peer_subject_is_quarantined("node-3-r2") is False


def test_fresh_peer_admission_is_not_rejected() -> None:
    detector = TrafficAwareLivenessDetector()
    subject = _subject("node-2", kind=SubjectKind.PEER)
    _register(detector, subject, observed_at_ms=1_000)
    route = _monitor_route(
        detector,
        subject,
        clock={"t": 6_000},
        session=_FakeProbeSession(),
        refresh_on_probe=True,
    )

    route._reject_if_affected_peer_quarantined(frozenset({"node-0", "node-2"}))
