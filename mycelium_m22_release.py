"""Closed M22 release audit and physical-proof projection."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any


PROTOCOL = "mycelium.m22_release_closure.v1"
UI_AUDIT_PROTOCOL = "mycelium.m22_ui_audit.v1"
PRIVACY = "no credentials, prompts, output, token arrays, tensors, activations, kv, raw endpoint ids, private addresses, paths, or usernames"
_DIGEST = "sha256:"
_FIELDS = frozenset(
    {
        "protocol",
        "generated_at_unix_ms",
        "source",
        "ui_audit",
        "services",
        "physical",
        "model",
        "qwen3_8b",
        "tests",
        "reviewer",
        "gate_state",
        "exclusions",
        "privacy",
        "evidence_digest",
    }
)
_SOURCE = frozenset(
    {"revision", "contract_manifest_digest", "sbom_digest", "clean_bootstrap"}
)
_UI = frozenset(
    {
        "protocol",
        "requirement_count",
        "verified_count",
        "excluded_count",
        "audit_digest",
    }
)
_SERVICES = frozenset(
    {
        "package_count",
        "roles",
        "platform_classes",
        "continuous_renewal",
        "bounded_restart",
        "foreground_route_restart_verified",
        "restart_verified",
        "coordinator_restart_verified",
        "managed_restart_evidence_digest",
        "log_rotation",
        "graceful_drain",
    }
)
_PHYSICAL = frozenset(
    {
        "simulated",
        "participant_count",
        "runtime_class_count",
        "activation_transport",
        "tailscale_product_dependency",
        "frame_count_before",
        "frame_count_after",
        "output_token_count",
        "request_completed",
    }
)
_MODEL = frozenset(
    {
        "model_id",
        "revision",
        "parameter_class",
        "weight_bytes",
        "architecture_adapter",
        "local_cache_reused",
        "network_download_performed",
        "qualified",
        "reason",
    }
)
_QWEN3 = frozenset(
    {
        "model_id",
        "revision",
        "adapter_id",
        "local_snapshot_complete",
        "adapter_verified",
        "qualified",
        "reason",
    }
)
_TESTS = frozenset(
    {
        "python_passed",
        "python_skipped",
        "ui_passed",
        "rust_passed",
        "browser_engines",
        "production_build",
        "accessibility",
        "performance",
        "privacy",
        "security",
        "claim_boundary",
    }
)
_REVIEWER = frozenset(
    {
        "bundle_version",
        "preflight_idempotent",
        "surrogate_verified",
        "external_network",
        "assigned_stage",
        "inference_completed",
        "negative_case_verified",
    }
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return _DIGEST + hashlib.sha256(_canonical(value)).hexdigest()


def _closed(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("m22_release_evidence_invalid")
    return copy.deepcopy(dict(value))


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("m22_release_evidence_invalid")
    return value


def _sha(value: Any) -> str:
    text = _text(value)
    if len(text) != 71 or not text.startswith(_DIGEST):
        raise ValueError("m22_release_evidence_invalid")
    try:
        int(text[7:], 16)
    except ValueError as exc:
        raise ValueError("m22_release_evidence_invalid") from exc
    return text


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("m22_release_evidence_invalid")
    return value


def validate_ui_audit(document: Mapping[str, Any]) -> dict[str, Any]:
    if (
        set(document) != {"protocol", "source", "requirements"}
        or document.get("protocol") != UI_AUDIT_PROTOCOL
    ):
        raise ValueError("m22_ui_audit_invalid")
    _text(document.get("source"))
    requirements = document.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("m22_ui_audit_invalid")
    ids: set[str] = set()
    for item in requirements:
        if not isinstance(item, Mapping) or set(item) != {
            "id",
            "status",
            "evidence",
            "boundary",
        }:
            raise ValueError("m22_ui_audit_invalid")
        requirement_id = _text(item["id"])
        if requirement_id in ids or item["status"] not in {"verified", "excluded"}:
            raise ValueError("m22_ui_audit_invalid")
        ids.add(requirement_id)
        if (
            not isinstance(item["evidence"], list)
            or not item["evidence"]
            or any(
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
                for path in item["evidence"]
            )
        ):
            raise ValueError("m22_ui_audit_invalid")
        _text(item["boundary"])
    return copy.deepcopy(dict(document))


def ui_audit_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    audit = validate_ui_audit(document)
    requirements = audit["requirements"]
    verified = sum(item["status"] == "verified" for item in requirements)
    excluded = len(requirements) - verified
    return {
        "protocol": UI_AUDIT_PROTOCOL,
        "requirement_count": len(requirements),
        "verified_count": verified,
        "excluded_count": excluded,
        "audit_digest": _digest(audit),
    }


def _derived_gate(result: Mapping[str, Any]) -> str:
    source = result["source"]
    ui = result["ui_audit"]
    services = result["services"]
    physical = result["physical"]
    model = result["model"]
    tests = result["tests"]
    reviewer = result["reviewer"]
    qualified = (
        source["clean_bootstrap"] is True
        and ui["verified_count"] == ui["requirement_count"]
        and ui["excluded_count"] == 0
        and services["package_count"] >= 3
        and set(services["roles"]) == {"seed", "node", "supervisor"}
        and services["continuous_renewal"] is True
        and services["bounded_restart"] is True
        and services["restart_verified"] is True
        and services["coordinator_restart_verified"] is True
        and isinstance(services["managed_restart_evidence_digest"], str)
        and services["log_rotation"] is True
        and services["graceful_drain"] is True
        and physical["simulated"] is False
        and physical["participant_count"] >= 3
        and physical["runtime_class_count"] >= 2
        and physical["activation_transport"] == "endpointid_authenticated_iroh"
        and physical["tailscale_product_dependency"] is False
        and physical["frame_count_after"] > physical["frame_count_before"]
        and physical["output_token_count"] > 0
        and physical["request_completed"] is True
        and model["model_id"] == "Qwen/Qwen2.5-3B-Instruct"
        and model["parameter_class"] == "3B"
        and model["qualified"] is True
        and model["local_cache_reused"] is True
        and model["network_download_performed"] is False
        and tests["python_passed"] > 0
        and tests["ui_passed"] > 0
        and tests["rust_passed"] > 0
        and set(tests["browser_engines"]) == {"chromium", "firefox", "webkit"}
        and all(
            tests[key] is True
            for key in (
                "production_build",
                "accessibility",
                "performance",
                "privacy",
                "security",
                "claim_boundary",
            )
        )
        and reviewer["preflight_idempotent"] is True
        and reviewer["surrogate_verified"] is True
        and reviewer["external_network"] is True
        and reviewer["assigned_stage"] is True
        and reviewer["inference_completed"] is True
        and reviewer["negative_case_verified"] is True
        and not result["exclusions"]
    )
    return "qualified" if qualified else "withheld"


def build_release_evidence(**values: Any) -> dict[str, Any]:
    body = {"protocol": PROTOCOL, **copy.deepcopy(values), "privacy": PRIVACY}
    body["gate_state"] = _derived_gate(body)
    body["evidence_digest"] = _digest(body)
    return validate_release_evidence(body)


def validate_release_evidence(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = _closed(document, _FIELDS)
        if result["protocol"] != PROTOCOL or result["privacy"] != PRIVACY:
            raise ValueError("m22_release_evidence_invalid")
        _integer(result["generated_at_unix_ms"], 1)
        source = _closed(result["source"], _SOURCE)
        _text(source["revision"])
        _sha(source["contract_manifest_digest"])
        _sha(source["sbom_digest"])
        if type(source["clean_bootstrap"]) is not bool:
            raise ValueError("m22_release_evidence_invalid")
        ui = _closed(result["ui_audit"], _UI)
        if ui["protocol"] != UI_AUDIT_PROTOCOL:
            raise ValueError("m22_release_evidence_invalid")
        for key in ("requirement_count", "verified_count", "excluded_count"):
            _integer(ui[key])
        if ui["verified_count"] + ui["excluded_count"] != ui["requirement_count"]:
            raise ValueError("m22_release_evidence_invalid")
        _sha(ui["audit_digest"])
        services = _closed(result["services"], _SERVICES)
        _integer(services["package_count"])
        if not isinstance(services["roles"], list) or not isinstance(
            services["platform_classes"], list
        ):
            raise ValueError("m22_release_evidence_invalid")
        for value in (*services["roles"], *services["platform_classes"]):
            _text(value)
        for key in (
            "continuous_renewal",
            "bounded_restart",
            "foreground_route_restart_verified",
            "restart_verified",
            "coordinator_restart_verified",
            "log_rotation",
            "graceful_drain",
        ):
            if type(services[key]) is not bool:
                raise ValueError("m22_release_evidence_invalid")
        _sha(services["managed_restart_evidence_digest"])
        physical = _closed(result["physical"], _PHYSICAL)
        for key in (
            "participant_count",
            "runtime_class_count",
            "frame_count_before",
            "frame_count_after",
            "output_token_count",
        ):
            _integer(physical[key])
        for key in ("simulated", "tailscale_product_dependency", "request_completed"):
            if type(physical[key]) is not bool:
                raise ValueError("m22_release_evidence_invalid")
        _text(physical["activation_transport"])
        model = _closed(result["model"], _MODEL)
        for key in (
            "model_id",
            "revision",
            "parameter_class",
            "architecture_adapter",
            "reason",
        ):
            _text(model[key])
        _integer(model["weight_bytes"], 1)
        for key in ("local_cache_reused", "network_download_performed", "qualified"):
            if type(model[key]) is not bool:
                raise ValueError("m22_release_evidence_invalid")
        qwen3 = _closed(result["qwen3_8b"], _QWEN3)
        for key in ("model_id", "revision", "adapter_id", "reason"):
            _text(qwen3[key])
        for key in ("local_snapshot_complete", "adapter_verified", "qualified"):
            if type(qwen3[key]) is not bool:
                raise ValueError("m22_release_evidence_invalid")
        tests = _closed(result["tests"], _TESTS)
        for key in ("python_passed", "python_skipped", "ui_passed", "rust_passed"):
            _integer(tests[key])
        if not isinstance(tests["browser_engines"], list):
            raise ValueError("m22_release_evidence_invalid")
        for value in tests["browser_engines"]:
            _text(value)
        for key in (
            "production_build",
            "accessibility",
            "performance",
            "privacy",
            "security",
            "claim_boundary",
        ):
            if type(tests[key]) is not bool:
                raise ValueError("m22_release_evidence_invalid")
        reviewer = _closed(result["reviewer"], _REVIEWER)
        _text(reviewer["bundle_version"])
        for key in (
            "preflight_idempotent",
            "surrogate_verified",
            "external_network",
            "assigned_stage",
            "inference_completed",
            "negative_case_verified",
        ):
            if type(reviewer[key]) is not bool:
                raise ValueError("m22_release_evidence_invalid")
        if not isinstance(result["exclusions"], list) or any(
            not isinstance(value, str) or not value for value in result["exclusions"]
        ):
            raise ValueError("m22_release_evidence_invalid")
        if result["gate_state"] not in {"qualified", "withheld"} or result[
            "gate_state"
        ] != _derived_gate(result):
            raise ValueError("m22_release_evidence_invalid")
        supplied = _sha(result["evidence_digest"])
        unsigned = copy.deepcopy(result)
        del unsigned["evidence_digest"]
        if supplied != _digest(unsigned):
            raise ValueError("m22_release_evidence_invalid")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "m22_release_evidence_invalid":
            raise
        raise ValueError("m22_release_evidence_invalid") from exc


__all__ = [
    "PRIVACY",
    "PROTOCOL",
    "build_release_evidence",
    "ui_audit_summary",
    "validate_release_evidence",
    "validate_ui_audit",
]
