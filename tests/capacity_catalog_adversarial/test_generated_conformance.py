from __future__ import annotations

import pytest

from tests.capacity_catalog_adversarial.corpus import (
    TRACE_SEEDS,
    generate_trace_corpus,
    trace_to_json,
)
from tests.capacity_catalog_adversarial.model import (
    CatalogReferenceModel,
    minimize_trace,
    run_reference_trace,
)
from tests.capacity_catalog_adversarial.support import (
    build_profile_fixtures,
    first_discrepancy,
    run_production_trace,
)


FIXTURES = build_profile_fixtures()
TRACES = generate_trace_corpus()


def _disagrees(trace):
    expected = run_reference_trace(
        CatalogReferenceModel(max_entries=5, max_ttl=10.0),
        trace,
        FIXTURES,
    )
    observed = run_production_trace(
        trace,
        FIXTURES,
        max_entries=5,
        max_ttl=10.0,
    )
    return expected != observed


@pytest.mark.parametrize(
    ("seed", "trace"),
    tuple(zip(TRACE_SEEDS, TRACES, strict=True)),
    ids=lambda value: f"seed-{value}" if isinstance(value, int) else None,
)
def test_generated_trace_matches_production_catalog(seed: int, trace) -> None:
    expected = run_reference_trace(
        CatalogReferenceModel(max_entries=5, max_ttl=10.0),
        trace,
        FIXTURES,
    )
    observed = run_production_trace(
        trace,
        FIXTURES,
        max_entries=5,
        max_ttl=10.0,
    )

    if observed != expected:
        minimized = minimize_trace(trace, _disagrees)
        minimized_expected = run_reference_trace(
            CatalogReferenceModel(max_entries=5, max_ttl=10.0),
            minimized,
            FIXTURES,
        )
        minimized_observed = run_production_trace(
            minimized,
            FIXTURES,
            max_entries=5,
            max_ttl=10.0,
        )
        difference = first_discrepancy(minimized_expected, minimized_observed)
        pytest.fail(
            "capacity catalog discrepancy\n"
            f"seed={seed}\n"
            f"first_difference={difference}\n"
            f"minimized_trace={trace_to_json(minimized)}"
        )

    assert observed == expected
