# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closed, privacy-reduced evidence for managed-service restart recovery."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import json
from typing import Any


PROTOCOL = "mycelium.managed_service_restart.v1"
PRIVACY = (
    "no credentials, prompts, output, token arrays, tensors, activations, kv, "
    "raw endpoint ids, private addresses, paths, usernames, or process ids"
)
_ROOT = frozenset(
    {
        "protocol",
        "generated_at_unix_ms",
        "platform_classes",
        "services",
        "coordinator",
        "route",
        "verified",
        "privacy",
        "evidence_digest",
    }
)
_SERVICE = frozenset(
    {
        "service_id",
        "role",
        "manager",
        "restart_limit",
        "restart_window_seconds",
        "child_replaced",
        "manager_continuous",
        "health_restored",
        "health_restored_within_seconds",
    }
)
_COORDINATOR = frozenset(
    {
        "member_count",
        "renewals_advanced",
        "all_leases_fresh",
        "generation_preserved_or_advanced",
    }
)
_ROUTE = frozenset(
    {
        "simulated",
        "request_completed_after_restart",
        "frames_before",
        "frames_after",
        "fatal",
    }
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _closed(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("managed_service_restart_evidence_invalid")
    return copy.deepcopy(dict(value))


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("managed_service_restart_evidence_invalid")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("managed_service_restart_evidence_invalid")
    return value


def _derived_verified(document: Mapping[str, Any]) -> bool:
    services = document["services"]
    roles = {item["role"] for item in services}
    managers = {item["manager"] for item in services}
    coordinator = document["coordinator"]
    route = document["route"]
    return (
        roles == {"seed", "node", "supervisor"}
        and managers == {"launchd", "systemd"}
        and all(
            item["restart_limit"] > 0
            and item["restart_window_seconds"] > 0
            and item["child_replaced"] is True
            and item["manager_continuous"] is True
            and item["health_restored"] is True
            and 0 < item["health_restored_within_seconds"] <= 300
            for item in services
        )
        and coordinator["member_count"] >= 3
        and coordinator["renewals_advanced"] == coordinator["member_count"]
        and coordinator["all_leases_fresh"] is True
        and coordinator["generation_preserved_or_advanced"] is True
        and route["simulated"] is False
        and route["request_completed_after_restart"] is True
        and route["frames_after"] > route["frames_before"]
        and route["fatal"] is False
    )


def build_managed_restart_evidence(**values: Any) -> dict[str, Any]:
    document = {
        "protocol": PROTOCOL,
        **copy.deepcopy(values),
        "privacy": PRIVACY,
    }
    document["verified"] = _derived_verified(document)
    document["evidence_digest"] = _digest(document)
    return validate_managed_restart_evidence(document)


def validate_managed_restart_evidence(
    document: Mapping[str, Any],
) -> dict[str, Any]:
    result = _closed(document, _ROOT)
    if result["protocol"] != PROTOCOL or result["privacy"] != PRIVACY:
        raise ValueError("managed_service_restart_evidence_invalid")
    _integer(result["generated_at_unix_ms"], 1)
    platforms = result["platform_classes"]
    services = result["services"]
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(_text(item) not in {"launchd", "systemd"} for item in platforms)
        or len(set(platforms)) != len(platforms)
        or not isinstance(services, list)
        or len(services) < 3
    ):
        raise ValueError("managed_service_restart_evidence_invalid")
    service_ids: set[str] = set()
    normalized_services: list[dict[str, Any]] = []
    for item in services:
        service = _closed(item, _SERVICE)
        service_id = _text(service["service_id"])
        if service_id in service_ids:
            raise ValueError("managed_service_restart_evidence_invalid")
        service_ids.add(service_id)
        if service["role"] not in {"seed", "node", "supervisor"}:
            raise ValueError("managed_service_restart_evidence_invalid")
        if service["manager"] not in {"launchd", "systemd"}:
            raise ValueError("managed_service_restart_evidence_invalid")
        for key in (
            "restart_limit",
            "restart_window_seconds",
            "health_restored_within_seconds",
        ):
            _integer(service[key], 1)
        for key in ("child_replaced", "manager_continuous", "health_restored"):
            if type(service[key]) is not bool:
                raise ValueError("managed_service_restart_evidence_invalid")
        normalized_services.append(service)
    result["services"] = normalized_services
    coordinator = _closed(result["coordinator"], _COORDINATOR)
    _integer(coordinator["member_count"], 1)
    _integer(coordinator["renewals_advanced"])
    for key in ("all_leases_fresh", "generation_preserved_or_advanced"):
        if type(coordinator[key]) is not bool:
            raise ValueError("managed_service_restart_evidence_invalid")
    result["coordinator"] = coordinator
    route = _closed(result["route"], _ROUTE)
    for key in ("frames_before", "frames_after"):
        _integer(route[key])
    for key in ("simulated", "request_completed_after_restart", "fatal"):
        if type(route[key]) is not bool:
            raise ValueError("managed_service_restart_evidence_invalid")
    result["route"] = route
    if (
        type(result["verified"]) is not bool
        or result["verified"] is not _derived_verified(result)
    ):
        raise ValueError("managed_service_restart_evidence_invalid")
    digest = _text(result["evidence_digest"])
    if len(digest) != 71 or not digest.startswith("sha256:"):
        raise ValueError("managed_service_restart_evidence_invalid")
    unsigned = copy.deepcopy(result)
    unsigned.pop("evidence_digest")
    if digest != _digest(unsigned):
        raise ValueError("managed_service_restart_evidence_invalid")
    return result


__all__ = [
    "PRIVACY",
    "PROTOCOL",
    "build_managed_restart_evidence",
    "validate_managed_restart_evidence",
]
