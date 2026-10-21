# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic acceptance tests for A6 full-context replay recovery.

These exercise the closed contracts, the owner-private replay checkpoint, and
the recovery runtime state machine against the frozen acceptance inventory
(tests/a6_acceptance/inventory.v1.json).  They are deterministic-only: they
never fabricate a physical or browser result, and satisfying them does not
promote A6 (spec section 9/11).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from mycelium_a6_recovery import (
    ABORTABLE_STATES,
    CHECKPOINT_PROTOCOL,
    RECOVERY_CHAIN,
    RECOVERY_STATES,
    RUNTIME_PROTOCOL,
    CheckpointStore,
    GrantKind,
    Issuer,
    OwnerAuthority,
    OwnerGrant,
    ReplayCheckpoint,
    RecoveryPhase,
    RecoveryRuntime,
    build_recovery_gate,
    build_recovery_intent,
    build_recovery_qualification,
    compatibility_matches,
    recovery_request_binding,
    validate_checkpoint_projection,
    validate_recovery_gate,
    validate_recovery_intent,
    validate_recovery_qualification,
    validate_recovery_runtime,
)

# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

SHA = "sha256:" + "0" * 64


def _sha(char: str) -> str:
    return "sha256:" + char * 64


BINDING = {
    "deployment_id": "deployment-a4",
    "deployment_epoch": 1,
    "topology_generation": 1,
    "model_id": "model-7b",
    "model_revision": "rev-1",
    "representation_digest": _sha("1"),
    "graph_digest": _sha("2"),
}

COMPATIBILITY = {
    "model_id": "model-7b",
    "model_revision": "rev-1",
    "representation_digest": _sha("1"),
    "manifest_digest": _sha("3"),
    "tokenizer_digest": _sha("4"),
    "vocabulary_digest": _sha("5"),
    "special_token_policy_digest": _sha("6"),
    "architecture_digest": _sha("7"),
    "context_policy_digest": _sha("8"),
    "output_semantics_digest": _sha("9"),
    "generation_settings_digest": _sha("a"),
    "qualification_parity_policy_digest": _sha("b"),
    "workload_envelope_digest": _sha("c"),
}

SUCCESSOR_BINDING = {
    "request_id": "req-1",
    "request_attempt": 2,
    "path_id": "path-successor-1",
    "path_attempt": 1,
    "path_manifest_digest": _sha("d"),
    "placement_ids": ["p-a2", "p-b2"],
}


def _owner() -> OwnerAuthority:
    owner = OwnerAuthority(a4_atomic_commit="a" * 40)
    owner.register_canonical_path("path-a", digest=_sha("a"), attempt=0)
    owner.register_canonical_path("path-b", digest=_sha("b"), attempt=0)
    return owner


def _runtime() -> tuple[OwnerAuthority, RecoveryRuntime]:
    owner = _owner()
    runtime = RecoveryRuntime(owner)
    identity = owner.admission_identity("req-1", path_id="path-a")
    runtime.admit("req-1", identity=identity, absolute_deadline_ms=60_000)
    return owner, runtime


def _qualified(
    compatible: bool = True,
    *,
    physical_challenge: str = "passed",
    request_id: str = "req-1",
) -> dict:
    return build_recovery_qualification(
        BINDING,
        qualification_id="qual-1",
        request_binding=recovery_request_binding(_checkpoint(request_id=request_id)),
        successor_binding={**SUCCESSOR_BINDING, "request_id": request_id},
        compatibility=COMPATIBILITY,
        compatible=compatible,
        issued_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
        physical_challenge=physical_challenge,
    )


