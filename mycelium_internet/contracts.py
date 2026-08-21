# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closed, privacy-reduced A8 internet-native contracts.

Four capability-named closed shapes:

- ``mycelium.internet_bootstrap_status.v1``
- ``mycelium.internet_activation_observation.v1``
- ``mycelium.relay_projection.v1``
- ``mycelium.internet_native_qualification.v1``

Every shape rejects unknown fields, non-finite numbers, invalid nullability,
raw network identity, and privacy-bearing keys (prompts, outputs, tokens,
tensors, activations, KV content, credentials, secrets).
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

BOOTSTRAP_STATUS_PROTOCOL = "mycelium.internet_bootstrap_status.v1"
ACTIVATION_OBSERVATION_PROTOCOL = "mycelium.internet_activation_observation.v1"
RELAY_PROJECTION_PROTOCOL = "mycelium.relay_projection.v1"
INTERNET_NATIVE_QUALIFICATION_PROTOCOL = (
    "mycelium.internet_native_qualification.v1"
)

_FRESHNESS_VALUES = {"current", "stale", "unknown"}
_TLS_STATES = {"publicly_trusted", "unverified", "unknown"}
_PIN_STATES = {"verified", "mismatch", "unknown"}
_ROUTE_STATES = {"available", "unavailable", "unknown"}
_INVITATION_STATES = {"pending", "accepted", "rejected", "unknown"}
_PATH_CLASSES = {"direct", "relay", "unknown"}
_PATH_SOURCES = {"bound_live_connection", "unknown"}
_GATE_KINDS = {"physical_positive", "physical_negative"}
_RESULTS = {"passed", "failed", "not_executed"}
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RELAY_REFERENCE_RE = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_FORBIDDEN_KEY_RE = re.compile(
    r"(?:hostname|host|address|ip_address|url|relay_url|dns_name|endpoint_id|"
    r"private_path|credential|credentials|secret|password|token|bearer|"
    r"authorization|cookie|prompt|output|logit|logits|tensor|tensors|"
    r"activation|activations|kv_[a-z0-9_]*|key_value[a-z0-9_]*)\Z"
)
_MAX_QUALIFICATION_CASES = 32
_MAX_EVIDENCE_DIGESTS = 32
_MAX_REGION_BYTES = 64


class InternetContractError(ValueError):
    """A closed A8 contract validation failure with a bounded code."""


def _bounded_text(value: object, *, maximum: int = 256) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= maximum
    )


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= (1 << 63) - 1


def _digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _exact(document: Mapping[str, Any], fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != fields:
        raise InternetContractError(code)
    return dict(document)


def _reject_forbidden_keys(document: Mapping[str, Any], code: str) -> None:
    for key in document:
        if not isinstance(key, str):
            raise InternetContractError(code)
        if _FORBIDDEN_KEY_RE.fullmatch(key) is not None:
            raise InternetContractError(code)
        value = document[key]
        if isinstance(value, Mapping):
            _reject_forbidden_keys(value, code)


def _bounded_unique_names(values: object, *, maximum: int, code: str) -> list[str]:
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= maximum
        or len(values) != len(set(values))
    ):
        raise InternetContractError(code)
    names = []
    for value in values:
        if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
            raise InternetContractError(code)
        names.append(value)
    return names


def _reject_non_finite(value: object, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not float(value) == float(value)
        or float(value) in (float("inf"), float("-inf"))
    ):
        raise InternetContractError(code)
    return float(value)


def validate_bootstrap_status(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "generation",
            "observed_at_unix_ms",
            "freshness",
            "tls_state",
            "canonical_origin_verified",
            "seed_pin_state",
            "route_state",
            "invitation_state",
            "counters",
        },
        "internet_bootstrap_status_invalid",
    )
    _reject_forbidden_keys(data, "internet_bootstrap_status_privacy")
    if data.get("protocol") != BOOTSTRAP_STATUS_PROTOCOL:
        raise InternetContractError("internet_bootstrap_status_invalid")
    if not _integer(data.get("generation"), minimum=1):
        raise InternetContractError("internet_bootstrap_status_invalid")
    _reject_non_finite(data.get("observed_at_unix_ms"), "internet_bootstrap_status_invalid")
    if not _integer(data.get("observed_at_unix_ms")):
        raise InternetContractError("internet_bootstrap_status_invalid")
    if data.get("freshness") not in _FRESHNESS_VALUES:
        raise InternetContractError("internet_bootstrap_status_invalid")
    if data.get("tls_state") not in _TLS_STATES:
        raise InternetContractError("internet_bootstrap_status_invalid")
    if not isinstance(data.get("canonical_origin_verified"), bool):
        raise InternetContractError("internet_bootstrap_status_invalid")
    if data.get("seed_pin_state") not in _PIN_STATES:
        raise InternetContractError("internet_bootstrap_status_invalid")
    if data.get("route_state") not in _ROUTE_STATES:
        raise InternetContractError("internet_bootstrap_status_invalid")
    if data.get("invitation_state") not in _INVITATION_STATES:
        raise InternetContractError("internet_bootstrap_status_invalid")
    counters = data.get("counters")
    if not isinstance(counters, Mapping) or set(counters) != {
        "requests",
        "joins_accepted",
        "joins_rejected",
    }:
        raise InternetContractError("internet_bootstrap_status_invalid")
    for key in ("requests", "joins_accepted", "joins_rejected"):
        if not _integer(counters.get(key)):
            raise InternetContractError("internet_bootstrap_status_invalid")
    return data


