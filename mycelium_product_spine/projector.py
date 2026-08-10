# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only M12 projector from authority snapshots into the product contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from typing import Any, Mapping, Sequence

from .contracts import ENTITY_KINDS, validate_product_snapshot


_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_code(value: object, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.lower()
        if _CODE.fullmatch(normalized) is not None:
            return normalized
    return fallback


def _binding(
    *,
    deployment_id: str | None = None,
    deployment_epoch: int | None = None,
    route_id: str | None = None,
    route_generation: int | None = None,
    topology_version: int | None = None,
) -> dict[str, Any]:
    return {
        "deployment_id": deployment_id,
        "deployment_epoch": deployment_epoch,
        "route_id": route_id,
        "route_generation": route_generation,
        "topology_version": topology_version,
    }


class ProductProjector:
    """Atomically publish privacy-reduced snapshots without mutating any authority."""

    def __init__(self, *, pseudonym_salt: bytes) -> None:
        if not isinstance(pseudonym_salt, bytes) or len(pseudonym_salt) < 32:
            raise ValueError("product_pseudonym_salt_invalid")
        self._salt = bytes(pseudonym_salt)
        self._lock = threading.Lock()
        self._generation = 0
        self._cursor = 0

    def restore_publication(self, *, generation: int, cursor: int) -> None:
        """Resume a durable publication sequence before the first projection."""

        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < 0
        ):
            raise ValueError("product_publication_state_invalid")
        with self._lock:
            if self._generation != 0 or self._cursor != 0:
                raise ValueError("product_publication_already_started")
            self._generation = generation
            self._cursor = cursor

    def _pseudonym(self, raw_id: str) -> str:
        digest = hmac.new(
            self._salt,
            raw_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"device-{digest[:20]}"

    def project(
        self,
        *,
        members: Sequence[Mapping[str, Any]],
        assignments: Sequence[Mapping[str, Any]] | None = None,
        route_status: Mapping[str, Any] | None,
        qualification: Mapping[str, Any] | None,
        now_unix_ms: int,
        source_mode: str = "live",
        source_errors: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(now_unix_ms, int)
            or isinstance(now_unix_ms, bool)
            or now_unix_ms < 0
            or source_mode not in {"fixture", "replay", "degraded", "live"}
        ):
            raise ValueError("product_projection_input_invalid")
        errors = dict(source_errors or {})
        if any(
            source_id not in {
                "membership-source",
                "assignment-source",
                "route-source",
                "qualification-source",
            }
            or _CODE.fullmatch(reason) is None
            for source_id, reason in errors.items()
        ):
            raise ValueError("product_projection_input_invalid")
        with self._lock:
            generation = self._generation + 1
            cursor = self._cursor + 1
            snapshot = self._build(
                members=members,
                assignments=assignments,
                route_status=route_status,
                qualification=qualification,
                now_unix_ms=now_unix_ms,
                source_mode=source_mode,
                generation=generation,
                cursor=cursor,
                source_errors=errors,
            )
            validated = validate_product_snapshot(snapshot)
            self._generation = generation
            self._cursor = cursor
            return validated

    def _build(
        self,
        *,
        members: Sequence[Mapping[str, Any]],
        assignments: Sequence[Mapping[str, Any]] | None,
        route_status: Mapping[str, Any] | None,
        qualification: Mapping[str, Any] | None,
        now_unix_ms: int,
        source_mode: str,
        generation: int,
        cursor: int,
        source_errors: Mapping[str, str],
    ) -> dict[str, Any]:
        route_current = (
            isinstance(route_status, Mapping)
            and route_status.get("protocol") == "mycelium.live_route_status.v1"
        )
        qualification_shape_current = (
            isinstance(qualification, Mapping)
            and qualification.get("protocol") == "mycelium.route_qualification.v1"
        )
        member_rows = [dict(member) for member in members]
        assignment_rows = (
            None if assignments is None else [dict(item) for item in assignments]
        )
        member_current = bool(member_rows)
        qualification_bound = (
            qualification_shape_current
            and route_current
            and qualification is not None
            and route_status is not None
            and qualification.get("deployment_id") == route_status.get("deployment_id")
            and qualification.get("model_id") == route_status.get("model_id")
            and qualification.get("topology_version") == route_status.get("topology_version")
        )
        qualification_current = qualification_shape_current and qualification_bound
        membership_error = source_errors.get("membership-source")
        assignment_error = source_errors.get("assignment-source")
        route_error = source_errors.get("route-source")
        qualification_error = source_errors.get("qualification-source")
        qualification_status = (
            "missing"
            if qualification_error is not None or not qualification_shape_current
            else "current"
            if qualification_bound
            else "conflict"
        )
        qualification_reason = (
            qualification_error
            if qualification_error is not None
            else None
            if qualification_bound
            else "qualification_binding_conflict"
            if qualification_shape_current
            else "qualification_source_missing"
        )
        source_states = [
            self._source_state(
                "membership-source",
                "seed_coordinator",
                "current" if member_current and membership_error is None else "missing",
                now_unix_ms,
                membership_error
                if membership_error is not None
                else None
                if member_current
                else "membership_source_missing",
            ),
            self._source_state(
                "assignment-source",
                "physical_assignment_authority",
                (
                    "unsupported"
                    if assignment_rows is None
                    else "current"
                    if assignment_rows and assignment_error is None
                    else "missing"
                ),
                now_unix_ms,
                (
                    "assignment_projection_unsupported"
                    if assignment_rows is None
                    else assignment_error
                    if assignment_error is not None
                    else None
                    if assignment_rows
                    else "assignment_source_missing"
                ),
            ),
            self._source_state(
                "route-source",
                "live_router_status",
                "current" if route_current and route_error is None else "missing",
                now_unix_ms,
                route_error
                if route_error is not None
                else None
                if route_current
                else "route_source_missing",
            ),
            self._source_state(
                "qualification-source",
                "route_qualification_authority",
                qualification_status,
                now_unix_ms,
                qualification_reason,
            ),
            self._source_state(
                "planner-source",
                "unsupported_until_m13",
                "unsupported",
                now_unix_ms,
                "planner_projection_unsupported",
            ),
            self._source_state(
                "transport-path-source",
                "unsupported_until_m14",
                "unsupported",
                now_unix_ms,
                "transport_path_unsupported",
            ),
            self._source_state(
                "artifact-source",
                "artifact_inventory_adapter_pending",
                "unsupported",
                now_unix_ms,
                "artifact_inventory_unavailable",
            ),
            self._source_state(
                "runtime-source",
                "runtime_kv_adapter_pending",
                "unsupported",
                now_unix_ms,
                "runtime_kv_projection_unavailable",
            ),
            self._source_state(
                "request-source",
                "request_lifecycle_adapter_pending",
                "unsupported",
                now_unix_ms,
                "request_projection_unavailable",
            ),
        ]
        stages = list(route_status.get("stages", [])) if route_current else []
        placed_by_raw_node: dict[str, list[Mapping[str, Any]]] = {}
        for stage in stages:
            if isinstance(stage, Mapping) and isinstance(stage.get("node_id"), str):
                placed_by_raw_node.setdefault(stage["node_id"], []).append(stage)

        entities: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        readiness: list[dict[str, Any]] = []
        notices: list[dict[str, Any]] = []
        for source in source_states:
            if source["status"] in {"missing", "conflict", "stale"}:
                notices.append(
                    {
                        "notice_id": f"source-{source['source_id']}-{source['status']}",
                        "scope_id": "product-root",
                        "severity": (
                            "error" if source["status"] == "conflict" else "warning"
                        ),
                        "code": source["reason_code"],
                        "source_id": source["source_id"],
                    }
                )
        public_member_ids: dict[str, str] = {}
        current_raw_members: set[str] = set()
        eligible_raw_members: set[str] = set()
        rotation_records = {
            (
                int(member.get("authority_generation", 1)),
                _safe_code(member.get("rotation_status"), "absent"),
                int(float(member.get("rotation_observed_at", 0)) * 1_000),
            )
            for member in member_rows
            if member.get("rotation_status") is not None
        }
        if len(rotation_records) == 1:
            authority_generation, rotation_status, rotation_observed_ms = next(
                iter(rotation_records)
            )
            if rotation_status in {"pending", "completed"}:
                rotation_code = f"seed_rotation_{rotation_status}"
                notices.append(
                    {
                        "notice_id": f"seed-rotation-{authority_generation}",
                        "scope_id": "product-root",
                        "severity": "warning" if rotation_status == "pending" else "info",
                        "code": rotation_code,
                        "source_id": "membership-source",
                    }
                )
                entities.append(
                    {
                        "entity_id": f"incident-seed-rotation-{authority_generation}",
                        "kind": "incident",
                        "label": "Seed authority rotation",
                        "source_id": "membership-source",
                        "binding": _binding(),
                        "freshness": {
                            "status": "current",
                            "observed_at_unix_ms": max(0, rotation_observed_ms),
                            "valid_until_unix_ms": max(0, rotation_observed_ms),
                        },
                        "attributes": {
                            "state": rotation_code,
                            "reason_code": rotation_code,
                            "observed_at_unix_ms": max(0, rotation_observed_ms),
                        },
                    }
                )
        for member in member_rows:
            raw_node_id = member.get("node_id")
            if not isinstance(raw_node_id, str) or not raw_node_id:
                continue
            public_id = self._pseudonym(raw_node_id)
            public_member_ids[raw_node_id] = public_id
            lease_expires_ms = int(float(member.get("lease_expires_at", 0)) * 1_000)
            lease_freshness = "fresh" if lease_expires_ms > now_unix_ms else "expired"
            if lease_freshness == "fresh":
                current_raw_members.add(raw_node_id)
            lifecycle = _safe_code(member.get("lifecycle_state"), "unknown")
            eligible = (
                member.get("activation_eligible") is True
                and lifecycle in {"configured", "running"}
            )
            if eligible and lease_freshness == "fresh":
                eligible_raw_members.add(raw_node_id)
            runtime = member.get("runtime_capability")
            runtime = runtime if isinstance(runtime, Mapping) else {}
            placements = placed_by_raw_node.get(raw_node_id, [])
            placement_id = (
                placements[0].get("placement_id")
                if len(placements) == 1
                and isinstance(placements[0].get("placement_id"), str)
                else None
            )
            observed_ms = int(float(member.get("last_liveness_at", 0)) * 1_000)
            entities.append(
                {
                    "entity_id": public_id,
                    "kind": "device",
                    "label": (
                        "Mobile conformance device"
                        if member.get("peer_class") == "android_termux_iroh"
                        else "Native inference device"
                    ),
                    "source_id": "membership-source",
                    "binding": _binding(),
                    "freshness": {
                        "status": "current" if lease_freshness == "fresh" else "stale",
                        "observed_at_unix_ms": max(0, observed_ms),
                        "valid_until_unix_ms": max(
                            0,
                            observed_ms,
                            lease_expires_ms,
                        ),
                    },
                    "attributes": {
                        "peer_class": str(member.get("peer_class", "linux_tbd")),
                        "membership_generation": int(member.get("generation", 1)),
                        "authority_generation": int(
                            member.get("authority_generation", 1)
                        ),
                        "incarnation": str(member.get("incarnation", "unknown")),
                        "lifecycle": lifecycle,
                        "lease_freshness": lease_freshness,
                        "runtime_backend": str(runtime.get("runtime_backend", "tbd")),
                        "transport": str(runtime.get("transport", "none")),
                        "activation_protocol": runtime.get("activation_protocol"),
                        "activation_eligible": eligible and lease_freshness == "fresh",
                        "placement_id": placement_id,
                    },
                }
            )
            readiness.append(
                {
                    "scope_id": public_id,
                    "dimension": "membership",
                    "state": (
                        "ready"
                        if lease_freshness == "fresh"
                        and lifecycle not in {"stopping", "stopped"}
                        else "not_ready"
                    ),
                    "reason_code": (
                        "member_revoked"
                        if lifecycle == "stopped"
                        else None
                        if lease_freshness == "fresh" and lifecycle != "stopping"
                        else "membership_stopping"
                        if lifecycle == "stopping"
                        else "membership_lease_expired"
                    ),
                    "source_id": "membership-source",
                }
            )
            if lifecycle == "stopped":
                notices.append(
                    {
                        "notice_id": f"revoked-{public_id}",
                        "scope_id": public_id,
                        "severity": "warning",
                        "code": "member_revoked",
                        "source_id": "membership-source",
                    }
                )
                entities.append(
                    {
                        "entity_id": f"incident-revoked-{public_id}",
                        "kind": "incident",
                        "label": "Member revoked",
                        "source_id": "membership-source",
                        "binding": _binding(),
                        "freshness": {
                            "status": "current",
                            "observed_at_unix_ms": max(0, observed_ms),
                            "valid_until_unix_ms": max(0, observed_ms),
                        },
                        "attributes": {
                            "state": "member_revoked",
                            "reason_code": "member_revoked",
                            "observed_at_unix_ms": max(0, observed_ms),
                        },
                    }
                )
            if not placements:
                notices.append(
                    {
                        "notice_id": f"unplaced-{public_id}",
                        "scope_id": public_id,
                        "severity": "info",
                        "code": "member_without_placement",
                        "source_id": "membership-source",
                    }
                )

        route_id: str | None = None
        route_binding = _binding()
        placed_raw_nodes = set(placed_by_raw_node)
        placement_members_present = placed_raw_nodes <= current_raw_members
        placement_members_eligible = placed_raw_nodes <= eligible_raw_members
        membership_ready = (
            bool(placed_raw_nodes)
            and placement_members_present
            and placement_members_eligible
        )
        if route_current:
            assert route_status is not None
            identity = route_status.get("route_identity_digest")
            identity = identity if isinstance(identity, str) and _DIGEST.fullmatch(identity) else _digest(route_status)
            route_id = f"route-{identity.split(':', 1)[1][:20]}"
            deployment_id = str(route_status.get("deployment_id", "deployment-unknown"))
            topology_version = int(route_status.get("topology_version", 0))
            route_binding = _binding(
                deployment_id=deployment_id,
                deployment_epoch=(
                    int(qualification.get("deployment_epoch", 0))
                    if qualification_current and qualification is not None
                    else None
                ),
                route_id=route_id,
                route_generation=topology_version,
                topology_version=topology_version,
            )
            entities.append(
                {
                    "entity_id": route_id,
                    "kind": "route",
                    "label": "Distributed inference route",
                    "source_id": "route-source",
                    "binding": route_binding,
                    "freshness": {
                        "status": "current",
                        "observed_at_unix_ms": now_unix_ms,
                        "valid_until_unix_ms": now_unix_ms,
                    },
                    "attributes": {
                        "deployment_id": deployment_id,
                        "model_id": str(route_status.get("model_id", "unknown-model")),
                        "topology_version": topology_version,
                        "decode_mode": str(
                            route_status.get("decode_mode", "complete_context_replay")
                        ),
                        "placement_provenance": str(
                            qualification.get("placement_provenance", "operator_selected")
                            if qualification_current and qualification is not None
                            else "operator_selected"
                        ),
                        "route_alive": route_status.get("route_alive") is True,
                    },
                }
            )
            for index, stage in enumerate(stages):
                if not isinstance(stage, Mapping):
                    continue
                raw_node_id = stage.get("node_id")
                stage_id = str(stage.get("stage_id", f"stage-{index}"))
                entities.append(
                    {
                        "entity_id": stage_id,
                        "kind": "stage",
                        "label": f"Inference stage {index + 1}",
                        "source_id": "route-source",
                        "binding": route_binding,
                        "freshness": {
                            "status": "current",
                            "observed_at_unix_ms": now_unix_ms,
                            "valid_until_unix_ms": now_unix_ms,
                        },
                        "attributes": {
                            "stage_index": index,
                            "start_layer": int(stage.get("start_layer", 0)),
                            "end_layer_exclusive": int(
                                stage.get("end_layer_exclusive", 1)
                            ),
                            "component_roles": list(stage.get("component_roles", [])),
                            "decode_mode": str(
                                route_status.get(
                                    "decode_mode",
                                    "complete_context_replay",
                                )
                            ),
                        },
                    }
                )
                relations.append(
                    {
                        "relation_id": f"stage-route-{index}",
                        "kind": "assigned_to",
                        "from_entity_id": stage_id,
                        "to_entity_id": route_id,
                        "source_id": "route-source",
                    }
                )
                public_device_id = public_member_ids.get(str(raw_node_id))
                if public_device_id is not None:
                    relations.append(
                        {
                            "relation_id": f"stage-device-{index}",
                            "kind": "placed_on",
                            "from_entity_id": stage_id,
                            "to_entity_id": public_device_id,
                            "source_id": "route-source",
                        }
                    )
            stage_ids = {
                str(stage.get("stage_id"))
                for stage in stages
                if isinstance(stage, Mapping) and isinstance(stage.get("stage_id"), str)
            }
            seen_assignment_ids: set[str] = set()
            seen_load_proof_ids: set[str] = set()
            if assignment_rows is not None:
                for index, assignment in enumerate(assignment_rows):
                    assignment_id = assignment.get("assignment_id")
                    raw_node_id = assignment.get("node_id")
                    stage_id = assignment.get("stage_id")
                    public_device_id = public_member_ids.get(str(raw_node_id))
                    if (
                        not isinstance(assignment_id, str)
                        or not isinstance(stage_id, str)
                        or stage_id not in stage_ids
                        or assignment_id in seen_assignment_ids
                        or public_device_id is None
                        or not isinstance(assignment.get("membership_generation"), int)
                        or not isinstance(assignment.get("load_generation"), int)
                        or not isinstance(assignment.get("assignment_digest"), str)
                        or _DIGEST.fullmatch(assignment["assignment_digest"]) is None
                        or not isinstance(assignment.get("stage_pack_digest"), str)
                        or _DIGEST.fullmatch(assignment["stage_pack_digest"]) is None
                        or not isinstance(assignment.get("load_proof_digest"), str)
                        or _DIGEST.fullmatch(assignment["load_proof_digest"]) is None
                    ):
                        notices.append(
                            {
                                "notice_id": f"assignment-invalid-{index}",
                                "scope_id": route_id,
                                "severity": "error",
                                "code": "assignment_record_invalid",
                                "source_id": "assignment-source",
                            }
                        )
                        continue
                    load_generation = int(assignment["load_generation"])
                    load_proof_id = (
                        "load-proof-"
                        + assignment["load_proof_digest"].split(":", 1)[1][:20]
                    )
                    if load_proof_id in seen_load_proof_ids:
                        notices.append(
                            {
                                "notice_id": f"assignment-invalid-{index}",
                                "scope_id": route_id,
                                "severity": "error",
                                "code": "assignment_record_invalid",
                                "source_id": "assignment-source",
                            }
                        )
                        continue
                    seen_assignment_ids.add(assignment_id)
                    seen_load_proof_ids.add(load_proof_id)
                    entities.append(
                        {
                            "entity_id": assignment_id,
                            "kind": "assignment",
                            "label": "Validated stage assignment",
                            "source_id": "assignment-source",
                            "binding": route_binding,
                            "freshness": {
                                "status": "current",
                                "observed_at_unix_ms": now_unix_ms,
                                "valid_until_unix_ms": now_unix_ms,
                            },
                            "attributes": {
                                "device_id": public_device_id,
                                "stage_id": stage_id,
                                "membership_generation": int(
                                    assignment["membership_generation"]
                                ),
                                "load_generation": load_generation,
                                "assignment_digest": assignment["assignment_digest"],
                                "stage_pack_digest": assignment["stage_pack_digest"],
                            },
                        }
                    )
                    entities.append(
                        {
                            "entity_id": load_proof_id,
                            "kind": "load_proof",
                            "label": "Qualified stage load proof",
                            "source_id": "assignment-source",
                            "binding": route_binding,
                            "freshness": {
                                "status": "current",
                                "observed_at_unix_ms": now_unix_ms,
                                "valid_until_unix_ms": now_unix_ms,
                            },
                            "attributes": {
                                "proof_digest": assignment["load_proof_digest"],
                                "assignment_id": assignment_id,
                                "load_generation": load_generation,
                                "ready": qualification_current
                                and qualification is not None
                                and qualification.get("route_ready") is True,
                            },
                        }
                    )
                    relations.extend(
                        [
                            {
                                "relation_id": f"assignment-stage-{index}",
                                "kind": "assigned_to",
                                "from_entity_id": assignment_id,
                                "to_entity_id": stage_id,
                                "source_id": "assignment-source",
                            },
                            {
                                "relation_id": f"assignment-device-{index}",
                                "kind": "placed_on",
                                "from_entity_id": assignment_id,
                                "to_entity_id": public_device_id,
                                "source_id": "assignment-source",
                            },
                            {
                                "relation_id": f"load-proof-assignment-{index}",
                                "kind": "reports",
                                "from_entity_id": load_proof_id,
                                "to_entity_id": assignment_id,
                                "source_id": "assignment-source",
                            },
                        ]
                    )
            for index, (source_stage, destination_stage) in enumerate(
                zip(stages, stages[1:])
            ):
                if not isinstance(source_stage, Mapping) or not isinstance(
                    destination_stage,
                    Mapping,
                ):
                    continue
                source_device = public_member_ids.get(
                    str(source_stage.get("node_id"))
                )
                destination_device = public_member_ids.get(
                    str(destination_stage.get("node_id"))
                )
                if (
                    source_device is None
                    or destination_device is None
                    or source_device == destination_device
                ):
                    continue
                entities.append(
                    {
                        "entity_id": f"directed-link-{index}",
                        "kind": "directed_link",
                        "label": "Activation-plane directed link",
                        "source_id": "route-source",
                        "binding": route_binding,
                        "freshness": {
                            "status": "current",
                            "observed_at_unix_ms": now_unix_ms,
                            "valid_until_unix_ms": now_unix_ms,
                        },
                        "attributes": {
                            "src_device_id": source_device,
                            "dst_device_id": destination_device,
                            "connectivity": "unknown",
                            "measurement_digest": None,
                        },
                    }
                )
            for index, incident in enumerate(route_status.get("incidents", [])):
                if not isinstance(incident, Mapping):
                    continue
                incident_id = f"incident-{index + 1}"
                entities.append(
                    {
                        "entity_id": incident_id,
                        "kind": "incident",
                        "label": "Route incident",
                        "source_id": "route-source",
                        "binding": route_binding,
                        "freshness": {
                            "status": "current",
                            "observed_at_unix_ms": int(
                                incident.get("observed_at_unix_ms", now_unix_ms)
                            ),
                            "valid_until_unix_ms": now_unix_ms,
                        },
                        "attributes": {
                            "state": _safe_code(incident.get("state"), "route_incident"),
                            "reason_code": _safe_code(
                                incident.get("reason"),
                                "upstream_reason_redacted",
                            ),
                            "observed_at_unix_ms": int(
                                incident.get("observed_at_unix_ms", now_unix_ms)
                            ),
                        },
                    }
                )

        qualification_ready = False
        if qualification_shape_current:
            assert qualification is not None
            qualification_id = str(
                qualification.get("qualification_id", "qualification-unknown")
            )
            qualification_digest = _digest(qualification)
            issued_at = int(qualification.get("issued_at_unix_ms", 0))
            authority_ready = qualification.get("route_ready") is True
            qualification_ready = (
                authority_ready
                and membership_ready
                and route_current
                and route_status is not None
                and route_status.get("route_alive") is True
            )
            entities.append(
                {
                    "entity_id": qualification_id,
                    "kind": "qualification",
                    "label": "Route qualification",
                    "source_id": "qualification-source",
                    "binding": route_binding,
                    "freshness": {
                        "status": qualification_status,
                        "observed_at_unix_ms": issued_at,
                        "valid_until_unix_ms": None,
                    },
                    "attributes": {
                        "qualification_digest": qualification_digest,
                        "route_ready": authority_ready,
                        "issued_at_unix_ms": issued_at,
                        "expires_at_unix_ms": None,
                        "reason_codes": [
                            _safe_code(reason, "upstream_reason_redacted")
                            for reason in qualification.get("reason_codes", [])
                        ],
                    },
                }
            )
            if route_id is not None and qualification_bound:
                relations.append(
                    {
                        "relation_id": "qualification-route",
                        "kind": "qualifies",
                        "from_entity_id": qualification_id,
                        "to_entity_id": route_id,
                        "source_id": "qualification-source",
                    }
                )

        scope_id = route_id or "product-root"
        readiness.extend(
            [
                {
                    "scope_id": scope_id,
                    "dimension": "artifacts",
                    "state": (
                        "unsupported"
                        if assignment_rows is None
                        else "ready"
                        if assignment_rows and qualification_current
                        else "not_ready"
                    ),
                    "reason_code": (
                        "assignment_projection_unsupported"
                        if assignment_rows is None
                        else None
                        if assignment_rows and qualification_current
                        else "load_proof_unavailable"
                    ),
                    "source_id": "assignment-source",
                },
                {
                    "scope_id": scope_id,
                    "dimension": "membership",
                    "state": "ready" if membership_ready else "not_ready",
                    "reason_code": (
                        None
                        if membership_ready
                        else (
                            "placement_member_ineligible"
                            if placement_members_present
                            else "placement_member_missing"
                        )
                    ),
                    "source_id": "membership-source",
                },
                {
                    "scope_id": scope_id,
                    "dimension": "transport",
                    "state": "unknown",
                    "reason_code": "transport_path_observation_unavailable",
                    "source_id": "transport-path-source",
                },
                {
                    "scope_id": scope_id,
                    "dimension": "qualification",
                    "state": "ready" if qualification_ready else "not_ready",
                    "reason_code": (
                        None
                        if qualification_ready
                        else "coherent_qualification_unavailable"
                    ),
                    "source_id": "qualification-source",
                },
                {
                    "scope_id": "product-root",
                    "dimension": "product_source",
                    "state": "ready" if route_current and member_current else "not_ready",
                    "reason_code": (
                        None
                        if route_current and member_current
                        else "product_source_incomplete"
                    ),
                    "source_id": "route-source",
                },
            ]
        )
        for source in source_states:
            entity_id = f"provenance-{source['source_id']}"
            entities.append(
                {
                    "entity_id": entity_id,
                    "kind": "source_provenance",
                    "label": "Evidence source",
                    "source_id": source["source_id"],
                    "binding": _binding(),
                    "freshness": {
                        "status": source["status"],
                        "observed_at_unix_ms": source["observed_at_unix_ms"],
                        "valid_until_unix_ms": source["valid_until_unix_ms"],
                    },
                    "attributes": {
                        "authority": source["authority"],
                        "source_protocol": {
                        "membership-source": "mycelium.membership.signed_message.v1",
                            "assignment-source": "mycelium.membership.assignment_offer.v1",
                            "route-source": "mycelium.live_route_status.v1",
                            "qualification-source": "mycelium.route_qualification.v1",
                            "planner-source": "mycelium.layer_planner_snapshot.v1",
                            "transport-path-source": "mycelium.transport_path_observation.v1",
                            "artifact-source": "mycelium.physical_deployment.v1",
                            "runtime-source": "mycelium.live_route_status.v1",
                            "request-source": "mycelium.observatory.request_projection.v1",
                        }[source["source_id"]],
                        "source_generation": source["generation"],
                        "evidence_digest": None,
                    },
                }
            )
        public_material = {
            "generation": generation,
            "cursor": cursor,
            "source_states": source_states,
            "entities": entities,
            "relations": relations,
            "readiness": readiness,
            "notices": notices,
        }
        snapshot_digest = _digest(public_material).split(":", 1)[1]
        publication_mode = (
            "degraded"
            if source_mode == "live"
            and any(
                source["source_id"]
                in {
                    "membership-source",
                    "assignment-source",
                    "route-source",
                    "qualification-source",
                }
                and source["status"] in {"missing", "stale", "conflict"}
                for source in source_states
            )
            else source_mode
        )
        return {
            "protocol": "mycelium.product_snapshot.v1",
            "publication": {
                "snapshot_id": f"snapshot-{generation}-{snapshot_digest[:16]}",
                "generation": generation,
                "cursor": cursor,
                "published_at_unix_ms": now_unix_ms,
                "source_mode": publication_mode,
            },
            "supported_entity_kinds": list(ENTITY_KINDS),
            "source_states": source_states,
            "entities": entities,
            "relations": relations,
            "readiness": readiness,
            "notices": notices,
            "provenance": {
                "projector": "mycelium_product_spine",
                "projector_version": "m12-v1",
                "source_mode": publication_mode,
            },
        }

    @staticmethod
    def _source_state(
        source_id: str,
        authority: str,
        status: str,
        now_unix_ms: int,
        reason_code: str | None,
    ) -> dict[str, Any]:
        current = status in {"current", "replay"}
        return {
            "source_id": source_id,
            "authority": authority,
            "status": status,
            "observed_at_unix_ms": now_unix_ms if current else None,
            "valid_until_unix_ms": now_unix_ms if current else None,
            "generation": None,
            "reason_code": reason_code,
        }


__all__ = ["ProductProjector"]