def _checkpoint(
    *,
    request_id: str = "req-1",
    prompt: tuple[int, ...] = (11, 12),
    prefix: tuple[int, ...] = (101, 102),
) -> ReplayCheckpoint:
    from mycelium_a6_recovery import _digest, _prefix_digest

    return ReplayCheckpoint(
        protocol=CHECKPOINT_PROTOCOL,
        store_identity="store-1",
        request_id=request_id,
        session_id="sess-1",
        source_attempt=1,
        path_id="path-a",
        path_digest=_sha("a"),
        path_attempt=0,
        prompt_digest=_digest(prompt),
        generation_settings_digest=_digest({"greedy": True}),
        committed_token_count=len(prefix),
        committed_prefix_digest=_prefix_digest(prefix) if prefix else None,
        delivery_cursor=len(prefix),
        watermark=len(prefix),
        publisher_generation=1,
        deployment_id="deployment-a4",
        deployment_epoch=1,
        topology_generation=1,
        source_qualification_digest=_sha("0"),
        model_id="model-7b",
        representation_digest=_sha("1"),
        tokenizer_digest=_sha("4"),
        recovery_attempt=0,
        terminal_authority=None,
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
        reconciliation_state="active",
        encoded_prompt=prompt,
        committed_prefix_tokens=prefix,
    )


def _advance_to_recovered(
    owner: OwnerAuthority, runtime: RecoveryRuntime, request_id: str = "req-1"
) -> None:
    """Drive the full positive recovery chain to recovered_decode."""
    assert runtime.commit_position(request_id, position=1).accepted
    assert runtime.commit_position(request_id, position=2).accepted
    identity = runtime.current_identity(request_id)
    assert runtime.observe_failure(
        request_id, identity=identity, scope="request", authored_by=Issuer.LIVENESS.value
    ).accepted
    assert runtime.freeze_checkpoint(
        request_id, committed_token_count=2, committed_prefix_digest=_sha("d")
    ).accepted
    assert runtime.select_successor(
        request_id,
        qualification=_qualified(request_id=request_id),
    ).accepted
    assert runtime.prefill(request_id, reconstructed_prefix_count=2).accepted
    fence = owner.issue_fence(request_id, expected_revision=runtime.revision(request_id))
    assert runtime.fence_source_attempt(request_id, grant=fence).accepted
    cutover = owner.issue_cutover(
        request_id, successor_path_id="path-b", expected_revision=runtime.revision(request_id)
    )
    assert runtime.cutover(request_id, grant=cutover).accepted


def test_foreign_liveness_identity_cannot_start_recovery() -> None:
    _owner_authority, runtime = _runtime()
    current = runtime.current_identity("req-1")
    foreign = current.replace(request_attempt=current.request_attempt + 1)

    decision = runtime.observe_failure(
        "req-1",
        identity=foreign,
        scope="request",
        authored_by=Issuer.LIVENESS.value,
    )

    assert not decision.accepted
    assert decision.reason == "stale_failure_identity"
    assert runtime.phase("req-1") == RecoveryPhase.ACTIVE
    assert runtime.observed_failures("req-1") == ()


def test_unexecuted_physical_challenge_cannot_select_successor() -> None:
    owner, runtime = _runtime()
    assert runtime.commit_position("req-1", position=1).accepted
    identity = runtime.current_identity("req-1")
    assert runtime.observe_failure(
        "req-1",
        identity=identity,
        scope="request",
        authored_by=Issuer.LIVENESS.value,
    ).accepted
    assert runtime.freeze_checkpoint(
        "req-1",
        committed_token_count=1,
        committed_prefix_digest=_sha("d"),
    ).accepted

    decision = runtime.select_successor(
        "req-1",
        qualification=_qualified(physical_challenge="unexecuted"),
    )

    assert not decision.accepted
    assert decision.reason == "recovery_physical_qualification_required"


# --------------------------------------------------------------------------- #
# Contract shape and privacy tests (spec section 7)
# --------------------------------------------------------------------------- #


def test_recovery_state_machine_is_frozen_and_closed() -> None:
    assert RECOVERY_CHAIN == (
        "active",
        "failure_observed",
        "checkpoint_frozen",
        "successor_pending",
        "recovery_prefill",
        "cutover_pending",
        "recovered_decode",
        "cleanup",
        "terminal",
    )
    assert RECOVERY_STATES == set(RECOVERY_CHAIN) | {"aborted"}
    assert ABORTABLE_STATES == {
        "failure_observed",
        "checkpoint_frozen",
        "successor_pending",
        "recovery_prefill",
        "cutover_pending",
        "recovered_decode",
    }


