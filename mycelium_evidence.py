"""Truthful live and historical evidence projections for the product gateway."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import uuid


EVIDENCE_PROJECTION_PROTOCOL = "mycelium.evidence_projection.v1"
EVIDENCE_HISTORY_PROTOCOL = "mycelium.evidence_history.v1"

SOURCE_KINDS = frozenset(
    {"live_runtime", "planner_intent", "sealed_historical", "replay", "fixture"}
)
FRESHNESS_BY_SOURCE_KIND = {
    "live_runtime": frozenset({"current", "degraded", "stale"}),
    "planner_intent": frozenset({"current", "degraded", "stale"}),
    "sealed_historical": frozenset({"historical"}),
    "replay": frozenset({"replay"}),
    "fixture": frozenset({"fixture"}),
}
CAPABILITIES = frozenset(
    {
        "route_execution",
        "replicated_serving",
        "scoped_recovery",
        "speculative_decoding",
        "heterogeneous_participation",
        "release_closure",
        "stage_local_kv",
    }
)

_ENVELOPE_FIELDS = frozenset(
    {
        "protocol",
        "record_id",
        "capability",
        "source_kind",
        "authority",
        "generation",
        "captured_at_unix_ms",
        "observed_at_unix_ms",
        "valid_until_unix_ms",
        "freshness",
        "payload_protocol",
        "payload",
    }
)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name}_invalid")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name}_invalid")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name}_invalid")
    return value


def validate_evidence_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach one closed browser-safe evidence envelope."""

    if not isinstance(value, Mapping) or set(value) != _ENVELOPE_FIELDS:
        raise ValueError("evidence_projection_shape_invalid")
    result = copy.deepcopy(dict(value))
    if result["protocol"] != EVIDENCE_PROJECTION_PROTOCOL:
        raise ValueError("evidence_projection_protocol_invalid")
    _text(result["record_id"], "record_id")
    capability = _text(result["capability"], "capability")
    if capability not in CAPABILITIES:
        raise ValueError("capability_invalid")
    source_kind = _text(result["source_kind"], "source_kind")
    if source_kind not in SOURCE_KINDS:
        raise ValueError("source_kind_invalid")
    freshness = _text(result["freshness"], "freshness")
    if freshness not in FRESHNESS_BY_SOURCE_KIND[source_kind]:
        raise ValueError("source_freshness_mismatch")
    _text(result["authority"], "authority")
    _nonnegative_integer(result["generation"], "generation")
    captured = _positive_integer(result["captured_at_unix_ms"], "captured_at_unix_ms")
    observed = _positive_integer(result["observed_at_unix_ms"], "observed_at_unix_ms")
    if captured < observed:
        raise ValueError("capture_precedes_observation")
    valid_until = result["valid_until_unix_ms"]
    if source_kind in {"sealed_historical", "replay", "fixture"}:
        if valid_until is not None:
            raise ValueError("immutable_source_cannot_expire")
    else:
        valid = _positive_integer(valid_until, "valid_until_unix_ms")
        if valid < captured:
            raise ValueError("evidence_already_expired_at_capture")
    payload_protocol = _text(result["payload_protocol"], "payload_protocol")
    payload = result["payload"]
    if not isinstance(payload, dict) or payload.get("protocol") != payload_protocol:
        raise ValueError("payload_protocol_mismatch")
    return result


def evidence_is_current_live(value: Mapping[str, Any], *, now_unix_ms: int) -> bool:
    """Return whether an envelope is fresh live runtime evidence, never gate authority."""

    result = validate_evidence_projection(value)
    now = _positive_integer(now_unix_ms, "now_unix_ms")
    return (
        result["source_kind"] == "live_runtime"
        and result["freshness"] == "current"
        and result["valid_until_unix_ms"] >= now
    )


