from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

import mycelium_capacity_profiles as profiles
import mycelium_capacity_profiles.catalog as catalog_api
from tests.capacity_catalog_adversarial.support import build_profile_fixtures


FIXTURES = build_profile_fixtures()
INVALID_TIMES = (
    pytest.param(True, id="boolean"),
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="positive-infinity"),
    pytest.param(-math.inf, id="negative-infinity"),
    pytest.param(10**10_000, id="oversized-integer"),
)
SLOT_VARIANTS = (
    "slot-model",
    "slot-quantization",
    "slot-backend",
    "slot-runtime",
    "slot-hardware",
    "slot-power",
    "slot-context",
    "slot-kv",
)


def _new_catalog(*, max_entries: int = 8, max_ttl: float = 10.0):
    return catalog_api.CapacityProfileCatalog(
        catalog_api.CapacityProfileCatalogPolicy(
            max_entries=max_entries,
            max_ttl=max_ttl,
        )
    )


def _slot(profile_id: str) -> catalog_api.CapacityProfileSlot:
    identity = FIXTURES[profile_id].slot
    return catalog_api.CapacityProfileSlot(
        model_digest=identity.model_digest,
        quantization=identity.quantization,
        backend=identity.backend,
        runtime_build=identity.runtime_build,
        hardware_class=identity.hardware_class,
        power_mode=identity.power_mode,
        context_bucket=identity.context_bucket,
        kv_mode=identity.kv_mode,
    )


