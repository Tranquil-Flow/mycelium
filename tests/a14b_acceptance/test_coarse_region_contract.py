# SPDX-License-Identifier: AGPL-3.0-only
"""A14b vocabulary and evidence contract; specification sections 3 and 4.

RED at `b90edaf`: `mycelium_product_spine.coarse_region` does not exist, so no A14b
region vocabulary, normalization, or coarse-region evidence record is implemented.
These cases freeze the target behaviour; they are not a claim that anything works.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.a14b_acceptance import conformance

SUBJECT = "sha256:" + "a" * 64


def test_vocabulary_is_exactly_the_frozen_inventory_set():
    coarse = conformance.contract()
    assert coarse.PROTOCOL == conformance.VOCABULARY_PROTOCOL
    assert coarse.EVIDENCE_PROTOCOL == conformance.EVIDENCE_PROTOCOL
    assert tuple(coarse.REGIONS) == conformance.REGIONS
    assert coarse.UNKNOWN == conformance.UNKNOWN
    assert set(coarse.SOURCE_KINDS) == set(conformance.SOURCE_KINDS)
    assert set(coarse.REGION_BLOCKERS) == set(conformance.BLOCKERS)
    assert set(coarse.DISTANCE_CLASSES) == set(conformance.DISTANCE_CLASSES)
    assert dict(coarse.DISPLAY_CELLS) == conformance.DISPLAY_CELLS


@pytest.mark.parametrize(
    "value,expected",
    [
        ("africa", "africa"),
        ("north_america", "americas_north"),
        ("central_america", "americas_north"),
        ("caribbean", "americas_north"),
        ("south_america", "americas_south"),
        ("asia", "asia"),
        ("europe", "europe"),
        ("oceania", "oceania"),
        ("arctic", "polar"),
        ("antarctica", "polar"),
        ("unknown", "unknown"),
        ("americas", "unknown"),
        ("us-east-1", "unknown"),
        ("DE-BE", "unknown"),
        ("Berlin", "unknown"),
        ("europe_west", "unknown"),
        ("", "unknown"),
        (42, "unknown"),
        (None, "unknown"),
        (["europe"], "unknown"),
    ],
)
def test_normalization_is_a_closed_alias_table_that_never_derives_a_region(value, expected):
    coarse = conformance.contract()
    assert coarse.normalize_region(value) == expected


def test_evidence_record_is_closed_reviewed_and_canonically_digested():
    coarse = conformance.contract()
    record = conformance.region_evidence(SUBJECT, "member")
    assert coarse.validate_coarse_region_evidence(record) == record
    assert record["digest"] == conformance.canonical_digest(
        {key: value for key, value in record.items() if key != "digest"}
    )
    assert (
        coarse.digest({key: value for key, value in record.items() if key != "digest"})
        == record["digest"]
    )


def test_validation_detaches_its_result_from_the_caller_record():
    coarse = conformance.contract()
    record = conformance.region_evidence(SUBJECT, "member")
    validated = coarse.validate_coarse_region_evidence(record)
    validated["region"] = "polar"
    assert record["region"] == "europe"


@pytest.mark.parametrize(
    "field,value",
    [
        ("latitude", 52.5),
        ("longitude", 13.4),
        ("geohash", "u33dc0"),
        ("ip_address", "192.0.2.23"),
        ("address", "Example Street 1"),
        ("city", "Berlin"),
        ("hostname", "private.example"),
        ("endpoint_id", "RAW-ENDPOINT-CANARY"),
        ("relay_url", "iroh://relay.example"),
        ("private_address", "100.64.0.1"),
        ("provider_region", "us-east-1"),
        ("free_form_location", "somewhere near the coast"),
    ],
)
def test_evidence_rejects_precise_or_network_identity_fields(field, value):
    coarse = conformance.contract()
    record = conformance.region_evidence(SUBJECT, "member")
    tampered = conformance.reseal({**record, field: value})
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(tampered)


@pytest.mark.parametrize(
    "region",
    ["us-east-1", "DE-BE", "Berlin", "europe_west", "americas", "", None, 42],
)
def test_evidence_rejects_any_region_outside_the_vocabulary(region):
    coarse = conformance.contract()
    record = conformance.region_evidence(SUBJECT, "member", region=region)
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(record)


@pytest.mark.parametrize(
    "subject_ref",
    [
        "RAW-ENDPOINT-CANARY",
        "192.0.2.23",
        "private.example",
        "sha256:" + "z" * 64,
        "sha256:" + "a" * 63,
    ],
)
def test_evidence_rejects_raw_or_malformed_subject_references(subject_ref):
    coarse = conformance.contract()
    record = conformance.region_evidence(subject_ref, "member")
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(record)


def test_a_latency_class_is_non_geographic_and_explicitly_modeled():
    coarse = conformance.contract()
    record = conformance.latency_evidence(SUBJECT, "member", "far")
    validated = coarse.validate_coarse_region_evidence(record)
    assert validated["region"] == conformance.UNKNOWN
    assert validated["distance_class"] == "far"
    assert validated["model_version"] == "latency_class_model_v1"
    assert validated["uncertainty"] == "model_estimate"


@pytest.mark.parametrize(
    "field,value",
    [("region", "europe"), ("distance_class", None), ("model_version", None)],
)
def test_a_latency_class_rejects_geography_or_a_missing_model(field, value):
    coarse = conformance.contract()
    record = conformance.latency_evidence(SUBJECT, "member", "far")
    tampered = conformance.reseal({**record, field: value})
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(tampered)


@pytest.mark.parametrize(
    "extra",
    [{"distance_class": "far"}, {"model_version": "latency_class_model_v1"}],
)
def test_geographic_evidence_carries_no_distance_class_and_no_model(extra):
    coarse = conformance.contract()
    record = conformance.region_evidence(SUBJECT, "member", extra=extra)
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(record)


@pytest.mark.parametrize(
    "source_kind,uncertainty",
    [
        ("owner_declared", "reviewed"),
        ("reviewed_relay_metadata", "declared_unverified"),
        ("latency_distance_class", "declared_unverified"),
    ],
)
def test_uncertainty_is_bound_to_the_reviewed_source(source_kind, uncertainty):
    coarse = conformance.contract()
    if source_kind == "latency_distance_class":
        record = conformance.latency_evidence(SUBJECT, "member", "far", uncertainty=uncertainty)
    else:
        record = conformance.region_evidence(
            SUBJECT, "member", source_kind, "europe", uncertainty=uncertainty
        )
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issued_at_unix_ms", True),
        ("issued_at_unix_ms", 1.5),
        ("issued_at_unix_ms", -1),
        ("expires_at_unix_ms", 998999),
    ],
)
def test_evidence_rejects_boolean_float_negative_and_inverted_times(field, value):
    coarse = conformance.contract()
    record = conformance.region_evidence(SUBJECT, "member")
    tampered = conformance.reseal({**record, field: value})
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(tampered)


def test_an_edited_record_with_a_stale_digest_is_rejected():
    coarse = conformance.contract()
    record = conformance.region_evidence(SUBJECT, "member")
    record["region"] = "polar"
    with pytest.raises(ValueError):
        coarse.validate_coarse_region_evidence(record)


def test_display_cells_are_fixed_schematic_geometry_without_coordinates():
    coarse = conformance.contract()
    cells = dict(coarse.DISPLAY_CELLS)
    assert set(cells) == set(conformance.REGIONS) - {conformance.UNKNOWN}
    assert cells == {code: f"cell_{code}" for code in cells}
    assert conformance.UNKNOWN not in cells
    assert re.search(r"\d", json.dumps(cells)) is None