def sealed_evidence_projection(
    *,
    record_id: str,
    capability: str,
    authority: str,
    generation: int,
    observed_at_unix_ms: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Create an immutable historical projection without refreshing its source time."""

    document = copy.deepcopy(dict(payload))
    captured = _positive_integer(observed_at_unix_ms, "observed_at_unix_ms")
    return validate_evidence_projection(
        {
            "protocol": EVIDENCE_PROJECTION_PROTOCOL,
            "record_id": record_id,
            "capability": capability,
            "source_kind": "sealed_historical",
            "authority": authority,
            "generation": generation,
            "captured_at_unix_ms": captured,
            "observed_at_unix_ms": captured,
            "valid_until_unix_ms": None,
            "freshness": "historical",
            "payload_protocol": document.get("protocol"),
            "payload": document,
        }
    )


class EvidenceProjectionRegistry:
    """Project changing runtime state separately from immutable historical records."""

    def __init__(
        self,
        *,
        runtime_source: Callable[[], Mapping[str, Any]],
        historical_records: Sequence[Mapping[str, Any]] = (),
        clock_unix_ms: Callable[[], int] | None = None,
        live_ttl_ms: int = 3_000,
        incarnation: str | None = None,
    ) -> None:
        if not callable(runtime_source):
            raise ValueError("runtime_evidence_source_invalid")
        if type(live_ttl_ms) is not int or live_ttl_ms <= 0:
            raise ValueError("runtime_evidence_ttl_invalid")
        self._runtime_source = runtime_source
        self._clock_unix_ms = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._live_ttl_ms = live_ttl_ms
        self._incarnation = incarnation or uuid.uuid4().hex
        self._generation = 0
        self._observed_at_unix_ms = 0
        self._last_payload_digest: str | None = None
        self._lock = threading.RLock()
        by_id: dict[str, dict[str, Any]] = {}
        digests: dict[str, str] = {}
        for raw in historical_records:
            record = validate_evidence_projection(raw)
            if record["source_kind"] != "sealed_historical":
                raise ValueError("historical_register_source_invalid")
            record_id = record["record_id"]
            digest = _digest(record)
            if record_id in by_id and digests[record_id] != digest:
                raise ValueError("historical_record_conflict")
            by_id[record_id] = record
            digests[record_id] = digest
        self._historical = tuple(
            sorted(
                by_id.values(),
                key=lambda item: (item["observed_at_unix_ms"], item["record_id"]),
            )
        )

    def runtime(self) -> dict[str, Any]:
        payload = copy.deepcopy(dict(self._runtime_source()))
        if payload.get("protocol") != "mycelium.live_route_status.v1":
            raise ValueError("runtime_evidence_payload_invalid")
        now = _positive_integer(self._clock_unix_ms(), "captured_at_unix_ms")
        payload_digest = _digest(payload)
        with self._lock:
            if payload_digest != self._last_payload_digest:
                self._generation += 1
                self._observed_at_unix_ms = now
                self._last_payload_digest = payload_digest
            route_alive = payload.get("route_alive") is True
            fatal = (payload.get("counters") or {}).get("fatal")
            freshness = "current" if route_alive and fatal is None else "degraded"
            return validate_evidence_projection(
                {
                    "protocol": EVIDENCE_PROJECTION_PROTOCOL,
                    "record_id": f"runtime-{self._incarnation}",
                    "capability": "route_execution",
                    "source_kind": "live_runtime",
                    "authority": "mycelium_live.route:public_status",
                    "generation": self._generation,
                    "captured_at_unix_ms": now,
                    "observed_at_unix_ms": self._observed_at_unix_ms,
                    "valid_until_unix_ms": now + self._live_ttl_ms,
                    "freshness": freshness,
                    "payload_protocol": payload["protocol"],
                    "payload": payload,
                }
            )

    def history(self, *, capability: str | None = None) -> dict[str, Any]:
        if capability is not None and capability not in CAPABILITIES:
            raise ValueError("evidence_capability_invalid")
        records = [
            copy.deepcopy(item)
            for item in self._historical
            if capability is None or item["capability"] == capability
        ]
        return {
            "protocol": EVIDENCE_HISTORY_PROTOCOL,
            "records": records,
        }
