# SPDX-License-Identifier: AGPL-3.0-only
"""Router decode reachability: the authoritative A11 execution must be dispatched.

RED before the router decode seam existed: ``mycelium_speculative_decode_execution``
was implemented and wired to the A10 verified-token path, but **no non-test module
imported it**, so a decode could never reach it.  GREEN when
``mycelium_router.decoding.resolve_speculative_decode`` dispatches one decode cycle to
``mycelium_speculative_decode_execution.run_speculative_cycle``.

What GREEN proves: the router decode module really invokes the A11 execution in
process -- the injected target decision source records exactly one bounded A10 call,
the cycle's own committed watermark advances by the admitted width, and the record
carries the execution module's protocol and stage.  What it does NOT prove: any live
route, authenticated network transfer, browser surface, physical qualification,
benefit or promotion.  The wired ``mycelium_speculative_decode`` helper stays the
fallback when the authoritative execution cannot be driven, and a decode with no bound
plan stays target-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mycelium_speculative_decode as sd
import mycelium_speculative_decode_execution as sx
from mycelium_router import decoding

from a11_seam_fixtures import CANDIDATES, admissible_plan, matching_target

WIDTH = 4


def dispatch(**overrides):
    """Drive the router decode seam with a bound plan and full in-process injections."""

    source = overrides.pop("source", None)
    arguments = {
        "plan": admissible_plan(),
        "request_id": "req-decode-1",
        "cycle_ref": "decode-cycle-1",
        "width": WIDTH,
        "transport": sx.ProposalTransport(),
        "cancellation": sx.CancellationScope(),
        "draft_tokens": CANDIDATES,
    }
    arguments["target"] = matching_target() if source is None else source
    arguments.update(overrides)
    return arguments["target"], decoding.resolve_speculative_decode(**arguments)


# ---------------------------------------------------------------------------
# The reachability test: this is the one that was RED before the seam existed
# ---------------------------------------------------------------------------


def test_the_router_decode_path_dispatches_to_the_a11_execution() -> None:
    source, record = dispatch()

    assert record["dispatched"] is True, record
    assert record["authority"] == decoding.AUTHORITATIVE_AUTHORITY
    assert record["execution_module"] == decoding.AUTHORITATIVE_EXECUTION_MODULE
    assert decoding.AUTHORITATIVE_EXECUTION_MODULE == "mycelium_speculative_decode_execution"
    assert record["reason"] is None and record["fallback_detail"] is None

    # The authoritative execution really ran: exactly one bounded A10 verification call
    # reached the injected target decision source, and it ran through the A11 stage.
    assert source.bounded_calls == 1
    assert source.bounded_widths == [WIDTH]
    assert record["execution_stage"] == sx.EXECUTION_STAGE
    assert record["execution_protocol"] == sx.PROTOCOL

    cycle = record["cycle"]
    assert cycle["mode"] == "speculative"
    assert cycle["admission"]["mode"] == "speculative_admitted"
    assert cycle["commit"]["advanced"] is True
    assert cycle["output"]["released_tokens"] == WIDTH
    assert cycle["target_kv"]["position"] == WIDTH


def test_the_dispatched_cycle_is_the_real_a10_wired_cycle() -> None:
    _source, record = dispatch()

    cycle = record["cycle"]
    # The A10 bounded verifier returned a real target-authoritative outcome...
    assert cycle["a10_outcome"]["verified_count"] == WIDTH
    assert cycle["a10_outcome"]["committed_position"] == WIDTH
    assert cycle["a10_request"]["transaction_mode"] in {
        "commit_verified_prefix",
        "commit_verified_prefix_plus_target_correction",
    }
    # ...the parity oracle ran over the same prefix...
    assert cycle["parity"]["parity"] == "matched"
    # ...and the cycle's runtime record is the frozen validated shape.
    assert sd.validate_speculative_decode_runtime(cycle["runtime_document"]) == cycle["runtime_document"]
    assert cycle["claim"] == sx.CLAIM_BOUNDARY


def test_the_router_decode_seam_names_the_authoritative_module_it_dispatches_to() -> None:
    """The dispatch target is a real module the record names, not a string in prose."""

    import importlib

    module = importlib.import_module(decoding.AUTHORITATIVE_EXECUTION_MODULE)
    assert module is sx
    assert getattr(module, decoding.AUTHORITATIVE_EXECUTION_FUNCTION) is sx.run_speculative_cycle


# ---------------------------------------------------------------------------
# The fallback: the wired helper stays reachable, and nothing is fabricated
# ---------------------------------------------------------------------------


def test_the_wired_helper_remains_the_fallback_when_the_execution_cannot_be_driven() -> None:
    record = decoding.resolve_speculative_decode(
        plan=admissible_plan(), request_id="req-decode-2", width=WIDTH
    )

    assert record["dispatched"] is False
    assert record["authority"] == decoding.FALLBACK_AUTHORITY
    assert record["execution_module"] == decoding.FALLBACK_HELPER_MODULE
    assert decoding.FALLBACK_HELPER_MODULE == "mycelium_speculative_decode"
    assert record["reason"] == "injected_execution_incomplete"
    assert "cycle" not in record

    # The fallback decision is the wired helper's own admission arithmetic.
    expected = sd.integrate_request_admission(
        plan=admissible_plan(), request_id="req-decode-2", preference="speculative_requested"
    )
    assert record["admission"] == expected
    assert record["speculative_available"] is True


def test_a_malformed_injection_falls_back_instead_of_fabricating_a_cycle() -> None:
    source = matching_target()
    record = decoding.resolve_speculative_decode(
        plan=admissible_plan(),
        request_id="req-decode-3",
        width=WIDTH,
        transport="not-a-transport",
        target=source,
        cancellation=sx.CancellationScope(),
        draft_tokens=CANDIDATES,
    )

    assert record["dispatched"] is False
    assert record["reason"] == "injected_execution_incomplete"
    assert record["fallback_detail"] == "injection_type_mismatch"
    assert record["fallback_detail"] in sx.SEAM_REFUSALS
    assert "cycle" not in record
    assert source.bounded_calls == 0


def test_a_decode_with_no_bound_plan_dispatches_nothing() -> None:
    record = decoding.resolve_speculative_decode(plan=None, request_id="req-decode-4")

    assert record["dispatched"] is False
    assert record["authority"] == decoding.TARGET_ONLY_AUTHORITY
    assert record["execution_module"] is None
    assert record["reason"] == "no_bound_speculative_plan"
    assert record["admission"] is None
    assert record["speculative_available"] is False


@pytest.mark.parametrize("reason", ["no_bound_speculative_plan", "injected_execution_incomplete"])
def test_the_reasons_the_seam_actually_reports_are_registered(reason: str) -> None:
    assert reason in decoding.SEAM_DISPATCH_REASONS


def test_a_malformed_plan_is_not_swallowed_by_the_fallback() -> None:
    """A plan that violates the closed contract raises; it is not a decode outage."""

    with pytest.raises(sd.SpeculativeDecodeContractError):
        decoding.resolve_speculative_decode(plan={"protocol": "nope"}, request_id="req-decode-5", width=WIDTH)


# ---------------------------------------------------------------------------
# Guards: the integration point stays narrow, and stays on the decode surface
# ---------------------------------------------------------------------------


def test_the_seam_never_claims_physical_or_product_evidence() -> None:
    _source, record = dispatch()

    assert record["claim_boundary"] == sx.CLAIM_BOUNDARY
    for key, value in record.items():
        if "claim" in key and isinstance(value, bool):
            assert value is False, key
    for forbidden in ("physical", "browser", "qualification", "promotion"):
        assert forbidden in record["claim_boundary"]


def test_the_router_decode_module_is_the_only_router_module_that_reaches_the_execution() -> None:
    """One narrow seam: no second router module may import the A11 execution."""

    root = Path(__file__).resolve().parents[2]
    importers = sorted(
        path.name
        for path in (root / "mycelium_router").glob("*.py")
        if "mycelium_speculative_decode_execution" in path.read_text(encoding="utf-8", errors="replace")
    )
    assert importers == ["decoding.py"], importers


def test_the_seam_sits_on_the_router_module_the_route_builders_import() -> None:
    """The seam lives on the module the shipped route/decode scripts import."""

    root = Path(__file__).resolve().parents[2]
    importers = sorted(
        str(path.relative_to(root))
        for path in (root / "scripts").glob("*.py")
        if "from mycelium_router.decoding import" in path.read_text(encoding="utf-8", errors="replace")
    )
    assert importers, "no script imports the router decode contract"
    assert decoding.quantized_greedy_token_id is not None
    assert decoding.resolve_speculative_decode is not None
