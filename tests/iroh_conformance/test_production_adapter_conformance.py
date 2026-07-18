# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model-based production Router-to-iroh adapter conformance tests."""

from __future__ import annotations

from dataclasses import fields

import pytest

from mycelium_iroh_conformance import (
    AdapterAction,
    REQUIRED_SCENARIOS,
    generate_bounded_traces,
)
from mycelium_iroh_conformance.checker import (
    check_trace,
    counterexample_json,
    minimize_discrepancy,
)
from mycelium_iroh_conformance.production import ProductionSnapshot


_OBSERVABLE_FIELDS = (
    "lifecycle",
    "router_bound",
    "installed_client_roles",
    "closed_client_count",
    "closed_replacement_count",
    "pending_receipt_ids",
    "queue_permits",
    "generation",
    "dispatch_count",
    "ack_count",
    "cancellation_count",
    "fatal_error",
    "evidence",
)


def _assert_trace_conforms(trace: tuple[AdapterAction, ...]) -> None:
    difference = check_trace(trace)
    if difference is None:
        return
    minimized = minimize_discrepancy(trace, difference)
    minimized_difference = check_trace(minimized)
    assert minimized_difference is not None
    pytest.fail(
        "production/reference discrepancy; minimized stable counterexample:\n"
        + counterexample_json(minimized, minimized_difference),
        pytrace=False,
    )


def test_projection_compares_every_requested_observable() -> None:
    assert tuple(field.name for field in fields(ProductionSnapshot)) == _OBSERVABLE_FIELDS


@pytest.mark.parametrize(
    "scenario",
    REQUIRED_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_required_scenario_matches_after_every_action(scenario) -> None:
    # check_trace creates and tears down a fresh production fixture per trace.
    _assert_trace_conforms(scenario.actions)


def test_deterministic_bounded_core_traces_match_after_every_action() -> None:
    alphabet = tuple(
        AdapterAction(name)
        for name in (
            "close",
            "restart",
            "queue_send",
            "send_confirmed",
            "rotate_peer",
        )
    )
    traces = generate_bounded_traces(maximum_tail_depth=2, actions=alphabet)
    assert len(traces) == 31 + 5
    assert traces == generate_bounded_traces(maximum_tail_depth=2, actions=alphabet)
    for trace in traces:
        _assert_trace_conforms(trace)
