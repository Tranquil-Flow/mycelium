from __future__ import annotations

import importlib
import inspect
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import mycelium_capacity_profiles as profiles


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def _catalog_api():
    return importlib.import_module("mycelium_capacity_profiles.catalog")


def _init_api():
    return importlib.import_module("mycelium_capacity_profiles.init")


def _profile(
    *,
    source_evidence_digest: str = DIGEST_B,
    runtime_build: str = "mlx-0.29.0",
    tpot_slo_ms: float = 100.0,
) -> profiles.CapacityProfile:
    key = profiles.CapacityProfileKey(
        model_digest=DIGEST_A,
        source_evidence_digest=source_evidence_digest,
        quantization="fp16",
        backend="mlx",
        runtime_build=runtime_build,
        hardware_class="apple-m4-pro-48gb",
        power_mode="ac",
        context_bucket="0-4096",
        kv_mode="stage-local",
    )
    policy = profiles.CapacityProfilePolicy(
        ttft_p95_slo_ms=1_000.0,
        tpot_p95_slo_ms=tpot_slo_ms,
        min_samples=3,
    )
    observation = profiles.CapacityObservation(
        concurrency=1,
        sample_count=5,
        p95_ttft_ms=500.0,
        p95_tpot_ms=50.0,
        aggregate_output_tps=10.0,
        peak_memory_bytes=10,
        memory_budget_bytes=100,
    )
    return profiles.compile_capacity_profile(key, (observation,), policy)


def _new_catalog(*, max_entries: int = 8, max_ttl: float = 100.0):
    api = _catalog_api()
    return api.CapacityProfileCatalog(
        api.CapacityProfileCatalogPolicy(
            max_entries=max_entries,
            max_ttl=max_ttl,
        )
    )


def _slot(profile: profiles.CapacityProfile):
    return _catalog_api().CapacityProfileSlot.from_profile(profile)


def test_catalog_policy_is_explicit_immutable_and_validated() -> None:
    api = _catalog_api()
    policy = api.CapacityProfileCatalogPolicy(max_entries=3, max_ttl=10.0)

    assert policy.max_entries == 3
    assert policy.max_ttl == 10.0
    with pytest.raises(FrozenInstanceError):
        policy.max_entries = 4


@pytest.mark.parametrize("max_entries", [True, False, 0, -1, 1.0, "1"])
def test_catalog_policy_rejects_invalid_entry_bounds(max_entries: object) -> None:
    api = _catalog_api()
    with pytest.raises(ValueError, match="max_entries"):
        api.CapacityProfileCatalogPolicy(max_entries=max_entries, max_ttl=10.0)


@pytest.mark.parametrize(
    "max_ttl",
    [True, False, 0, -1, math.nan, math.inf, -math.inf, "10"],
)
def test_catalog_policy_rejects_invalid_ttl_bounds(max_ttl: object) -> None:
    api = _catalog_api()
    with pytest.raises(ValueError, match="max_ttl"):
        api.CapacityProfileCatalogPolicy(max_entries=3, max_ttl=max_ttl)


def test_every_insert_passes_through_canonical_document_parser(monkeypatch) -> None:
    api = _catalog_api()
    catalog = _new_catalog()
    payload = _profile().canonical_json_bytes()
    calls: list[bytes] = []
    real_parser = profiles.parse_capacity_profile_bytes

    def recording_parser(candidate: bytes) -> profiles.CapacityProfile:
        calls.append(candidate)
        return real_parser(candidate)

    monkeypatch.setattr(api, "parse_capacity_profile_bytes", recording_parser)

    catalog.insert(payload, now=1.0, ttl=10.0)
    assert calls == [payload]

    with pytest.raises(ValueError):
        catalog.insert(b"{}", now=2.0, ttl=10.0)
    assert calls == [payload, b"{}"]


def test_insert_rejects_noncanonical_or_nonexact_bytes() -> None:
    catalog = _new_catalog()
    payload = _profile().canonical_json_bytes()

    class BytesSubclass(bytes):
        pass

    for candidate in (
        _profile(),
        payload + b"\n",
        BytesSubclass(payload),
    ):
        with pytest.raises(ValueError):
            catalog.insert(candidate, now=1.0, ttl=10.0)  # type: ignore[arg-type]
    assert catalog.entry_count == 0


def test_operational_slot_excludes_source_evidence_revision_only() -> None:
    first = _profile(source_evidence_digest=DIGEST_B)
    revision = _profile(source_evidence_digest=DIGEST_C)
    different_runtime = _profile(
        source_evidence_digest=DIGEST_C,
        runtime_build="mlx-0.29.1",
    )

    first_slot = _slot(first)
    assert first_slot == _slot(revision)
    assert first_slot != _slot(different_runtime)
    assert not hasattr(first_slot, "source_evidence_digest")
    assert first.profile_digest != revision.profile_digest


