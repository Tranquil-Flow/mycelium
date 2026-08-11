from __future__ import annotations

import copy

import pytest

from mycelium_m19_recovery import (
    RecoveryLedger,
    TrafficAwareLivenessDetector,
    build_recovery_plan,
    validate_liveness,
    validate_recovery_plan,
    validate_recovery_runtime,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _binding() -> dict:
    return {
        "deployment_id": "deployment-m19",
        "deployment_epoch": 19,
        "topology_version": 7,
        "model_id": "org/model",
        "model_revision": "a" * 40,
        "representation_digest": DIGEST_A,
        "graph_digest": DIGEST_B,
        "membership_generation": 4,
    }


def _successor(*, compatible: bool = True) -> dict:
    return {
        "track_id": "track-successor",
        "qualification_id": "qualification-successor",
        "qualification_digest": DIGEST_A,
        "decode_mode": "stage_local_kv",
        "kv_compatibility": "compatible" if compatible else "incompatible",
        "kv_schema_digest": DIGEST_B if compatible else None,
        "failure_domain": "host-b",
    }


def test_liveness_suppresses_one_miss_and_scopes_active_failure() -> None:
    detector = TrafficAwareLivenessDetector(
        _binding(),
        generated_at_unix_ms=1_000,
    )
    detector.observe("peer-a", observed_at_unix_ms=1_000)
    detector.miss("peer-a", observed_at_unix_ms=6_000, active_traffic=False)
    assert detector.status()["subjects"][0]["state"] == "suspect"
    assert detector.status()["incidents"] == []

    detector.active_disconnect(
        "peer-a",
        observed_at_unix_ms=6_500,
        scope="placement",
        affected_track_ids=("track-primary",),
    )
    status = validate_liveness(detector.status())
    assert status["subjects"][0]["state"] == "failed"
    assert status["incidents"][0]["scope"] == "placement"
    assert status["incidents"][0]["reason"] == "active_disconnect"


def test_idle_peer_requires_three_misses_and_fifteen_seconds() -> None:
    detector = TrafficAwareLivenessDetector(_binding(), generated_at_unix_ms=1_000)
    detector.observe("peer-a", observed_at_unix_ms=1_000)
    detector.miss("peer-a", observed_at_unix_ms=6_000, active_traffic=False)
    detector.miss("peer-a", observed_at_unix_ms=11_000, active_traffic=False)
    detector.miss("peer-a", observed_at_unix_ms=16_000, active_traffic=False)
    assert detector.status()["subjects"][0]["state"] == "quarantined"


def test_recovery_plan_retains_surviving_track_and_gates_candidate() -> None:
    plan = build_recovery_plan(
        _binding(),
        incumbent_track_ids=("track-primary", "track-replica"),
        failed_track_ids=("track-primary",),
        successors=(_successor(),),
        equivalent_candidate_generations=2,
        candidate_first_seen_unix_ms=1_000,
        generated_at_unix_ms=12_000,
    )
    validated = validate_recovery_plan(plan)
    assert validated["surviving_track_ids"] == ["track-replica"]
    assert validated["candidate_state"] == "hysteresis_pending"
    assert validated["provisioning_allowed"] is False

    ready = build_recovery_plan(
        _binding(),
        incumbent_track_ids=("track-primary",),
        failed_track_ids=("track-primary",),
        successors=(_successor(),),
        equivalent_candidate_generations=1,
        candidate_first_seen_unix_ms=12_000,
        generated_at_unix_ms=12_000,
    )
    assert ready["candidate_state"] == "emergency_candidate"
    assert ready["provisioning_allowed"] is True


def test_full_context_replay_is_monotonic_and_exactly_terminal() -> None:
    ledger = RecoveryLedger(_binding(), maximum_recovery_attempts=2)
    ledger.admit(
        "request-a",
        path_id="path-a",
        track_id="track-primary",
        qualification_id="qualification-primary",
        qualification_digest=DIGEST_B,
    )
    ledger.commit("request-a", committed_token_count=2, committed_token_digest=DIGEST_A)
    ledger.recover(
        "request-a",
        successor=_successor(),
        expected_attempt=2,
        committed_token_count=2,
        committed_token_digest=DIGEST_A,
        recovery_mode="full_context_replay",
        successor_path_id="path-b",
        replay_performed=True,
    )
    ledger.complete("request-a", committed_token_count=4, committed_token_digest=DIGEST_B)
    runtime = validate_recovery_runtime(ledger.status())
    request = runtime["requests"][0]
    assert request["attempt"] == 2
    assert request["recovery_mode"] == "full_context_replay"
    assert request["kv_outcome"] == "not_transferred"
    assert request["terminal_state"] == "completed"
    assert request["cleanup_complete"] is True

    with pytest.raises(ValueError, match="request_already_terminal"):
        ledger.abort("request-a", reason="late_abort")


def test_stale_watermark_and_incompatible_kv_fail_closed() -> None:
    ledger = RecoveryLedger(_binding(), maximum_recovery_attempts=2)
    ledger.admit(
        "request-b",
        path_id="path-a",
        track_id="track-primary",
        qualification_id="qualification-primary",
        qualification_digest=DIGEST_B,
    )
    ledger.commit("request-b", committed_token_count=3, committed_token_digest=DIGEST_A)
    with pytest.raises(ValueError, match="recovery_watermark_stale"):
        ledger.recover(
            "request-b",
            successor=_successor(),
            expected_attempt=2,
            committed_token_count=2,
            committed_token_digest=DIGEST_A,
            recovery_mode="full_context_replay",
            successor_path_id="path-b",
            replay_performed=True,
        )
    with pytest.raises(ValueError, match="kv_successor_incompatible"):
        ledger.recover(
            "request-b",
            successor=_successor(compatible=False),
            expected_attempt=2,
            committed_token_count=3,
            committed_token_digest=DIGEST_A,
            recovery_mode="fenced_kv_successor",
            successor_path_id="path-b",
            replay_performed=False,
        )
    ledger.abort("request-b", reason="no_compatible_successor")
    assert ledger.status()["requests"][0]["terminal_state"] == "aborted"


def test_compatible_fenced_kv_successor_resumes_without_replay() -> None:
    ledger = RecoveryLedger(_binding(), maximum_recovery_attempts=2)
    ledger.admit(
        "request-kv",
        path_id="path-a",
        track_id="track-primary",
        qualification_id="qualification-primary",
        qualification_digest=DIGEST_B,
    )
    ledger.commit("request-kv", committed_token_count=1, committed_token_digest=DIGEST_A)
    ledger.recover(
        "request-kv",
        successor=_successor(compatible=True),
        expected_attempt=2,
        committed_token_count=1,
        committed_token_digest=DIGEST_A,
        recovery_mode="fenced_kv_successor",
        successor_path_id="path-b",
        replay_performed=False,
    )
    ledger.complete(
        "request-kv", committed_token_count=1, committed_token_digest=DIGEST_A
    )

    request = validate_recovery_runtime(ledger.status())["requests"][0]
    assert request["kv_outcome"] == "resumed_from_checkpoint"
    assert request["replay_performed"] is False
    assert request["terminal_state"] == "completed"
    assert request["cleanup_complete"] is True


def test_contracts_reject_unknown_and_private_fields() -> None:
    detector = TrafficAwareLivenessDetector(_binding(), generated_at_unix_ms=1_000)
    detector.observe("peer-a", observed_at_unix_ms=1_000)
    unknown = copy.deepcopy(detector.status())
    unknown["surprise"] = True
    with pytest.raises(ValueError, match="liveness_invalid"):
        validate_liveness(unknown)

    ledger = RecoveryLedger(_binding(), maximum_recovery_attempts=1)
    ledger.admit(
        "request-c",
        path_id="path-a",
        track_id="track-primary",
        qualification_id="qualification-primary",
        qualification_digest=DIGEST_B,
    )
    private = copy.deepcopy(ledger.status())
    private["requests"][0]["prompt"] = "private"
    with pytest.raises(ValueError, match="recovery_runtime_invalid"):
        validate_recovery_runtime(private)


def test_circuit_breaker_opens_after_three_failures_and_blocks_bypass() -> None:
    ledger = RecoveryLedger(_binding(), maximum_recovery_attempts=2)
    for observed_at in (1_000, 20_000, 40_000):
        ledger.record_successor_failure(observed_at_unix_ms=observed_at)
    ledger.admit(
        "request-breaker",
        path_id="path-a",
        track_id="track-primary",
        qualification_id="qualification-primary",
        qualification_digest=DIGEST_B,
    )
    ledger.commit(
        "request-breaker", committed_token_count=2, committed_token_digest=DIGEST_A
    )
    with pytest.raises(ValueError, match="recovery_circuit_breaker_open"):
        ledger.recover(
            "request-breaker",
            successor=_successor(),
            expected_attempt=2,
            committed_token_count=2,
            committed_token_digest=DIGEST_A,
            recovery_mode="full_context_replay",
            successor_path_id="path-b",
            replay_performed=True,
            observed_at_unix_ms=50_000,
        )
    assert ledger.status()["breaker"]["open_until_unix_ms"] == 70_000


def test_restart_reconciliation_is_durable_and_exactly_once() -> None:
    ledger = RecoveryLedger(_binding(), maximum_recovery_attempts=2)
    ledger.admit(
        "request-terminal",
        path_id="path-a",
        track_id="track-primary",
        qualification_id="qualification-primary",
        qualification_digest=DIGEST_B,
    )
    ledger.abort("request-terminal", reason="no_successor")
    ledger.admit(
        "request-active",
        path_id="path-a",
        track_id="track-primary",
        qualification_id="qualification-primary",
        qualification_digest=DIGEST_B,
    )

    restored = RecoveryLedger.restore(ledger.status())
    assert restored.reconcile_after_restart() == {
        "request-active": "aborted",
        "request-terminal": "already_terminal",
    }
    checkpoint = restored.status()
    assert RecoveryLedger.restore(checkpoint).reconcile_after_restart() == checkpoint[
        "reconciliation"
    ]
