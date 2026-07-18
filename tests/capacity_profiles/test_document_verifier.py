from __future__ import annotations

import copy
import json

import pytest

import mycelium_capacity_profiles as api


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _key() -> api.CapacityProfileKey:
    return api.CapacityProfileKey(
        model_digest=DIGEST_A,
        source_evidence_digest=DIGEST_B,
        quantization="fp16",
        backend="mlx",
        runtime_build="mlx-0.29.0",
        hardware_class="apple-m4-pro-48gb",
        power_mode="ac",
        context_bucket="0-4096",
        kv_mode="stage-local",
    )


def _policy() -> api.CapacityProfilePolicy:
    return api.CapacityProfilePolicy(
        ttft_p95_slo_ms=1_000.0,
        tpot_p95_slo_ms=100.0,
        min_samples=3,
    )


def _point(
    concurrency: int,
    *,
    ttft: float | None = 500.0,
    tpot: float | None = 50.0,
    aggregate_tps: float | None = 10.0,
    oom: bool = False,
) -> api.CapacityObservation:
    return api.CapacityObservation(
        concurrency=concurrency,
        sample_count=1 if oom else 5,
        p95_ttft_ms=ttft,
        p95_tpot_ms=tpot,
        aggregate_output_tps=aggregate_tps,
        peak_memory_bytes=10,
        memory_budget_bytes=100,
        oom=oom,
    )


def _profile() -> api.CapacityProfile:
    return api.compile_capacity_profile(
        _key(),
        (
            _point(1, aggregate_tps=10),
            _point(2, ttft=1_200, aggregate_tps=18),
            _point(
                3,
                ttft=None,
                tpot=None,
                aggregate_tps=None,
                oom=True,
            ),
        ),
        _policy(),
    )


def _single_point_profile() -> api.CapacityProfile:
    return api.compile_capacity_profile(_key(), (_point(1),), _policy())


def _canonical(document: object) -> bytes:
    return api.canonical_json_bytes(document)


def test_exact_compiler_bytes_round_trip_to_same_verified_profile() -> None:
    profile = _profile()

    parsed = api.parse_capacity_profile_bytes(profile.canonical_json_bytes())

    assert parsed == profile
    assert parsed.profile_digest == profile.profile_digest
    assert parsed.to_document()["route_ready"] is False
    assert parsed.to_document()["release_ready"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "route_ready",
        "release_ready",
        "derived_limit",
        "source_evidence",
        "evaluated_flag",
        "boundary",
        "profile_digest",
        "protocol",
    ],
)
def test_semantic_or_digest_tampering_is_rejected(mutation: str) -> None:
    document = copy.deepcopy(_profile().to_document())
    if mutation == "route_ready":
        document["route_ready"] = True
    elif mutation == "release_ready":
        document["release_ready"] = True
    elif mutation == "derived_limit":
        document["max_safe_concurrency"] = 99
    elif mutation == "source_evidence":
        document["key"]["source_evidence_digest"] = DIGEST_C
    elif mutation == "evaluated_flag":
        document["points"][0]["safe"] = False
    elif mutation == "boundary":
        document["safety_boundary"]["kind"] = "highest_observed"
    elif mutation == "profile_digest":
        document["profile_digest"] = DIGEST_C
    elif mutation == "protocol":
        document["protocol"] = "mycelium.capacity_profile.v2"
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match="derived profile"):
        api.parse_capacity_profile_bytes(_canonical(document))


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_top",
        "normalized_alias",
        "unknown_key",
        "unknown_point",
        "missing_top",
    ],
)
def test_every_object_uses_an_exact_closed_schema(mutation: str) -> None:
    document = copy.deepcopy(_profile().to_document())
    if mutation == "unknown_top":
        document["metadata"] = {}
    elif mutation == "normalized_alias":
        document["route-ready"] = document["route_ready"]
    elif mutation == "unknown_key":
        document["key"]["publisher"] = "node-a"
    elif mutation == "unknown_point":
        document["points"][0]["prompt"] = "hidden"
    elif mutation == "missing_top":
        del document["profile_digest"]
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValueError, match="exact schema"):
        api.parse_capacity_profile_bytes(_canonical(document))


@pytest.mark.parametrize(
    "raw, error",
    [
        (b'{"protocol":"x","protocol":"y"}', "duplicate"),
        (b"[]", "object"),
        (b'{"value":NaN}', "finite"),
        (b"\xff", "UTF-8"),
    ],
)
def test_malformed_payloads_fail_closed_without_parser_details(
    raw: bytes, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        api.parse_capacity_profile_bytes(raw)


def test_noncanonical_whitespace_and_newlines_are_rejected() -> None:
    document = _profile().to_document()
    pretty = json.dumps(document, sort_keys=True, indent=2).encode("utf-8")

    for raw in (pretty, _canonical(document) + b"\n"):
        with pytest.raises(ValueError, match="canonical"):
            api.parse_capacity_profile_bytes(raw)


def test_payload_size_and_observation_count_are_bounded() -> None:
    oversized = b" " * (256 * 1024 + 1)
    with pytest.raises(ValueError, match="bounded"):
        api.parse_capacity_profile_bytes(oversized)

    observations = tuple(_point(index) for index in range(1, 258))
    with pytest.raises(ValueError, match="at most 256"):
        api.compile_capacity_profile(_key(), observations, _policy())


def test_json_boolean_cannot_alias_an_integer_limit() -> None:
    document = _single_point_profile().to_document()
    document["max_safe_concurrency"] = True

    with pytest.raises(ValueError, match="derived profile"):
        api.parse_capacity_profile_bytes(_canonical(document))


def test_parser_accepts_exact_bytes_only() -> None:
    class MisleadingBytes(bytes):
        def __len__(self) -> int:
            return 1

    for payload in ("{}", MisleadingBytes(b" " * (256 * 1024 + 1))):
        with pytest.raises(ValueError, match="bytes"):
            api.parse_capacity_profile_bytes(payload)  # type: ignore[arg-type]
