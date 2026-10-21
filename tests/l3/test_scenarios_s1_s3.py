# SPDX-License-Identifier: AGPL-3.0-only
"""L3: the S1/S2/S3 scenario capability on the shipped request plane (PLAN v2 §2.3).

One test per scenario.  Each one runs the scenario through the same path the CLI
uses (``mycelium_testbed.l3_scenarios``), writes its own JSON evidence record into
``tmp_path``, and then asserts the verdict the scenario recorded - so a scenario
that goes RED fails here with the exact unmet requirement, instead of being
retried until it looks green.

The preconditions are asserted as well, and before the verdict: the two iroh
sidecars must be real native child processes, the transports must be the shipped
``IrohTransport``, and the route gate must refuse an unqualified POST.  A RED
below therefore cannot be an artefact of a harness that quietly degraded into a
double.

Nothing here is the M2 fix: no worker replacement is implemented, and
``mycelium_a13_macos`` is untouched.  The executor under the Router is the
deterministic stub (see ``research/l3-scenarios-s1-s3.md``, "real vs simulated"),
so a pass here is a statement about the shipped *request plane*, not about a real
model's tokens.
"""
from __future__ import annotations

import json

import pytest

from mycelium_testbed.l3_scenarios import (
    MAX_NEW_TOKENS,
    RUNNERS,
    SCENARIOS,
    local_model_facts,
)

pytestmark = pytest.mark.emulation


def _labelling_is_explicit(record: dict) -> None:
    """A verdict is only usable if the record says what was real and what was not."""

    assert record["model"]["loaded_by_this_capability"] is False, record["model"]
    assert record["model"]["executor_actually_used"].startswith(
        "tests/e2e_request_iroh/harness.py"
    ), record["model"]
    assert record["real"] and record["simulated"] and record["not_covered"]
    assert record["repository"]["commit"], record["repository"]


def _plane_is_real(record: dict) -> None:
    """The shipped path really ran: real native children, real transports, real gate."""

    infrastructure = record["infrastructure"]
    assert infrastructure["sidecar_is_native_child_process"] is True, infrastructure
    assert infrastructure["sidecar_binary_digest"], infrastructure
    assert infrastructure["transport_types"] == ["IrohTransport", "IrohTransport"], (
        infrastructure
    )
    assert infrastructure["production_router_count"] == 2, infrastructure
    assert infrastructure["unqualified_post"]["refused_as_expected"] is True, infrastructure


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_scenario_records_an_honest_verdict(tmp_path, scenario: str) -> None:
    record, evidence = RUNNERS[scenario](
        run_id=f"l3-pytest-{scenario.lower()}", evidence_dir=tmp_path
    )
    print(json.dumps({"scenario": scenario, "verdict": record["verdict"],
                      "counts": record["counts"], "gaps": record["gaps"],
                      "evidence": str(evidence)}, indent=2, default=str))

    _labelling_is_explicit(record)
    _plane_is_real(record)
    assert record["gaps"] == [], (
        f"L3 {scenario} ({SCENARIOS[scenario].title}) did not meet its own recorded "
        f"requirements on commit {record['repository']['commit']}:\n"
        + json.dumps(record["gaps"], indent=2)
        + f"\nevidence: {evidence}"
    )
    assert record["verdict"] == "GREEN", record


def test_the_0_5b_facts_are_measured_not_asserted() -> None:
    """The model labelling is a measurement: present on disk, never claimed as loaded."""

    facts = local_model_facts()
    assert facts["loaded_by_this_capability"] is False, facts
    assert facts["probe"].startswith("local HuggingFace hub cache"), facts
    if facts["real_0_5b_present_locally"]:
        chosen = next(
            item for item in facts["candidates"] if item["model_id"] == facts["selected"]
        )
        assert chosen["weights_bytes"] > 0, chosen
        assert chosen["snapshot"], chosen
    assert MAX_NEW_TOKENS >= 1
