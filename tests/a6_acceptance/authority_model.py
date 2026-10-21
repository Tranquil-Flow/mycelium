# SPDX-License-Identifier: AGPL-3.0-only
"""Design-only deterministic reference model for the A6 authority boundary.

This module is an A6 acceptance *test helper*.  It is not A6 runtime behaviour,
it is not importable by production code, and satisfying it cannot promote A6 or
substitute for a physical or browser gate.

It exists to make one property executable before A4 closes: A6 is an authority
*consumer*.  Only :class:`OwnerAuthority` mints request attempts, canonical path
identity, command identity, cancellation generations, publisher generations,
terminal revisions, and cleanup receipts.  Recovery quotes an owner-issued grant
back verbatim; anything absent, stale, forged, or self-minted fails closed with a
bounded reason and no observable effect.

The A4 surfaces modelled here are provisional.  Every seam must be reconciled
against the atomic A4 commit before A6 implementation begins; see section 12 of
the A6 specification and ``unresolved_bindings`` in ``inventory.v1.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum

__all__ = [
    "CanonicalIdentity",
    "Decision",
    "GrantKind",
    "Issuer",
    "OwnerAuthority",
    "OwnerGrant",
    "RecoveryModel",
    "RecoveryPhase",
]

SELF_MINTED_TOKEN = "self_minted"
COMMIT_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")

#: The only reasons that may latch deployment-fatal state; owned by A4.
FATAL_ALLOWLIST = frozenset(
    {
        "immutable_authority_contradiction",
        "reservation_ledger_corruption",
        "deployment_authority_conflict",
        "all_qualified_tracks_lost",
    }
)
SCOPED_SUBJECTS = frozenset({"request", "edge", "placement"})
#: Outcomes a cancelled request must never be relabelled as.
RECOVERY_FAILURE_STATUSES = frozenset(
    {"recovery_failed", "successor_failed", "deadline_exceeded", "aborted"}
)


class Issuer(str, Enum):
    REQUEST_GATEWAY = "request_gateway"
    ROUTER = "router"
    M16_LEDGER = "m16_admission_ledger"
    COMMAND_CONTROLLER = "a4_command_controller"
    LIVENESS = "a4_liveness_authority"


class GrantKind(str, Enum):
    FENCE = "fence"
    CUTOVER = "cutover"
    CLEANUP = "cleanup"
    TERMINAL = "terminal"
    PUBLISHER_RESET = "publisher_reset"


class RecoveryPhase(str, Enum):
    ACTIVE = "active"
    FAILURE_OBSERVED = "failure_observed"
    CHECKPOINT_FROZEN = "checkpoint_frozen"
    SUCCESSOR_PENDING = "successor_pending"
    RECOVERY_PREFILL = "recovery_prefill"
    CUTOVER_PENDING = "cutover_pending"
    RECOVERED_DECODE = "recovered_decode"
    CLEANUP = "cleanup"
    TERMINAL = "terminal"
    ABORTED = "aborted"


RESUMABLE_PHASES = frozenset(
    {
        RecoveryPhase.ACTIVE,
        RecoveryPhase.CHECKPOINT_FROZEN,
        RecoveryPhase.RECOVERY_PREFILL,
        RecoveryPhase.RECOVERED_DECODE,
    }
)


@dataclass(frozen=True)
class CanonicalIdentity:
    """The complete identity every recovery mutation and receipt must bind."""

    deployment_id: str
    deployment_epoch: int
    qualification_digest: str
    request_id: str
    request_attempt: int
    path_id: str
    path_digest: str
    path_attempt: int
    topology_generation: int
    command_id: str
    cancellation_generation: int
    publisher_generation: int
    recovery_transition_id: str
    recovery_revision: int

    def __post_init__(self) -> None:
        texts = (
            self.deployment_id,
            self.qualification_digest,
            self.request_id,
            self.path_id,
            self.path_digest,
            self.command_id,
            self.recovery_transition_id,
        )
        counters = (
            (self.deployment_epoch, 1),
            (self.request_attempt, 1),
            (self.path_attempt, 0),
            (self.topology_generation, 1),
            (self.cancellation_generation, 0),
            (self.publisher_generation, 1),
            (self.recovery_revision, 0),
        )
        if not all(isinstance(value, str) and value for value in texts) or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= minimum
            for value, minimum in counters
        ):
            raise ValueError("incomplete_canonical_identity")

    def replace(self, **overrides: object) -> CanonicalIdentity:
        return replace(self, **overrides)  # type: ignore[arg-type]


@dataclass(frozen=True)
class OwnerGrant:
    """An owner decision recovery may quote back; recovery never builds one."""

    issuer: Issuer
    kind: GrantKind
    identity: CanonicalIdentity
    expected_revision: int
    a4_atomic_commit: str | None
    token: str = SELF_MINTED_TOKEN


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str
    effects: frozenset[str] = frozenset()


@dataclass
class _RequestState:
    identity: CanonicalIdentity
    absolute_deadline_ms: int
    phase: RecoveryPhase = RecoveryPhase.ACTIVE
    revision: int = 0
    cleanup_revision: int = 0
    watermark: int = 0
    ledger: list[int] = field(default_factory=list)
    fenced: set[int] = field(default_factory=set)
    cleanup_subjects: set[str] = field(default_factory=set)
    cutover_grant: OwnerGrant | None = None
    terminal: str | None = None
    cancel_observed_at_ms: int | None = None
    recovery_policy_deadline_ms: int | None = None
    successor_model_work: int = 0
    replayed: tuple[int, ...] = ()
    observed_failures: list[str] = field(default_factory=list)
    reconciled: int = 0
    reconciliation_outcome: str | None = None
    orphan_checkpoint: bool = False
    browser_cursor: int | None = None


class OwnerAuthority:
    """The A4 owners.  The single source of every authority A6 consumes."""

    deployment_id = "deployment-a4"
    deployment_epoch = 1
    qualification_digest = "sha256:qualification"

    def __init__(self, *, a4_atomic_commit: str | None = None) -> None:
        self._a4_atomic_commit: str | None = None
        if a4_atomic_commit is not None:
            self.resolve_a4_atomic_commit(a4_atomic_commit)
        self.topology_generation = 1
        self.selected_deployment_changed = False
        self._paths: dict[str, tuple[str, int]] = {}
        self._tokens: dict[str, OwnerGrant] = {}
        self._current: dict[tuple[str, GrantKind], str] = {}
        self._issued_attempts: dict[str, set[int]] = {}
        self._revisions: dict[str, int] = {}
        self._sequence = 0
        self._model: RecoveryModel | None = None

    # -- binding sentinel ---------------------------------------------------

    @property
    def a4_atomic_commit(self) -> str | None:
        return self._a4_atomic_commit

    def resolve_a4_atomic_commit(self, digest: str) -> str:
        """Resolve the unresolved A4 binding.  Owner action, after A4 closes."""

        if not isinstance(digest, str) or not COMMIT_PATTERN.match(digest):
            raise ValueError("invalid_a4_atomic_commit")
        self._a4_atomic_commit = digest
        return digest

    # -- registries ---------------------------------------------------------

    def attach(self, model: RecoveryModel) -> None:
        self._model = model

    def register_canonical_path(
        self, path_id: str, *, digest: str, attempt: int
    ) -> None:
        self._paths[path_id] = (digest, attempt)

    def canonical_path(self, path_id: str) -> tuple[str, int] | None:
        return self._paths.get(path_id)

    def issued_attempts(self, request_id: str) -> frozenset[int]:
        return frozenset(self._issued_attempts.get(request_id, set()))

    def advance_topology_generation(self) -> int:
        self.topology_generation += 1
        return self.topology_generation

    def bump_revision(self, request_id: str) -> int:
        self._revisions[request_id] = self._revisions.get(request_id, 0) + 1
        return self._revisions[request_id]

    # -- identity and grant issuance ---------------------------------------

    def admission_identity(self, request_id: str, *, path_id: str) -> CanonicalIdentity:
        digest, attempt = self._require_path(path_id)
        self._issued_attempts.setdefault(request_id, set()).add(1)
        return CanonicalIdentity(
            deployment_id=self.deployment_id,
            deployment_epoch=self.deployment_epoch,
            qualification_digest=self.qualification_digest,
            request_id=request_id,
            request_attempt=1,
            path_id=path_id,
            path_digest=digest,
            path_attempt=attempt,
            topology_generation=self.topology_generation,
            command_id=f"cmd-{request_id}-1",
            cancellation_generation=0,
            publisher_generation=1,
            recovery_transition_id=f"rt-{request_id}-0",
            recovery_revision=0,
        )

    def issue_fence(self, request_id: str, *, expected_revision: int) -> OwnerGrant:
        return self._issue(
            Issuer.ROUTER,
            GrantKind.FENCE,
            self._current_identity(request_id),
            expected_revision,
        )

    def issue_cutover(
        self,
        request_id: str,
        *,
        successor_path_id: str,
        expected_revision: int,
    ) -> OwnerGrant:
        current = self._current_identity(request_id)
        digest, attempt = self._require_path(successor_path_id)
        successor_attempt = current.request_attempt + 1
        self._issued_attempts.setdefault(request_id, set()).add(successor_attempt)
        identity = current.replace(
            request_attempt=successor_attempt,
            path_id=successor_path_id,
            path_digest=digest,
            path_attempt=attempt,
            topology_generation=self.topology_generation,
            qualification_digest=self.qualification_digest,
            command_id=f"cmd-{request_id}-{successor_attempt}",
            recovery_transition_id=f"rt-{request_id}-{successor_attempt}",
            recovery_revision=current.recovery_revision + 1,
        )
        return self._issue(
            Issuer.ROUTER, GrantKind.CUTOVER, identity, expected_revision
        )

    def issue_cleanup(
        self,
        request_id: str,
        *,
        identity: CanonicalIdentity,
        expected_revision: int,
    ) -> OwnerGrant:
        return self._issue(
            Issuer.M16_LEDGER, GrantKind.CLEANUP, identity, expected_revision
        )

    def issue_terminal(
        self,
        request_id: str,
        *,
        identity: CanonicalIdentity,
        expected_revision: int,
    ) -> OwnerGrant:
        return self._issue(
            Issuer.COMMAND_CONTROLLER, GrantKind.TERMINAL, identity, expected_revision
        )

    def issue_publisher_reset(
        self,
        request_id: str,
        *,
        identity: CanonicalIdentity,
        expected_revision: int,
    ) -> OwnerGrant:
        return self._issue(
            Issuer.REQUEST_GATEWAY,
            GrantKind.PUBLISHER_RESET,
            identity,
            expected_revision,
        )

    def reissue(self, grant: OwnerGrant, *, identity: CanonicalIdentity) -> OwnerGrant:
        """Re-mint an existing decision over a different identity."""

        return self._issue(grant.issuer, grant.kind, identity, grant.expected_revision)

    # -- validation ---------------------------------------------------------

    def is_issued(self, grant: OwnerGrant) -> bool:
        recorded = self._tokens.get(grant.token)
        return recorded is not None and recorded == grant

    def is_current(self, grant: OwnerGrant) -> bool:
        key = (grant.identity.request_id, grant.kind)
        return self._current.get(key) == grant.token

    # -- internals ----------------------------------------------------------

    def _issue(
        self,
        issuer: Issuer,
        kind: GrantKind,
        identity: CanonicalIdentity,
        expected_revision: int,
    ) -> OwnerGrant:
        self._sequence += 1
        token = f"owner-grant-{self._sequence}"
        grant = OwnerGrant(
            issuer=issuer,
            kind=kind,
            identity=identity,
            expected_revision=expected_revision,
            a4_atomic_commit=self._a4_atomic_commit,
            token=token,
        )
        self._tokens[token] = grant
        self._current[(identity.request_id, kind)] = token
        return grant

    def _require_path(self, path_id: str) -> tuple[str, int]:
        path = self._paths.get(path_id)
        if path is None:
            raise ValueError("unknown_canonical_path")
        return path

    def _current_identity(self, request_id: str) -> CanonicalIdentity:
        if self._model is None:
            raise ValueError("owner_not_attached_to_a_model")
        return self._model.current_identity(request_id)


class RecoveryModel:
    """Deterministic fail-closed model of the surface A6 is allowed to touch."""

    qualification_claim = False
    promotion_authorized = False

    def __init__(self, owner: OwnerAuthority) -> None:
        self._owner = owner
        owner.attach(self)
        self._requests: dict[str, _RequestState] = {}
        self._live_resources: dict[str, int] = {}
        self.shared_node_running = True
        self.response_reader_running = True
        self.deployment_fatal = False
        self._failed_waiters: list[str] = []
        self._untrusted_consulted: list[str] = []

    # -- construction and inspection ---------------------------------------

    @property
    def owner(self) -> OwnerAuthority:
        return self._owner

    def admit(
        self,
        request_id: str,
        *,
        identity: CanonicalIdentity,
        absolute_deadline_ms: int,
    ) -> None:
        self._requests[request_id] = _RequestState(
            identity=identity, absolute_deadline_ms=absolute_deadline_ms
        )
        self._live_resources[self.subject(identity)] = 1

    def subject(self, identity: CanonicalIdentity) -> str:
        return f"{identity.request_id}:{identity.request_attempt}:{identity.path_id}"

    def current_identity(self, request_id: str) -> CanonicalIdentity:
        return self._state(request_id).identity

    def phase(self, request_id: str) -> RecoveryPhase:
        return self._state(request_id).phase

    def revision(self, request_id: str) -> int:
        return self._state(request_id).revision

    def cleanup_revision(self, request_id: str) -> int:
        return self._state(request_id).cleanup_revision

    def committed_watermark(self, request_id: str) -> int:
        return self._state(request_id).watermark

    def delivery_ledger(self, request_id: str) -> tuple[int, ...]:
        return tuple(self._state(request_id).ledger)

    def replayed_positions(self, request_id: str) -> tuple[int, ...]:
        return self._state(request_id).replayed

    def terminal(self, request_id: str) -> str | None:
        return self._state(request_id).terminal

    def successor_model_work(self, request_id: str) -> int:
        return self._state(request_id).successor_model_work

    def observed_failures(self, request_id: str) -> tuple[str, ...]:
        return tuple(self._state(request_id).observed_failures)

    def reconciliation_count(self, request_id: str) -> int:
        return self._state(request_id).reconciled

    def live_resources(self, subject: str) -> int:
        return self._live_resources.get(subject, 0)

    def node_global_live_resources(self) -> int:
        return sum(self._live_resources.values())

    def failed_waiters(self) -> tuple[str, ...]:
        return tuple(self._failed_waiters)

    def untrusted_inputs_consulted(self) -> tuple[str, ...]:
        return tuple(self._untrusted_consulted)

    # -- deterministic test scaffolding ------------------------------------

    def enter_phase(self, request_id: str, phase: RecoveryPhase) -> None:
        self._state(request_id).phase = phase

    def force_revision(self, request_id: str, revision: int) -> None:
        self._state(request_id).revision = revision

    def write_orphan_checkpoint(self, request_id: str) -> None:
        self._state(request_id).orphan_checkpoint = True

    def set_browser_authored_cursor(self, request_id: str, *, position: int) -> None:
        self._state(request_id).browser_cursor = position

    # -- gateway-owned delivery ledger -------------------------------------

    def commit_position(self, request_id: str, *, position: int) -> Decision:
        state = self._state(request_id)
        rejection = self._position_rejection(state, position)
        if rejection is not None:
            return rejection
        state.ledger.append(position)
        state.watermark = position
        return Decision(True, "committed", frozenset({"ledger_append_and_watermark"}))

    def publish_position(
        self,
        request_id: str,
        *,
        position: int,
        identity: CanonicalIdentity | None = None,
    ) -> Decision:
        state = self._state(request_id)
        identity = state.identity if identity is None else identity
        if identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if identity.publisher_generation != state.identity.publisher_generation:
            return Decision(False, "stale_publisher_generation")
        if identity.request_attempt != state.identity.request_attempt:
            return Decision(False, "stale_attempt")
        if state.terminal is not None:
            return Decision(False, "late_position")
        rejection = self._position_rejection(state, position)
        if rejection is not None:
            return rejection
        state.ledger.append(position)
        state.watermark = position
        return Decision(True, "published", frozenset({"ledger_append_and_watermark"}))

    def reconnect_replay(self, request_id: str, *, from_position: int) -> Decision:
        state = self._state(request_id)
        state.replayed = tuple(
            position for position in state.ledger if position > from_position
        )
        return Decision(True, "replayed", frozenset({"no_watermark_advance"}))

    def advance_publisher_generation(self, request_id: str) -> Decision:
        state = self._state(request_id)
        grant = self._owner.issue_publisher_reset(
            request_id, identity=state.identity, expected_revision=state.revision
        )
        state.identity = grant.identity.replace(
            publisher_generation=grant.identity.publisher_generation + 1
        )
        return Decision(True, "publisher_generation_advanced")

    # -- fencing and cutover ------------------------------------------------

    def fence_source_attempt(self, request_id: str, *, grant: OwnerGrant) -> Decision:
        state = self._state(request_id)
        rejection = self._grant_rejection(grant, GrantKind.FENCE)
        if rejection is not None:
            return rejection
        if grant.expected_revision != state.revision:
            return Decision(False, "expected_revision_mismatch")
        state.fenced.add(state.identity.request_attempt)
        state.revision += 1
        return Decision(True, "source_attempt_fenced", frozenset({"fenced"}))

    def cutover(self, request_id: str, *, grant: OwnerGrant) -> Decision:
        state = self._state(request_id)
        if not self._owner.is_issued(grant):
            return Decision(False, "self_minted_authority")
        if state.cutover_grant == grant:
            return Decision(True, "duplicate_cutover")
        rejection = self._grant_rejection(grant, GrantKind.CUTOVER)
        if rejection is not None:
            return rejection
        if state.cancel_observed_at_ms is not None:
            return Decision(False, "cancellation_precedes_recovery")
        if grant.expected_revision != state.revision:
            if state.cutover_grant is not None:
                return Decision(False, "conflicting_cutover")
            return Decision(False, "expected_revision_mismatch")
        identity = grant.identity
        if self._owner.canonical_path(identity.path_id) != (
            identity.path_digest,
            identity.path_attempt,
        ):
            return Decision(False, "canonical_path_mismatch")
        if identity.request_attempt not in self._owner.issued_attempts(request_id):
            return Decision(False, "self_minted_authority")
        if (
            identity.topology_generation != self._owner.topology_generation
            or identity.qualification_digest != self._owner.qualification_digest
        ):
            return Decision(False, "stale_a4_generation")
        if identity.publisher_generation != state.identity.publisher_generation:
            return Decision(False, "stale_publisher_generation")
        if state.identity.request_attempt not in state.fenced:
            return Decision(False, "fence_required_before_cutover")
        state.identity = identity
        state.revision += 1
        state.cutover_grant = grant
        state.phase = RecoveryPhase.RECOVERED_DECODE
        state.successor_model_work += 1
        self._live_resources[self.subject(identity)] = 1
        return Decision(True, "cutover", frozenset({"authority_advanced"}))

    # -- cancellation and deadlines ----------------------------------------

    def observe_cancellation(self, request_id: str, *, at_ms: int) -> Decision:
        state = self._state(request_id)
        state.cancel_observed_at_ms = at_ms
        return Decision(True, "cancellation_observed")

    def effective_cancellation_deadline_ms(self, request_id: str) -> int:
        return self._state(request_id).absolute_deadline_ms

    def set_recovery_policy_deadline(
        self, request_id: str, deadline_ms: int
    ) -> Decision:
        state = self._state(request_id)
        if deadline_ms > state.absolute_deadline_ms:
            if state.cancel_observed_at_ms is not None:
                return Decision(False, "cancellation_budget_restart")
            return Decision(False, "recovery_policy_exceeds_absolute_deadline")
        state.recovery_policy_deadline_ms = deadline_ms
        return Decision(True, "recovery_policy_set")

    # -- cleanup and terminal ordering -------------------------------------

    def record_cleanup(
        self,
        request_id: str,
        *,
        grant: OwnerGrant,
        subject: str,
        stale_source: bool = False,
    ) -> Decision:
        state = self._state(request_id)
        rejection = self._grant_rejection(grant, GrantKind.CLEANUP)
        if rejection is not None:
            return rejection
        if stale_source or grant.identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if grant.expected_revision != state.cleanup_revision:
            return Decision(False, "cleanup_cas_mismatch")
        if subject != self.subject(grant.identity):
            return Decision(False, "cleanup_subject_mismatch")
        self._live_resources[subject] = 0
        state.cleanup_subjects.add(subject)
        state.cleanup_revision += 1
        return Decision(True, "cleanup_recorded", frozenset({"exact_subject_released"}))

    def publish_terminal(
        self, request_id: str, *, grant: OwnerGrant, status: str
    ) -> Decision:
        state = self._state(request_id)
        rejection = self._grant_rejection(grant, GrantKind.TERMINAL)
        if rejection is not None:
            return rejection
        if grant.identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if state.terminal is not None:
            if state.terminal == status:
                return Decision(True, "duplicate_terminal")
            return Decision(False, "already_terminal")
        if state.cancel_observed_at_ms is not None:
            if status in RECOVERY_FAILURE_STATUSES:
                return Decision(False, "cancellation_mislabelled_as_recovery_failure")
            if status not in {"cancelled", "cleanup_failed"}:
                return Decision(False, "cancelled_request_cannot_complete")
        if (
            status != "cleanup_failed"
            and self.subject(grant.identity) not in state.cleanup_subjects
        ):
            return Decision(False, "cleanup_required_before_terminal")
        state.terminal = status
        state.phase = RecoveryPhase.TERMINAL
        return Decision(True, "terminal_published", frozenset({"one_terminal_result"}))

    def terminate_shared_node(self, request_id: str) -> Decision:
        del request_id
        return Decision(False, "shared_node_termination_is_not_cleanup")

    def late_result(self, request_id: str, *, identity: CanonicalIdentity) -> Decision:
        del request_id, identity
        return Decision(False, "late_result_discarded")

    # -- liveness -----------------------------------------------------------

    def observe_liveness(
        self,
        request_id: str,
        *,
        identity: CanonicalIdentity,
        subject_scope: str,
        authored_by: str,
        fatal_reason: str | None = None,
    ) -> Decision:
        state = self._state(request_id)
        if authored_by != Issuer.LIVENESS.value:
            return Decision(False, "a6_cannot_author_failure")
        if identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if subject_scope == "deployment":
            if fatal_reason not in FATAL_ALLOWLIST:
                return Decision(False, "deployment_fatal_requires_a4_allowlist")
            self.deployment_fatal = True
            state.observed_failures.append(subject_scope)
            return Decision(True, "deployment_fatal_allowlisted")
        if subject_scope not in SCOPED_SUBJECTS:
            return Decision(False, "unknown_liveness_scope")
        state.observed_failures.append(subject_scope)
        return Decision(True, "failure_observed", frozenset({"scoped"}))

    def fail_successor(self, request_id: str, *, reason: str) -> Decision:
        state = self._state(request_id)
        state.phase = RecoveryPhase.CLEANUP
        state.observed_failures.append(reason)
        return Decision(True, "successor_failed", frozenset({"request_scoped"}))

    # -- restart reconciliation --------------------------------------------

    def restart(self) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for request_id, state in self._requests.items():
            if state.reconciliation_outcome is not None:
                outcomes[request_id] = state.reconciliation_outcome
                continue
            if state.terminal is not None:
                outcome = "already_terminal"
            elif state.phase in RESUMABLE_PHASES:
                outcome = "resumed"
            else:
                outcome = "aborted"
            state.reconciliation_outcome = outcome
            state.reconciled += 1
            state.identity = state.identity.replace(
                publisher_generation=state.identity.publisher_generation + 1
            )
            outcomes[request_id] = outcome
        return outcomes

    # -- claim boundary -----------------------------------------------------

    def authorize_integration(self) -> Decision:
        if self._owner.a4_atomic_commit is None:
            return Decision(False, "unresolved_a4_atomic_commit_binding")
        return Decision(True, "integration_authorization_available")

    def simulate_physical_gate(self, scenario_id: str) -> Decision:
        del scenario_id
        return Decision(False, "physical_gate_requires_executed_product_path")

    # -- internals ----------------------------------------------------------

    def _state(self, request_id: str) -> _RequestState:
        state = self._requests.get(request_id)
        if state is None:
            raise ValueError("unknown_request")
        return state

    def _grant_rejection(self, grant: OwnerGrant, kind: GrantKind) -> Decision | None:
        if not self._owner.is_issued(grant):
            return Decision(False, "self_minted_authority")
        if not self._owner.is_current(grant):
            return Decision(False, "stale_owner_issued_transition")
        if grant.kind is not kind:
            return Decision(False, "invalid_grant_kind")
        return None

    @staticmethod
    def _position_rejection(state: _RequestState, position: int) -> Decision | None:
        if position in state.ledger:
            return Decision(False, "duplicate_position")
        if position <= state.watermark:
            return Decision(False, "regressed_position")
        if position > state.watermark + 1:
            return Decision(False, "skipped_position")
        return None
