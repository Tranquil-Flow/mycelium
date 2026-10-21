# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic A14b coarse-region inputs; the production reduction does the work.

These helpers build data only. No projection, projection digest, or rendering is
hand-authored as a shortcut, no region authority is created, and every constant
below is an independent literal encoding of
`docs/superpowers/specs/2026-08-18-mycelium-a14b-coarse-region-globe.md` and the
frozen inventory `tests/a14_acceptance/inventory.v1.json`, never of an
implementation. This is local conformance input, not physical region evidence.
"""
from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_A14A = runpy.run_path(str(ROOT / "tests/a14_acceptance/conformance.py"))
A14A_SNAPSHOT = _A14A["conformance_snapshot"]
a14a_inputs = _A14A["inputs"]
projector = _A14A["ConformanceProjector"]

VOCABULARY_PROTOCOL = "mycelium.coarse_region.v1"
EVIDENCE_PROTOCOL = "mycelium.coarse_region_evidence.v1"
REGIONS = (
    "africa",
    "americas_north",
    "americas_south",
    "asia",
    "europe",
    "oceania",
    "polar",
    "unknown",
)
UNKNOWN = "unknown"
DISPLAY_CELLS = {
    "africa": "cell_africa",
    "americas_north": "cell_americas_north",
    "americas_south": "cell_americas_south",
    "asia": "cell_asia",
    "europe": "cell_europe",
    "oceania": "cell_oceania",
    "polar": "cell_polar",
}
SOURCE_KINDS = ("owner_declared", "reviewed_relay_metadata", "latency_distance_class")
UNCERTAINTY = {
    "owner_declared": "declared_unverified",
    "reviewed_relay_metadata": "reviewed",
    "latency_distance_class": "model_estimate",
}
DISTANCE_CLASSES = ("near", "mid", "far", "unknown")
BLOCKERS = ("missing", "expired", "conflicting", "unauthorized")
ROW_FIELDS = {"node", "region", "region_source", "region_blocker", "distance_class"}


def contract():
    """Return the A14b production module, or fail saying it does not exist yet."""
    try:
        from mycelium_product_spine import coarse_region
    except ImportError as exc:  # pragma: no cover - the RED path at b90edaf
        pytest.fail(
            "A14b production surface is not implemented at this revision: "
            f"{exc}. Expected mycelium_product_spine/coarse_region.py per "
            "docs/superpowers/specs/2026-08-18-mycelium-a14b-coarse-region-globe.md"
            " sections 3-6.",
            pytrace=False,
        )
    return coarse_region


def canonical_digest(value: object) -> str:
    """Repository canonical digest rule, as used by route_explorer.digest."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def region_evidence(
    subject_ref: str,
    subject_kind: str,
    source_kind: str = "owner_declared",
    region: str = "europe",
    *,
    distance_class: str | None = None,
    model_version: str | None = None,
    uncertainty: str | None = None,
    issued_at_unix_ms: int = 999000,
    expires_at_unix_ms: int = 1600000,
    authority_digest: str | None = None,
    extra: dict | None = None,
) -> dict:
    """One reviewed coarse-region candidate; never an authority of its own."""
    record = {
        "protocol": EVIDENCE_PROTOCOL,
        "subject_ref": subject_ref,
        "subject_kind": subject_kind,
        "source_kind": source_kind,
        "region": region,
        "distance_class": distance_class,
        "vocabulary_version": VOCABULARY_PROTOCOL,
        "model_version": model_version,
        "uncertainty": (
            UNCERTAINTY[source_kind] if uncertainty is None else uncertainty
        ),
        "issued_at_unix_ms": issued_at_unix_ms,
        "expires_at_unix_ms": expires_at_unix_ms,
        "authority_digest": (
            canonical_digest([subject_ref, source_kind, region, issued_at_unix_ms])
            if authority_digest is None
            else authority_digest
        ),
    }
    record.update(extra or {})
    record["digest"] = canonical_digest(record)
    return record


def latency_evidence(
    subject_ref: str,
    subject_kind: str,
    distance_class: str = "far",
    **kwargs,
) -> dict:
    """One non-geographic latency-distance statement: off-globe by construction."""
    kwargs.setdefault("model_version", "latency_class_model_v1")
    return region_evidence(
        subject_ref,
        subject_kind,
        "latency_distance_class",
        UNKNOWN,
        distance_class=distance_class,
        **kwargs,
    )


def reseal(record: dict) -> dict:
    """Recompute a record or projection digest, as a tamper or edit would."""
    sealed = {key: value for key, value in record.items() if key != "digest"}
    sealed["digest"] = canonical_digest(sealed)
    return sealed


def accepted(*records: dict) -> frozenset:
    """The accepted authority set a caller publishes: every digested statement."""
    return frozenset(record["authority_digest"] for record in records)


def snapshot(now_unix_ms: int = 1000000) -> dict:
    """The A14a synthetic product snapshot: two members and one endpoint peer."""
    return A14A_SNAPSHOT(now=now_unix_ms)


def projection(value: dict) -> dict:
    return value["route_explorer"]


def nodes(value: dict) -> list:
    return projection(value)["nodes"]


def node_of_kind(value: dict, kind: str) -> dict:
    return next(item for item in nodes(value) if item["kind"] == kind)


def members(value: dict) -> list:
    return [item for item in nodes(value) if item["kind"] == "member"]


def _detach_projection(value: dict) -> dict:
    """Accept a snapshot or a projection and return a detached projection copy."""
    source = value["route_explorer"] if "route_explorer" in value else value
    return json.loads(json.dumps(source))


def with_regions(value: dict, rows: list) -> dict:
    """Attach reduction rows and the matching node regions; re-seal the projection."""
    import sys

    sys.path.insert(0, str(ROOT))
    from mycelium_product_spine.route_explorer import digest

    result = _detach_projection(value)
    by_node = {row["node"]: row for row in rows}
    for item in result["nodes"]:
        row = by_node[item["id"]]
        item["region"] = row["region"]
        item["region_source"] = row["region_source"]
    result["regions"] = sorted(rows, key=lambda row: row["node"])
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    return result


def without_regions(value: dict) -> dict:
    """The same projection with the optional A14b section omitted, re-sealed."""
    import sys

    sys.path.insert(0, str(ROOT))
    from mycelium_product_spine.route_explorer import digest

    result = _detach_projection(value)
    result.pop("regions", None)
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    return result
