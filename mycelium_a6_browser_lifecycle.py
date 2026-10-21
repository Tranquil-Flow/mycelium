# SPDX-License-Identifier: AGPL-3.0-only
"""Three-engine browser lifecycle aggregation over receiver lifetimes.

The A6 browser gate needs three engines.  Each engine must own an *independent*
direct-loopback fault receiver lifetime with one use per case, and a partial or
duplicated set must never be reported as aggregate success.

This module aggregates the transport/lifecycle half only, from the owned
``mycelium_a6_direct_socket`` primitives.  The browser scenario half is a
separate runner (``ui/web/scripts/a6-product-browser-matrix.mjs``) with its own
receipt protocol; neither is a physical qualification and neither authorises a
promotion.  Browser-side evidence absent means this aggregate stays a
lifecycle witness, not a gate result.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

PROTOCOL = "mycelium.a6_browser_lifecycle.v1"
LIFECYCLE_PROTOCOL = PROTOCOL
ENGINES: tuple[str, ...] = ("chromium", "firefox", "webkit")
RECEIPT_FIELDS = frozenset({
    "protocol", "engine", "lifetime_id", "case_ids", "statuses", "executed_case_ids",
    "torn_down", "repeat_status", "source_binding", "candidate_binding",
    "live_evidence", "physical_qualification",
})
_HEX64 = set("0123456789abcdef")


class LifecycleRefused(Exception):
    """A bounded, non-disclosing refusal; a partial set is never an aggregate."""


def _bound(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 71 and value.startswith("sha256:")
            and set(value[7:]) <= _HEX64)


def engine_lifetime_receipt(*, engine: str, lifetime_id: str, case_ids: Iterable[str],
                            statuses: Iterable[int], executed_case_ids: Iterable[str],
                            torn_down: bool, repeat_status: int, source_binding: str,
                            candidate_binding: str) -> dict[str, Any]:
    """Closed-shape receipt for exactly one engine's one receiver lifetime."""
    if engine not in ENGINES:
        raise LifecycleRefused("engine_selection_invalid")
    if (not isinstance(lifetime_id, str) or len(lifetime_id) != 64
            or set(lifetime_id) - _HEX64):
        raise LifecycleRefused("receiver_lifetime_invalid")
    if not _bound(source_binding) or not _bound(candidate_binding):
        raise LifecycleRefused("lifecycle_binding_invalid")
    cases = tuple(case_ids)
    if not cases or len(set(cases)) != len(cases):
        raise LifecycleRefused("receiver_case_set_invalid")
    return {
        "protocol": PROTOCOL,
        "engine": engine,
        "lifetime_id": lifetime_id,
        "case_ids": cases,
        "statuses": tuple((case, int(status)) for case, status in
                          zip(cases, statuses, strict=True)),
        "executed_case_ids": tuple(executed_case_ids),
        "torn_down": bool(torn_down),
        "repeat_status": int(repeat_status),
        "source_binding": source_binding,
        "candidate_binding": candidate_binding,
        "live_evidence": False,
        "physical_qualification": False,
    }


def aggregate_engine_lifetimes(receipts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate exactly one receipt per engine; anything partial is refused."""
    items = [dict(receipt) for receipt in receipts]
    if len(items) != len(ENGINES):
        raise LifecycleRefused("engine_set_invalid")
    seen_engines: set[str] = set()
    seen_lifetimes: set[str] = set()
    for item in items:
        if set(item) != RECEIPT_FIELDS or item.get("protocol") != PROTOCOL:
            raise LifecycleRefused("receipt_closed_shape_invalid")
        engine = item["engine"]
        if engine not in ENGINES or engine in seen_engines:
            raise LifecycleRefused("engine_set_invalid")
        if (item["live_evidence"] is not False or item["physical_qualification"] is not False):
            raise LifecycleRefused("lifecycle_claim_boundary_invalid")
        if item["torn_down"] is not True:
            raise LifecycleRefused("receiver_not_torn_down")
        if tuple(item["executed_case_ids"]) != tuple(item["case_ids"]):
            raise LifecycleRefused("executed_case_set_mismatch")
        if any(status != 200 for _case, status in item["statuses"]):
            raise LifecycleRefused("receiver_case_not_executed")
        if item["repeat_status"] != 409:
            raise LifecycleRefused("receiver_case_not_one_use")
        if item["lifetime_id"] in seen_lifetimes:
            raise LifecycleRefused("receiver_lifetime_not_independent")
        seen_engines.add(engine)
        seen_lifetimes.add(item["lifetime_id"])
    if seen_engines != set(ENGINES):
        raise LifecycleRefused("engine_set_invalid")
    return {
        "protocol": PROTOCOL,
        "engines": list(ENGINES),
        "independent_receiver_lifetimes": len(seen_lifetimes),
        "passed": True,
        "live_evidence": False,
        "physical_qualification_pending": True,
        "qualification_claim": False,
        "promotion_authorized": False,
        "claim_boundary": (
            "Three independent one-use direct-loopback receiver lifetimes torn down "
            "after the declared case set; no browser scenario, product or physical "
            "qualification is claimed here."
        ),
    }


__all__ = [
    "ENGINES",
    "LIFECYCLE_PROTOCOL",
    "LifecycleRefused",
    "PROTOCOL",
    "aggregate_engine_lifetimes",
    "engine_lifetime_receipt",
]