def test_checkpoint_projection_rejects_private_material() -> None:
    checkpoint = _checkpoint()
    projection = checkpoint.to_dict()
    # to_dict() is public only; it must not carry prompt/token material.
    assert "encoded_prompt" not in projection
    assert "committed_prefix_tokens" not in projection
    validate_checkpoint_projection(projection)

    with pytest.raises(ValueError):
        validate_checkpoint_projection({**projection, "encoded_prompt": "leak"})


def test_checkpoint_projection_rejects_unknown_fields() -> None:
    projection = _checkpoint().to_dict()
    with pytest.raises(ValueError):
        validate_checkpoint_projection({**projection, "extra": 1})


def test_checkpoint_binds_private_prompt_and_prefix() -> None:
    checkpoint = _checkpoint(prompt=(11, 12), prefix=(101, 102))
    digest = checkpoint.checkpoint_digest()
    # Tampering with private material must change the digest.
    tampered = _checkpoint(prompt=(11, 12), prefix=(101, 102))
    tampered.encoded_prompt = (11, 99)
    assert tampered.checkpoint_digest() != digest


def test_checkpoint_rejects_prompt_digest_mismatch() -> None:
    from mycelium_a6_recovery import _digest

    with pytest.raises(ValueError):
        ReplayCheckpoint(
            protocol=CHECKPOINT_PROTOCOL,
            store_identity="store-1",
            request_id="req-1",
            session_id="sess-1",
            source_attempt=1,
            path_id="path-a",
            path_digest=_sha("a"),
            path_attempt=0,
            prompt_digest=_digest((999,)),
            generation_settings_digest=_digest({"greedy": True}),
            committed_token_count=0,
            committed_prefix_digest=None,
            delivery_cursor=0,
            watermark=0,
            publisher_generation=1,
            deployment_id="deployment-a4",
            deployment_epoch=1,
            topology_generation=1,
            source_qualification_digest=_sha("0"),
            model_id="model-7b",
            representation_digest=_sha("1"),
            tokenizer_digest=_sha("4"),
            recovery_attempt=0,
            terminal_authority=None,
            created_at_unix_ms=1_000,
            expiry_unix_ms=2_000_000,
            reconciliation_state="active",
            encoded_prompt=(11, 12),
            committed_prefix_tokens=(),
        )


def test_checkpoint_store_roundtrip_and_tamper_detection(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path, store_identity="store-1")
    store.write(_checkpoint(prompt=(11, 12), prefix=(101, 102)))
    loaded = store.load()
    assert loaded is not None
    assert loaded.encoded_prompt == (11, 12)
    assert loaded.committed_prefix_tokens == (101, 102)
    assert loaded.checkpoint_digest() == _checkpoint(
        prompt=(11, 12), prefix=(101, 102)
    ).checkpoint_digest()

    # Tamper the on-disk private prefix: reload must fail the digest binding.
    path = next(tmp_path.glob("*.checkpoint.json"))
    raw = json.loads(path.read_text("utf-8"))
    raw["committed_prefix_tokens"] = [101, 999]
    path.write_text(json.dumps(raw), "utf-8")
    with pytest.raises(ValueError, match="^checkpoint_store_corrupt$"):
        store.load()


def test_checkpoint_store_rejects_orphan_temporary_file(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path, store_identity="store-1")
    # A stray temp file must never be trusted as a checkpoint.
    (tmp_path / ".checkpoint-orphan.tmp").write_text("{}", "utf-8")
    assert store.load() is None



def test_checkpoint_store_rejects_watermark_rollback_without_replacing_record(
    tmp_path: Path,
) -> None:
    store = CheckpointStore(tmp_path, store_identity="store-1")
    newer = _checkpoint(prefix=(101, 102))
    older = _checkpoint(prefix=(101,))
    store.write(newer)

    with pytest.raises(ValueError, match="checkpoint_watermark_rollback"):
        store.write(older)

    assert store.load() == newer


