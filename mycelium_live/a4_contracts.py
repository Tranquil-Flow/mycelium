"""Closed, privacy-reduced A4 product-path contracts."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


INTERRUPTIBLE_STAGE_COMMAND_PROTOCOL = "mycelium.interruptible_stage_command.v1"
TRAFFIC_LIVENESS_PROTOCOL = "mycelium.traffic_liveness.v1"
SCOPED_RUNTIME_INCIDENT_PROTOCOL = "mycelium.scoped_runtime_incident.v1"
PRODUCT_QUALIFICATION_PROTOCOL = (
    "mycelium.product_concurrency_liveness_qualification.v1"
)
_SUBJECT_KINDS = {"edge", "placement", "peer", "deployment"}
_LIVENESS_STATES = {"fresh", "suspect", "quarantined", "failed", "recovered"}
_OBSERVATION_SOURCES = {
    "application_receipt", "activation_receipt", "signed_keepalive",
    "idle_keepalive", "command_deadline", "active_transport_failure",
    "membership_exit", "worker_exception", "deployment_fatal",
}
_FATAL_REASONS = {
    "immutable_authority_contradiction",
    "deployment_resource_ledger_corruption",
    "active_authority_compromise",
    "all_qualified_tracks_lost",
}


def _bounded_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 256


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= (1 << 63) - 1


def _exact(document: Mapping[str, Any], fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != fields:
        raise ValueError(code)
    return dict(document)


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def validate_interruptible_stage_command(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "deployment_id",
            "deployment_epoch",
            "qualification_digest",
            "request_id",
            "request_attempt",
            "path_id",
            "path_attempt",
            "path_digest",
            "topology_generation",
            "command_id",
            "stage_id",
            "placement_id",
            "assignment_id",
            "command_kind",
            "issued_at_ms",
            "idempotency_digest",
            "cancellation_generation",
            "publisher_generation",
            "absolute_deadline_ms",
            "cooperative_step_ms",
            "cleanup_owner_id",
            "maximum_request_bytes",
            "maximum_response_bytes",
            "expected_terminal_revision",
        },
        "invalid_interruptible_stage_command",
    )
    if (
        data["protocol"] != INTERRUPTIBLE_STAGE_COMMAND_PROTOCOL
        or not all(
            _bounded_text(data[field])
            for field in (
                "deployment_id",
                "request_id",
                "path_id",
                "command_id",
                "stage_id",
                "placement_id",
                "assignment_id",
                "cleanup_owner_id",
            )
        )
        or data["command_kind"] not in {"prefill", "decode", "cleanup", "shutdown", "probe"}
        or not _digest(data["qualification_digest"])
        or not _digest(data["path_digest"])
        or not _digest(data["idempotency_digest"])
        or not all(
            _integer(data[field], minimum=minimum)
            for field, minimum in (
                ("deployment_epoch", 1),
                ("request_attempt", 1),
                ("path_attempt", 0),
                ("topology_generation", 1),
                ("cancellation_generation", 0),
                ("publisher_generation", 1),
                ("absolute_deadline_ms", 1),
                ("cooperative_step_ms", 1),
                ("issued_at_ms", 0),
                ("maximum_request_bytes", 1),
                ("maximum_response_bytes", 1),
                ("expected_terminal_revision", 0),
            )
        )
        or data["issued_at_ms"] >= data["absolute_deadline_ms"]
        or data["expected_terminal_revision"] != 0
        or data["cooperative_step_ms"] > 2_000
        or data["maximum_request_bytes"] > 1 << 30
        or data["maximum_response_bytes"] > 1 << 34
    ):
        raise ValueError("invalid_interruptible_stage_command")
    return data


def validate_traffic_liveness(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "deployment_id",
            "generated_at_monotonic_ms",
            "subjects",
            "incidents",
            "deployment_fatal_reason",
        },
        "invalid_traffic_liveness",
    )
    if (
        data["protocol"] != TRAFFIC_LIVENESS_PROTOCOL
        or not isinstance(data["deployment_id"], str)
        or type(data["generated_at_monotonic_ms"]) is not int
        or not _bounded_text(data["deployment_id"])
        or not _integer(data["generated_at_monotonic_ms"])
        or not isinstance(data["subjects"], list)
        or len(data["subjects"]) > 4_096
        or not isinstance(data["incidents"], list)
        or len(data["incidents"]) > 256
        or data["deployment_fatal_reason"] is not None
        and data["deployment_fatal_reason"] not in _FATAL_REASONS
    ):
        raise ValueError("invalid_traffic_liveness")
    subject_fields = {
        "subject_id", "kind", "membership_generation", "state", "last_fresh_ms",
        "last_observed_ms", "next_keepalive_due_ms", "consecutive_misses", "last_source",
    }
    subject_ids: set[str] = set()
    for subject in data["subjects"]:
        item = _exact(subject, subject_fields, "invalid_traffic_liveness")
        if (
            not _bounded_text(item["subject_id"])
            or item["subject_id"] in subject_ids
            or item["kind"] not in _SUBJECT_KINDS
            or not _integer(item["membership_generation"], minimum=1)
            or item["state"] not in _LIVENESS_STATES
            or item["last_source"] not in _OBSERVATION_SOURCES
            or not all(
                _integer(item[field])
                for field in (
                    "last_fresh_ms", "last_observed_ms", "next_keepalive_due_ms",
                    "consecutive_misses",
                )
            )
            or item["last_fresh_ms"] > item["last_observed_ms"]
            or item["last_observed_ms"] > data["generated_at_monotonic_ms"]
            or item["next_keepalive_due_ms"] < item["last_fresh_ms"]
        ):
            raise ValueError("invalid_traffic_liveness")
        subject_ids.add(item["subject_id"])
    incident_fields = {
        "sequence", "source", "scope", "subject_id", "membership_generation",
        "observed_at_ms", "affected_track_ids", "action", "outcome",
        "detection_latency_ms", "within_detection_budget",
    }
    incident_sequences: set[int] = set()
    for incident in data["incidents"]:
        item = _exact(incident, incident_fields, "invalid_traffic_liveness")
        if (
            not _integer(item["sequence"], minimum=1)
            or item["sequence"] in incident_sequences
            or item["source"] not in _OBSERVATION_SOURCES
            or item["scope"] not in {"request", *_SUBJECT_KINDS}
            or not _bounded_text(item["subject_id"])
            or not _integer(item["membership_generation"], minimum=1)
            or not _integer(item["observed_at_ms"])
            or not isinstance(item["affected_track_ids"], list)
            or len(item["affected_track_ids"]) > 256
            or not all(_bounded_text(track) for track in item["affected_track_ids"])
            or len(set(item["affected_track_ids"])) != len(item["affected_track_ids"])
            or not _bounded_text(item["action"])
            or not _bounded_text(item["outcome"])
            or item["detection_latency_ms"] is not None
            and not _integer(item["detection_latency_ms"])
            or item["within_detection_budget"] is not None
            and type(item["within_detection_budget"]) is not bool
            or item["observed_at_ms"] > data["generated_at_monotonic_ms"]
        ):
            raise ValueError("invalid_traffic_liveness")
        incident_sequences.add(item["sequence"])
    return json.loads(json.dumps(data, sort_keys=True, allow_nan=False))


def validate_scoped_runtime_incident(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "incident_id",
            "deployment_id",
            "deployment_epoch",
            "qualification_digest",
            "request_id",
            "request_attempt",
            "path_id",
            "path_attempt",
            "path_digest",
            "topology_generation",
            "command_id",
            "cancellation_generation",
            "publisher_generation",
            "cleanup_owner_id",
            "subject_id",
            "scope",
            "reason",
            "fatal_requested",
            "fatal_accepted",
            "observed_at_monotonic_ms",
        },
        "invalid_scoped_runtime_incident",
    )
    if (
        data["protocol"] != SCOPED_RUNTIME_INCIDENT_PROTOCOL
        or data["scope"] not in {"request", "edge", "placement", "peer", "deployment"}
        or not _digest(data["qualification_digest"])
        or not _digest(data["path_digest"])
        or type(data["fatal_requested"]) is not bool
        or type(data["fatal_accepted"]) is not bool
        or data["fatal_accepted"] and not data["fatal_requested"]
        or not all(
            _bounded_text(data[field])
            for field in (
                "incident_id", "deployment_id", "request_id", "path_id",
                "command_id", "cleanup_owner_id", "subject_id", "reason",
            )
        )
        or not _integer(data["deployment_epoch"], minimum=1)
        or not _integer(data["request_attempt"], minimum=1)
        or not _integer(data["path_attempt"])
        or not _integer(data["topology_generation"], minimum=1)
        or not _integer(data["cancellation_generation"])
        or not _integer(data["publisher_generation"], minimum=1)
        or not _integer(data["observed_at_monotonic_ms"])
    ):
        raise ValueError("invalid_scoped_runtime_incident")
    return data


def validate_product_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
    data = _exact(
        document,
        {
            "protocol",
            "deployment_id",
            "qualification_digest",
            "maximum_concurrent_requests",
            "cancellation_and_cleanup_bound_ms",
            "cooperative_interruption_proven",
            "request_scoped_cleanup_proven",
            "shared_process_termination_used",
            "publisher_generation_fencing_proven",
            "scoped_liveness_proven",
            "eligible",
            "evidence_digest",
        },
        "invalid_product_concurrency_liveness_qualification",
    )
    gates = (
        data["cooperative_interruption_proven"] is True,
        data["request_scoped_cleanup_proven"] is True,
        data["shared_process_termination_used"] is False,
        data["publisher_generation_fencing_proven"] is True,
        data["scoped_liveness_proven"] is True,
        data["cancellation_and_cleanup_bound_ms"] == 2_000,
    )
    if (
        data["protocol"] != PRODUCT_QUALIFICATION_PROTOCOL
        or not _digest(data["qualification_digest"])
        or not _digest(data["evidence_digest"])
        or not _bounded_text(data["deployment_id"])
        or type(data["maximum_concurrent_requests"]) is not int
        or not 2 <= data["maximum_concurrent_requests"] <= 64
        or any(
            type(data[field]) is not bool
            for field in (
                "cooperative_interruption_proven",
                "request_scoped_cleanup_proven",
                "shared_process_termination_used",
                "publisher_generation_fencing_proven",
                "scoped_liveness_proven",
                "eligible",
            )
        )
        or data["eligible"] is not all(gates)
    ):
        raise ValueError("invalid_product_concurrency_liveness_qualification")
    return data


def compatibility_fixtures() -> dict[str, dict[str, Any]]:
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    return {
        "interruptible-stage-command-v1.json": validate_interruptible_stage_command(
            {
                "protocol": INTERRUPTIBLE_STAGE_COMMAND_PROTOCOL,
                "deployment_id": "deployment-fixture",
                "deployment_epoch": 16,
                "qualification_digest": digest_a,
                "request_id": "request-fixture",
                "request_attempt": 1,
                "path_id": "path-fixture",
                "path_attempt": 0,
                "path_digest": digest_b,
                "topology_generation": 7,
                "command_id": "command-fixture",
                "stage_id": "stage-fixture",
                "placement_id": "placement-fixture",
                "assignment_id": "assignment-fixture",
                "command_kind": "prefill",
                "issued_at_ms": 10_000,
                "idempotency_digest": digest_a,
                "cancellation_generation": 1,
                "publisher_generation": 2,
                "absolute_deadline_ms": 12_000,
                "cooperative_step_ms": 100,
                "cleanup_owner_id": "placement-owner-fixture",
                "maximum_request_bytes": 4_096,
                "maximum_response_bytes": 16_384,
                "expected_terminal_revision": 0,
            }
        ),
        "traffic-liveness-v1.json": validate_traffic_liveness(
            {
                "protocol": TRAFFIC_LIVENESS_PROTOCOL,
                "deployment_id": "deployment-fixture",
                "generated_at_monotonic_ms": 10_000,
                "subjects": [],
                "incidents": [],
                "deployment_fatal_reason": None,
            }
        ),
        "scoped-runtime-incident-v1.json": validate_scoped_runtime_incident(
            {
                "protocol": SCOPED_RUNTIME_INCIDENT_PROTOCOL,
                "incident_id": "incident-fixture",
                "deployment_id": "deployment-fixture",
                "deployment_epoch": 16,
                "qualification_digest": digest_a,
                "request_id": "request-fixture",
                "request_attempt": 1,
                "path_id": "path-fixture",
                "path_attempt": 0,
                "path_digest": digest_b,
                "topology_generation": 7,
                "command_id": "command-fixture",
                "cancellation_generation": 1,
                "publisher_generation": 2,
                "cleanup_owner_id": "placement-owner-fixture",
                "subject_id": "edge-fixture",
                "scope": "edge",
                "reason": "delivery_timeout",
                "fatal_requested": False,
                "fatal_accepted": False,
                "observed_at_monotonic_ms": 10_500,
            }
        ),
        "product-concurrency-liveness-qualification-v1.json": (
            validate_product_qualification(
                {
                    "protocol": PRODUCT_QUALIFICATION_PROTOCOL,
                    "deployment_id": "deployment-fixture",
                    "qualification_digest": digest_a,
                    "maximum_concurrent_requests": 4,
                    "cancellation_and_cleanup_bound_ms": 2_000,
                    "cooperative_interruption_proven": True,
                    "request_scoped_cleanup_proven": True,
                    "shared_process_termination_used": False,
                    "publisher_generation_fencing_proven": True,
                    "scoped_liveness_proven": True,
                    "eligible": True,
                    "evidence_digest": digest_b,
                }
            )
        ),
    }


__all__ = [
    "INTERRUPTIBLE_STAGE_COMMAND_PROTOCOL",
    "PRODUCT_QUALIFICATION_PROTOCOL",
    "SCOPED_RUNTIME_INCIDENT_PROTOCOL",
    "TRAFFIC_LIVENESS_PROTOCOL",
    "compatibility_fixtures",
    "validate_interruptible_stage_command",
    "validate_product_qualification",
    "validate_scoped_runtime_incident",
    "validate_traffic_liveness",
]
