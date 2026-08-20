"""Traffic-aware, generation-fenced liveness state transitions.

The detector consumes membership identities and transport observations.  It does not
change membership, routes, deployment selection, or request attempts, and it contains
no replay or recovery behavior.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


ACTIVE_FAILURE_DETECTION_MS = 2_000
IDLE_KEEPALIVE_MS = 5_000
SUSPECT_MISSES = 2
QUARANTINE_MISSES = 3
QUARANTINE_STALE_MS = 15_000
RECOVERY_FRESH_OBSERVATIONS = 2
MAXIMUM_SUBJECTS = 4_096
MAXIMUM_INCIDENTS = 256

_MAX_TEXT_BYTES = 256
_MAX_TRACKS_PER_INCIDENT = 256
_MAX_MONOTONIC_MS = (1 << 63) - 1


class SubjectKind(str, Enum):
    EDGE = "edge"
    PLACEMENT = "placement"
    PEER = "peer"
    DEPLOYMENT = "deployment"


class LivenessState(str, Enum):
    FRESH = "fresh"
    SUSPECT = "suspect"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    RECOVERED = "recovered"


class ObservationSource(str, Enum):
    APPLICATION_RECEIPT = "application_receipt"
    ACTIVATION_RECEIPT = "activation_receipt"
    SIGNED_KEEPALIVE = "signed_keepalive"


class IncidentSource(str, Enum):
    IDLE_KEEPALIVE = "idle_keepalive"
    COMMAND_DEADLINE = "command_deadline"
    ACTIVE_TRANSPORT_FAILURE = "active_transport_failure"
    MEMBERSHIP_EXIT = "membership_exit"
    WORKER_EXCEPTION = "worker_exception"
    DEPLOYMENT_FATAL = "deployment_fatal"


_FATAL_ALLOWLIST = frozenset(
    {
        "immutable_authority_contradiction",
        "deployment_resource_ledger_corruption",
        "active_authority_compromise",
        "all_qualified_tracks_lost",
    }
)


def _bounded_text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_MONOTONIC_MS:
        raise ValueError(f"{name} must be an integer in [{minimum}, {_MAX_MONOTONIC_MS}]")
    return value


def _bounded_tracks(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or len(values) > _MAX_TRACKS_PER_INCIDENT:
        raise ValueError("affected_track_ids must be a bounded sequence")
    return tuple(sorted({_bounded_text(value, "track_id") for value in values}))


@dataclass(frozen=True, slots=True)
class LivenessSubject:
    """Membership-owned identity consumed by the detector."""

    subject_id: str
    kind: SubjectKind
    membership_generation: int

    def __post_init__(self) -> None:
        _bounded_text(self.subject_id, "subject_id")
        if not isinstance(self.kind, SubjectKind):
            raise ValueError("kind must be a SubjectKind")
        _integer(self.membership_generation, "membership_generation", minimum=1)


@dataclass(frozen=True, slots=True)
class LivenessPolicy:
    active_failure_detection_ms: int = ACTIVE_FAILURE_DETECTION_MS
    idle_keepalive_ms: int = IDLE_KEEPALIVE_MS
    suspect_misses: int = SUSPECT_MISSES
    quarantine_misses: int = QUARANTINE_MISSES
    quarantine_stale_ms: int = QUARANTINE_STALE_MS
    recovery_fresh_observations: int = RECOVERY_FRESH_OBSERVATIONS
    maximum_subjects: int = MAXIMUM_SUBJECTS
    maximum_incidents: int = MAXIMUM_INCIDENTS

    def __post_init__(self) -> None:
        _integer(self.active_failure_detection_ms, "active_failure_detection_ms", minimum=1)
        _integer(self.idle_keepalive_ms, "idle_keepalive_ms", minimum=1)
        _integer(self.suspect_misses, "suspect_misses", minimum=1)
        _integer(self.quarantine_misses, "quarantine_misses", minimum=1)
        _integer(self.quarantine_stale_ms, "quarantine_stale_ms", minimum=1)
        _integer(
            self.recovery_fresh_observations,
            "recovery_fresh_observations",
            minimum=1,
        )
        _integer(self.maximum_subjects, "maximum_subjects", minimum=1)
        _integer(self.maximum_incidents, "maximum_incidents", minimum=1)
        if self.suspect_misses > self.quarantine_misses:
            raise ValueError("suspect threshold cannot exceed quarantine threshold")
        if self.maximum_subjects > MAXIMUM_SUBJECTS:
            raise ValueError("maximum_subjects exceeds the frozen bound")
        if self.maximum_incidents > MAXIMUM_INCIDENTS:
            raise ValueError("maximum_incidents exceeds the frozen bound")
        if self.active_failure_detection_ms > ACTIVE_FAILURE_DETECTION_MS:
            raise ValueError("active failure detection cannot exceed 2000 ms")


@dataclass(frozen=True, slots=True)
class SubjectSnapshot:
    identity: LivenessSubject
    state: LivenessState
    last_fresh_ms: int
    last_observed_ms: int
    next_keepalive_due_ms: int
    consecutive_misses: int
    consecutive_signed_fresh: int
    last_source: ObservationSource | IncidentSource


@dataclass(frozen=True, slots=True)
class LivenessIncident:
    sequence: int
    source: IncidentSource
    scope: str
    subject: LivenessSubject
    observed_at_ms: int
    affected_track_ids: tuple[str, ...]
    action: str
    outcome: str
    detection_latency_ms: int | None = None
    within_detection_budget: bool | None = None


@dataclass(frozen=True, slots=True)
class TransitionResult:
    accepted: bool
    reason: str
    snapshot: SubjectSnapshot | None
    incident: LivenessIncident | None = None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class KeepaliveDecision:
    accepted: bool
    due: bool
    reason: str
    next_keepalive_due_ms: int | None
    snapshot: SubjectSnapshot | None


@dataclass(frozen=True, slots=True)
class FatalDecision:
    accepted: bool
    reason: str
    incident: LivenessIncident | None


@dataclass(slots=True)
class _SubjectState:
    identity: LivenessSubject
    state: LivenessState
    last_fresh_ms: int
    last_observed_ms: int
    next_keepalive_due_ms: int
    consecutive_misses: int
    consecutive_signed_fresh: int
    last_source: ObservationSource | IncidentSource
    last_event: tuple[int, str]
    lock: threading.RLock
    current: bool = True


class TrafficAwareLivenessDetector:
    """Bounded owner of subject freshness and narrow incident history."""

    def __init__(self, *, policy: LivenessPolicy | None = None) -> None:
        self.policy = policy or LivenessPolicy()
        self._metadata_lock = threading.RLock()
        self._subjects: dict[tuple[SubjectKind, str], _SubjectState] = {}
        self._incidents: list[LivenessIncident] = []
        self._incident_sequence = 0
        self._deployment_fatal_reason: str | None = None

    def register_subject(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
        source: ObservationSource = ObservationSource.SIGNED_KEEPALIVE,
    ) -> TransitionResult:
        """Install membership's current generation with one valid fresh observation."""

        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        if not isinstance(source, ObservationSource):
            raise ValueError("source must be an ObservationSource")
        key = (subject.kind, subject.subject_id)
        with self._metadata_lock:
            current = self._subjects.get(key)
            if current is not None:
                with current.lock:
                    if subject.membership_generation < current.identity.membership_generation:
                        return TransitionResult(
                            False,
                            "stale_generation",
                            self._snapshot(current),
                        )
                    if subject.membership_generation == current.identity.membership_generation:
                        return TransitionResult(
                            True,
                            "duplicate",
                            self._snapshot(current),
                            duplicate=True,
                        )
                    if observed_at_ms < current.last_observed_ms:
                        return TransitionResult(
                            False,
                            "stale_observation",
                            self._snapshot(current),
                        )
                    current.current = False
            elif len(self._subjects) >= self.policy.maximum_subjects:
                return TransitionResult(False, "subject_limit", None)

            state = _SubjectState(
                identity=subject,
                state=LivenessState.FRESH,
                last_fresh_ms=observed_at_ms,
                last_observed_ms=observed_at_ms,
                next_keepalive_due_ms=observed_at_ms + self.policy.idle_keepalive_ms,
                consecutive_misses=0,
                consecutive_signed_fresh=1,
                last_source=source,
                last_event=(observed_at_ms, source.value),
                lock=threading.RLock(),
            )
            self._subjects[key] = state
        return TransitionResult(True, "registered", self._snapshot(state))

    def observe_receipt(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
        source: ObservationSource,
        signed: bool,
    ) -> TransitionResult:
        """Refresh only the exact directed subject and membership generation."""

        if source not in {
            ObservationSource.APPLICATION_RECEIPT,
            ObservationSource.ACTIVATION_RECEIPT,
        }:
            raise ValueError("receipt source must be application or activation delivery")
        return self._observe_fresh(
            subject,
            observed_at_ms=observed_at_ms,
            source=source,
            signed=signed,
        )

    def observe_keepalive(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
        signed: bool,
    ) -> TransitionResult:
        return self._observe_fresh(
            subject,
            observed_at_ms=observed_at_ms,
            source=ObservationSource.SIGNED_KEEPALIVE,
            signed=signed,
        )

    def keepalive_due(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
    ) -> KeepaliveDecision:
        """Report whether traffic freshness has expired without mutating the subject."""

        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        state, rejection = self._locate(subject)
        if rejection is not None:
            return KeepaliveDecision(False, False, rejection.reason, None, rejection.snapshot)
        assert state is not None
        with state.lock:
            if not state.current:
                return KeepaliveDecision(False, False, "stale_generation", None, None)
            due = observed_at_ms >= state.next_keepalive_due_ms
            return KeepaliveDecision(
                True,
                due,
                "idle_probe_due" if due else "traffic_fresh",
                state.next_keepalive_due_ms,
                self._snapshot(state),
            )

    def record_keepalive_miss(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
    ) -> TransitionResult:
        """Apply one explicit idle probe miss; elapsed time never invents misses."""

        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        state, rejection = self._locate(subject)
        if rejection is not None:
            return rejection
        assert state is not None
        incident_parameters: tuple[IncidentSource, str, str, str] | None = None
        with state.lock:
            if not state.current:
                return TransitionResult(False, "stale_generation", None)
            duplicate = self._duplicate_or_stale(
                state,
                observed_at_ms,
                IncidentSource.IDLE_KEEPALIVE.value,
            )
            if duplicate is not None:
                return duplicate
            if observed_at_ms < state.next_keepalive_due_ms:
                return TransitionResult(False, "keepalive_not_due", self._snapshot(state))
            prior_state = state.state
            state.last_observed_ms = observed_at_ms
            state.next_keepalive_due_ms = observed_at_ms + self.policy.idle_keepalive_ms
            state.consecutive_misses += 1
            state.consecutive_signed_fresh = 0
            state.last_source = IncidentSource.IDLE_KEEPALIVE
            state.last_event = (observed_at_ms, IncidentSource.IDLE_KEEPALIVE.value)
            stale_ms = observed_at_ms - state.last_fresh_ms
            if (
                state.consecutive_misses >= self.policy.quarantine_misses
                and stale_ms >= self.policy.quarantine_stale_ms
            ):
                state.state = LivenessState.QUARANTINED
                if prior_state is not LivenessState.QUARANTINED:
                    incident_parameters = (
                        IncidentSource.IDLE_KEEPALIVE,
                        subject.kind.value,
                        "remove_from_affected_admission",
                        LivenessState.QUARANTINED.value,
                    )
            else:
                state.state = LivenessState.SUSPECT
                if prior_state is not LivenessState.SUSPECT:
                    incident_parameters = (
                        IncidentSource.IDLE_KEEPALIVE,
                        subject.kind.value,
                        "observe_only",
                        LivenessState.SUSPECT.value,
                    )
            snapshot = self._snapshot(state)
        incident = None
        if incident_parameters is not None:
            source, scope, action, outcome = incident_parameters
            incident = self._incident(
                source=source,
                scope=scope,
                subject=subject,
                observed_at_ms=observed_at_ms,
                affected_track_ids=(),
                action=action,
                outcome=outcome,
            )
        return TransitionResult(True, snapshot.state.value, snapshot, incident)

    def record_command_deadline(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
        affected_track_ids: Sequence[str] = (),
    ) -> TransitionResult:
        """A command deadline is suspect evidence, never an active disconnect."""

        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        tracks = _bounded_tracks(affected_track_ids)
        state, rejection = self._locate(subject)
        if rejection is not None:
            return rejection
        assert state is not None
        with state.lock:
            if not state.current:
                return TransitionResult(False, "stale_generation", None)
            duplicate = self._duplicate_or_stale(
                state,
                observed_at_ms,
                IncidentSource.COMMAND_DEADLINE.value,
            )
            if duplicate is not None:
                return duplicate
            state.state = LivenessState.SUSPECT
            state.last_observed_ms = observed_at_ms
            state.consecutive_misses += 1
            state.consecutive_signed_fresh = 0
            state.last_source = IncidentSource.COMMAND_DEADLINE
            state.last_event = (observed_at_ms, IncidentSource.COMMAND_DEADLINE.value)
            snapshot = self._snapshot(state)
        incident = self._incident(
            source=IncidentSource.COMMAND_DEADLINE,
            scope=subject.kind.value,
            subject=subject,
            observed_at_ms=observed_at_ms,
            affected_track_ids=tracks,
            action="observe_only",
            outcome=LivenessState.SUSPECT.value,
        )
        return TransitionResult(True, "suspect", snapshot, incident)

    def record_active_failure(
        self,
        subject: LivenessSubject,
        *,
        failure_started_at_ms: int,
        observed_at_ms: int,
        scope: str,
        affected_track_ids: Sequence[str],
        verified: bool,
    ) -> TransitionResult:
        """Record verified active failure separately from idle liveness staleness."""

        if scope not in {"request", "edge", "placement", "peer", "deployment"}:
            raise ValueError("active failure scope is invalid")
        failure_started_at_ms = _integer(
            failure_started_at_ms,
            "failure_started_at_ms",
        )
        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        if observed_at_ms < failure_started_at_ms:
            raise ValueError("active failure observation precedes the failure")
        tracks = _bounded_tracks(affected_track_ids)
        state, rejection = self._locate(subject)
        if rejection is not None:
            return rejection
        assert state is not None
        with state.lock:
            if not state.current:
                return TransitionResult(False, "stale_generation", None)
            if not verified:
                return TransitionResult(
                    False,
                    "active_failure_unverified",
                    self._snapshot(state),
                )
            duplicate = self._duplicate_or_stale(
                state,
                observed_at_ms,
                IncidentSource.ACTIVE_TRANSPORT_FAILURE.value,
            )
            if duplicate is not None:
                return duplicate
            state.state = LivenessState.FAILED
            state.last_observed_ms = observed_at_ms
            state.consecutive_signed_fresh = 0
            state.last_source = IncidentSource.ACTIVE_TRANSPORT_FAILURE
            state.last_event = (
                observed_at_ms,
                IncidentSource.ACTIVE_TRANSPORT_FAILURE.value,
            )
            snapshot = self._snapshot(state)
        latency = observed_at_ms - failure_started_at_ms
        incident = self._incident(
            source=IncidentSource.ACTIVE_TRANSPORT_FAILURE,
            scope=scope,
            subject=subject,
            observed_at_ms=observed_at_ms,
            affected_track_ids=tracks,
            action="interrupt_affected_request",
            outcome=LivenessState.FAILED.value,
            detection_latency_ms=latency,
            within_detection_budget=latency <= self.policy.active_failure_detection_ms,
        )
        return TransitionResult(True, "active_failure", snapshot, incident)

    def record_nonparticipating_peer_exit(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
    ) -> TransitionResult:
        """Change only peer evidence; no current path is listed or mutated."""

        if subject.kind is not SubjectKind.PEER:
            raise ValueError("nonparticipating exit requires a peer subject")
        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        state, rejection = self._locate(subject)
        if rejection is not None:
            return rejection
        assert state is not None
        with state.lock:
            if not state.current:
                return TransitionResult(False, "stale_generation", None)
            duplicate = self._duplicate_or_stale(
                state,
                observed_at_ms,
                IncidentSource.MEMBERSHIP_EXIT.value,
            )
            if duplicate is not None:
                return duplicate
            state.state = LivenessState.FAILED
            state.last_observed_ms = observed_at_ms
            state.consecutive_signed_fresh = 0
            state.last_source = IncidentSource.MEMBERSHIP_EXIT
            state.last_event = (observed_at_ms, IncidentSource.MEMBERSHIP_EXIT.value)
            snapshot = self._snapshot(state)
        incident = self._incident(
            source=IncidentSource.MEMBERSHIP_EXIT,
            scope="peer",
            subject=subject,
            observed_at_ms=observed_at_ms,
            affected_track_ids=(),
            action="membership_evidence_only",
            outcome=LivenessState.FAILED.value,
        )
        return TransitionResult(True, "nonparticipating_peer_failed", snapshot, incident)

    def record_worker_exception(
        self,
        subject: LivenessSubject,
        *,
        request_id: str,
        observed_at_ms: int,
    ) -> LivenessIncident:
        """Open a request incident without promoting an unknown exception to fatal."""

        request_id = _bounded_text(request_id, "request_id")
        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        state, rejection = self._locate(subject)
        if rejection is not None:
            raise ValueError(rejection.reason)
        assert state is not None
        with state.lock:
            if not state.current:
                raise ValueError("stale_generation")
        return self._incident(
            source=IncidentSource.WORKER_EXCEPTION,
            scope="request",
            subject=subject,
            observed_at_ms=observed_at_ms,
            affected_track_ids=(),
            action=f"fail_owned_request:{request_id}",
            outcome="request_failed",
        )

    def request_deployment_fatal(
        self,
        subject: LivenessSubject,
        *,
        reason: str,
        observed_at_ms: int,
        verified: bool,
        current_qualified_track_ids: Sequence[str] = (),
        lost_track_ids: Sequence[str] = (),
    ) -> FatalDecision:
        """Latch only one of the four reviewed deployment-wide contradictions."""

        reason = _bounded_text(reason, "reason")
        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        current_tracks = _bounded_tracks(current_qualified_track_ids)
        lost_tracks = _bounded_tracks(lost_track_ids)
        state, rejection = self._locate(subject)
        if rejection is not None:
            return FatalDecision(False, rejection.reason, None)
        assert state is not None
        with state.lock:
            if not state.current:
                return FatalDecision(False, "stale_generation", None)
        if subject.kind is not SubjectKind.DEPLOYMENT:
            return FatalDecision(False, "deployment_subject_required", None)
        if reason not in _FATAL_ALLOWLIST:
            return FatalDecision(False, "fatal_reason_not_allowlisted", None)
        if not verified:
            return FatalDecision(False, "fatal_evidence_unverified", None)
        if reason == "all_qualified_tracks_lost" and (
            not current_tracks or set(current_tracks) != set(lost_tracks)
        ):
            return FatalDecision(False, "surviving_qualified_track", None)
        with self._metadata_lock:
            if self._deployment_fatal_reason is not None:
                if self._deployment_fatal_reason == reason:
                    return FatalDecision(True, "duplicate", None)
                return FatalDecision(False, "deployment_already_fatal", None)
            self._deployment_fatal_reason = reason
        incident = self._incident(
            source=IncidentSource.DEPLOYMENT_FATAL,
            scope="deployment",
            subject=subject,
            observed_at_ms=observed_at_ms,
            affected_track_ids=current_tracks,
            action="stop_future_admission",
            outcome=reason,
        )
        return FatalDecision(True, "deployment_fatal", incident)

    @property
    def deployment_fatal_reason(self) -> str | None:
        with self._metadata_lock:
            return self._deployment_fatal_reason

    def subject_snapshot(self, subject: LivenessSubject) -> SubjectSnapshot | None:
        state, rejection = self._locate(subject)
        if rejection is not None or state is None:
            return None
        with state.lock:
            if not state.current:
                return None
            return self._snapshot(state)

    def snapshots(self) -> tuple[SubjectSnapshot, ...]:
        with self._metadata_lock:
            states = tuple(self._subjects.values())
        detached: list[SubjectSnapshot] = []
        for state in states:
            with state.lock:
                if state.current:
                    detached.append(self._snapshot(state))
        return tuple(
            sorted(
                detached,
                key=lambda item: (
                    item.identity.kind.value,
                    item.identity.subject_id,
                ),
            )
        )

    def incidents(self) -> tuple[LivenessIncident, ...]:
        with self._metadata_lock:
            return tuple(self._incidents)

    def _observe_fresh(
        self,
        subject: LivenessSubject,
        *,
        observed_at_ms: int,
        source: ObservationSource,
        signed: bool,
    ) -> TransitionResult:
        observed_at_ms = _integer(observed_at_ms, "observed_at_ms")
        if type(signed) is not bool:
            raise ValueError("signed must be a boolean")
        if source is ObservationSource.SIGNED_KEEPALIVE and not signed:
            return TransitionResult(False, "keepalive_signature_required", None)
        state, rejection = self._locate(subject)
        if rejection is not None:
            return rejection
        assert state is not None
        with state.lock:
            if not state.current:
                return TransitionResult(False, "stale_generation", None)
            duplicate = self._duplicate_or_stale(state, observed_at_ms, source.value)
            if duplicate is not None:
                return duplicate
            prior = state.state
            state.last_fresh_ms = observed_at_ms
            state.last_observed_ms = observed_at_ms
            state.next_keepalive_due_ms = observed_at_ms + self.policy.idle_keepalive_ms
            state.consecutive_misses = 0
            state.last_source = source
            state.last_event = (observed_at_ms, source.value)
            if signed:
                state.consecutive_signed_fresh += 1
            else:
                state.consecutive_signed_fresh = 0
            if prior in {
                LivenessState.SUSPECT,
                LivenessState.QUARANTINED,
                LivenessState.FAILED,
            }:
                if (
                    signed
                    and state.consecutive_signed_fresh
                    >= self.policy.recovery_fresh_observations
                ):
                    state.state = LivenessState.RECOVERED
                else:
                    state.state = prior
            elif prior is LivenessState.RECOVERED:
                state.state = LivenessState.RECOVERED
            else:
                state.state = LivenessState.FRESH
            return TransitionResult(True, state.state.value, self._snapshot(state))

    def _locate(
        self,
        subject: LivenessSubject,
    ) -> tuple[_SubjectState | None, TransitionResult | None]:
        with self._metadata_lock:
            state = self._subjects.get((subject.kind, subject.subject_id))
        if state is None:
            return None, TransitionResult(False, "subject_unknown", None)
        with state.lock:
            if not state.current:
                return None, TransitionResult(False, "stale_generation", None)
            current_generation = state.identity.membership_generation
            if subject.membership_generation < current_generation:
                return None, TransitionResult(
                    False,
                    "stale_generation",
                    self._snapshot(state),
                )
            if subject.membership_generation > current_generation:
                return None, TransitionResult(
                    False,
                    "future_generation",
                    self._snapshot(state),
                )
        return state, None

    def _duplicate_or_stale(
        self,
        state: _SubjectState,
        observed_at_ms: int,
        event: str,
    ) -> TransitionResult | None:
        if observed_at_ms < state.last_observed_ms:
            return TransitionResult(False, "stale_observation", self._snapshot(state))
        if observed_at_ms == state.last_observed_ms:
            if state.last_event == (observed_at_ms, event):
                return TransitionResult(
                    True,
                    "duplicate",
                    self._snapshot(state),
                    duplicate=True,
                )
            return TransitionResult(False, "observation_conflict", self._snapshot(state))
        return None

    def _incident(
        self,
        *,
        source: IncidentSource,
        scope: str,
        subject: LivenessSubject,
        observed_at_ms: int,
        affected_track_ids: Sequence[str],
        action: str,
        outcome: str,
        detection_latency_ms: int | None = None,
        within_detection_budget: bool | None = None,
    ) -> LivenessIncident:
        tracks = _bounded_tracks(affected_track_ids)
        _bounded_text(scope, "scope")
        _bounded_text(action, "action")
        _bounded_text(outcome, "outcome")
        with self._metadata_lock:
            self._incident_sequence += 1
            incident = LivenessIncident(
                sequence=self._incident_sequence,
                source=source,
                scope=scope,
                subject=subject,
                observed_at_ms=observed_at_ms,
                affected_track_ids=tracks,
                action=action,
                outcome=outcome,
                detection_latency_ms=detection_latency_ms,
                within_detection_budget=within_detection_budget,
            )
            self._incidents.append(incident)
            del self._incidents[: -self.policy.maximum_incidents]
        return incident

    @staticmethod
    def _snapshot(state: _SubjectState) -> SubjectSnapshot:
        return SubjectSnapshot(
            identity=state.identity,
            state=state.state,
            last_fresh_ms=state.last_fresh_ms,
            last_observed_ms=state.last_observed_ms,
            next_keepalive_due_ms=state.next_keepalive_due_ms,
            consecutive_misses=state.consecutive_misses,
            consecutive_signed_fresh=state.consecutive_signed_fresh,
            last_source=state.last_source,
        )


__all__ = [
    "ACTIVE_FAILURE_DETECTION_MS",
    "FatalDecision",
    "IDLE_KEEPALIVE_MS",
    "IncidentSource",
    "KeepaliveDecision",
    "LivenessIncident",
    "LivenessPolicy",
    "LivenessState",
    "LivenessSubject",
    "MAXIMUM_INCIDENTS",
    "MAXIMUM_SUBJECTS",
    "ObservationSource",
    "QUARANTINE_MISSES",
    "QUARANTINE_STALE_MS",
    "RECOVERY_FRESH_OBSERVATIONS",
    "SUSPECT_MISSES",
    "SubjectKind",
    "SubjectSnapshot",
    "TrafficAwareLivenessDetector",
    "TransitionResult",
]