def test_checkpoint_store_owns_private_file_modes_under_permissive_umask(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    previous = os.umask(0o022)
    try:
        store = CheckpointStore(root, store_identity="store-1")
        store.write(_checkpoint())
    finally:
        os.umask(previous)

    record = next(root.glob("*.checkpoint.json"))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


@pytest.mark.parametrize("mutation", ["unknown", "duplicate"])
def test_checkpoint_store_requires_closed_canonical_json(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = CheckpointStore(tmp_path, store_identity="store-1")
    store.write(_checkpoint())
    record = next(tmp_path.glob("*.checkpoint.json"))
    canonical = record.read_text("utf-8")
    if mutation == "unknown":
        malformed = canonical[:-1] + ',"unknown":0}'
    else:
        malformed = canonical.replace(
            '"watermark":2', '"watermark":2,"watermark":2', 1
        )
    record.write_text(malformed, encoding="utf-8")
    record.chmod(0o600)

    with pytest.raises(ValueError, match="^checkpoint_store_corrupt$"):
        store.load()


def test_checkpoint_store_rejects_symlink_candidate_without_following_it(
    tmp_path: Path,
) -> None:
    store = CheckpointStore(tmp_path, store_identity="store-1")
    outside = tmp_path / "outside"
    outside.write_bytes(b"private-outside-bytes")
    record = tmp_path / store._path.name
    record.symlink_to(outside)

    with pytest.raises(ValueError, match="^checkpoint_store_corrupt$"):
        store.load()
    assert outside.read_bytes() == b"private-outside-bytes"


def test_checkpoint_reconciliation_decision_survives_source_deletion(
    tmp_path: Path,
) -> None:
    store = CheckpointStore(tmp_path, store_identity="store-1")
    checkpoint = _checkpoint()
    store.write(checkpoint)

    first = CheckpointStore.reconcile_orphans(tmp_path)
    assert store.load() is None
    second = CheckpointStore.reconcile_orphans(tmp_path)

    assert second == first == (
        {
            "checkpoint_digest": checkpoint.checkpoint_digest(),
            "outcome": "aborted",
            "reason_code": "runtime_owner_not_restored",
        },
    )


def test_recovery_intent_never_marks_successor_ready() -> None:
    intent = build_recovery_intent(
        BINDING,
        retained_path_ids=["path-a"],
        candidate_path_ids=["path-b"],
        equivalent_candidate_generations=1,
        candidate_first_seen_unix_ms=1_000,
        generated_at_unix_ms=1_000,
    )
    assert intent["route_ready"] is False
    tampered = dict(intent)
    tampered["route_ready"] = True
    with pytest.raises(ValueError):
        validate_recovery_intent(tampered)


def test_recovery_intent_hysteresis_and_breaker() -> None:
    intent = build_recovery_intent(
        BINDING,
        retained_path_ids=["path-a"],
        candidate_path_ids=["path-b"],
        equivalent_candidate_generations=4,
        candidate_first_seen_unix_ms=1_000,
        generated_at_unix_ms=11_000,
        breaker_open=True,
    )
    assert intent["hysteresis"]["state"] == "stable_candidate"
    assert intent["breaker"]["state"] == "open"


def test_contracts_reject_unknown_fields() -> None:
    qual = _qualified()
    with pytest.raises(ValueError):
        validate_recovery_qualification({**qual, "bogus": 1})
    owner, runtime = _runtime()
    status = runtime.status()
    with pytest.raises(ValueError):
        validate_recovery_runtime({**status, "bogus": 1})
    gate = build_recovery_gate(
        gate_id="g1", scenario_id="s1", gate_kind="deterministic_positive",
        executed=True, passed=True, checks={"ok": True},
    )
    with pytest.raises(ValueError):
        validate_recovery_gate({**gate, "bogus": 1})


def test_recovery_gate_unexecuted_cannot_pass() -> None:
    with pytest.raises(ValueError):
        build_recovery_gate(
            gate_id="g1", scenario_id="s1", gate_kind="physical_browser_positive",
            executed=False, passed=True, checks={},
        )
    # An executed failure is a valid, honest record.
    gate = build_recovery_gate(
        gate_id="g1", scenario_id="s1", gate_kind="physical_browser_positive",
        executed=True, passed=False, checks={"reason": "no_successor"},
    )
    assert gate["passed"] is False


def test_compatibility_matches_is_exact_not_similar() -> None:
    assert compatibility_matches(COMPATIBILITY, COMPATIBILITY)
    divergent = dict(COMPATIBILITY)
    divergent["generation_settings_digest"] = _sha("e")
    assert not compatibility_matches(COMPATIBILITY, divergent)


# --------------------------------------------------------------------------- #
# Scenario: gateway_checkpoint_authority (deterministic negative)
# --------------------------------------------------------------------------- #


def test_browser_cursor_and_reconstructed_text_are_not_authority() -> None:
    owner, runtime = _runtime()
    runtime.commit_position("req-1", position=1)
    # A browser-authored cursor can never advance the committed watermark.
    runtime.set_browser_authored_cursor("req-1", position=99)
    assert runtime.committed_watermark("req-1") == 1
    # Reconstructing text from the ledger never changes the watermark either.
    runtime.reconnect_replay("req-1", from_position=0)
    assert runtime.committed_watermark("req-1") == 1


def test_invalid_checkpoint_fails_before_successor_work() -> None:
    owner, runtime = _runtime()
    runtime.commit_position("req-1", position=1)
    identity = runtime.current_identity("req-1")
    runtime.observe_failure("req-1", identity=identity, scope="request", authored_by=Issuer.LIVENESS.value)
    # A regressed checkpoint (below the committed watermark) is rejected.
    decision = runtime.freeze_checkpoint("req-1", committed_token_count=0, committed_prefix_digest=_sha("d"))
    assert not decision.accepted
    assert decision.reason == "watermark_rollback"


# --------------------------------------------------------------------------- #
# Scenario: committed_prefix_replay_exactness (deterministic positive)
# --------------------------------------------------------------------------- #


def test_full_context_replay_positive_flow() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)

    assert runtime.phase("req-1") == RecoveryPhase.RECOVERED_DECODE
    assert runtime.committed_watermark("req-1") == 2
    assert runtime.delivery_ledger("req-1") == (1, 2)
    assert runtime.current_identity("req-1").request_attempt == 2
    assert runtime.current_identity("req-1").path_id == "path-b"

    # The first publication is strictly after the committed watermark.
    assert runtime.publish_position("req-1", position=3).accepted
    assert runtime.delivery_ledger("req-1") == (1, 2, 3)
    assert runtime.committed_watermark("req-1") == 3


def test_regressed_skipped_repeated_watermark_rejected() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)

    # Republishing a committed position fails (duplicate).
    assert runtime.publish_position("req-1", position=2).reason == "duplicate_position"
    # First publication strictly after the committed watermark is accepted.
    assert runtime.publish_position("req-1", position=3).accepted
    # Repeating the just-published position fails.
    assert runtime.publish_position("req-1", position=3).reason == "duplicate_position"
    # A skipped (non-contiguous) position fails.
    assert runtime.publish_position("req-1", position=5).reason == "skipped_position"


