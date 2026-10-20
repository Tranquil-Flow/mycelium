# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which registered models fit the swarm's usable memory (PV7), before any download.

Inputs are plain member rows the owner already holds: ``node_id``,
``memory_bytes`` and, when known, ``reserve_bytes`` (the owner's Adaptive-sharing
headroom reserve, subtracted from that Mac), ``max_memory_bytes`` (a standing
memory cap) and ``available_storage_bytes``.  Fit is decided by the same
functions that enforce a run (``model_resources.plan_for_members``: the
memory-proportional split, the per-member RAM guard against the app's 3/4-of-RAM
ceiling, free disk, and the member's memory cap), so a model reported as fitting
is one the run would not refuse on memory, and the other way round.

Nothing here reads the network, a snapshot or a host.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .layer_split import MAX_HOSTS, MIN_HOSTS, SplitRefused
from .model_resources import (DEMO_CONTEXT_TOKENS, GROUP_FIXED_BYTES, GUARD_HEADROOM, LOAD_PEAK_FACTOR,
                              ResourceRefused, _resident_factor, check_member_caps, check_members,
                              member_memory_ceiling, plan_for_members, require_shape)

FITS, TOO_LARGE, UNKNOWN = "fits", "too_large", "unknown"


def _gb(value: int) -> str:
    return f"{value / 1e9:.1f} GB"


def _valid(row: Any) -> bool:
    return (isinstance(row, Mapping) and isinstance(row.get("node_id"), str)
            and type(row.get("memory_bytes")) is int and row["memory_bytes"] > 0)


def _reserve(row: Mapping[str, Any]) -> int:
    value = row.get("reserve_bytes")
    return value if type(value) is int and value > 0 else 0


def _usable(row: Mapping[str, Any]) -> int:
    """Memory one Mac can give the swarm: the app ceiling less its reserve, within its cap."""
    ceiling = member_memory_ceiling(row["memory_bytes"])
    cap = row.get("max_memory_bytes")
    if type(cap) is int and cap > 0:
        ceiling = min(ceiling, cap)
    return max(0, ceiling - _reserve(row))


def member_rows_usable(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``{node_id: usable bytes}`` for every row with a known memory size."""
    return {row["node_id"]: _usable(row) for row in rows if _valid(row)}


def _hosts(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    valid = [row for row in rows if _valid(row)]
    return sorted(valid, key=lambda r: (-_usable(r), r["node_id"]))[:MAX_HOSTS]


def estimated_need_bytes(model: Any, hosts: int) -> int:
    """Total guard memory a split over ``hosts`` Macs needs (the planner's formula, summed)."""
    shape = require_shape(model)
    resident = math.ceil(shape.weight_source_bytes * _resident_factor(model))
    kv = shape.num_layers * shape.kv_bytes_per_token_per_layer(model.runtime_dtype) * DEMO_CONTEXT_TOKENS
    return math.ceil(GUARD_HEADROOM * (LOAD_PEAK_FACTOR * resident + hosts * GROUP_FIXED_BYTES + kv))


def _short_reason(code: str, message: str) -> str:
    if code == "member_memory_insufficient_for_share":
        return "a Mac's share is more than the memory the app lets it use"
    if code == "member_disk_insufficient_for_share":
        return "a Mac does not have the free disk for its share"
    return message.rstrip(".").lower()[:90]


def _refusal(hosts: Sequence[Mapping[str, Any]], model: Any) -> tuple[str | None, dict[str, Any] | None]:
    """``(reason, plan)``: why the planner/guards refuse this model, or the accepted plan."""
    members = [{"node_id": r["node_id"], "memory_bytes": max(1, r["memory_bytes"] - _reserve(r))} for r in hosts]
    capabilities = {r["node_id"]: {"available_storage_bytes": r["available_storage_bytes"]}
                    for r in hosts if type(r.get("available_storage_bytes")) is int}
    try:
        plan = plan_for_members(model, members)
        check_members(model, plan["members"], capabilities)
    except ResourceRefused as refusal:
        return _short_reason(refusal.code, refusal.message), None
    except SplitRefused as refusal:
        if refusal.code == "member_cannot_hold_share":
            return "a Mac cannot hold its share of this model", None
        return f"it cannot be split over these Macs ({refusal.code})", None
    by_node = {r["node_id"]: r for r in hosts}
    for share in plan["members"]:
        caps = {"max_memory_bytes": by_node[share["node_id"]].get("max_memory_bytes")}
        if check_member_caps(share, caps) is not None:
            return f"a Mac's memory limit is below its {_gb(share['max_rss_bytes'])} share", None
    return None, plan


def assess_model(model: Any, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Does ``model`` fit these Macs?  ``state`` is fits | too_large | unknown, with a one-line ``line``."""
    hosts = _hosts(rows)
    usable_total = sum(_usable(r) for r in hosts)
    if len(hosts) < MIN_HOSTS:
        need = estimated_need_bytes(model, MIN_HOSTS)
        return {"state": UNKNOWN, "needs_bytes": need, "usable_bytes": usable_total, "members": [],
                "line": (f"needs about {_gb(need)} across at least two Macs; invite a Mac to check "
                         f"({_gb(usable_total)} usable so far)")}
    reason, plan = _refusal(hosts, model)
    if reason is None and plan is not None:
        need = sum(int(m["max_rss_bytes"]) for m in plan["members"])
        if need <= usable_total:
            shares = [{"layers": m["layers"], "max_rss_bytes": m["max_rss_bytes"],
                       "disk_footprint_bytes": m["disk_footprint_bytes"]} for m in plan["members"]]
            return {"state": FITS, "needs_bytes": need, "usable_bytes": usable_total, "members": shares,
                    "line": f"fits: needs {_gb(need)} of your swarm's {_gb(usable_total)}"}
        reason = "the swarm's memory is short"
    else:
        need = estimated_need_bytes(model, len(hosts))
    return {"state": TOO_LARGE, "needs_bytes": need, "usable_bytes": usable_total, "members": [],
            "line": (f"too big: needs {_gb(need)} of your swarm's {_gb(usable_total)} ({reason}); "
                     "add a Mac or choose a smaller model")}


def suggest_models(choices: Sequence[Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every choice with its fit, largest model first, and the largest one that fits."""
    ordered = sorted(choices, key=lambda c: (-c.snapshot_bytes, c.name))
    assessed = [{"name": c.name, "label": c.label, "fit": assess_model(c.model, rows)} for c in ordered]
    suggested = next((a["name"] for a in assessed if a["fit"]["state"] == FITS), None)
    hosts = _hosts(rows)
    return {"choices": assessed, "suggested": suggested, "members": len(hosts),
            "usable_bytes": sum(_usable(r) for r in hosts)}


__all__ = ["FITS", "TOO_LARGE", "UNKNOWN", "assess_model", "estimated_need_bytes", "member_rows_usable",
           "suggest_models"]
