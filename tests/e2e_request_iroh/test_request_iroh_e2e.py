from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e_request_iroh.harness import (
    _RunningSidecar,
    CancellationEvidence,
    CompleteRequestEvidence,
    GenerationRotationEvidence,
    RecoveryEvidence,
    run_cancellation_probe,
    run_complete_request,
    run_generation_rotation_probe,
    run_recovery_probe,
)


def test_e2e_sidecar_stop_closes_captured_child_streams() -> None:
    sidecar = object.__new__(_RunningSidecar)
    stdout = io.StringIO("ready\n")
    stderr = io.StringIO("diagnostic\n")
    sidecar.__dict__["process"] = SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        poll=lambda: 0,
    )

    assert sidecar.stop() == "diagnostic\n"
    assert stdout.closed is True
    assert stderr.closed is True
    assert sidecar.stop() == ""


@pytest.fixture(scope="module")
def completed_runs(
    native_iroh_sidecar_binary: Path,
) -> tuple[CompleteRequestEvidence, CompleteRequestEvidence]:
    return (
        run_complete_request(native_iroh_sidecar_binary),
        run_complete_request(native_iroh_sidecar_binary),
    )


def test_authenticated_qualified_request_crosses_real_router_and_iroh_path(
    completed_runs: tuple[CompleteRequestEvidence, CompleteRequestEvidence],
) -> None:
    evidence = completed_runs[0]

    assert evidence.authenticated_status == 409
    assert evidence.unqualified_router_mutations == 0
    assert evidence.accepted_status == 202
    assert evidence.router_admissions == 1
    assert evidence.production_router_count == 2
    assert evidence.native_sidecar_count == 2
    assert evidence.transport_type == "IrohTransport"
    assert evidence.prefill_stage_indexes == (0, 1, 2)
    assert evidence.decode_route_steps >= 8
    assert evidence.decode_stage_indexes == tuple(range(3)) * 8
    assert evidence.token_indexes == tuple(range(9))
    assert evidence.acknowledged_cursor == 9
    assert evidence.replayed_token_indexes == tuple(range(1, 9))
    assert len(set(evidence.replayed_token_indexes)) == 8
    assert evidence.local_evidence_only is True
    assert evidence.route_ready is False


def test_activation_decode_and_token_frame_digests_are_stable(
    completed_runs: tuple[CompleteRequestEvidence, CompleteRequestEvidence],
) -> None:
    first, second = completed_runs

    assert first.activation_digests == second.activation_digests
    assert first.decode_payload_digests == second.decode_payload_digests
    assert first.token_frame_digests == second.token_frame_digests
    assert len(first.activation_digests) == 3
    assert len(first.decode_payload_digests) == 24
    assert len(first.token_frame_digests) == 9


def test_endpoint_rotation_rejects_stale_in_flight_delivery(
    native_iroh_sidecar_binary: Path,
) -> None:
    evidence: GenerationRotationEvidence = run_generation_rotation_probe(
        native_iroh_sidecar_binary
    )

    assert evidence.rejected is True
    assert evidence.error_code == "peer_rotated"
    assert evidence.old_generation == 1
    assert evidence.new_generation == 2
    assert evidence.pending_deliveries == 0
    assert evidence.local_evidence_only is True
    assert evidence.route_ready is False


def test_sidecar_process_rebirth_recovers_same_request_with_fresh_generation(
    native_iroh_sidecar_binary: Path,
) -> None:
    evidence: RecoveryEvidence = run_recovery_probe(native_iroh_sidecar_binary)

    assert evidence.old_process_exited is True
    assert evidence.replacement_process_started is True
    assert evidence.old_process_id != evidence.new_process_id
    assert evidence.old_endpoint_id != evidence.new_endpoint_id
    assert evidence.old_peer_generation == 1
    assert evidence.new_peer_generation == 2
    assert evidence.stale_generation_rejected is True
    assert evidence.recovery_phase == "RECOVERY_PREFILL"
    assert evidence.recovery_prefill_observed is True
    assert evidence.generated_token_ids_before_failure
    assert evidence.generated_token_ids_after_recovery
    assert (
        evidence.generated_token_ids_before_failure
        + evidence.generated_token_ids_after_recovery
        == evidence.final_token_ids
    )
    assert evidence.pending_deliveries == 0
    assert evidence.local_evidence_only is True
    assert evidence.route_ready is False


def test_cancellation_releases_gateway_adapter_and_all_router_resources(
    native_iroh_sidecar_binary: Path,
) -> None:
    evidence: CancellationEvidence = run_cancellation_probe(
        native_iroh_sidecar_binary
    )

    assert (
        evidence.gateway_released,
        evidence.adapter_released,
        evidence.entry_router_released,
        evidence.remote_router_released,
        evidence.pending_deliveries,
    ) == (True, True, True, True, 0)
    assert evidence.local_evidence_only is True
    assert evidence.route_ready is False
