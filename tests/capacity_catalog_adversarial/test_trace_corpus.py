from __future__ import annotations

from tests.capacity_catalog_adversarial.corpus import (
    MAX_TRACE_LENGTH,
    MIN_TRACE_LENGTH,
    TRACE_SEEDS,
    generate_trace_corpus,
    trace_to_json,
)
from tests.capacity_catalog_adversarial.model import (
    AdversarialValue,
    CatalogReferenceModel,
    run_reference_trace,
)
from tests.capacity_catalog_adversarial.support import build_profile_fixtures


FIXTURES = build_profile_fixtures()


def test_generated_corpus_is_seeded_deterministic_bounded_and_duplicate_free() -> None:
    first = generate_trace_corpus()
    second = generate_trace_corpus()
    encoded = tuple(trace_to_json(trace) for trace in first)

    assert first == second
    assert len(first) == len(TRACE_SEEDS) == 16
    assert len(set(encoded)) == len(encoded)
    assert min(map(len, first)) == MIN_TRACE_LENGTH == 20
    assert max(map(len, first)) == MAX_TRACE_LENGTH == 20


def test_generated_corpus_covers_required_operations_states_and_adversarial_values() -> None:
    traces = generate_trace_corpus()
    operations = {operation.kind for trace in traces for operation in trace}
    adversarial_values = {
        value
        for trace in traces
        for operation in trace
        for value in (operation.now, operation.ttl)
        if isinstance(value, AdversarialValue)
    }
    codes = {
        observation.code
        for trace in traces
        for observation in run_reference_trace(
            CatalogReferenceModel(max_entries=5, max_ttl=10.0),
            trace,
            FIXTURES,
        )
    }

    assert operations == {"insert", "resolve"}
    assert {
        AdversarialValue.TRUE,
        AdversarialValue.NAN,
        AdversarialValue.POSITIVE_INFINITY,
        AdversarialValue.NEGATIVE_INFINITY,
        AdversarialValue.OVERSIZED_INTEGER,
    } <= adversarial_values
    assert {
        "added",
        "replayed",
        "replaced",
        "current",
        "stale",
        "deprecated",
        "missing",
        "replacement_not_authorized",
        "cas_failed",
        "capacity_exhausted",
        "source_evidence_reused",
        "invalid_time",
        "invalid_ttl",
        "backward_time",
    } <= codes


def test_reference_replay_is_deterministic_for_every_generated_trace() -> None:
    for trace in generate_trace_corpus():
        first = run_reference_trace(
            CatalogReferenceModel(max_entries=5, max_ttl=10.0),
            trace,
            FIXTURES,
        )
        second = run_reference_trace(
            CatalogReferenceModel(max_entries=5, max_ttl=10.0),
            trace,
            FIXTURES,
        )
        assert first == second


def test_trace_serialization_is_stable_reproducible_and_omits_profile_bytes() -> None:
    trace = generate_trace_corpus()[0]
    encoded = trace_to_json(trace)

    assert encoded == trace_to_json(trace)
    assert "canonical_profile_bytes" not in encoded
    assert "source_evidence_digest" not in encoded
    assert FIXTURES["base"].canonical_profile_bytes.decode("utf-8") not in encoded
