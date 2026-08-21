# SPDX-License-Identifier: AGPL-3.0-or-later
"""Internet-native activation observations and the relay projection (spec §6).

Path class comes only from bound live-connection evidence - never from LAN
membership, OS, or configuration. Every metric is nullable; missing, stale,
rejected, or unmeasured values project as ``unknown``, and a numeric zero
requires a current measured sample. Relay identity is projected only as a
versioned domain-separated HMAC under an owner-private persistent key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac as hmac_module
import re
from typing import Any, Callable, Mapping

from .contracts import (
    ACTIVATION_OBSERVATION_PROTOCOL,
    validate_activation_observation,
)

ACTIVATION_PROTOCOL = ACTIVATION_OBSERVATION_PROTOCOL
_METRIC_KEYS = frozenset(
    {
        "rtt_ms",
        "warm_rtt_ms",
        "jitter_ms",
        "goodput_bytes_per_second",
        "loss_ratio",
        "sample_count",
        "measured_zero",
    }
)
_DOMAIN_SEPARATOR_PREFIX = b"mycelium.relay.projection.v"
_MAX_HISTORY = 64
_MAX_RELAY_IDENTITY_BYTES = 256
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ActivationError(RuntimeError):
    def __init__(self, code: str) -> None:
        if _CODE_RE.fullmatch(code) is None:
            raise ValueError("activation error code is invalid")
        self.code = code
        super().__init__(code)


class PathClass(str, Enum):
    DIRECT = "direct"
    RELAY = "relay"
    UNKNOWN = "unknown"


def _validate_metrics(metrics: Mapping[str, Any] | None) -> None:
    if metrics is None:
        return
    if not isinstance(metrics, Mapping) or set(metrics) != _METRIC_KEYS:
        raise ActivationError("evidence_invalid")
    if not isinstance(metrics.get("measured_zero"), bool):
        raise ActivationError("evidence_invalid")
    sample_count = metrics.get("sample_count")
    if sample_count is not None and (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < 0
    ):
        raise ActivationError("evidence_invalid")
    for key in ("rtt_ms", "warm_rtt_ms", "jitter_ms", "goodput_bytes_per_second"):
        value = metrics.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ActivationError("evidence_invalid")
    loss_ratio = metrics.get("loss_ratio")
    if loss_ratio is not None:
        if (
            isinstance(loss_ratio, bool)
            or not isinstance(loss_ratio, (int, float))
            or not 0.0 <= float(loss_ratio) <= 1.0
            or float(loss_ratio) != float(loss_ratio)
        ):
            raise ActivationError("evidence_invalid")
        if sample_count is None or sample_count < 1:
            raise ActivationError("zero_loss_requires_samples")
        if float(loss_ratio) == 0.0:
            if not metrics.get("measured_zero"):
                raise ActivationError("zero_loss_requires_samples")
        elif metrics.get("measured_zero"):
            raise ActivationError("evidence_invalid")
    elif sample_count is not None:
        raise ActivationError("evidence_invalid")
    if sample_count is None and any(
        metrics.get(key) is not None
        for key in ("rtt_ms", "warm_rtt_ms", "jitter_ms", "goodput_bytes_per_second")
    ):
        raise ActivationError("evidence_invalid")


@dataclass(frozen=True, slots=True)
class ConnectionEvidence:
    """One bound live-connection observation.

    ``relay_identity`` and ``relay_region`` are redaction inputs only; they
    are never projected raw. ``hints`` must be absent or empty: same-LAN,
    different-network, OS, or configuration hints are not path evidence.
    """

    connection_generation: int
    connection_reuse: int
    path_class: PathClass
    endpoint_id: str
    metrics: Mapping[str, Any] | None
    observed_at_unix_ms: int
    relay_identity: str | None = None
    relay_region: str | None = None
    hints: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.connection_generation, bool)
            or not isinstance(self.connection_generation, int)
            or self.connection_generation < 0
        ):
            raise ValueError("connection_generation is invalid")
        if (
            isinstance(self.connection_reuse, bool)
            or not isinstance(self.connection_reuse, int)
            or self.connection_reuse < 0
        ):
            raise ValueError("connection_reuse is invalid")
        if not isinstance(self.path_class, PathClass):
            raise ValueError("path_class is invalid")
        if (
            not isinstance(self.endpoint_id, str)
            or not self.endpoint_id
            or len(self.endpoint_id.encode("utf-8")) > 128
        ):
            raise ValueError("endpoint_id is invalid")
        if (
            isinstance(self.observed_at_unix_ms, bool)
            or not isinstance(self.observed_at_unix_ms, int)
            or self.observed_at_unix_ms < 0
        ):
            raise ValueError("observed_at_unix_ms is invalid")
        if self.hints is not None:
            if not isinstance(self.hints, Mapping) or self.hints:
                raise ValueError("path hints are not evidence")
        if self.path_class is PathClass.UNKNOWN and self.metrics is not None:
            raise ValueError("unknown path carries no metrics")
        _validate_metrics(self.metrics)
        if self.relay_identity is not None and (
            not isinstance(self.relay_identity, str)
            or not self.relay_identity
            or len(self.relay_identity.encode("utf-8")) > _MAX_RELAY_IDENTITY_BYTES
        ):
            raise ValueError("relay_identity is invalid")
        if self.relay_region is not None and (
            not isinstance(self.relay_region, str)
            or not 1 <= len(self.relay_region.encode("utf-8")) <= 64
        ):
            raise ValueError("relay_region is invalid")


def _pseudonym(endpoint_id: str) -> str:
    return "sha256:" + hashlib.sha256(endpoint_id.encode("utf-8")).hexdigest()


def build_activation_observation(
    *,
    evidence: ConnectionEvidence,
    endpoint_pseudonym: str | None,
    observation_id: str,
    freshness: str = "current",
) -> dict[str, Any]:
    """Emit one closed observation document, raw relay identity redacted."""

    if not isinstance(evidence, ConnectionEvidence):
        raise ActivationError("evidence_invalid")
    if (
        not isinstance(observation_id, str)
        or not 1 <= len(observation_id.encode("utf-8")) <= 128
    ):
        raise ActivationError("evidence_invalid")
    if freshness not in {"current", "stale", "unknown"}:
        raise ActivationError("evidence_invalid")
    metrics = evidence.metrics
    document: dict[str, Any] = {
        "protocol": ACTIVATION_PROTOCOL,
        "observation_id": observation_id,
        "connection_generation": evidence.connection_generation,
        "connection_reuse": evidence.connection_reuse,
        "path_class": evidence.path_class.value,
        "path_source": (
            "bound_live_connection"
            if evidence.path_class is not PathClass.UNKNOWN
            else "unknown"
        ),
        "endpoint_pseudonym": endpoint_pseudonym,
        "observed_at_unix_ms": evidence.observed_at_unix_ms,
        "freshness": freshness,
        "evidence_lifetime_until_unix_ms": evidence.observed_at_unix_ms + 90_000,
        "metrics": {
            "rtt_ms": None,
            "warm_rtt_ms": None,
            "jitter_ms": None,
            "goodput_bytes_per_second": None,
            "loss_ratio": None,
            "sample_count": None,
            "measured_zero": False,
        },
    }
    if metrics is not None:
        for key in (
            "rtt_ms",
            "warm_rtt_ms",
            "jitter_ms",
            "goodput_bytes_per_second",
            "loss_ratio",
            "sample_count",
        ):
            document["metrics"][key] = metrics.get(key)
        document["metrics"]["measured_zero"] = metrics.get("measured_zero", False)
    validate_activation_observation(document)
    return document


class ActivationObservations:
    """Bounded per-device activation observation ledger.

    The ledger is the only source of path claims. It enforces exact
    EndpointID binding, the unknown-not-zero invariant, transition history
    retention, and stale-evidence projection.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        freshness_seconds: float = 90.0,
        max_history: int = _MAX_HISTORY,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock is invalid")
        if (
            isinstance(freshness_seconds, bool)
            or not isinstance(freshness_seconds, (int, float))
            or not 0.0 < float(freshness_seconds) <= 86400.0
        ):
            raise ValueError("freshness_seconds is invalid")
        if (
            isinstance(max_history, bool)
            or not isinstance(max_history, int)
            or max_history < 1
        ):
            raise ValueError("max_history is invalid")
        self._clock = clock
        self._freshness_seconds = float(freshness_seconds)
        self._max_history = max_history
        self._history: list[dict[str, Any]] = []
        self._endpoint_id: str | None = None
        self._endpoint_pseudonym: str | None = None
        self._last_observed_at_unix_ms: int | None = None
        self._sequence = 0

    def record(self, evidence: ConnectionEvidence) -> dict[str, Any]:
        if not isinstance(evidence, ConnectionEvidence):
            raise ActivationError("evidence_invalid")
        if self._endpoint_id is None:
            self._endpoint_id = evidence.endpoint_id
            self._endpoint_pseudonym = _pseudonym(evidence.endpoint_id)
        elif evidence.endpoint_id != self._endpoint_id:
            raise ActivationError("endpoint_mismatch")
        # Defense in depth: the explicit-zero rule is enforced at intake too.
        _validate_metrics(evidence.metrics)
        self._sequence += 1
        document = build_activation_observation(
            evidence=evidence,
            endpoint_pseudonym=self._endpoint_pseudonym,
            observation_id=f"observation-{self._sequence}",
        )
        self._history.append(document)
        if len(self._history) > self._max_history:
            del self._history[: len(self._history) - self._max_history]
        self._last_observed_at_unix_ms = evidence.observed_at_unix_ms
        return dict(document)

    def _unknown_projection(self, *, freshness: str) -> dict[str, Any]:
        return {
            "protocol": ACTIVATION_PROTOCOL,
            "observation_id": "projection-unknown",
            "connection_generation": 0,
            "connection_reuse": 0,
            "path_class": "unknown",
            "path_source": "unknown",
            "endpoint_pseudonym": self._endpoint_pseudonym,
            "observed_at_unix_ms": int(float(self._clock()) * 1000.0),
            "freshness": freshness,
            "evidence_lifetime_until_unix_ms": (
                int(float(self._clock()) * 1000.0) + 90_000
            ),
            "metrics": {
                "rtt_ms": None,
                "warm_rtt_ms": None,
                "jitter_ms": None,
                "goodput_bytes_per_second": None,
                "loss_ratio": None,
                "sample_count": None,
                "measured_zero": False,
            },
        }

    def current_projection(self) -> dict[str, Any]:
        if not self._history or self._last_observed_at_unix_ms is None:
            return self._unknown_projection(freshness="unknown")
        now_ms = int(float(self._clock()) * 1000.0)
        age_seconds = (now_ms - self._last_observed_at_unix_ms) / 1000.0
        if age_seconds > self._freshness_seconds:
            return self._unknown_projection(freshness="stale")
        return dict(self._history[-1])

    def history(self) -> list[dict[str, Any]]:
        return [dict(document) for document in self._history]