@pytest.mark.parametrize("now", [True, False, -1, math.nan, math.inf, -math.inf, "1"])
def test_insert_rejects_invalid_monotonic_time(now: object) -> None:
    catalog = _new_catalog()
    with pytest.raises(ValueError, match="monotonic time"):
        catalog.insert(_profile().canonical_json_bytes(), now=now, ttl=10.0)
    assert catalog.entry_count == 0


@pytest.mark.parametrize(
    "ttl",
    [True, False, 0, -1, math.nan, math.inf, -math.inf, 100.1, "10"],
)
def test_insert_rejects_invalid_ttl(ttl: object) -> None:
    catalog = _new_catalog(max_ttl=100.0)
    with pytest.raises(ValueError, match="TTL"):
        catalog.insert(_profile().canonical_json_bytes(), now=1.0, ttl=ttl)
    assert catalog.entry_count == 0


def test_insert_rejects_nonfinite_computed_expiry() -> None:
    catalog = _new_catalog(max_ttl=1e308)
    with pytest.raises(ValueError, match="expiry"):
        catalog.insert(
            _profile().canonical_json_bytes(),
            now=1e308,
            ttl=1e308,
        )
    assert catalog.entry_count == 0


def test_caller_time_must_not_move_backward() -> None:
    catalog = _new_catalog()
    profile = _profile()
    catalog.insert(profile.canonical_json_bytes(), now=10.0, ttl=10.0)
    catalog.resolve(_slot(profile), now=12.0)

    with pytest.raises(ValueError, match="move backward"):
        catalog.resolve(_slot(profile), now=11.0)
    with pytest.raises(ValueError, match="move backward"):
        catalog.insert(profile.canonical_json_bytes(), now=11.0, ttl=10.0)


def test_insert_and_lookup_require_explicit_caller_time() -> None:
    catalog = _new_catalog()
    profile = _profile()
    insert_signature = inspect.signature(catalog.insert)
    resolve_signature = inspect.signature(catalog.resolve)

    assert insert_signature.parameters["now"].default is inspect.Parameter.empty
    assert resolve_signature.parameters["now"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        catalog.insert(profile.canonical_json_bytes(), ttl=1.0)
    with pytest.raises(TypeError):
        catalog.resolve(_slot(profile))


def test_new_profile_resolves_current_without_readiness_authority() -> None:
    api = _catalog_api()
    catalog = _new_catalog()
    profile = _profile()
    payload = profile.canonical_json_bytes()

    inserted = catalog.insert(payload, now=10.0, ttl=5.0)
    lookup = catalog.resolve(_slot(profile), now=10.0)

    assert inserted.action is api.CatalogInsertAction.ADDED
    assert inserted.state is api.CapacityProfileCatalogState.CURRENT
    assert inserted.profile_digest == profile.profile_digest
    assert inserted.inserted_at == 10.0
    assert inserted.expires_at == 15.0
    assert lookup.state is api.CapacityProfileCatalogState.CURRENT
    assert lookup.profile == profile
    assert lookup.canonical_profile_bytes == payload
    assert lookup.route_ready is False
    assert lookup.release_ready is False
    assert lookup.qualification_evaluated is False
    assert lookup.profile.to_document()["route_ready"] is False
    assert lookup.profile.to_document()["release_ready"] is False
    assert lookup.profile.to_document()["qualification_evaluated"] is False


def test_expiry_boundary_is_stale_and_stale_profile_never_reactivates() -> None:
    api = _catalog_api()
    catalog = _new_catalog()
    profile = _profile()
    slot = _slot(profile)
    catalog.insert(profile.canonical_json_bytes(), now=10.0, ttl=5.0)

    assert catalog.resolve(slot, now=14.999).state is api.CapacityProfileCatalogState.CURRENT
    stale = catalog.resolve(slot, now=15.0)
    assert stale.state is api.CapacityProfileCatalogState.STALE
    assert stale.profile_digest == profile.profile_digest

    with pytest.raises(ValueError, match="move backward"):
        catalog.resolve(slot, now=14.0)
    assert catalog.resolve(slot, now=20.0).state is api.CapacityProfileCatalogState.STALE


def test_exact_digest_replay_is_idempotent_and_never_extends_expiry() -> None:
    api = _catalog_api()
    catalog = _new_catalog(max_ttl=100.0)
    profile = _profile()
    payload = profile.canonical_json_bytes()

    first = catalog.insert(payload, now=10.0, ttl=5.0)
    replay = catalog.insert(payload, now=12.0, ttl=100.0)

    assert first.action is api.CatalogInsertAction.ADDED
    assert replay.action is api.CatalogInsertAction.REPLAYED
    assert replay.state is api.CapacityProfileCatalogState.CURRENT
    assert replay.inserted_at == 10.0
    assert replay.expires_at == 15.0
    assert catalog.entry_count == 1

    stale_replay = catalog.insert(payload, now=15.0, ttl=100.0)
    assert stale_replay.action is api.CatalogInsertAction.REPLAYED
    assert stale_replay.state is api.CapacityProfileCatalogState.STALE
    assert stale_replay.expires_at == 15.0
    assert catalog.resolve(_slot(profile), now=15.0).state is api.CapacityProfileCatalogState.STALE


@pytest.mark.parametrize("allow_replacement", [False, 1, 0, "true", None])
def test_replacement_requires_allow_replacement_exactly_true(
    allow_replacement: object,
) -> None:
    catalog = _new_catalog()
    first = _profile(source_evidence_digest=DIGEST_B)
    revision = _profile(source_evidence_digest=DIGEST_C)
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=20.0)

    with pytest.raises(ValueError, match="explicit replacement"):
        catalog.insert(
            revision.canonical_json_bytes(),
            now=2.0,
            ttl=20.0,
            allow_replacement=allow_replacement,
            expected_current_digest=first.profile_digest,
        )
    assert catalog.entry_count == 1


