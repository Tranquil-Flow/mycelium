"""WEAK-RED tests: A11 execution behaviour this scaffold deliberately does not implement.

Every test in this file is RED in the scaffold lane and fails with
``SpeculativeDecodeUnavailable`` raised by this lane's own boundary functions.
That is WEAK evidence of absence: the failure is produced by the scaffold, not
by a demonstrated gap in a7b412a. It is recorded here so the lane's incompleteness
is machine-visible, and so these tests can be driven to GREEN only by landing the
real execution path (A10 verifier, private transport, live-route benchmark,
physical qualification, product projection).

Do not cite a failure in this file as proof that the baseline lacks the
behaviour; cite ``test_a11_contract_scaffold.py`` and the baseline probes in the
A11 implementation contract for that.
"""

from __future__ import annotations

import mycelium_speculative_decode as speculative_decode


def _plan() -> dict:
    return speculative_decode.compatibility_fixtures()["speculative_decode_plan"]


def test_request_admission_projects_a_target_only_or_speculative_decision() -> None:
    decision = speculative_decode.integrate_request_admission(
        plan=_plan(), request_id="req-1", preference="speculative_requested"
    )
    assert decision["mode"] in {"target_only", "speculative_admitted"}
    assert decision["fallback_reason"] in {None, "no_authorized_local_draft"}


def test_target_verification_transaction_commits_only_the_common_prefix() -> None:
    transaction = speculative_decode.open_target_verification_transaction(plan=_plan(), width=4, deadline_ms=60.0)
    assert transaction["accepted_prefix_length"] <= transaction["proposal_width"]


def test_proposal_transfer_moves_only_the_minimum_private_envelope() -> None:
    envelope = speculative_decode.transfer_proposal_envelope(plan=_plan(), proposal_tokens=[1, 2, 3, 4])
    assert envelope["token_count"] == 4
    assert "prompt" not in envelope


def test_verified_prefix_commit_advances_the_target_watermark_once() -> None:
    watermark = speculative_decode.commit_verified_prefix(plan=_plan(), accepted_prefix_length=3, correction_token=9)
    assert watermark["committed_position_count"] == 4


def test_paired_product_benchmark_executes_six_windows_on_one_route() -> None:
    benchmark = speculative_decode.run_paired_product_benchmark(plan=_plan(), route_digest="sha256:" + "0" * 64)
    assert len(benchmark["windows"]) == speculative_decode.BENCHMARK_MEASURED_WINDOWS


def test_qualification_bundle_executes_browser_and_negative_gates() -> None:
    qualification = speculative_decode.execute_qualification_bundle(plan=_plan(), benchmark_digest="sha256:" + "0" * 64)
    assert qualification["executed"] is True
    assert set(qualification["browser_workspaces"]) == {
        "inference",
        "device_lab",
        "network",
        "nodes",
        "plans",
        "readiness",
        "incidents",
        "settings",
    }


def test_workspace_projection_reconstructs_public_state_without_private_material() -> None:
    projection = speculative_decode.project_workspace_state(plan=_plan(), workspace="inference")
    assert projection["state"] in {"disabled", "target_only", "speculative", "fallback"}

