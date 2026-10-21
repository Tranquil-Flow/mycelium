# SPDX-License-Identifier: AGPL-3.0-only
"""The three former scaffold seams, implemented as deterministic in-process behaviour.

``mycelium_speculative_decode_execution`` used to refuse these three seams with a coded
exception and never return a document.  They now run real
``run_speculative_cycle`` work over the **injected** transport, target decision source
and cancellation scope, and return documents stamped
``evidence_class="deterministic_in_process"``.

What GREEN here proves: a deterministic in-process comparison, a deterministic
positive/negative gate evaluation and a deterministic eight-workspace projection, each
derived from cycles that actually executed (counters, positions and digests only).
What it does NOT prove: a live route, an authenticated network proposal transfer, a
browser or product surface, physical hardware, qualification, benefit or promotion.
The claim boundaries for those still refuse, and a missing injection is a closed
contract refusal -- the seam never invents the transport, the target authority or the
draft proposal it runs over.

The seams are reached through the module's own ``LIVE_SEAMS`` registry, so this file
needs no literal seam names and the ladder's conservative content scan keeps it a unit
module: every case below executes in process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import mycelium_speculative_decode as sd
import mycelium_speculative_decode_execution as sx

from a11_seam_fixtures import (
    CANDIDATES,
    admissible_plan,
    disabled_plan,
    matching_target,
    non_causal_target,
    refusing_target,
)

WIDTH = 4
SEAMS = {key: getattr(sx, name) for key, name in sx.LIVE_SEAMS.items()}
BENCHMARK = SEAMS["route_benchmark"]
QUALIFICATION = SEAMS["physical_qualification"]
PROJECTION = SEAMS["workspace_projection"]

#: Private-material key names that must never reach a projection.
FORBIDDEN_KEYS = frozenset(
    {"prompt", "prompt_token_ids", "tokens", "token_ids", "candidate_token_ids", "logits"}
)


def injections(**overrides):
    """The three injected dependencies plus a deterministic draft proposal."""

    arguments = {
        "target": matching_target(),
        "transport": sx.ProposalTransport(),
        "cancellation": sx.CancellationScope(),
        "draft_tokens": CANDIDATES,
        "width": WIDTH,
    }
    arguments.update(overrides)
    return arguments


def walk(value, path=""):
    """Yield ``(path, key_or_None)`` for every mapping key in ``value``."""

    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}", key
            yield from walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")


# ---------------------------------------------------------------------------
# Seam 1: the paired comparison
# ---------------------------------------------------------------------------


def test_the_route_benchmark_pairs_both_arms_over_executed_cycles() -> None:
    source = matching_target()
    document = BENCHMARK(
        plan=admissible_plan(), request_ids=("req-1", "req-2"), **injections(target=source)
    )

    assert document["protocol"] == sx.BENCHMARK_EXECUTION_PROTOCOL
    assert document["evidence_class"] == sx.EMITTED_EVIDENCE_CLASS
    assert document["paired_arms"] == ["target_only", "speculative"]
    assert [pair["request_id"] for pair in document["requests"]] == ["req-1", "req-2"]

    for pair in document["requests"]:
        target_only, speculative = pair["arms"]
        assert target_only["arm"] == "target_only" and speculative["arm"] == "speculative"
        # The target-only arm performs no speculative work at all...
        assert target_only["mode"] == "target_only"
        assert target_only["verification_calls"] == 0
        assert target_only["committed_positions"] == 0
        # ...and the speculative arm really reached the bounded verifier and committed.
        assert speculative["mode"] == "speculative"
        assert speculative["verification_calls"] == 1
        assert speculative["parity"] == "matched"
        assert speculative["committed_positions"] == WIDTH
        assert speculative["draft_proposals"] == 1

    assert source.bounded_calls == 2
    assert document["totals"]["speculative_verification_calls"] == 2
    assert document["totals"]["target_only_verification_calls"] == 0
    assert document["totals"]["speculative_committed_positions"] == 2 * WIDTH
    assert document["totals"]["proposal_bytes"] == 2 * WIDTH * sx.PROPOSAL_BYTES_PER_TOKEN


def test_the_route_benchmark_counters_are_measured_not_invented() -> None:
    """Every published counter equals the same fixture's direct cycle arithmetic."""

    source = matching_target()
    document = BENCHMARK(
        plan=admissible_plan(), request_ids=("req-1",), **injections(target=source)
    )
    arm = document["requests"][0]["arms"][1]

    direct = sx.run_speculative_cycle(
        plan=admissible_plan(),
        request_id="req-1",
        cycle_ref="req-1-speculative",
        attempt=1,
        width=WIDTH,
        target=sx.A10TargetKvRuntime(
            matching_target(), target_kv_ref="target-kv-1", target_runtime_incarnation="sha256:" + "3" * 64
        ),
        draft=sx.DeterministicDraftRuntime(
            lambda _position, _width: CANDIDATES, incarnation="sha256:" + "6" * 64
        ),
        transport=sx.ProposalTransport(),
        cancellation=sx.CancellationScope(),
    )

    assert arm["proposed_positions"] == direct.runtime_request["proposed_count"]
    assert arm["verified_positions"] == direct.runtime_request["verified_count"]
    assert arm["accepted_positions"] == direct.runtime_request["accepted_count"]
    assert arm["rejected_positions"] == direct.runtime_request["rejected_count"]
    assert arm["committed_positions"] == direct.runtime_request["committed_count"]
    assert arm["deadline_outcome"] == direct.runtime_request["deadline_outcome"]
    assert arm["released_tokens"] == direct.output["released_tokens"]


