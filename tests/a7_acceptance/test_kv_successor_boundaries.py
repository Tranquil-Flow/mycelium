# SPDX-License-Identifier: AGPL-3.0-only
"""Isolation-proof registry check for the A7 runtime half.

History: this module was written before `mycelium_a7_kv_successor.py` existed.
At the baseline commit it failed with a collection error (module absent); against
the raising skeleton it passed *by construction*, because the skeleton WAS the
boundary it pinned -- every seam in the implementation contract section 4 raised
`A7ScaffoldBoundary`. That was the deterministic-core-only scaffold.

The runtime half now implements all ten seams in-process, so the assertions that
pinned ten refusals are inverted here: the registry of remaining scaffold
boundaries is empty, and no seam may refuse with `A7ScaffoldBoundary`. At
`0fe6037` (base) this module is RED for exactly that reason.

This is the *isolation proof*, not the reachability guard. The reachability guard
that runs in default collection is
`tests/a7_acceptance/test_kv_successor_runtime.py`; this module keeps the
historical opt-in surface (`-m scaffold_red`) and additionally pins that the
exception class survives as an explicit, non-`ValueError` first-class surface for
any future seam.

* the boundary registry must be empty and closed;
* every implemented seam must be named in `IMPLEMENTED_RUNTIME_SEAMS`;
* no seam may fabricate the boundary error as a contract `ValueError`;
* the module still declares the honest claim boundary: `design_only`, no product
  runtime execution, no integration, no physical evidence, no qualification, no
  promotion, A6 untouched.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import mycelium_a7_kv_successor as a7


# The closed set of seams the implementation contract section 4 names, now all
# implemented in-process by the runtime half.
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


def test_the_boundary_registry_is_closed_and_empty() -> None:
    assert set(a7.SCAFFOLD_BOUNDARIES) == set()
    assert a7.SCAFFOLD_BOUNDARIES == {}
    assert set(a7.IMPLEMENTED_RUNTIME_SEAMS) == SEAM_NAMES
    # the frozen vocabulary of reasons survives as history, unused by any seam
    assert len(a7.SCAFFOLD_BOUNDARY_REASONS) == 6


def test_no_declared_seam_raises_a_scaffold_boundary_any_more() -> None:
    calls = _seam_calls()
    assert set(calls) == SEAM_NAMES

    for name, call in calls.items():
        try:
            value = call()
        except a7.A7ScaffoldBoundary as exc:  # pragma: no cover - the RED path
            pytest.fail(f"{name} still refuses with the scaffold boundary: {exc}")
        except ValueError:
            continue
        pytest.fail(f"{name} returned {value!r} for a hostile input")


def test_no_seam_silently_succeeds_on_a_hostile_input() -> None:
    for name, call in _seam_calls().items():
        with pytest.raises(ValueError):
            call()


def test_boundary_errors_stay_distinct_from_contract_value_errors() -> None:
    assert issubclass(a7.A7ScaffoldBoundary, NotImplementedError)
    assert not issubclass(a7.A7ScaffoldBoundary, ValueError)


def test_the_scaffold_still_declares_an_honest_claim_boundary() -> None:
    boundary = a7.scaffold_claim_boundary()

    assert boundary["gate_state"] == "design_only"
    assert boundary["scaffold_state"] == "scaffold_unintegrated"
    assert boundary["runtime_executed"] is False
    assert boundary["integrated"] is False
    assert boundary["physical_evidence"] is False
    assert boundary["qualification_claim"] is False
    assert boundary["promotion_authorized"] is False
    assert boundary["a6_dependency_integrated"] is False
    assert boundary["unearned_count"] == 12