def _noncanonical_candidates(payload: bytes) -> tuple[tuple[str, object], ...]:
    document = json.loads(payload)
    reversed_document = dict(reversed(tuple(document.items())))
    reordered = json.dumps(
        reversed_document,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    with_unknown = dict(document)
    with_unknown["unknown_field"] = False
    unknown = profiles.canonical_json_bytes(with_unknown)
    duplicate = payload.replace(
        b'{"batch_concurrency_limit":1',
        b'{"batch_concurrency_limit":1,"batch_concurrency_limit":1',
        1,
    )
    nonfinite = payload.replace(
        b'"aggregate_output_tps":10.0',
        b'"aggregate_output_tps":NaN',
        1,
    )

    class BytesSubclass(bytes):
        pass

    return (
        ("trailing-newline", payload + b"\n"),
        ("leading-whitespace", b" " + payload),
        ("reordered-keys", reordered),
        ("duplicate-key", duplicate),
        ("nonfinite-json-number", nonfinite),
        ("unknown-field", unknown),
        ("bytes-subclass", BytesSubclass(payload)),
        (
            "oversized-document",
            b"x" * (profiles.MAX_PROFILE_DOCUMENT_BYTES + 1),
        ),
    )


def test_insert_enforces_canonical_document_parser_before_observing_time(monkeypatch) -> None:
    fixture = FIXTURES["base"]
    catalog = _new_catalog()
    calls: list[bytes] = []
    real_parser = catalog_api.parse_capacity_profile_bytes

    def recording_parser(payload: bytes):
        calls.append(payload)
        return real_parser(payload)

    monkeypatch.setattr(catalog_api, "parse_capacity_profile_bytes", recording_parser)

    with pytest.raises(ValueError):
        catalog.insert(fixture.canonical_profile_bytes + b"\n", now=9.0, ttl=1.0)
    inserted = catalog.insert(fixture.canonical_profile_bytes, now=1.0, ttl=1.0)

    assert inserted.action is catalog_api.CatalogInsertAction.ADDED
    assert calls == [fixture.canonical_profile_bytes + b"\n", fixture.canonical_profile_bytes]


def test_catalog_rejects_noncanonical_wrong_type_and_oversized_documents() -> None:
    fixture = FIXTURES["base"]
    candidates = _noncanonical_candidates(fixture.canonical_profile_bytes)

    for name, candidate in candidates:
        catalog = _new_catalog()
        with pytest.raises(ValueError):
            catalog.insert(candidate, now=0.0, ttl=1.0)  # type: ignore[arg-type]
        assert catalog.entry_count == 0, name


def test_exact_max_ttl_is_accepted_next_float_is_rejected_and_expiry_is_exact() -> None:
    fixture = FIXTURES["base"]
    catalog = _new_catalog(max_ttl=10.0)
    inserted = catalog.insert(fixture.canonical_profile_bytes, now=0.0, ttl=10.0)

    with pytest.raises(ValueError, match="TTL"):
        catalog.insert(
            FIXTURES["slot-runtime"].canonical_profile_bytes,
            now=0.0,
            ttl=math.nextafter(10.0, math.inf),
        )

    before = catalog.resolve(_slot("base"), now=math.nextafter(10.0, -math.inf))
    boundary = catalog.resolve(_slot("base"), now=10.0)

    assert inserted.expires_at == 10.0
    assert before.state is catalog_api.CapacityProfileCatalogState.CURRENT
    assert boundary.state is catalog_api.CapacityProfileCatalogState.STALE


@pytest.mark.parametrize("now", INVALID_TIMES)
@pytest.mark.parametrize("operation", ("insert", "resolve"))
def test_insert_and_lookup_reject_adversarial_time_inputs(
    operation: str,
    now: object,
) -> None:
    catalog = _new_catalog()
    fixture = FIXTURES["base"]

    with pytest.raises(ValueError, match="monotonic time"):
        if operation == "insert":
            catalog.insert(fixture.canonical_profile_bytes, now=now, ttl=1.0)
        else:
            catalog.resolve(_slot("base"), now=now)

    assert catalog.entry_count == 0


def test_caller_time_rejects_backward_transition_without_mutating_entry() -> None:
    fixture = FIXTURES["base"]
    catalog = _new_catalog()
    catalog.insert(fixture.canonical_profile_bytes, now=5.0, ttl=5.0)

    with pytest.raises(ValueError, match="move backward"):
        catalog.resolve(_slot("base"), now=4.0)

    current = catalog.resolve(_slot("base"), now=5.0)
    assert current.state is catalog_api.CapacityProfileCatalogState.CURRENT
    assert catalog.entry_count == 1


@pytest.mark.parametrize(
    ("rejection", "message"),
    (
        ("invalid-ttl", "TTL"),
        ("unauthorized", "explicit replacement"),
        ("cas", "compare-and-swap"),
        ("capacity", "capacity exhausted"),
    ),
)
def test_rejected_insert_sets_fail_closed_time_floor_without_entry_mutation(
    rejection: str,
    message: str,
) -> None:
    base = FIXTURES["base"]
    revision = FIXTURES["revision-1"]
    catalog = _new_catalog(max_entries=1 if rejection == "capacity" else 2)
    catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)
    before = catalog.resolve(_slot("base"), now=0.0)

    with pytest.raises(ValueError, match=message):
        if rejection == "invalid-ttl":
            catalog.insert(base.canonical_profile_bytes, now=5.0, ttl=True)
        elif rejection == "unauthorized":
            catalog.insert(revision.canonical_profile_bytes, now=5.0, ttl=1.0)
        elif rejection == "cas":
            catalog.insert(
                revision.canonical_profile_bytes,
                now=5.0,
                ttl=1.0,
                allow_replacement=True,
                expected_current_digest=FIXTURES["slot-model"].profile_digest,
            )
        else:
            catalog.insert(
                FIXTURES["slot-runtime"].canonical_profile_bytes,
                now=5.0,
                ttl=1.0,
            )

    with pytest.raises(ValueError, match="move backward"):
        catalog.resolve(_slot("base"), now=4.0)

    after = catalog.resolve(_slot("base"), now=5.0)
    assert after == before
    assert catalog.entry_count == 1


def test_max_entry_exhaustion_rejects_new_slot_without_eviction() -> None:
    catalog = _new_catalog(max_entries=1)
    base = FIXTURES["base"]
    catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)

    with pytest.raises(ValueError, match="capacity exhausted"):
        catalog.insert(
            FIXTURES["slot-runtime"].canonical_profile_bytes,
            now=1.0,
            ttl=1.0,
        )

    assert catalog.entry_count == 1
    assert catalog.resolve(_slot("base"), now=1.0).profile_digest == base.profile_digest
    assert (
        catalog.resolve(_slot("slot-runtime"), now=1.0).state
        is catalog_api.CapacityProfileCatalogState.MISSING
    )