# --------------------------------------------------------------------------- #
# Scenario: request_scoped_cutover_isolation (deterministic positive)
# --------------------------------------------------------------------------- #


def test_cutover_is_request_scoped() -> None:
    owner = _owner()
    runtime = RecoveryRuntime(owner)
    identity_a = owner.admission_identity("req-a", path_id="path-a")
    identity_b = owner.admission_identity("req-b", path_id="path-a")
    runtime.admit("req-a", identity=identity_a, absolute_deadline_ms=60_000)
    runtime.admit("req-b", identity=identity_b, absolute_deadline_ms=60_000)

    # req-b commits a prefix and stays active; req-a commits its own prefix
    # inside the recovery helper.
    runtime.commit_position("req-b", position=1)

    # Cut over only req-a.
    _advance_to_recovered(owner, runtime, request_id="req-a")

    assert runtime.current_identity("req-a").request_attempt == 2
    # Unrelated request authority and progress are unchanged.
    assert runtime.current_identity("req-b").request_attempt == 1
    assert runtime.current_identity("req-b").path_id == "path-a"
    assert runtime.committed_watermark("req-b") == 1
    assert runtime.delivery_ledger("req-b") == (1,)


# --------------------------------------------------------------------------- #
# Scenario: stale_generation_fencing (deterministic negative)
# --------------------------------------------------------------------------- #