@pytest.mark.parametrize("expected_digest", [None, DIGEST_D, 1, True])
def test_replacement_requires_exact_current_digest_compare_and_swap(
    expected_digest: object,
) -> None:
    catalog = _new_catalog()
    first = _profile(source_evidence_digest=DIGEST_B)
    revision = _profile(source_evidence_digest=DIGEST_C)
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=20.0)

    with pytest.raises(ValueError, match="compare-and-swap"):
        catalog.insert(
            revision.canonical_json_bytes(),
            now=2.0,
            ttl=20.0,
            allow_replacement=True,
            expected_current_digest=expected_digest,
        )
    assert catalog.entry_count == 1


def test_replacement_requires_different_verified_source_evidence() -> None:
    catalog = _new_catalog()
    first = _profile(source_evidence_digest=DIGEST_B, tpot_slo_ms=100.0)
    changed_claims = _profile(source_evidence_digest=DIGEST_B, tpot_slo_ms=101.0)
    assert first.profile_digest != changed_claims.profile_digest
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=20.0)

    with pytest.raises(ValueError, match="source-evidence digest"):
        catalog.insert(
            changed_claims.canonical_json_bytes(),
            now=2.0,
            ttl=20.0,
            allow_replacement=True,
            expected_current_digest=first.profile_digest,
        )
    assert catalog.entry_count == 1


def test_replacement_preserves_deprecated_history_and_new_current() -> None:
    api = _catalog_api()
    catalog = _new_catalog(max_entries=2)
    first = _profile(source_evidence_digest=DIGEST_B)
    revision = _profile(source_evidence_digest=DIGEST_C)
    slot = _slot(first)
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=50.0)

    replaced = catalog.insert(
        revision.canonical_json_bytes(),
        now=2.0,
        ttl=10.0,
        allow_replacement=True,
        expected_current_digest=first.profile_digest,
    )

    assert replaced.action is api.CatalogInsertAction.REPLACED
    assert replaced.state is api.CapacityProfileCatalogState.CURRENT
    assert catalog.entry_count == 2

    current = catalog.resolve(slot, now=2.0)
    deprecated = catalog.resolve(
        slot,
        now=2.0,
        profile_digest=first.profile_digest,
    )
    assert current.state is api.CapacityProfileCatalogState.CURRENT
    assert current.profile_digest == revision.profile_digest
    assert deprecated.state is api.CapacityProfileCatalogState.DEPRECATED
    assert deprecated.profile_digest == first.profile_digest
    assert deprecated.deprecated_at == 2.0
    assert deprecated.replaced_by_profile_digest == revision.profile_digest
    assert deprecated.route_ready is False
    assert deprecated.release_ready is False
    assert deprecated.qualification_evaluated is False


def test_deprecated_digest_replay_does_not_reactivate_or_extend_it() -> None:
    api = _catalog_api()
    catalog = _new_catalog(max_entries=2, max_ttl=100.0)
    first = _profile(source_evidence_digest=DIGEST_B)
    revision = _profile(source_evidence_digest=DIGEST_C)
    slot = _slot(first)
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=50.0)
    catalog.insert(
        revision.canonical_json_bytes(),
        now=2.0,
        ttl=10.0,
        allow_replacement=True,
        expected_current_digest=first.profile_digest,
    )

    replay = catalog.insert(first.canonical_json_bytes(), now=3.0, ttl=100.0)

    assert replay.action is api.CatalogInsertAction.REPLAYED
    assert replay.state is api.CapacityProfileCatalogState.DEPRECATED
    assert replay.expires_at == 51.0
    assert catalog.entry_count == 2
    assert (
        catalog.resolve(slot, now=3.0, profile_digest=first.profile_digest).state
        is api.CapacityProfileCatalogState.DEPRECATED
    )
    assert catalog.resolve(slot, now=3.0).profile_digest == revision.profile_digest


