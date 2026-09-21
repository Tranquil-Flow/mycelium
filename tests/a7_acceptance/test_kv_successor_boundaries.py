"""RED-first tests pinning the A7 scaffold's honest not-implemented boundaries.

Written before `mycelium_a7_kv_successor.py` existed. At the baseline commit
(`b90edaf`) they fail with a collection error because the module is absent;
against the raising skeleton they pass by construction, because the skeleton IS
the boundary they pin. Their job is not to prove behaviour -- it is to make a
silent substitution of behaviour impossible:

* every seam in the implementation contract section 4 must raise
  `A7ScaffoldBoundary` with an `a7_scaffold_boundary:<name>:<reason>` message;
* the boundary registry and the exercised seam set must be closed and equal;
* no seam may return a value, fabricate state, or succeed silently;
* the boundary error must be a `NotImplementedError`, not a contract
  `ValueError` that a caller could mistake for ordinary input validation.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import mycelium_a7_kv_successor as a7


# The closed set of seams the scaffold declares unimplemented, copied from the
# implementation contract section 4.
SEAM_NAMES = frozenset(
    {
        "validate_kv_standby_intent",
        "validate_kv_successor_runtime",
        "validate_kv_successor_qualification",
        "validate_kv_successor_recovery_gate",
        "seal_checkpoint_payload",
        "open_checkpoint_payload",
        "transfer_shadow_checkpoint",
        "persist_fence_generation",
        "perform_fenced_cutover",
        "run_successor_recovery",
    }
)


def _seam_calls() -> dict[str, "Callable[[], object]"]:
    zero_digest = "sha256:" + "0" * 64
    return {
        "validate_kv_standby_intent": lambda: a7.validate_kv_standby_intent({}),
        "validate_kv_successor_runtime": lambda: a7.validate_kv_successor_runtime({}),
        "validate_kv_successor_qualification": lambda: (
            a7.validate_kv_successor_qualification({})
        ),
        "validate_kv_successor_recovery_gate": lambda: (
            a7.validate_kv_successor_recovery_gate({})
        ),
        "seal_checkpoint_payload": lambda: a7.seal_checkpoint_payload(
            checkpoint={}, kv_payload=b"private"
        ),
        "open_checkpoint_payload": lambda: a7.open_checkpoint_payload(
            reference="ref:0"
        ),
        "transfer_shadow_checkpoint": lambda: a7.transfer_shadow_checkpoint(
            checkpoint={}, standby_placement_id=zero_digest
        ),
        "persist_fence_generation": lambda: a7.persist_fence_generation(
            fence_state={}
        ),
        "perform_fenced_cutover": lambda: a7.perform_fenced_cutover(
            fence_state={}, checkpoint={}
        ),
        "run_successor_recovery": lambda: a7.run_successor_recovery(
            request_id=zero_digest
        ),
    }


def test_the_boundary_registry_is_closed_and_names_known_reasons() -> None:
    assert set(a7.SCAFFOLD_BOUNDARIES) == SEAM_NAMES
    assert set(a7.SCAFFOLD_BOUNDARIES.values()) <= set(a7.SCAFFOLD_BOUNDARY_REASONS)
    assert len(a7.SCAFFOLD_BOUNDARIES) == len(SEAM_NAMES) == 10


def test_every_declared_seam_raises_an_explicit_scaffold_boundary() -> None:
    calls = _seam_calls()
    assert set(calls) == SEAM_NAMES

    for name, call in calls.items():
        with pytest.raises(a7.A7ScaffoldBoundary) as excinfo:
            call()
        assert f"a7_scaffold_boundary:{name}:" in str(excinfo.value)


def test_no_seam_can_silently_succeed_or_return_a_value() -> None:
    for name, call in _seam_calls().items():
        try:
            value = call()
        except a7.A7ScaffoldBoundary:
            continue
        pytest.fail(
            f"{name} returned {value!r} instead of raising the scaffold boundary"
        )


def test_boundary_errors_are_not_contract_value_errors() -> None:
    assert issubclass(a7.A7ScaffoldBoundary, NotImplementedError)
    assert not issubclass(a7.A7ScaffoldBoundary, ValueError)


def test_the_scaffold_refuses_to_produce_a_runtime_outcome() -> None:
    zero_digest = "sha256:" + "0" * 64

    with pytest.raises(a7.A7ScaffoldBoundary) as excinfo:
        a7.run_successor_recovery(request_id=zero_digest)
    assert "recovery_orchestration_not_implemented" in str(excinfo.value)

    with pytest.raises(a7.A7ScaffoldBoundary) as excinfo:
        a7.perform_fenced_cutover(fence_state={}, checkpoint={})
    assert "cutover_execution_not_implemented" in str(excinfo.value)