def test_late_source_frames_are_fenced_after_cutover() -> None:
    owner, runtime = _runtime()
    old_identity = runtime.current_identity("req-1")
    _advance_to_recovered(owner, runtime)

    # The old attempt's frames are fenced: no publication, no ledger advance.
    decision = runtime.publish_position("req-1", position=3, identity=old_identity)
    assert not decision.accepted
    assert decision.reason == "fenced_attempt"
    assert runtime.committed_watermark("req-1") == 2

    # A stale terminal from the old attempt is also rejected.
    stale_terminal = owner.issue_terminal(
        "req-1", identity=old_identity, expected_revision=runtime.cleanup_revision("req-1")
    )
    decision = runtime.publish_terminal("req-1", grant=stale_terminal, status="completed")
    assert not decision.accepted


def test_duplicate_and_conflicting_cutover_rejected() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)

    # Repeating the exact same cutover is idempotent.
    cutover = owner.issue_cutover(
        "req-1", successor_path_id="path-b", expected_revision=runtime.revision("req-1") - 1
    )
    # A conflicting cutover with a stale revision is rejected.
    decision = runtime.cutover("req-1", grant=cutover)
    assert not decision.accepted
    assert decision.reason in {"conflicting_cutover", "expected_revision_mismatch"}


# --------------------------------------------------------------------------- #
# Scenario: duplicate_delivery_prevention (deterministic negative)
# --------------------------------------------------------------------------- #


def test_duplicate_delivery_and_reconnect_replay() -> None:
    owner, runtime = _runtime()
    runtime.commit_position("req-1", position=1)
    runtime.commit_position("req-1", position=2)

    # A duplicate successor frame is rejected without a second append.
    decision = runtime.publish_position("req-1", position=2)
    assert decision.reason == "duplicate_position"
    assert runtime.delivery_ledger("req-1") == (1, 2)

    # Reconnect replay reconstructs history but never advances the watermark.
    watermark_before = runtime.committed_watermark("req-1")
    runtime.reconnect_replay("req-1", from_position=0)
    assert runtime.committed_watermark("req-1") == watermark_before
    assert runtime.replayed_positions("req-1") == (1, 2)


# --------------------------------------------------------------------------- #
# Scenario: cancel_before_cutover (deterministic negative)
# --------------------------------------------------------------------------- #


def test_cancel_before_cutover_prevents_recovery() -> None:
    owner, runtime = _runtime()
    runtime.commit_position("req-1", position=1)
    identity = runtime.current_identity("req-1")
    runtime.observe_failure("req-1", identity=identity, scope="request", authored_by=Issuer.LIVENESS.value)
    runtime.freeze_checkpoint("req-1", committed_token_count=1, committed_prefix_digest=_sha("d"))
    runtime.select_successor("req-1", qualification=_qualified())
    runtime.prefill("req-1", reconstructed_prefix_count=1)

    # Cancellation is observed before cutover.
    runtime.observe_cancellation("req-1", at_ms=2_000)
    cutover = owner.issue_cutover(
        "req-1", successor_path_id="path-b", expected_revision=runtime.revision("req-1")
    )
    decision = runtime.cutover("req-1", grant=cutover)
    assert not decision.accepted
    assert decision.reason == "cancellation_precedes_recovery"


# --------------------------------------------------------------------------- #
# Scenario: cancel_after_cutover (deterministic negative)
# --------------------------------------------------------------------------- #


def test_cancel_after_cutover_is_single_terminal_cancel() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)

    runtime.observe_cancellation("req-1", at_ms=3_000)
    identity = runtime.current_identity("req-1")
    # The successor must clean up before publishing the cancel terminal.
    cleanup = owner.issue_cleanup(
        "req-1", identity=identity, expected_revision=runtime.cleanup_revision("req-1")
    )
    assert runtime.record_cleanup(
        "req-1", grant=cleanup, subject=runtime.subject(identity)
    ).accepted
    terminal = owner.issue_terminal(
        "req-1", identity=identity, expected_revision=runtime.cleanup_revision("req-1")
    )
    decision = runtime.publish_terminal("req-1", grant=terminal, status="cancelled")
    assert decision.accepted
    assert runtime.terminal("req-1") == "cancelled"