def test_capacity_exhaustion_rejects_replacement_without_partial_deprecation() -> None:
    api = _catalog_api()
    catalog = _new_catalog(max_entries=1)
    first = _profile(source_evidence_digest=DIGEST_B)
    revision = _profile(source_evidence_digest=DIGEST_C)
    slot = _slot(first)
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=50.0)

    with pytest.raises(ValueError, match="capacity exhausted"):
        catalog.insert(
            revision.canonical_json_bytes(),
            now=2.0,
            ttl=10.0,
            allow_replacement=True,
            expected_current_digest=first.profile_digest,
        )

    assert catalog.entry_count == 1
    current = catalog.resolve(slot, now=2.0)
    assert current.state is api.CapacityProfileCatalogState.CURRENT
    assert current.profile_digest == first.profile_digest
    assert current.deprecated_at is None


def test_capacity_exhaustion_never_evicts_an_existing_current_slot() -> None:
    api = _catalog_api()
    catalog = _new_catalog(max_entries=1)
    first = _profile(runtime_build="mlx-0.29.0")
    other_slot_profile = _profile(runtime_build="mlx-0.29.1")
    first_slot = _slot(first)
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=50.0)

    with pytest.raises(ValueError, match="capacity exhausted"):
        catalog.insert(other_slot_profile.canonical_json_bytes(), now=2.0, ttl=10.0)

    assert catalog.entry_count == 1
    assert catalog.resolve(first_slot, now=2.0).state is api.CapacityProfileCatalogState.CURRENT
    assert (
        catalog.resolve(_slot(other_slot_profile), now=2.0).state
        is api.CapacityProfileCatalogState.MISSING
    )


def test_lookup_distinguishes_missing_current_stale_and_deprecated() -> None:
    api = _catalog_api()
    catalog = _new_catalog(max_entries=3)
    first = _profile(source_evidence_digest=DIGEST_B)
    revision = _profile(source_evidence_digest=DIGEST_C)
    missing_slot = replace(_slot(first), runtime_build="mlx-missing")
    slot = _slot(first)

    assert catalog.resolve(missing_slot, now=0.0).state is api.CapacityProfileCatalogState.MISSING
    catalog.insert(first.canonical_json_bytes(), now=1.0, ttl=2.0)
    assert catalog.resolve(slot, now=1.0).state is api.CapacityProfileCatalogState.CURRENT
    assert catalog.resolve(slot, now=3.0).state is api.CapacityProfileCatalogState.STALE
    catalog.insert(
        revision.canonical_json_bytes(),
        now=3.0,
        ttl=2.0,
        allow_replacement=True,
        expected_current_digest=first.profile_digest,
    )
    assert (
        catalog.resolve(slot, now=3.0, profile_digest=first.profile_digest).state
        is api.CapacityProfileCatalogState.DEPRECATED
    )
    assert catalog.resolve(slot, now=5.0).state is api.CapacityProfileCatalogState.STALE
    assert (
        catalog.resolve(slot, now=5.0, profile_digest=DIGEST_D).state
        is api.CapacityProfileCatalogState.MISSING
    )


def test_explicit_initializer_builds_a_process_local_unconnected_catalog() -> None:
    catalog_api = _catalog_api()
    init_api = _init_api()

    first = init_api.initialize_capacity_profile_catalog(
        max_entries=2,
        max_ttl=30.0,
    )
    second = init_api.initialize_capacity_profile_catalog(
        max_entries=2,
        max_ttl=30.0,
    )
    first.insert(_profile().canonical_json_bytes(), now=1.0, ttl=10.0)

    assert isinstance(first, catalog_api.CapacityProfileCatalog)
    assert first.policy == catalog_api.CapacityProfileCatalogPolicy(2, 30.0)
    assert first.entry_count == 1
    assert second.entry_count == 0


def test_catalog_source_has_no_clock_io_background_or_runtime_consumer_imports() -> None:
    catalog_api = _catalog_api()
    init_api = _init_api()
    assert catalog_api.__file__ is not None
    assert init_api.__file__ is not None
    source = Path(catalog_api.__file__).read_text(encoding="utf-8")
    source += Path(init_api.__file__).read_text(encoding="utf-8")

    forbidden = (
        "time.time",
        "time.monotonic",
        "datetime.now",
        "open(",
        "Path(",
        "threading",
        "asyncio",
        "mycelium_router",
        "mycelium_gossip",
        "mycelium_gateway",
        "status_with_capacity_profile",
    )
    for symbol in forbidden:
        assert symbol not in source