def test_exact_digest_replay_succeeds_at_capacity_without_extending_stale_entry() -> None:
    catalog = _new_catalog(max_entries=1)
    base = FIXTURES["base"]
    first = catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=1.0)
    replay = catalog.insert(base.canonical_profile_bytes, now=1.0, ttl=10.0)

    assert first.action is catalog_api.CatalogInsertAction.ADDED
    assert replay.action is catalog_api.CatalogInsertAction.REPLAYED
    assert replay.state is catalog_api.CapacityProfileCatalogState.STALE
    assert replay.inserted_at == 0.0
    assert replay.expires_at == 1.0
    assert catalog.entry_count == 1


def test_replacement_at_capacity_fails_without_partial_deprecation() -> None:
    catalog = _new_catalog(max_entries=1)
    base = FIXTURES["base"]
    revision = FIXTURES["revision-1"]
    catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)

    with pytest.raises(ValueError, match="capacity exhausted"):
        catalog.insert(
            revision.canonical_profile_bytes,
            now=1.0,
            ttl=1.0,
            allow_replacement=True,
            expected_current_digest=base.profile_digest,
        )

    lookup = catalog.resolve(_slot("base"), now=1.0)
    assert lookup.profile_digest == base.profile_digest
    assert lookup.state is catalog_api.CapacityProfileCatalogState.CURRENT
    assert lookup.deprecated_at is None
    assert lookup.replaced_by_profile_digest is None


def test_digest_collision_simulation_fails_closed_without_mutation(monkeypatch) -> None:
    catalog = _new_catalog(max_entries=2)
    base = FIXTURES["base"]
    parsed = profiles.parse_capacity_profile_bytes(base.canonical_profile_bytes)
    catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)
    collision_payload = b"simulated-canonical-digest-collision"
    collision_profile = SimpleNamespace(
        key=parsed.key,
        profile_digest=base.profile_digest,
    )
    real_parser = catalog_api.parse_capacity_profile_bytes

    def collision_parser(payload: bytes):
        if payload == collision_payload:
            return collision_profile
        return real_parser(payload)

    monkeypatch.setattr(catalog_api, "parse_capacity_profile_bytes", collision_parser)

    with pytest.raises(ValueError, match="digest collision"):
        catalog.insert(collision_payload, now=1.0, ttl=1.0)

    lookup = catalog.resolve(_slot("base"), now=1.0)
    assert catalog.entry_count == 1
    assert lookup.profile_digest == base.profile_digest
    assert lookup.canonical_profile_bytes == base.canonical_profile_bytes
    assert lookup.state is catalog_api.CapacityProfileCatalogState.CURRENT


def test_three_revision_replacement_lineage_remains_direct_and_immutable() -> None:
    catalog = _new_catalog(max_entries=3)
    base = FIXTURES["base"]
    first = FIXTURES["revision-1"]
    second = FIXTURES["revision-2"]
    catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)
    catalog.insert(
        first.canonical_profile_bytes,
        now=1.0,
        ttl=10.0,
        allow_replacement=True,
        expected_current_digest=base.profile_digest,
    )
    catalog.insert(
        second.canonical_profile_bytes,
        now=2.0,
        ttl=10.0,
        allow_replacement=True,
        expected_current_digest=first.profile_digest,
    )

    oldest = catalog.resolve(_slot("base"), now=2.0, profile_digest=base.profile_digest)
    middle = catalog.resolve(_slot("base"), now=2.0, profile_digest=first.profile_digest)
    current = catalog.resolve(_slot("base"), now=2.0)

    assert oldest.state is catalog_api.CapacityProfileCatalogState.DEPRECATED
    assert oldest.deprecated_at == 1.0
    assert oldest.replaced_by_profile_digest == first.profile_digest
    assert middle.state is catalog_api.CapacityProfileCatalogState.DEPRECATED
    assert middle.deprecated_at == 2.0
    assert middle.replaced_by_profile_digest == second.profile_digest
    assert current.profile_digest == second.profile_digest
    assert current.replaced_by_profile_digest is None