class RelayProjector:
    """HMAC-SHA256 relay projection with a versioned domain separator.

    ``reference`` is stable for the same canonical relay identity under one
    persistent owner-private key and domain version. Raw URLs, IPs, ports,
    and DNS names never leave this class as projection content.
    """

    def __init__(self, *, projection_key: bytes, domain_version: int = 1) -> None:
        if (
            not isinstance(projection_key, bytes)
            or len(projection_key) < 32
        ):
            raise ValueError("projection_key is invalid")
        if (
            isinstance(domain_version, bool)
            or not isinstance(domain_version, int)
            or domain_version < 1
        ):
            raise ValueError("domain_version is invalid")
        self.projection_key = projection_key
        self._domain_separator = _DOMAIN_SEPARATOR_PREFIX + str(
            domain_version
        ).encode("ascii")

    def reference(self, canonical_identity: str) -> str:
        if (
            not isinstance(canonical_identity, str)
            or not canonical_identity
            or len(canonical_identity.encode("utf-8")) > _MAX_RELAY_IDENTITY_BYTES
        ):
            raise ValueError("canonical_identity is invalid")
        digest = hmac_module.new(
            self.projection_key,
            self._domain_separator + b"||" + canonical_identity.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def region(self, value: str | None, *, reviewed: bool) -> str:
        if not reviewed or value is None:
            return "unknown"
        if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 64:
            raise ValueError("region is invalid")
        return value


__all__ = [
    "ACTIVATION_PROTOCOL",
    "ActivationError",
    "ActivationObservations",
    "ConnectionEvidence",
    "PathClass",
    "RelayProjector",
    "build_activation_observation",
]