def validate_activation_observation(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "observation_id",
            "connection_generation",
            "connection_reuse",
            "path_class",
            "path_source",
            "endpoint_pseudonym",
            "observed_at_unix_ms",
            "freshness",
            "evidence_lifetime_until_unix_ms",
            "metrics",
        },
        "internet_activation_observation_invalid",
    )
    _reject_forbidden_keys(data, "internet_activation_observation_privacy")
    if data.get("protocol") != ACTIVATION_OBSERVATION_PROTOCOL:
        raise InternetContractError("internet_activation_observation_invalid")
    if not _bounded_text(data.get("observation_id"), maximum=128):
        raise InternetContractError("internet_activation_observation_invalid")
    if not _integer(data.get("connection_generation")):
        raise InternetContractError("internet_activation_observation_invalid")
    if not _integer(data.get("connection_reuse")):
        raise InternetContractError("internet_activation_observation_invalid")
    if data.get("path_class") not in _PATH_CLASSES:
        raise InternetContractError("internet_activation_observation_invalid")
    if data.get("path_source") not in _PATH_SOURCES:
        raise InternetContractError("internet_activation_observation_invalid")
    pseudonym = data.get("endpoint_pseudonym")
    if pseudonym is not None and not _digest(pseudonym):
        raise InternetContractError("internet_activation_observation_invalid")
    if not _integer(data.get("observed_at_unix_ms")):
        raise InternetContractError("internet_activation_observation_invalid")
    if not _integer(data.get("evidence_lifetime_until_unix_ms")):
        raise InternetContractError("internet_activation_observation_invalid")
    if data.get("freshness") not in _FRESHNESS_VALUES:
        raise InternetContractError("internet_activation_observation_invalid")
    metrics = data.get("metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != {
        "rtt_ms",
        "warm_rtt_ms",
        "jitter_ms",
        "goodput_bytes_per_second",
        "loss_ratio",
        "sample_count",
        "measured_zero",
    }:
        raise InternetContractError("internet_activation_observation_invalid")
    if not isinstance(metrics.get("measured_zero"), bool):
        raise InternetContractError("internet_activation_observation_invalid")
    sample_count = metrics.get("sample_count")
    if sample_count is not None and not _integer(sample_count):
        raise InternetContractError("internet_activation_observation_invalid")
    for key in ("rtt_ms", "warm_rtt_ms", "jitter_ms", "goodput_bytes_per_second"):
        value = metrics.get(key)
        if value is not None and not _integer(value):
            raise InternetContractError("internet_activation_observation_invalid")
    loss_ratio = metrics.get("loss_ratio")
    if loss_ratio is not None:
        value = _reject_non_finite(loss_ratio, "internet_activation_observation_invalid")
        if not 0.0 <= value <= 1.0:
            raise InternetContractError("internet_activation_observation_invalid")
        if sample_count is None or sample_count < 1:
            raise InternetContractError("internet_activation_observation_invalid")
        if value == 0.0:
            if not metrics.get("measured_zero"):
                raise InternetContractError("internet_activation_observation_invalid")
        elif metrics.get("measured_zero"):
            raise InternetContractError("internet_activation_observation_invalid")
    elif sample_count is not None:
        raise InternetContractError("internet_activation_observation_invalid")
    if (
        sample_count is None
        and any(metrics.get(key) is not None for key in
                ("rtt_ms", "warm_rtt_ms", "jitter_ms", "goodput_bytes_per_second"))
    ):
        raise InternetContractError("internet_activation_observation_invalid")
    claimed_path = data.get("path_class") in {"direct", "relay"}
    if claimed_path:
        if data.get("path_source") != "bound_live_connection":
            raise InternetContractError("internet_activation_observation_invalid")
        if data.get("freshness") != "current":
            raise InternetContractError("internet_activation_observation_invalid")
        if pseudonym is None:
            raise InternetContractError("internet_activation_observation_invalid")
    if data.get("freshness") != "current":
        if data.get("path_class") != "unknown" or data.get("path_source") != "unknown":
            raise InternetContractError("internet_activation_observation_invalid")
        if any(value is not None for key, value in metrics.items() if key != "measured_zero"):
            raise InternetContractError("internet_activation_observation_invalid")
    if data.get("path_class") == "unknown":
        if data.get("path_source") != "unknown":
            raise InternetContractError("internet_activation_observation_invalid")
        if metrics.get("measured_zero") or any(
            value is not None for key, value in metrics.items() if key != "measured_zero"
        ):
            raise InternetContractError("internet_activation_observation_invalid")
    return data


