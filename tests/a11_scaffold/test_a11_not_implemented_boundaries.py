"""A11 scaffold boundary tests: explicit, honest not-implemented seams.

WEAK-RED DISCIPLINE (read before trusting a failure in this file): every test
here fails because the scaffold under test raises ``SpeculativeDecodeUnavailable``
with a closed reason code. That failure is evidence of an *honest boundary*, not
evidence of missing baseline behaviour. The A11 execution seams (proposal
transfer, A10 target verification, target KV commit, live-route benchmark,
physical qualification, registry admission, product projection) are deliberately
unimplemented; these tests pin that they cannot silently succeed.
"""

from __future__ import annotations

import pytest

import mycelium_speculative_decode as speculative_decode


BOUNDARY_FUNCTIONS = {
    "open_target_verification_transaction": "a10_target_verifier_not_available",
    "transfer_proposal_envelope": "private_proposal_transfer_not_implemented",
    "commit_verified_prefix": "target_kv_commit_not_implemented",
    "run_paired_product_benchmark": "live_route_benchmark_not_implemented",
    "execute_qualification_bundle": "physical_qualification_not_implemented",
    "integrate_request_admission": "registry_admission_integration_not_implemented",
    "project_workspace_state": "product_projection_not_implemented",
}


def _plausible_payload() -> dict[str, object]:
    return {
        "plan": speculative_decode.compatibility_fixtures()["speculative_decode_plan"],
        "request_id": "req-1",
        "proposal_width": 4,
    }


@pytest.mark.parametrize("function_name,code", sorted(BOUNDARY_FUNCTIONS.items()))
def test_boundary_function_raises_closed_unavailable_code(function_name: str, code: str) -> None:
    function = getattr(speculative_decode, function_name)

    with pytest.raises(speculative_decode.SpeculativeDecodeUnavailable) as error:
        function()

    assert error.value.code == code


@pytest.mark.parametrize("function_name,code", sorted(BOUNDARY_FUNCTIONS.items()))
def test_boundary_function_never_returns_a_success_document(function_name: str, code: str) -> None:
    function = getattr(speculative_decode, function_name)
    raised = False

    try:
        result = function(**_plausible_payload())
    except speculative_decode.SpeculativeDecodeUnavailable as error:
        raised = True
        assert error.code == code
    else:  # pragma: no cover - a return here is exactly what this test forbids
        assert not isinstance(result, dict)

    assert raised is True


def test_boundary_codes_are_closed_and_each_function_pins_one() -> None:
    codes = speculative_decode.BOUNDARY_CODES

    assert isinstance(codes, frozenset)
    assert len(codes) == len(set(codes))
    assert set(BOUNDARY_FUNCTIONS.values()) == set(codes)
    for function_name in BOUNDARY_FUNCTIONS:
        function = getattr(speculative_decode, function_name)
        assert function.__module__ == speculative_decode.__name__


def test_boundary_error_is_a_runtime_error_and_never_a_contract_error() -> None:
    error = speculative_decode.SpeculativeDecodeUnavailable("private_proposal_transfer_not_implemented", "scaffold")

    assert isinstance(error, RuntimeError)
    assert not isinstance(error, speculative_decode.SpeculativeDecodeContractError)
    assert error.code == "private_proposal_transfer_not_implemented"
    assert str(error).startswith("private_proposal_transfer_not_implemented")
    assert error.detail == "scaffold"


def test_scaffold_stage_and_claim_boundary_are_pinned() -> None:
    assert speculative_decode.SCAFFOLD_STAGE == "scaffold"
    assert speculative_decode.IMPLEMENTED_CLAIM == "closed contract validation and policy arithmetic only"
    assert "no runtime execution" in speculative_decode.CLAIM_BOUNDARY
    assert "no qualification claim" in speculative_decode.CLAIM_BOUNDARY