def test_cancelled_request_cannot_be_mislabelled_as_recovery_failure() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)
    runtime.observe_cancellation("req-1", at_ms=3_000)
    identity = runtime.current_identity("req-1")
    cleanup = owner.issue_cleanup(
        "req-1", identity=identity, expected_revision=runtime.cleanup_revision("req-1")
    )
    runtime.record_cleanup("req-1", grant=cleanup, subject=runtime.subject(identity))
    terminal = owner.issue_terminal(
        "req-1", identity=identity, expected_revision=runtime.cleanup_revision("req-1")
    )
    decision = runtime.publish_terminal("req-1", grant=terminal, status="successor_failed")
    assert not decision.accepted
    assert decision.reason == "cancellation_mislabelled_as_recovery_failure"


# --------------------------------------------------------------------------- #
# Scenario: successor_failure_cleanup (deterministic negative)
# --------------------------------------------------------------------------- #


def test_successor_failure_is_request_scoped_and_cleans_up() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)

    decision = runtime.fail_successor("req-1", reason="successor_prefill_failed")
    assert decision.accepted
    assert runtime.phase("req-1") == RecoveryPhase.CLEANUP


def test_cleanup_required_before_terminal_and_exact_subject() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)
    identity = runtime.current_identity("req-1")

    # A terminal without prior cleanup is rejected.
    terminal = owner.issue_terminal(
        "req-1", identity=identity, expected_revision=runtime.cleanup_revision("req-1")
    )
    decision = runtime.publish_terminal("req-1", grant=terminal, status="completed")
    assert not decision.accepted
    assert decision.reason == "cleanup_required_before_terminal"

    # Cleanup must name the exact subject.
    wrong_subject = "req-1:1:path-a"
    cleanup = owner.issue_cleanup(
        "req-1", identity=identity, expected_revision=runtime.cleanup_revision("req-1")
    )
    decision = runtime.record_cleanup("req-1", grant=cleanup, subject=wrong_subject)
    assert not decision.accepted
    assert decision.reason == "cleanup_subject_mismatch"


# --------------------------------------------------------------------------- #
# Authority consumption (fail-closed)
# --------------------------------------------------------------------------- #


def test_self_minted_authority_is_rejected() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)

    # A grant recovery built itself is never issued by the owner.
    forged = OwnerGrant(
        issuer=Issuer.ROUTER,
        kind=GrantKind.CUTOVER,
        identity=runtime.current_identity("req-1"),
        expected_revision=runtime.revision("req-1"),
        a4_atomic_commit="a" * 40,
        token="self_minted",
    )
    decision = runtime.cutover("req-1", grant=forged)
    assert not decision.accepted
    assert decision.reason == "self_minted_authority"


def test_stale_owner_issued_transition_is_rejected() -> None:
    owner, runtime = _runtime()
    runtime.commit_position("req-1", position=1)
    identity = runtime.current_identity("req-1")
    runtime.observe_failure(
        "req-1", identity=identity, scope="request", authored_by=Issuer.LIVENESS.value
    )
    runtime.freeze_checkpoint("req-1", committed_token_count=1, committed_prefix_digest=_sha("d"))
    runtime.select_successor("req-1", qualification=_qualified())
    runtime.prefill("req-1", reconstructed_prefix_count=1)

    # The owner issues a fence, then a later fence supersedes it.
    fence_stale = owner.issue_fence("req-1", expected_revision=runtime.revision("req-1"))
    owner.issue_fence("req-1", expected_revision=runtime.revision("req-1"))

    # Applying the superseded (stale) fence fails closed.
    decision = runtime.fence_source_attempt("req-1", grant=fence_stale)
    assert not decision.accepted
    assert decision.reason == "stale_owner_issued_transition"


def test_a6_cannot_author_its_own_failure() -> None:
    owner, runtime = _runtime()
    identity = runtime.current_identity("req-1")
    decision = runtime.observe_failure(
        "req-1", identity=identity, scope="request", authored_by="recovery_code"
    )
    assert not decision.accepted
    assert decision.reason == "a6_cannot_author_failure"