def validate_relay_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "relay_reference",
            "region",
            "projection_generation",
            "stable",
            "observed_at_unix_ms",
        },
        "relay_projection_invalid",
    )
    _reject_forbidden_keys(data, "relay_projection_privacy")
    if data.get("protocol") != RELAY_PROJECTION_PROTOCOL:
        raise InternetContractError("relay_projection_invalid")
    reference = data.get("relay_reference")
    if (
        not isinstance(reference, str)
        or _RELAY_REFERENCE_RE.fullmatch(reference) is None
    ):
        raise InternetContractError("relay_projection_invalid")
    region = data.get("region")
    if region != "unknown" and not (
        _bounded_text(region, maximum=_MAX_REGION_BYTES)
        and region == region.strip()
    ):
        raise InternetContractError("relay_projection_invalid")
    if not _integer(data.get("projection_generation"), minimum=1):
        raise InternetContractError("relay_projection_invalid")
    if not isinstance(data.get("stable"), bool):
        raise InternetContractError("relay_projection_invalid")
    if not _integer(data.get("observed_at_unix_ms")):
        raise InternetContractError("relay_projection_invalid")
    return data


def _validate_public_projection(
    document: Mapping[str, Any],
    case_ids: list[str],
) -> None:
    projection = _exact(
        document,
        {
            "gate_case_ids",
            "outcomes",
            "relay_reference",
            "observed_at_unix_ms",
        },
        "internet_native_qualification_invalid",
    )
    _reject_forbidden_keys(projection, "internet_native_qualification_privacy")
    if not set(projection.get("gate_case_ids")).issubset(set(case_ids)):
        raise InternetContractError("internet_native_qualification_invalid")
    _bounded_unique_names(
        projection.get("gate_case_ids"),
        maximum=_MAX_QUALIFICATION_CASES,
        code="internet_native_qualification_invalid",
    )
    outcomes = projection.get("outcomes")
    _bounded_unique_names(
        outcomes,
        maximum=_MAX_QUALIFICATION_CASES,
        code="internet_native_qualification_invalid",
    )
    if outcomes != sorted(outcomes):
        raise InternetContractError("internet_native_qualification_invalid")
    reference = projection.get("relay_reference")
    if reference is not None and (
        not isinstance(reference, str)
        or _RELAY_REFERENCE_RE.fullmatch(reference) is None
    ):
        raise InternetContractError("internet_native_qualification_invalid")
    if not _integer(projection.get("observed_at_unix_ms")):
        raise InternetContractError("internet_native_qualification_invalid")