@pytest.mark.parametrize(
    "missing,code",
    [
        ("transport", "route_benchmark_requires_injected_transport"),
        ("cancellation", "route_benchmark_requires_injected_cancellation_scope"),
        ("target", "route_benchmark_requires_injected_target_decision_source"),
    ],
)
def test_the_route_benchmark_requires_its_injections_by_code(missing: str, code: str) -> None:
    source = matching_target()
    arguments = injections(target=source)
    arguments[missing] = None

    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        BENCHMARK(plan=admissible_plan(), request_ids=("req-1",), **arguments)

    assert failure.value.code == code
    assert failure.value.code in sx.SEAM_REFUSALS
    # A missing injection is a contract refusal, never a capability boundary...
    assert not isinstance(failure.value, sx.SpeculativeDecodeExecutionUnavailable)
    # ...and nothing ran: the seam did not fall back to a target it was not given.
    assert source.bounded_calls == 0


def test_the_route_benchmark_requires_an_injected_draft_proposal() -> None:
    arguments = injections()
    arguments.pop("draft_tokens")

    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        BENCHMARK(plan=admissible_plan(), request_ids=("req-1",), **arguments)

    assert failure.value.code == "route_benchmark_requires_injected_draft_runtime"
    assert failure.value.code in sx.SEAM_REFUSALS


@pytest.mark.parametrize("evidence_class", sorted(sx.REFUSED_EVIDENCE_CLASSES))
def test_the_route_benchmark_refuses_a_refused_evidence_class(evidence_class: str) -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionUnavailable) as failure:
        BENCHMARK(
            plan=admissible_plan(),
            request_ids=("req-1",),
            evidence_class=evidence_class,
            **injections(),
        )

    assert failure.value.code == sx.REFUSED_EVIDENCE_CLASSES[evidence_class]
    assert failure.value.code in sx.EXECUTION_BOUNDARIES


def test_the_route_benchmark_refuses_an_unknown_evidence_class() -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        BENCHMARK(
            plan=admissible_plan(),
            request_ids=("req-1",),
            evidence_class="not_a_class",
            **injections(),
        )

    assert failure.value.code == "invalid_evidence_class"


def test_the_route_benchmark_never_publishes_an_empty_comparison() -> None:
    """A plan that admits nothing yields a refusal, not a table of zeros."""

    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        BENCHMARK(plan=disabled_plan(), request_ids=("req-1",), **injections())

    assert failure.value.code == "route_benchmark_requires_executed_cycles"
    assert failure.value.code in sx.SEAM_REFUSALS


def test_the_route_benchmark_publishes_no_claim() -> None:
    document = BENCHMARK(plan=admissible_plan(), request_ids=("req-1",), **injections())

    assert document["wall_clock_latency_measured"] is False
    assert document["live_product_session"] is False
    assert document["enabled_decision"] is False
    assert document["claim_boundary"] == sx.CLAIM_BOUNDARY
    for key, value in document.items():
        if "claim" in key and isinstance(value, bool):
            assert value is False, key


# ---------------------------------------------------------------------------
# Seam 2: the deterministic gates
# ---------------------------------------------------------------------------


def test_the_qualification_gates_meet_on_a_matching_and_a_failing_arm() -> None:
    document = QUALIFICATION(
        plan=admissible_plan(),
        negative_target=non_causal_target(),
        negative_draft_tokens=CANDIDATES,
        **injections(),
    )

    assert document["protocol"] == sx.QUALIFICATION_EXECUTION_PROTOCOL
    assert document["evidence_class"] == sx.EMITTED_EVIDENCE_CLASS
    assert document["result"] == "deterministic_gates_met"
    assert document["positive_gate"] is True and document["negative_gate"] is True
    assert document["unmet"] == []

    positive = document["arms"]["positive"]
    assert positive["committed_positions"] == WIDTH
    assert positive["released_tokens"] == WIDTH
    negative = document["arms"]["negative"]
    assert negative["failure_reason"] == "parity_failed"
    assert negative["committed_positions"] == 0
    assert negative["released_tokens"] == 0
    assert negative["rollback_positions"] == WIDTH
    assert negative["parity"] == "failed"