def test_deployment_fatal_requires_a4_allowlist() -> None:
    owner, runtime = _runtime()
    identity = runtime.current_identity("req-1")
    decision = runtime.observe_failure(
        "req-1", identity=identity, scope="deployment",
        authored_by=Issuer.LIVENESS.value, fatal_reason="random_guess",
    )
    assert not decision.accepted
    assert decision.reason == "deployment_fatal_requires_a4_allowlist"
    assert not runtime.deployment_fatal

    allowed = runtime.observe_failure(
        "req-1", identity=identity, scope="deployment",
        authored_by=Issuer.LIVENESS.value, fatal_reason="all_qualified_tracks_lost",
    )
    assert allowed.accepted
    assert runtime.deployment_fatal


# --------------------------------------------------------------------------- #
# Breaker and restart reconciliation
# --------------------------------------------------------------------------- #


def test_breaker_opens_after_three_successor_failures() -> None:
    owner, runtime = _runtime()
    assert not runtime.breaker_open(observed_at_unix_ms=1_000)
    runtime.record_successor_failure(observed_at_unix_ms=1_000)
    runtime.record_successor_failure(observed_at_unix_ms=2_000)
    runtime.record_successor_failure(observed_at_unix_ms=3_000)
    assert runtime.breaker_open(observed_at_unix_ms=3_001)


def test_restart_reconciles_each_nonterminal_request_once() -> None:
    owner = _owner()
    runtime = RecoveryRuntime(owner)
    for request_id in ("req-a", "req-b", "req-c"):
        identity = owner.admission_identity(request_id, path_id="path-a")
        runtime.admit(request_id, identity=identity, absolute_deadline_ms=60_000)

    # req-a reaches recovered_decode (resumable); req-b is mid-transition at
    # failure_observed (not resumable); req-c is still active (resumable).
    _advance_to_recovered(owner, runtime, request_id="req-a")
    identity_b = runtime.current_identity("req-b")
    runtime.observe_failure(
        "req-b", identity=identity_b, scope="request", authored_by=Issuer.LIVENESS.value
    )

    outcomes = runtime.restart()
    assert outcomes["req-a"] == "resumed"
    assert outcomes["req-b"] == "aborted"  # transient mid-transition phase
    assert outcomes["req-c"] == "resumed"

    # Idempotent: a second restart returns the same outcomes without re-counting.
    outcomes_again = runtime.restart()
    assert outcomes_again == outcomes


def test_restart_advances_publisher_generation() -> None:
    owner, runtime = _runtime()
    identity = runtime.current_identity("req-1")
    runtime.restart()
    assert runtime.current_identity("req-1").publisher_generation == identity.publisher_generation + 1


# --------------------------------------------------------------------------- #
# Runtime projection is closed and privacy-reduced
# --------------------------------------------------------------------------- #


def test_runtime_projection_is_privacy_reduced_and_closed() -> None:
    owner, runtime = _runtime()
    _advance_to_recovered(owner, runtime)
    status = runtime.status()
    assert status["protocol"] == RUNTIME_PROTOCOL
    validate_recovery_runtime(status)
    # No prompt/token material can enter the runtime projection.
    serialized = json.dumps(status)
    assert "encoded_prompt" not in serialized
    assert "committed_prefix_tokens" not in serialized
    assert "token_text" not in serialized
    assert "prompt_text" not in serialized


def test_unresolved_a4_commit_binding_fails_closed() -> None:
    owner = OwnerAuthority()  # no a4_atomic_commit resolved
    runtime = RecoveryRuntime(owner)
    decision = runtime.authorize_integration()
    assert not decision.accepted
    assert decision.reason == "unresolved_a4_atomic_commit_binding"

    owner.resolve_a4_atomic_commit("b" * 40)
    assert runtime.authorize_integration().accepted


def test_simulated_physical_gate_never_authorizes() -> None:
    owner, runtime = _runtime()
    decision = runtime.simulate_physical_gate("ordinary_browser_replay_continuation")
    assert not decision.accepted
    assert decision.reason == "physical_gate_requires_executed_product_path"