def validate_internet_native_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "qualification_id",
            "gate_kind",
            "case_ids",
            "observed_at_unix_ms",
            "executed",
            "result",
            "spec_digest",
            "source_digest",
            "evidence_digests",
            "fresh_until_unix_ms",
            "projection_digest",
            "public_projection",
        },
        "internet_native_qualification_invalid",
    )
    _reject_forbidden_keys(data, "internet_native_qualification_privacy")
    if data.get("protocol") != INTERNET_NATIVE_QUALIFICATION_PROTOCOL:
        raise InternetContractError("internet_native_qualification_invalid")
    if not _bounded_text(data.get("qualification_id"), maximum=128):
        raise InternetContractError("internet_native_qualification_invalid")
    if data.get("gate_kind") not in _GATE_KINDS:
        raise InternetContractError("internet_native_qualification_invalid")
    case_ids = _bounded_unique_names(
        data.get("case_ids"),
        maximum=_MAX_QUALIFICATION_CASES,
        code="internet_native_qualification_invalid",
    )
    if not _integer(data.get("observed_at_unix_ms")):
        raise InternetContractError("internet_native_qualification_invalid")
    if not isinstance(data.get("executed"), bool):
        raise InternetContractError("internet_native_qualification_invalid")
    if data.get("result") not in _RESULTS:
        raise InternetContractError("internet_native_qualification_invalid")
    if not _digest(data.get("spec_digest")) or not _digest(data.get("source_digest")):
        raise InternetContractError("internet_native_qualification_invalid")
    evidence = data.get("evidence_digests")
    if (
        not isinstance(evidence, list)
        or len(evidence) > _MAX_EVIDENCE_DIGESTS
        or len(evidence) != len(set(evidence))
        or any(not _digest(value) for value in evidence)
    ):
        raise InternetContractError("internet_native_qualification_invalid")
    fresh_until = data.get("fresh_until_unix_ms")
    if fresh_until is not None and not _integer(fresh_until):
        raise InternetContractError("internet_native_qualification_invalid")
    projection_digest = data.get("projection_digest")
    if projection_digest is not None and not _digest(projection_digest):
        raise InternetContractError("internet_native_qualification_invalid")
    executed = data.get("executed")
    result = data.get("result")
    if not executed:
        if result != "not_executed" or evidence or projection_digest is not None:
            raise InternetContractError("internet_native_qualification_invalid")
    elif result not in {"passed", "failed"}:
        raise InternetContractError("internet_native_qualification_invalid")
    _validate_public_projection(data.get("public_projection"), case_ids)
    return data


def compatibility_fixtures() -> dict[str, dict[str, Any]]:
    """Privacy-clean, claim-free fixtures for the four A8 closed shapes."""

    placeholder_digest = "sha256:" + "a" * 64
    placeholder_reference = "hmac-sha256:" + "b" * 64
    observed_at = 1_752_500_000_000
    return {
        "internet-bootstrap-status-v1.json": {
            "protocol": BOOTSTRAP_STATUS_PROTOCOL,
            "generation": 1,
            "observed_at_unix_ms": observed_at,
            "freshness": "current",
            "tls_state": "publicly_trusted",
            "canonical_origin_verified": True,
            "seed_pin_state": "verified",
            "route_state": "available",
            "invitation_state": "pending",
            "counters": {"requests": 3, "joins_accepted": 1, "joins_rejected": 2},
        },
        "internet-activation-observation-v1.json": {
            "protocol": ACTIVATION_OBSERVATION_PROTOCOL,
            "observation_id": "fixture-observation",
            "connection_generation": 3,
            "connection_reuse": 2,
            "path_class": "direct",
            "path_source": "bound_live_connection",
            "endpoint_pseudonym": placeholder_digest,
            "observed_at_unix_ms": observed_at,
            "freshness": "current",
            "evidence_lifetime_until_unix_ms": observed_at + 90_000,
            "metrics": {
                "rtt_ms": 12,
                "warm_rtt_ms": 9,
                "jitter_ms": 2,
                "goodput_bytes_per_second": 1_500_000,
                "loss_ratio": 0.0,
                "sample_count": 64,
                "measured_zero": True,
            },
        },
        "relay-projection-v1.json": {
            "protocol": RELAY_PROJECTION_PROTOCOL,
            "relay_reference": placeholder_reference,
            "region": "unknown",
            "projection_generation": 1,
            "stable": True,
            "observed_at_unix_ms": observed_at,
        },
        "internet-native-qualification-v1.json": {
            "protocol": INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
            "qualification_id": "fixture-qualification",
            "gate_kind": "physical_positive",
            "case_ids": ["unrelated_https_invite_without_tailscale"],
            "observed_at_unix_ms": observed_at,
            "executed": False,
            "result": "not_executed",
            "spec_digest": placeholder_digest,
            "source_digest": placeholder_digest,
            "evidence_digests": [],
            "fresh_until_unix_ms": None,
            "projection_digest": None,
            "public_projection": {
                "gate_case_ids": ["unrelated_https_invite_without_tailscale"],
                "outcomes": ["not_executed"],
                "relay_reference": None,
                "observed_at_unix_ms": observed_at,
            },
        },
    }


__all__ = [
    "BOOTSTRAP_STATUS_PROTOCOL",
    "ACTIVATION_OBSERVATION_PROTOCOL",
    "RELAY_PROJECTION_PROTOCOL",
    "INTERNET_NATIVE_QUALIFICATION_PROTOCOL",
    "InternetContractError",
    "compatibility_fixtures",
    "validate_bootstrap_status",
    "validate_activation_observation",
    "validate_relay_projection",
    "validate_internet_native_qualification",
]