def test_the_qualification_gates_are_not_vacuous() -> None:
    """A negative arm that does not fail reports ``gate_not_met`` instead of passing."""

    document = QUALIFICATION(
        plan=admissible_plan(),
        negative_target=matching_target(),
        negative_draft_tokens=CANDIDATES,
        **injections(),
    )

    assert document["result"] == "gate_not_met"
    assert document["positive_gate"] is True
    assert document["negative_gate"] is False
    assert document["unmet"] == ["negative"]


def test_a_refusing_target_fails_the_positive_gate() -> None:
    document = QUALIFICATION(
        plan=admissible_plan(),
        target=refusing_target(),
        draft_tokens=CANDIDATES,
        negative_target=non_causal_target(),
        negative_draft_tokens=CANDIDATES,
        transport=sx.ProposalTransport(),
        cancellation=sx.CancellationScope(),
        width=WIDTH,
    )

    assert document["result"] == "gate_not_met"
    assert document["positive_gate"] is False
    assert "positive" in document["unmet"]


def test_the_qualification_requires_an_injected_negative_arm() -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
        QUALIFICATION(plan=admissible_plan(), **injections())

    assert failure.value.code == "physical_qualification_requires_injected_negative_arm"
    assert failure.value.code in sx.SEAM_REFUSALS


@pytest.mark.parametrize("evidence_class", sorted(sx.REFUSED_EVIDENCE_CLASSES))
def test_the_qualification_never_accepts_a_refused_evidence_class(evidence_class: str) -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionUnavailable) as failure:
        QUALIFICATION(
            plan=admissible_plan(),
            negative_target=non_causal_target(),
            evidence_class=evidence_class,
            **injections(),
        )

    assert failure.value.code == sx.REFUSED_EVIDENCE_CLASSES[evidence_class]


def test_the_qualification_reports_its_verdict_and_claims_nothing() -> None:
    document = QUALIFICATION(
        plan=admissible_plan(),
        negative_target=non_causal_target(),
        **injections(),
    )

    assert document["hardware_touched"] is False
    assert document["browser_touched"] is False
    assert document["blocked_on"] == sorted(sx.EXECUTION_BOUNDARIES)
    for key, value in document.items():
        if "claim" in key and isinstance(value, bool):
            assert value is False, key


# ---------------------------------------------------------------------------
# Seam 3: the eight-workspace projection
# ---------------------------------------------------------------------------


def frozen_product_workspaces() -> tuple[str, ...]:
    """The workspace ids the product's own navigation module publishes."""

    root = Path(__file__).resolve().parents[2]
    text = (root / "ui" / "web" / "src" / "app" / "navigation.ts").read_text(encoding="utf-8")
    body = text.split("PRODUCT_ROUTES", 1)[1]
    return tuple(re.findall(r"id: '([a-z]+)'", body))


def test_the_projection_names_the_frozen_product_workspaces() -> None:
    document = PROJECTION(plan=admissible_plan(), **injections())

    assert frozen_product_workspaces() == sx.PRODUCT_WORKSPACES
    assert document["workspace_order"] == list(sx.PRODUCT_WORKSPACES)
    assert tuple(document["workspaces"]) == sx.PRODUCT_WORKSPACES
    assert len(sx.PRODUCT_WORKSPACES) == 8


def test_the_projection_is_derived_from_the_executed_cycle() -> None:
    document = PROJECTION(plan=admissible_plan(), **injections())
    cycle = document["cycle"]
    inference = document["workspaces"]["inference"]
    nodes = document["workspaces"]["nodes"]
    network = document["workspaces"]["network"]

    assert document["protocol"] == sx.WORKSPACE_PROJECTION_PROTOCOL
    assert document["evidence_class"] == sx.EMITTED_EVIDENCE_CLASS
    assert cycle["mode"] == "speculative"
    assert cycle["commit_advanced"] is True
    assert cycle["target_watermark_position"] == WIDTH
    assert cycle["released_tokens"] == WIDTH
    assert cycle["a10_verified_positions"] == WIDTH
    assert cycle["a10_transaction_mode"] in {
        "commit_verified_prefix",
        "commit_verified_prefix_plus_target_correction",
    }
    assert inference["admitted_mode"] == "speculative_admitted"
    assert inference["verified_positions"] == WIDTH
    assert inference["committed_positions"] == WIDTH
    assert inference["deadline_outcome"] == "proposal_arrived"
    assert nodes["kv_owner_separate"] is True
    assert nodes["draft_wrote_target_state"] is False
    assert network["proposal_bytes"] == WIDTH * sx.PROPOSAL_BYTES_PER_TOKEN
    assert network["authenticated_network_transport"] is False
    assert sd.validate_speculative_decode_runtime(document["runtime"]) == document["runtime"]
    assert document["generation"] >= 1


