# SPDX-License-Identifier: AGPL-3.0-only
"""Command mailbox health per peer, read from the owner mailbox opened READ-ONLY.

The mailbox is the replay ledger of the control protocol; diagnostics must never be able to alter it, so the
connection is a ``mode=ro`` URI with ``query_only`` on, and a missing file is reported rather than created.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path
from typing import Any

STATES = ("pending", "claimed", "reconciliation_required", "completed", "failed", "expired")
SHORT_TTL_SECONDS = 10.0
MAX_ROWS = 20000


def open_readonly(path: str | Path) -> sqlite3.Connection:
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _statement(raw: Any) -> dict[str, Any]:
    try:
        statement = json.loads(raw).get("statement")
        return statement if isinstance(statement, dict) else {}
    except Exception:
        return {}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _median(values: list[float]) -> float | None:
    return None if not values else round(statistics.median(values), 4)


def empty_peer() -> dict[str, Any]:
    return {"counts": {state: 0 for state in STATES}, "total": 0, "median_claim_latency_s": None,
            "median_ttl_s": None, "min_round_trip_s": None,
            "short_ttl": {"threshold_s": SHORT_TTL_SECONDS, "total": 0, "completed": 0, "expired": 0,
                          "reconciliation_required": 0}}


def mailbox_health(path: str | Path, *, now: float, window_s: float = 600.0,
                   short_ttl_s: float = SHORT_TTL_SECONDS) -> dict[str, Any]:
    try:
        connection = open_readonly(path)
    except (sqlite3.Error, OSError):
        return {"available": False, "reason": "the command mailbox could not be opened (read-only)"}
    try:
        rows = connection.execute(
            "SELECT target_node_id, state, claimed_at, updated_at, envelope_json FROM managed_commands "
            "WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT ?", (now - window_s, MAX_ROWS)).fetchall()
    except sqlite3.Error:
        return {"available": False, "reason": "the command mailbox has an unexpected layout"}
    finally:
        connection.close()
    peers: dict[str, dict[str, Any]] = {}
    latencies: dict[str, list[float]] = {}
    ttls: dict[str, list[float]] = {}
    trips: dict[str, list[float]] = {}
    for row in rows:
        node, state = str(row["target_node_id"]), str(row["state"])
        peer = peers.setdefault(node, empty_peer())
        peer["counts"][state] = peer["counts"].get(state, 0) + 1
        peer["total"] += 1
        statement = _statement(row["envelope_json"])
        issued, expires = _number(statement.get("issued_at")), _number(statement.get("expires_at"))
        claimed, updated = _number(row["claimed_at"]), _number(row["updated_at"])
        if issued is not None and expires is not None:
            ttl = expires - issued
            ttls.setdefault(node, []).append(ttl)
            if ttl < short_ttl_s:
                short = peer["short_ttl"]
                short["total"] += 1
                if state in short:
                    short[state] += 1
        if claimed is not None and issued is not None:
            latencies.setdefault(node, []).append(max(0.0, claimed - issued))
        if state == "completed" and claimed is not None and updated is not None:
            trips.setdefault(node, []).append(max(0.0, updated - claimed))
    for node, peer in peers.items():
        peer["median_claim_latency_s"] = _median(latencies.get(node, []))
        peer["median_ttl_s"] = _median(ttls.get(node, []))
        peer["min_round_trip_s"] = None if not trips.get(node) else round(min(trips[node]), 4)
    return {"available": True, "window_minutes": round(window_s / 60.0, 2), "rows": len(rows), "peers": peers}
