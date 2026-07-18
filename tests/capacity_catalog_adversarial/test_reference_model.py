from __future__ import annotations

import ast
from pathlib import Path

from tests.capacity_catalog_adversarial.model import (
    AdversarialValue,
    CatalogReferenceModel,
    Operation,
    ProfileFixture,
    SlotIdentity,
    minimize_trace,
    run_reference_trace,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _fixture(
    profile_id: str,
    digest_character: str,
    source_character: str,
    *,
    runtime_build: str = "runtime-a",
) -> ProfileFixture:
    return ProfileFixture(
        profile_id=profile_id,
        profile_digest=_digest(digest_character),
        source_evidence_digest=_digest(source_character),
        slot=SlotIdentity(
            model_digest=_digest("a"),
            quantization="fp16",
            backend="mlx",
            runtime_build=runtime_build,
            hardware_class="m4-pro-48gb",
            power_mode="ac",
            context_bucket="0-4096",
            kv_mode="stage-local",
        ),
        canonical_profile_bytes=profile_id.encode("ascii"),
    )


def test_reference_model_is_independent_of_production_modules() -> None:
    source_path = Path(__file__).with_name("model.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not {name for name in imported_roots if name.startswith("mycelium")}


def test_reference_model_add_replay_replace_deprecate_lookup_and_exact_ttl() -> None:
    fixtures = {
        "first": _fixture("first", "1", "b"),
        "second": _fixture("second", "2", "c"),
    }
    model = CatalogReferenceModel(max_entries=2, max_ttl=10.0)
    trace = (
        Operation.insert("first", now=0.0, ttl=2.0),
        Operation.insert("first", now=1.0, ttl=10.0),
        Operation.resolve("first", now=2.0),
        Operation.insert(
            "second",
            now=2.0,
            ttl=2.0,
            allow_replacement=True,
            expected_current_profile_id="first",
        ),
        Operation.resolve("first", now=2.0, lookup_profile_id="first"),
        Operation.resolve("second", now=4.0),
    )

    observations = run_reference_trace(model, trace, fixtures)

    assert [item.code for item in observations] == [
        "added",
        "replayed",
        "stale",
        "replaced",
        "deprecated",
        "stale",
    ]
    assert observations[1].inserted_at == 0.0
    assert observations[1].expires_at == 2.0
    assert observations[1].state == "current"
    assert observations[2].state == "stale"
    assert observations[3].state == "current"
    assert observations[4].deprecated_at == 2.0
    assert observations[4].replaced_by_profile_digest == fixtures["second"].profile_digest
    assert all(item.authority_flags == (False, False, False) for item in observations)


def test_reference_model_capacity_is_atomic_and_replay_at_capacity_is_allowed() -> None:
    fixtures = {
        "first": _fixture("first", "1", "b"),
        "other": _fixture("other", "2", "c", runtime_build="runtime-b"),
    }
    model = CatalogReferenceModel(max_entries=1, max_ttl=10.0)
    observations = run_reference_trace(
        model,
        (
            Operation.insert("first", now=0.0, ttl=1.0),
            Operation.insert("first", now=1.0, ttl=10.0),
            Operation.insert("other", now=2.0, ttl=1.0),
            Operation.resolve("first", now=2.0),
        ),
        fixtures,
    )

    assert [item.code for item in observations] == [
        "added",
        "replayed",
        "capacity_exhausted",
        "stale",
    ]
    assert [item.entry_count for item in observations] == [1, 1, 1, 1]


def test_reference_model_fail_closed_time_floor_survives_later_ttl_rejection() -> None:
    fixtures = {"first": _fixture("first", "1", "b")}
    model = CatalogReferenceModel(max_entries=1, max_ttl=10.0)
    observations = run_reference_trace(
        model,
        (
            Operation.insert("first", now=2.0, ttl=AdversarialValue.TRUE),
            Operation.resolve("first", now=AdversarialValue.NAN),
            Operation.resolve("first", now=1.0),
            Operation.resolve("first", now=2.0),
        ),
        fixtures,
    )

    assert [item.code for item in observations] == [
        "invalid_ttl",
        "invalid_time",
        "backward_time",
        "missing",
    ]


def test_trace_minimizer_returns_deterministic_deletion_one_minimal_trace() -> None:
    trace = (
        Operation.resolve("first", now=0.0),
        Operation.insert("first", now=1.0, ttl=1.0),
        Operation.resolve("first", now=0.0),
        Operation.resolve("first", now=2.0),
    )

    def failure(candidate: tuple[Operation, ...]) -> bool:
        return any(
            current.kind == "insert"
            for current in candidate
        ) and any(
            current.kind == "resolve" and current.now == 0.0
            for current in candidate[candidate.index(next(item for item in candidate if item.kind == "insert")) + 1 :]
        )

    first = minimize_trace(trace, failure)
    second = minimize_trace(trace, failure)

    assert first == second
    assert first == (trace[1], trace[2])
    for index in range(len(first)):
        assert not failure(first[:index] + first[index + 1 :])