def test_compare_and_swap_rejects_stale_lineage_digest() -> None:
    catalog = _new_catalog(max_entries=3)
    base = FIXTURES["base"]
    first = FIXTURES["revision-1"]
    second = FIXTURES["revision-2"]
    catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)
    catalog.insert(
        first.canonical_profile_bytes,
        now=1.0,
        ttl=10.0,
        allow_replacement=True,
        expected_current_digest=base.profile_digest,
    )

    with pytest.raises(ValueError, match="compare-and-swap"):
        catalog.insert(
            second.canonical_profile_bytes,
            now=2.0,
            ttl=10.0,
            allow_replacement=True,
            expected_current_digest=base.profile_digest,
        )

    assert catalog.entry_count == 2
    assert catalog.resolve(_slot("base"), now=2.0).profile_digest == first.profile_digest


@pytest.mark.parametrize("variant_id", SLOT_VARIANTS)
def test_every_operational_slot_dimension_isolated_from_other_slots(variant_id: str) -> None:
    catalog = _new_catalog(max_entries=2)
    base = FIXTURES["base"]
    variant = FIXTURES[variant_id]

    first = catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)
    second = catalog.insert(variant.canonical_profile_bytes, now=0.0, ttl=10.0)

    assert _slot("base") != _slot(variant_id)
    assert first.action is catalog_api.CatalogInsertAction.ADDED
    assert second.action is catalog_api.CatalogInsertAction.ADDED
    assert catalog.entry_count == 2
    assert catalog.resolve(_slot("base"), now=0.0).profile_digest == base.profile_digest
    assert catalog.resolve(_slot(variant_id), now=0.0).profile_digest == variant.profile_digest


def test_source_evidence_digest_is_excluded_from_slot_but_isolates_revisions() -> None:
    base = FIXTURES["base"]
    revision = FIXTURES["revision-1"]

    assert _slot("base") == _slot("revision-1")
    assert base.source_evidence_digest != revision.source_evidence_digest
    assert base.profile_digest != revision.profile_digest
    assert not hasattr(_slot("base"), "source_evidence_digest")


def test_replacement_rejects_changed_claims_reusing_source_evidence_digest() -> None:
    catalog = _new_catalog(max_entries=2)
    base = FIXTURES["base"]
    reused = FIXTURES["same-source-claims"]
    assert base.profile_digest != reused.profile_digest
    assert base.source_evidence_digest == reused.source_evidence_digest
    catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)

    with pytest.raises(ValueError, match="source-evidence digest"):
        catalog.insert(
            reused.canonical_profile_bytes,
            now=1.0,
            ttl=10.0,
            allow_replacement=True,
            expected_current_digest=base.profile_digest,
        )

    assert catalog.entry_count == 1
    assert catalog.resolve(_slot("base"), now=1.0).profile_digest == base.profile_digest


def test_catalog_results_and_documents_never_grant_activation_authority() -> None:
    catalog = _new_catalog(max_entries=2)
    base = FIXTURES["base"]
    revision = FIXTURES["revision-1"]
    added = catalog.insert(base.canonical_profile_bytes, now=0.0, ttl=10.0)
    replayed = catalog.insert(base.canonical_profile_bytes, now=0.5, ttl=10.0)
    replaced = catalog.insert(
        revision.canonical_profile_bytes,
        now=1.0,
        ttl=10.0,
        allow_replacement=True,
        expected_current_digest=base.profile_digest,
    )
    deprecated = catalog.resolve(
        _slot("base"),
        now=1.0,
        profile_digest=base.profile_digest,
    )
    current = catalog.resolve(_slot("base"), now=1.0)

    for result in (added, replayed, replaced, deprecated, current):
        assert result.route_ready is False
        assert result.release_ready is False
        assert result.qualification_evaluated is False
    for fixture in (base, revision):
        document = profiles.parse_capacity_profile_bytes(
            fixture.canonical_profile_bytes
        ).to_document()
        assert document["route_ready"] is False
        assert document["release_ready"] is False
        assert document["qualification_evaluated"] is False
    for forbidden_method in ("activate", "admit", "route", "qualify", "release"):
        assert not hasattr(catalog, forbidden_method)
