# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic A6 authority-consumption cases.

These exercise the design-only reference model in ``authority_model``.  The model
encodes the fail-closed rules the A6 specification requires of a recovery
consumer; it is not A6 runtime behaviour and cannot satisfy any A6 gate.
"""

from __future__ import annotations

import pytest

from tests.a6_acceptance.authority_model import (
    CanonicalIdentity,
    GrantKind,
    Issuer,
    OwnerAuthority,
    OwnerGrant,
    RecoveryModel,
    RecoveryPhase,
)


SOURCE_PATH = "path-source"
SUCCESSOR_PATH = "path-successor"
UNRELATED_PATH = "path-unrelated"


def _owner(*, a4_atomic_commit: str | None = None) -> OwnerAuthority:
    owner = OwnerAuthority(a4_atomic_commit=a4_atomic_commit)
    owner.register_canonical_path(SOURCE_PATH, digest="sha256:aaa", attempt=0)
    owner.register_canonical_path(SUCCESSOR_PATH, digest="sha256:bbb", attempt=1)
    owner.register_canonical_path(UNRELATED_PATH, digest="sha256:ccc", attempt=0)
    return owner


def _model(*, a4_atomic_commit: str | None = None) -> RecoveryModel:
    owner = _owner(a4_atomic_commit=a4_atomic_commit)
    model = RecoveryModel(owner)
    model.admit(
        "req-1",
        identity=owner.admission_identity("req-1", path_id=SOURCE_PATH),
        absolute_deadline_ms=2_000,
    )
    return model


def _committed(model: RecoveryModel, positions: int = 3) -> None:
    for position in range(1, positions + 1):
        decision = model.commit_position("req-1", position=position)
        assert decision.accepted, decision.reason


def _fenced_successor_grant(model: RecoveryModel) -> OwnerGrant:
    owner = model.owner
    fence = owner.issue_fence("req-1", expected_revision=model.revision("req-1"))
    assert model.fence_source_attempt("req-1", grant=fence).accepted
    return owner.issue_cutover(
        "req-1",
        successor_path_id=SUCCESSOR_PATH,
        expected_revision=model.revision("req-1"),
    )


# --- authority consumption -------------------------------------------------


def test_a6_cannot_mint_its_own_recovery_transition() -> None:
    model = _model()
    _committed(model)
    owner = model.owner
    legitimate = owner.issue_cutover(
        "req-1",
        successor_path_id=SUCCESSOR_PATH,
        expected_revision=model.revision("req-1"),
    )
    forged = OwnerGrant(
        issuer=Issuer.ROUTER,
        kind=GrantKind.CUTOVER,
        identity=legitimate.identity,
        expected_revision=legitimate.expected_revision,
        a4_atomic_commit=legitimate.a4_atomic_commit,
    )

    decision = model.cutover("req-1", grant=forged)

    assert not decision.accepted
    assert decision.reason == "self_minted_authority"
    assert decision.effects == frozenset()
    assert model.phase("req-1") is RecoveryPhase.ACTIVE


def test_stale_owner_issued_transition_is_rejected() -> None:
    model = _model()
    _committed(model)
    owner = model.owner
    stale = owner.issue_cutover(
        "req-1",
        successor_path_id=SUCCESSOR_PATH,
        expected_revision=model.revision("req-1"),
    )
    owner.issue_cutover(
        "req-1",
        successor_path_id=SUCCESSOR_PATH,
        expected_revision=model.revision("req-1"),
    )

    decision = model.cutover("req-1", grant=stale)

    assert not decision.accepted
    assert decision.reason == "stale_owner_issued_transition"
    assert model.committed_watermark("req-1") == 3


def test_expected_revision_mismatch_is_rejected() -> None:
    model = _model()
    _committed(model)
    grant = _fenced_successor_grant(model)
    model.owner.bump_revision("req-1")
    model.force_revision("req-1", grant.expected_revision + 1)

    decision = model.cutover("req-1", grant=grant)

    assert not decision.accepted
    assert decision.reason == "expected_revision_mismatch"


def test_incomplete_canonical_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="incomplete_canonical_identity"):
        CanonicalIdentity(
            deployment_id="",
            deployment_epoch=1,
            qualification_digest="sha256:q",
            request_id="req-1",
            request_attempt=1,
            path_id=SOURCE_PATH,
            path_digest="sha256:aaa",
            path_attempt=0,
            topology_generation=1,
            command_id="cmd-1",
            cancellation_generation=0,
            publisher_generation=1,
            recovery_transition_id="rt-1",
            recovery_revision=0,
        )


def test_stale_a4_generation_is_rejected_before_successor_work() -> None:
    model = _model()
    _committed(model)
    grant = _fenced_successor_grant(model)
    model.owner.advance_topology_generation()

    decision = model.cutover("req-1", grant=grant)

    assert not decision.accepted
    assert decision.reason == "stale_a4_generation"
    assert model.successor_model_work("req-1") == 0


def test_stale_publisher_generation_cannot_publish_or_move_the_ledger() -> None:
    model = _model()
    _committed(model)
    stale_identity = model.current_identity("req-1")
    assert model.advance_publisher_generation("req-1").accepted

    decision = model.publish_position("req-1", position=4, identity=stale_identity)

    assert not decision.accepted
    assert decision.reason == "stale_publisher_generation"
    assert model.committed_watermark("req-1") == 3
    assert model.delivery_ledger("req-1") == (1, 2, 3)


def test_reconstructed_or_hardcoded_path_identity_is_rejected() -> None:
    model = _model()
    _committed(model)
    grant = _fenced_successor_grant(model)
    reconstructed = grant.identity.replace(path_digest="sha256:guessed-from-placements")
    forged_attempt = grant.identity.replace(path_attempt=0)

    for identity, reason in (
        (reconstructed, "canonical_path_mismatch"),
        (forged_attempt, "canonical_path_mismatch"),
    ):
        decision = model.cutover(
            "req-1",
            grant=model.owner.reissue(grant, identity=identity),
        )
        assert not decision.accepted
        assert decision.reason == reason


# --- delivery authority ----------------------------------------------------


def test_successor_output_before_fencing_is_rejected() -> None:
    model = _model()
    _committed(model)
    owner = model.owner
    unfenced = owner.issue_cutover(
        "req-1",
        successor_path_id=SUCCESSOR_PATH,
        expected_revision=model.revision("req-1"),
    )

    decision = model.cutover("req-1", grant=unfenced)

    assert not decision.accepted
    assert decision.reason == "fence_required_before_cutover"
    assert model.committed_watermark("req-1") == 3


def test_fenced_source_cannot_publish_anything() -> None:
    model = _model()
    _committed(model)
    source_identity = model.current_identity("req-1")
    grant = _fenced_successor_grant(model)
    assert model.cutover("req-1", grant=grant).accepted

    for call, reason in (
        (
            lambda: model.publish_position(
                "req-1", position=4, identity=source_identity
            ),
            "fenced_attempt",
        ),
        (
            lambda: model.publish_terminal(
                "req-1",
                grant=model.owner.issue_terminal(
                    "req-1",
                    identity=source_identity,
                    expected_revision=model.revision("req-1"),
                ),
                status="completed",
            ),
            "fenced_attempt",
        ),
        (
            lambda: model.record_cleanup(
                "req-1",
                grant=model.owner.issue_cleanup(
                    "req-1",
                    identity=source_identity,
                    expected_revision=model.cleanup_revision("req-1"),
                ),
                subject=model.subject(source_identity),
                stale_source=True,
            ),
            "fenced_attempt",
        ),
        (
            lambda: model.observe_liveness(
                "req-1",
                identity=source_identity,
                subject_scope="request",
                authored_by="a4_liveness_authority",
            ),
            "fenced_attempt",
        ),
    ):
        decision = call()
        assert not decision.accepted
        assert decision.reason == reason

    assert model.committed_watermark("req-1") == 3
    assert model.terminal("req-1") is None


def test_committed_watermark_rejects_duplicate_skipped_and_regressed_positions() -> (
    None
):
    model = _model()
    _committed(model)

    assert model.publish_position("req-1", position=3).reason == "duplicate_position"
    assert model.publish_position("req-1", position=5).reason == "skipped_position"
    assert model.publish_position("req-1", position=0).reason == "regressed_position"
    assert model.committed_watermark("req-1") == 3
    assert model.delivery_ledger("req-1") == (1, 2, 3)


def test_reconnect_replay_never_advances_the_committed_watermark() -> None:
    model = _model()
    _committed(model)

    replay = model.reconnect_replay("req-1", from_position=0)

    assert replay.accepted
    assert replay.effects == frozenset({"no_watermark_advance"})
    assert model.committed_watermark("req-1") == 3
    assert model.replayed_positions("req-1") == (1, 2, 3)


def test_reconnect_across_publisher_reset_resumes_after_the_watermark() -> None:
    model = _model()
    _committed(model)

    assert model.advance_publisher_generation("req-1").accepted
    assert model.reconnect_replay("req-1", from_position=0).accepted
    assert model.committed_watermark("req-1") == 3

    grant = _fenced_successor_grant(model)
    assert model.cutover("req-1", grant=grant).accepted
    assert model.publish_position("req-1", position=3).reason == "duplicate_position"
    assert model.publish_position("req-1", position=4).accepted
    assert model.committed_watermark("req-1") == 4


def test_repeated_cutover_is_idempotent_and_conflicting_cutover_fails_closed() -> None:
    model = _model()
    _committed(model)
    grant = _fenced_successor_grant(model)
    assert model.cutover("req-1", grant=grant).accepted
    authority_after_first = model.current_identity("req-1")

    repeat = model.cutover("req-1", grant=grant)
    assert repeat.accepted
    assert repeat.reason == "duplicate_cutover"

    conflicting = model.owner.issue_cutover(
        "req-1",
        successor_path_id=UNRELATED_PATH,
        expected_revision=grant.expected_revision,
    )
    decision = model.cutover("req-1", grant=conflicting)

    assert not decision.accepted
    assert decision.reason == "conflicting_cutover"
    assert model.current_identity("req-1") == authority_after_first
    assert model.committed_watermark("req-1") == 3
    assert model.delivery_ledger("req-1") == (1, 2, 3)


def test_cutover_leaves_unrelated_request_authority_untouched() -> None:
    model = _model()
    _committed(model)
    owner = model.owner
    model.admit(
        "req-2",
        identity=owner.admission_identity("req-2", path_id=UNRELATED_PATH),
        absolute_deadline_ms=2_000,
    )
    before = model.current_identity("req-2")

    assert model.cutover("req-1", grant=_fenced_successor_grant(model)).accepted

    assert model.current_identity("req-2") == before
    assert model.phase("req-2") is RecoveryPhase.ACTIVE
    assert model.owner.selected_deployment_changed is False


# --- cancellation and deadlines -------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [RecoveryPhase.SUCCESSOR_PENDING, RecoveryPhase.RECOVERY_PREFILL],
)
def test_cancellation_preempts_pending_recovery_without_a_new_budget(
    phase: RecoveryPhase,
) -> None:
    model = _model()
    _committed(model)
    model.enter_phase("req-1", phase)

    cancel = model.observe_cancellation("req-1", at_ms=500)

    assert cancel.accepted
    assert model.effective_cancellation_deadline_ms("req-1") == 2_000
    assert model.set_recovery_policy_deadline("req-1", 4_000).reason == (
        "cancellation_budget_restart"
    )
    assert model.effective_cancellation_deadline_ms("req-1") == 2_000

    grant = _fenced_successor_grant(model)
    decision = model.cutover("req-1", grant=grant)
    assert not decision.accepted
    assert decision.reason == "cancellation_precedes_recovery"


def test_cancellation_is_never_published_as_recovery_failure() -> None:
    model = _model()
    _committed(model)
    model.enter_phase("req-1", RecoveryPhase.RECOVERY_PREFILL)
    assert model.observe_cancellation("req-1", at_ms=500).accepted
    assert model.record_cleanup(
        "req-1",
        grant=model.owner.issue_cleanup(
            "req-1",
            identity=model.current_identity("req-1"),
            expected_revision=model.cleanup_revision("req-1"),
        ),
        subject=model.subject(model.current_identity("req-1")),
    ).accepted

    mislabelled = model.publish_terminal(
        "req-1",
        grant=model.owner.issue_terminal(
            "req-1",
            identity=model.current_identity("req-1"),
            expected_revision=model.revision("req-1"),
        ),
        status="recovery_failed",
    )

    assert not mislabelled.accepted
    assert mislabelled.reason == "cancellation_mislabelled_as_recovery_failure"
    assert model.terminal("req-1") is None

    honest = model.publish_terminal(
        "req-1",
        grant=model.owner.issue_terminal(
            "req-1",
            identity=model.current_identity("req-1"),
            expected_revision=model.revision("req-1"),
        ),
        status="cancelled",
    )
    assert honest.accepted
    assert model.terminal("req-1") == "cancelled"


def test_cancellation_after_cutover_keeps_the_original_absolute_deadline() -> None:
    model = _model()
    _committed(model)
    assert model.cutover("req-1", grant=_fenced_successor_grant(model)).accepted
    model.enter_phase("req-1", RecoveryPhase.RECOVERED_DECODE)

    assert model.observe_cancellation("req-1", at_ms=900).accepted
    assert model.effective_cancellation_deadline_ms("req-1") == 2_000
    assert model.set_recovery_policy_deadline("req-1", 2_500).reason == (
        "cancellation_budget_restart"
    )


# --- cleanup and terminal ordering ----------------------------------------


def test_terminal_requires_recorded_cleanup_or_an_explicit_cleanup_failure() -> None:
    model = _model()
    _committed(model)
    identity = model.current_identity("req-1")

    premature = model.publish_terminal(
        "req-1",
        grant=model.owner.issue_terminal(
            "req-1", identity=identity, expected_revision=model.revision("req-1")
        ),
        status="completed",
    )
    assert not premature.accepted
    assert premature.reason == "cleanup_required_before_terminal"
    assert model.terminal("req-1") is None

    explicit = model.publish_terminal(
        "req-1",
        grant=model.owner.issue_terminal(
            "req-1", identity=identity, expected_revision=model.revision("req-1")
        ),
        status="cleanup_failed",
    )
    assert explicit.accepted
    assert model.terminal("req-1") == "cleanup_failed"


def test_cleanup_is_exact_while_an_unrelated_request_stays_active() -> None:
    model = _model()
    _committed(model)
    owner = model.owner
    model.admit(
        "req-2",
        identity=owner.admission_identity("req-2", path_id=UNRELATED_PATH),
        absolute_deadline_ms=2_000,
    )
    identity = model.current_identity("req-1")

    decision = model.record_cleanup(
        "req-1",
        grant=owner.issue_cleanup(
            "req-1",
            identity=identity,
            expected_revision=model.cleanup_revision("req-1"),
        ),
        subject=model.subject(identity),
    )

    assert decision.accepted
    assert model.live_resources(model.subject(identity)) == 0
    assert model.live_resources(model.subject(model.current_identity("req-2"))) > 0
    assert model.node_global_live_resources() > 0
    assert model.phase("req-2") is RecoveryPhase.ACTIVE


def test_cleanup_cannot_target_another_owners_subject() -> None:
    model = _model()
    owner = model.owner
    model.admit(
        "req-2",
        identity=owner.admission_identity("req-2", path_id=UNRELATED_PATH),
        absolute_deadline_ms=2_000,
    )
    other = model.subject(model.current_identity("req-2"))

    decision = model.record_cleanup(
        "req-1",
        grant=owner.issue_cleanup(
            "req-1",
            identity=model.current_identity("req-1"),
            expected_revision=model.cleanup_revision("req-1"),
        ),
        subject=other,
    )

    assert not decision.accepted
    assert decision.reason == "cleanup_subject_mismatch"
    assert model.live_resources(other) > 0


def test_terminating_a_shared_node_is_never_cleanup_proof() -> None:
    model = _model()

    decision = model.terminate_shared_node("req-1")

    assert not decision.accepted
    assert decision.reason == "shared_node_termination_is_not_cleanup"
    assert model.shared_node_running is True


def test_late_source_and_successor_results_are_isolated() -> None:
    model = _model()
    _committed(model)
    owner = model.owner
    model.admit(
        "req-2",
        identity=owner.admission_identity("req-2", path_id=UNRELATED_PATH),
        absolute_deadline_ms=2_000,
    )
    source_identity = model.current_identity("req-1")
    assert model.cutover("req-1", grant=_fenced_successor_grant(model)).accepted
    successor_identity = model.current_identity("req-1")
    superseded_successor = successor_identity.replace(
        request_attempt=successor_identity.request_attempt - 1
    )

    for identity in (source_identity, superseded_successor):
        decision = model.late_result("req-1", identity=identity)
        assert not decision.accepted
        assert decision.reason == "late_result_discarded"
        assert decision.effects == frozenset()

    assert model.shared_node_running is True
    assert model.failed_waiters() == ()
    assert model.response_reader_running is True
    assert model.current_identity("req-1") == successor_identity
    assert model.committed_watermark("req-1") == 3
    assert model.phase("req-2") is RecoveryPhase.ACTIVE


# --- liveness --------------------------------------------------------------


def test_a6_cannot_author_the_failure_that_grants_its_authority() -> None:
    model = _model()

    decision = model.observe_liveness(
        "req-1",
        identity=model.current_identity("req-1"),
        subject_scope="request",
        authored_by="a6_recovery",
    )

    assert not decision.accepted
    assert decision.reason == "a6_cannot_author_failure"
    assert model.observed_failures("req-1") == ()


def test_scoped_liveness_stays_scoped_and_fatal_needs_the_allowlist() -> None:
    model = _model()
    owner = model.owner
    model.admit(
        "req-2",
        identity=owner.admission_identity("req-2", path_id=UNRELATED_PATH),
        absolute_deadline_ms=2_000,
    )

    for scope in ("request", "edge", "placement"):
        decision = model.observe_liveness(
            "req-1",
            identity=model.current_identity("req-1"),
            subject_scope=scope,
            authored_by="a4_liveness_authority",
        )
        assert decision.accepted
        assert model.phase("req-2") is RecoveryPhase.ACTIVE
        assert model.deployment_fatal is False

    unallowlisted = model.observe_liveness(
        "req-1",
        identity=model.current_identity("req-1"),
        subject_scope="deployment",
        authored_by="a4_liveness_authority",
        fatal_reason="unknown_recovery_exception",
    )
    assert not unallowlisted.accepted
    assert unallowlisted.reason == "deployment_fatal_requires_a4_allowlist"
    assert model.deployment_fatal is False


def test_failed_successor_does_not_poison_incumbent_or_unrelated_requests() -> None:
    model = _model()
    _committed(model)
    owner = model.owner
    model.admit(
        "req-2",
        identity=owner.admission_identity("req-2", path_id=UNRELATED_PATH),
        absolute_deadline_ms=2_000,
    )
    assert model.cutover("req-1", grant=_fenced_successor_grant(model)).accepted

    decision = model.fail_successor("req-1", reason="successor_unavailable")

    assert decision.accepted
    assert model.deployment_fatal is False
    assert model.owner.selected_deployment_changed is False
    assert model.phase("req-2") is RecoveryPhase.ACTIVE


# --- restart reconciliation ------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        RecoveryPhase.FAILURE_OBSERVED,
        RecoveryPhase.CHECKPOINT_FROZEN,
        RecoveryPhase.SUCCESSOR_PENDING,
        RecoveryPhase.RECOVERY_PREFILL,
        RecoveryPhase.CUTOVER_PENDING,
        RecoveryPhase.RECOVERED_DECODE,
        RecoveryPhase.CLEANUP,
    ],
)
def test_restart_reconciles_every_nonterminal_phase_exactly_once(
    phase: RecoveryPhase,
) -> None:
    model = _model()
    _committed(model)
    model.enter_phase("req-1", phase)
    model.write_orphan_checkpoint("req-1")
    model.set_browser_authored_cursor("req-1", position=99)

    first = model.restart()
    second = model.restart()

    assert first["req-1"] in {"resumed", "aborted", "already_terminal"}
    assert model.reconciliation_count("req-1") == 1
    assert second["req-1"] == first["req-1"]
    assert model.reconciliation_count("req-1") == 1
    assert model.committed_watermark("req-1") == 3
    assert model.untrusted_inputs_consulted() == ()


def test_a_stale_process_cannot_reacquire_publication_authority() -> None:
    model = _model()
    _committed(model)
    stale_identity = model.current_identity("req-1")
    model.restart()

    decision = model.publish_position("req-1", position=4, identity=stale_identity)

    assert not decision.accepted
    assert decision.reason == "stale_publisher_generation"
    assert model.committed_watermark("req-1") == 3


# --- fail-closed A4 binding and honesty ------------------------------------


def test_integration_authorization_fails_closed_while_a4_is_unresolved() -> None:
    model = _model()

    decision = model.authorize_integration()

    assert not decision.accepted
    assert decision.reason == "unresolved_a4_atomic_commit_binding"
    assert model.owner.a4_atomic_commit is None


@pytest.mark.parametrize(
    "placeholder",
    ["", "TBD", "dirty-worktree", "0000000", "codex/a6-readiness-redesign"],
)
def test_placeholder_a4_commit_digests_are_refused(placeholder: str) -> None:
    owner = _owner()
    with pytest.raises(ValueError, match="invalid_a4_atomic_commit"):
        owner.resolve_a4_atomic_commit(placeholder)


def test_the_model_cannot_satisfy_a_physical_or_browser_gate() -> None:
    model = _model(a4_atomic_commit="a" * 40)

    decision = model.simulate_physical_gate("ordinary_browser_replay_continuation")

    assert not decision.accepted
    assert decision.reason == "physical_gate_requires_executed_product_path"
    assert model.qualification_claim is False
    assert model.promotion_authorized is False
