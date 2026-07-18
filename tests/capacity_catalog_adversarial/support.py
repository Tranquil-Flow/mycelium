"""Production fixture builder and public-surface conformance adapter."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

import mycelium_capacity_profiles as profiles
import mycelium_capacity_profiles.catalog as catalog_api

from tests.capacity_catalog_adversarial.model import (
    AUTHORITY_FLAGS,
    Observation,
    Operation,
    ProfileFixture,
    SlotIdentity,
    materialize_value,
)


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _build_fixture(
    profile_id: str,
    *,
    source_label: str | None = None,
    tpot_slo_ms: float = 100.0,
    **slot_overrides: str,
) -> ProfileFixture:
    slot_values = {
        "model_digest": _digest("model:base"),
        "quantization": "fp16",
        "backend": "mlx",
        "runtime_build": "mlx-0.29.0",
        "hardware_class": "apple-m4-pro-48gb",
        "power_mode": "ac",
        "context_bucket": "0-4096",
        "kv_mode": "stage-local",
    }
    slot_values.update(slot_overrides)
    source_evidence_digest = _digest(source_label or f"evidence:{profile_id}")
    key = profiles.CapacityProfileKey(
        source_evidence_digest=source_evidence_digest,
        **slot_values,
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
    profile = profiles.compile_capacity_profile(key, (observation,), policy)
    return ProfileFixture(
        profile_id=profile_id,
        profile_digest=profile.profile_digest,
        source_evidence_digest=source_evidence_digest,
        slot=SlotIdentity(**slot_values),
        canonical_profile_bytes=profile.canonical_json_bytes(),
    )


def build_profile_fixtures() -> dict[str, ProfileFixture]:
    return {
        "base": _build_fixture("base"),
        "revision-1": _build_fixture("revision-1"),
        "revision-2": _build_fixture("revision-2"),
        "same-source-claims": _build_fixture(
            "same-source-claims",
            source_label="evidence:base",
            tpot_slo_ms=101.0,
        ),
        "slot-model": _build_fixture(
            "slot-model",
            model_digest=_digest("model:other"),
        ),
        "slot-quantization": _build_fixture(
            "slot-quantization",
            quantization="q4-k-m",
        ),
        "slot-backend": _build_fixture(
            "slot-backend",
            backend="llama.cpp",
        ),
        "slot-runtime": _build_fixture(
            "slot-runtime",
            runtime_build="mlx-0.30.0",
        ),
        "slot-hardware": _build_fixture(
            "slot-hardware",
            hardware_class="apple-m4-max-128gb",
        ),
        "slot-power": _build_fixture(
            "slot-power",
            power_mode="battery",
        ),
        "slot-context": _build_fixture(
            "slot-context",
            context_bucket="4097-8192",
        ),
        "slot-kv": _build_fixture(
            "slot-kv",
            kv_mode="distributed",
        ),
    }


def _production_slot(identity: SlotIdentity) -> catalog_api.CapacityProfileSlot:
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


def _error_code(error: ValueError) -> str:
    message = str(error)
    classifications = (
        ("move backward", "backward_time"),
        ("monotonic time", "invalid_time"),
        ("TTL", "invalid_ttl"),
        ("expiry", "nonfinite_expiry"),
        ("explicit replacement", "replacement_not_authorized"),
        ("compare-and-swap", "cas_failed"),
        ("different verified source-evidence", "source_evidence_reused"),
        ("capacity exhausted", "capacity_exhausted"),
        ("digest collision", "digest_collision"),
    )
    for fragment, code in classifications:
        if fragment in message:
            return code
    return f"unclassified:{message}"


def _insert_observation(result, entry_count: int) -> Observation:
    return Observation(
        operation="insert",
        code=result.action.value,
        entry_count=entry_count,
        state=result.state.value,
        slot=SlotIdentity(
            model_digest=result.slot.model_digest,
            quantization=result.slot.quantization,
            backend=result.slot.backend,
            runtime_build=result.slot.runtime_build,
            hardware_class=result.slot.hardware_class,
            power_mode=result.slot.power_mode,
            context_bucket=result.slot.context_bucket,
            kv_mode=result.slot.kv_mode,
        ),
        profile_digest=result.profile_digest,
        source_evidence_digest=result.source_evidence_digest,
        inserted_at=result.inserted_at,
        expires_at=result.expires_at,
        authority_flags=(
            result.route_ready,
            result.release_ready,
            result.qualification_evaluated,
        ),
    )


def _lookup_observation(result, entry_count: int) -> Observation:
    return Observation(
        operation="resolve",
        code=result.state.value,
        entry_count=entry_count,
        state=result.state.value,
        slot=SlotIdentity(
            model_digest=result.slot.model_digest,
            quantization=result.slot.quantization,
            backend=result.slot.backend,
            runtime_build=result.slot.runtime_build,
            hardware_class=result.slot.hardware_class,
            power_mode=result.slot.power_mode,
            context_bucket=result.slot.context_bucket,
            kv_mode=result.slot.kv_mode,
        ),
        profile_digest=result.profile_digest,
        source_evidence_digest=result.source_evidence_digest,
        canonical_profile_bytes=result.canonical_profile_bytes,
        inserted_at=result.inserted_at,
        expires_at=result.expires_at,
        deprecated_at=result.deprecated_at,
        replaced_by_profile_digest=result.replaced_by_profile_digest,
        authority_flags=(
            result.route_ready,
            result.release_ready,
            result.qualification_evaluated,
        ),
    )


def run_production_trace(
    trace: Sequence[Operation],
    fixtures: Mapping[str, ProfileFixture],
    *,
    max_entries: int,
    max_ttl: float,
) -> tuple[Observation, ...]:
    catalog = catalog_api.CapacityProfileCatalog(
        catalog_api.CapacityProfileCatalogPolicy(
            max_entries=max_entries,
            max_ttl=max_ttl,
        )
    )
    observations: list[Observation] = []
    for operation in trace:
        try:
            if operation.kind == "insert":
                fixture = fixtures[operation.profile_id]
                expected_digest = (
                    None
                    if operation.expected_current_profile_id is None
                    else fixtures[operation.expected_current_profile_id].profile_digest
                )
                result = catalog.insert(
                    fixture.canonical_profile_bytes,
                    now=materialize_value(operation.now),
                    ttl=materialize_value(operation.ttl),
                    allow_replacement=operation.allow_replacement,
                    expected_current_digest=expected_digest,
                )
                observations.append(_insert_observation(result, catalog.entry_count))
            elif operation.kind == "resolve":
                fixture = fixtures[operation.profile_id]
                lookup_digest = (
                    None
                    if operation.lookup_profile_id is None
                    else fixtures[operation.lookup_profile_id].profile_digest
                )
                result = catalog.resolve(
                    _production_slot(fixture.slot),
                    now=materialize_value(operation.now),
                    profile_digest=lookup_digest,
                )
                observations.append(_lookup_observation(result, catalog.entry_count))
            else:
                raise AssertionError(f"unsupported_operation:{operation.kind}")
        except ValueError as exc:
            observations.append(
                Observation(
                    operation=operation.kind,
                    code=_error_code(exc),
                    entry_count=catalog.entry_count,
                    authority_flags=AUTHORITY_FLAGS,
                )
            )
    return tuple(observations)


def first_discrepancy(
    expected: Sequence[Observation],
    observed: Sequence[Observation],
) -> dict[str, object] | None:
    for index, pair in enumerate(zip(expected, observed, strict=False)):
        wanted, actual = pair
        if wanted != actual:
            return {"index": index, "expected": repr(wanted), "observed": repr(actual)}
    if len(expected) != len(observed):
        return {"index": min(len(expected), len(observed)), "lengths": (len(expected), len(observed))}
    return None