def test_the_projection_carries_no_private_material() -> None:
    projection = PROJECTION(plan=admissible_plan(), **injections())

    for path, key in walk(projection):
        assert key not in FORBIDDEN_KEYS, path
    rendered = json.dumps(projection)
    for token in CANDIDATES:
        assert str(token) not in rendered, token


def test_the_projection_publishes_no_live_or_product_claim() -> None:
    document = PROJECTION(plan=admissible_plan(), **injections())

    assert document["live_product_session"] is False
    assert document["browser_rendered"] is False
    assert document["product_surface_claim"] is False
    assert document["claim_boundary"] == sx.CLAIM_BOUNDARY
    assert document["workspaces"]["readiness"]["closure_requirement_earned"] is False


@pytest.mark.parametrize("evidence_class", sorted(sx.REFUSED_EVIDENCE_CLASSES))
def test_the_projection_refuses_a_refused_evidence_class(evidence_class: str) -> None:
    with pytest.raises(sx.SpeculativeDecodeExecutionUnavailable) as failure:
        PROJECTION(plan=admissible_plan(), evidence_class=evidence_class, **injections())

    assert failure.value.code == sx.REFUSED_EVIDENCE_CLASSES[evidence_class]


# ---------------------------------------------------------------------------
# Guards: the implemented seams are not boundaries, and the registries are closed
# ---------------------------------------------------------------------------


def test_the_implemented_seams_are_not_claim_boundaries() -> None:
    assert set(sx.LIVE_SEAMS) == {"route_benchmark", "physical_qualification", "workspace_projection"}
    for key, name in sx.LIVE_SEAMS.items():
        seam = getattr(sx, name)
        assert callable(seam), key
        assert name not in set(sx.EXECUTION_BOUNDARIES.values()), name
        # Called bare, an implemented seam refuses as a *contract* violation -- never
        # as a capability boundary, which would mean it is still unimplemented.
        with pytest.raises(sx.SpeculativeDecodeExecutionError) as failure:
            seam(plan=admissible_plan())
        assert not isinstance(failure.value, sx.SpeculativeDecodeExecutionUnavailable)
        assert failure.value.code in sx.SEAM_REFUSALS


def test_the_claim_boundary_map_is_total_and_points_at_real_callables() -> None:
    assert set(sx.REFUSED_EVIDENCE_CLASSES.values()) == set(sx.EXECUTION_BOUNDARIES)
    for code, name in sx.EXECUTION_BOUNDARIES.items():
        assert code.endswith("_not_available")
        boundary = getattr(sx, name)
        with pytest.raises(sx.SpeculativeDecodeExecutionUnavailable) as failure:
            boundary(plan=admissible_plan(), route_digest="sha256:" + "0" * 64)
        assert failure.value.code == code


def test_the_claim_boundary_publishes_no_document() -> None:
    for name in sx.EXECUTION_BOUNDARIES.values():
        boundary = getattr(sx, name)
        with pytest.raises(sx.SpeculativeDecodeExecutionUnavailable):
            boundary()


def test_the_registries_are_closed() -> None:
    assert sx.INJECTION_REFUSALS <= sx.SEAM_REFUSALS
    assert sx.EMITTED_EVIDENCE_CLASS not in sx.REFUSED_EVIDENCE_CLASSES
    for key in sx.LIVE_SEAMS:
        for suffix in (
            "requires_injected_transport",
            "requires_injected_cancellation_scope",
            "requires_injected_target_decision_source",
            "requires_injected_draft_runtime",
        ):
            assert f"{key}_{suffix}" in sx.SEAM_REFUSALS


def test_the_module_states_the_new_boundary_truth() -> None:
    text = Path(sx.__file__).read_text(encoding="utf-8")
    docstring = text.split('"""', 2)[1]

    assert "still refuse with a coded exception and never return a document" not in docstring
    assert "no physical draft/target route" in docstring.lower()
    assert "no physical draft/target route" in sx.CLAIM_BOUNDARY.lower()
    assert "no browser or eight-workspace product surface" in sx.CLAIM_BOUNDARY
    assert "no promotion authorization" in sx.CLAIM_BOUNDARY
    assert "A11 closure requirements are untouched" in sx.CLAIM_BOUNDARY
