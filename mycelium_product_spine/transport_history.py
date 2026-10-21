# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable direct/relay transition history from live transport observations.

The live route projects its verified sidecar path observations into a bounded
A8 ``activation_history``. That list is only as durable as the route object: a
cold route restart starts it empty, a later publication can reuse an
observation id with different facts, and nothing refuses an observation that is
older than, or contradicts, what was already observed for the same endpoint.

This module is the product spine's own append-only ledger over those
observations. For every publication it:

* retains each resolved ``bound_live_connection`` observation exactly as first
  observed (every field but ``freshness`` is immutable), so a direct segment is
  never rewritten when a relay segment is added;
* refuses a rewrite of a known observation id, an observation whose connection
  generation or observation time regresses for its endpoint (stale, unbound to
  the current connection generation), and one that claims a different path
  class under the same connection generation. Evidence lifetime stays the A8
  projection's authority. A refused current observation never becomes current:
  the published current observation is the unknown projection;
* bounds the ledger deterministically and never invents an observation, a
  transition, a region or a metric;
* optionally persists the ledger in the owner-private product state root,
  bound to one deployment, so a transition across a planned serve restart is
  still observed. A restored ledger is re-admitted through the same refusal
  rules; any refusal, duplicate, malformed item or overflow fails closed.

A transition is two consecutive retained observations of one endpoint whose
path classes differ; the route explorer projects each as its own immutable
segment carrying its connection generation.
"""
from __future__ import annotations

import threading
from typing import Any, Mapping

from mycelium_internet.contracts import validate_activation_observation

from .contracts import ProductContractError, validate_internet_native_snapshot
from .state import ProductEvidenceStateError, TransportHistoryStateStore

MAX_RETAINED = 63
_RESOLVED = frozenset({"direct", "relay"})


def _resolved(item: Mapping[str, Any]) -> bool:
    return (
        item.get("path_source") == "bound_live_connection"
        and item.get("path_class") in _RESOLVED
        and item.get("endpoint_pseudonym") is not None
    )


def _immutable(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "freshness"}


def _unknown_current(now_unix_ms: int) -> dict[str, Any]:
    from .projector import _unknown_internet_native

    return _unknown_internet_native(now_unix_ms)["activation_observation"]


class TransportTransitionHistory:
    """Append-only, bounded ledger of accepted live transport observations."""

    def __init__(
        self,
        *,
        limit: int = MAX_RETAINED,
        store: TransportHistoryStateStore | None = None,
    ) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_RETAINED:
            raise ValueError("transport_history_limit_invalid")
        if store is not None and not isinstance(store, TransportHistoryStateStore):
            raise ValueError("transport_history_store_invalid")
        self._limit = limit
        self._lock = threading.Lock()
        self._ledger: dict[str, dict[str, Any]] = {}
        self._rejections = {"rewrite": 0, "regression": 0, "contradiction": 0}
        self._store = store
        if store is not None:
            self._restore(store.load())

    def _restore(self, items: list[Any]) -> None:
        """Re-admit a persisted ledger exactly, or refuse the whole of it."""

        if len(items) > self._limit:
            raise ProductEvidenceStateError("product_transport_history_corrupt")
        for item in items:
            try:
                valid = dict(validate_activation_observation(item)) == item
            except (TypeError, ValueError, KeyError):
                valid = False
            if (
                not valid
                or not _resolved(item)
                or item["observation_id"] in self._ledger
                or self._refusal(item, 0) is not None
            ):
                raise ProductEvidenceStateError("product_transport_history_corrupt")
            self._ledger[item["observation_id"]] = dict(item)

    @property
    def rejections(self) -> dict[str, int]:
        with self._lock:
            return dict(self._rejections)

    def transitions(self) -> list[dict[str, Any]]:
        """Consecutive per-endpoint class changes over the retained ledger."""

        with self._lock:
            ordered = list(self._ledger.values())
        last: dict[str, dict[str, Any]] = {}
        result = []
        for item in ordered:
            endpoint = item["endpoint_pseudonym"]
            prior = last.get(endpoint)
            if prior is not None and prior["path_class"] != item["path_class"]:
                result.append({
                    "endpoint_pseudonym": endpoint,
                    "from_path_class": prior["path_class"],
                    "to_path_class": item["path_class"],
                    "from_connection_generation": prior["connection_generation"],
                    "to_connection_generation": item["connection_generation"],
                    "from_observation_id": prior["observation_id"],
                    "to_observation_id": item["observation_id"],
                })
            last[endpoint] = item
        return result

    def _refusal(self, item: Mapping[str, Any], now_unix_ms: int) -> str | None:
        key = item["observation_id"]
        known = self._ledger.get(key)
        if known is not None:
            return None if _immutable(known) == _immutable(item) else "rewrite"
        endpoint = item["endpoint_pseudonym"]
        prior = None
        for retained in self._ledger.values():
            if retained["endpoint_pseudonym"] == endpoint:
                prior = retained
        if prior is None:
            return None
        if (item["connection_generation"] < prior["connection_generation"]
                or item["observed_at_unix_ms"] <= prior["observed_at_unix_ms"]):
            return "regression"
        if (item["connection_generation"] == prior["connection_generation"]
                and item["path_class"] != prior["path_class"]):
            return "contradiction"
        return None

    def observe(self, document: Mapping[str, Any], *, now_unix_ms: int) -> dict[str, Any]:
        """Admit one live A8 projection and publish the immutable history."""

        if type(now_unix_ms) is not int or now_unix_ms < 0:
            raise ValueError("transport_history_clock_invalid")
        try:
            detached = validate_internet_native_snapshot(document)
        except ProductContractError:
            raise
        current = detached["activation_observation"]
        current_refused = False
        with self._lock:
            before = list(self._ledger)
            for item in detached["activation_history"]:
                if not _resolved(item):
                    continue
                refusal = self._refusal(item, now_unix_ms)
                if refusal is not None:
                    self._rejections[refusal] += 1
                    if item["observation_id"] == current["observation_id"]:
                        current_refused = True
                    continue
                if item["observation_id"] not in self._ledger:
                    self._ledger[item["observation_id"]] = dict(item)
            while len(self._ledger) > self._limit:
                self._ledger.pop(next(iter(self._ledger)))
            retained = [dict(item) for item in self._ledger.values()]
            if self._store is not None and list(self._ledger) != before:
                self._store.write(retained)
        if current_refused:
            current = _unknown_current(now_unix_ms)
            detached["relay_projection"] = None
        accepted = {item["observation_id"]: item for item in retained}
        if _resolved(current) and current["observation_id"] in accepted:
            # The accepted current observation is published last, exactly as
            # first retained (its later freshness may differ).
            current = {**accepted[current["observation_id"]], "freshness": current["freshness"]}
            retained = [item for item in retained
                        if item["observation_id"] != current["observation_id"]]
        history = [*retained[-(MAX_RETAINED):], current]
        detached["activation_history"] = history
        detached["activation_observation"] = history[-1]
        return validate_internet_native_snapshot(detached)


__all__ = ["MAX_RETAINED", "TransportTransitionHistory"]
