# SPDX-License-Identifier: AGPL-3.0-only
"""Passive A9 authority fault overlay: identity unless a grant-gated publisher wrote to it.

The product evidence application reads membership through :func:`overlay_membership`.
With an empty ledger (the default, and after the publisher door closes) the records
are returned unchanged.  Only :meth:`AuthorityLedger.publish` with the exact A9
grant can change what the overlay returns, and it can only withdraw or hide
eligibility, never grant it.  Nothing here is qualification or physical evidence.
"""
from __future__ import annotations

import hmac
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from mycelium_membership.contracts import _PEER_RUNTIME_CAPABILITIES

EXACT_FLEET_GRANT = "FLEET GRANTED TO A9"
GRANT_ENV = "MYCELIUM_A9_FLEET_GRANT"
KINDS = frozenset({"stage_withdrawal", "codec_withdrawal", "unknown_registry_value", "restore"})
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_KNOWN_VALUES = frozenset(
    str(item) for caps in _PEER_RUNTIME_CAPABILITIES.values() for item in caps.values() if item is not None
)


class LedgerRefused(ValueError):
    pass


def exact_grant(value: Any) -> bool:
    return isinstance(value, str) and hmac.compare_digest(
        value.encode("utf-8"), EXACT_FLEET_GRANT.encode("utf-8"))


class AuthorityLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._entries: dict[str, tuple[str, int, str | None]] = {}

    def publish(self, kind: str, node_id: str, *, grant: Any, value: str | None = None) -> int:
        if not exact_grant(grant):
            raise LedgerRefused("explicit_fleet_grant_required")
        if kind not in KINDS:
            raise LedgerRefused("a9_authority_fault_kind_invalid")
        if not isinstance(node_id, str) or _SEGMENT.fullmatch(node_id) is None:
            raise LedgerRefused("a9_authority_fault_node_invalid")
        if kind == "unknown_registry_value":
            if (not isinstance(value, str) or _SEGMENT.fullmatch(value) is None
                    or value.lower() in {v.lower() for v in _KNOWN_VALUES}):
                raise LedgerRefused("a9_authority_fault_value_invalid")
        elif value is not None:
            raise LedgerRefused("a9_authority_fault_value_invalid")
        with self._lock:
            self._generation += 1
            self._entries[node_id] = (kind, self._generation, value)
            return self._generation

    def clear(self) -> None:
        with self._lock:
            self._generation = 0
            self._entries = {}

    def snapshot(self) -> tuple[int, dict[str, tuple[str, int, str | None]]]:
        with self._lock:
            return self._generation, dict(self._entries)


LEDGER = AuthorityLedger()


def overlay_membership(records: Sequence[Mapping[str, Any]], ledger: AuthorityLedger | None = None) -> tuple[Any, ...]:
    generation, entries = (ledger or LEDGER).snapshot()
    if not entries:
        return tuple(records)
    out: list[Any] = []
    for record in records:
        entry = entries.get(record.get("node_id")) if isinstance(record, Mapping) else None
        if entry is None:
            out.append(record)
            continue
        kind, entry_generation, value = entry
        item = dict(record)
        capability = dict(item.get("runtime_capability") or {})
        item["authority_generation"] = int(item.get("authority_generation", 1)) + entry_generation
        if kind in {"stage_withdrawal", "codec_withdrawal", "unknown_registry_value"}:
            item["activation_eligible"] = False
        if kind == "codec_withdrawal":
            capability["activation_protocol"] = None
        elif kind == "unknown_registry_value":
            capability["runtime_backend"] = value
        item["runtime_capability"] = capability
        out.append(item)
    return tuple(out)
