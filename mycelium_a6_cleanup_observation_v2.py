# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prospective A6 cleanup-authority-failure v2, separate from immutable case v1.

A validated envelope is a closed shape, not proof its producer ran. The local
collector below reads command snapshots and gateway events independently; neither
an unavailable ledger nor an empty snapshot can be counted as zero.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

PROTOCOL = "mycelium.a6_cleanup_authority_observation.v2"
AUTHORITY = "command_controller_and_request_gateway"
CASE = "cleanup_authority_failure"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z")
DOCUMENT_FIELDS = {"protocol", "authority", "case_id", "bindings", "observation", "observation_digest"}
BINDING_FIELDS = {"request_id", "request_attempt", "source_digest", "candidate_digest",
                  "spec_digest", "contract_digest", "operator_plan_digest", "run_id",
                  "deployment_id", "deployment_epoch", "source_path_id", "source_attempt",
                  "source_path_attempt", "source_path_generation", "successor_path_id",
                  "successor_attempt", "successor_path_attempt", "successor_path_generation"}
COMMAND_FIELDS = {"request_id", "request_attempt", "registered_command_count", "terminal_cas_count"}
GATEWAY_FIELDS = {"request_id", "request_attempt", "terminal_event_count", "terminal_kind", "terminal_code"}
OBSERVATION_FIELDS = {"case_id", "executed", "ordinary_product_path", "private_material_exposed",
                      "rejection_observed", "fail_closed", "terminal_state", "unpublishable_suffix_discarded",
                      "process_cleanup_complete", "stale_reservation_count", "command_authority", "gateway_authority"}
PRIVATE_MARKERS = ("encoded_prompt_token_ids", "committed_prefix_tokens", "checkpoint_path", "node_secret",
                   "ssh_identity", "private_key")


def _exact(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("cleanup_v2_closed_shape_invalid")
    return dict(value)


def _int(value: object, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def validate_observation(document: Mapping[str, Any]) -> dict[str, Any]:
    doc = _exact(document, DOCUMENT_FIELDS)
    if doc["protocol"] != PROTOCOL or doc["authority"] != AUTHORITY or doc["case_id"] != CASE:
        raise ValueError("cleanup_v2_protocol_invalid")
    if not isinstance(doc["observation_digest"], str) or _digest({k: v for k, v in doc.items() if k != "observation_digest"}) != doc["observation_digest"]:
        raise ValueError("cleanup_v2_digest_invalid")
    if any(marker in json.dumps(doc, sort_keys=True) for marker in PRIVATE_MARKERS):
        raise ValueError("cleanup_v2_private_material")
    binding = _exact(doc["bindings"], BINDING_FIELDS)
    if (any(not isinstance(binding[key], str) or not IDENTIFIER.fullmatch(binding[key])
            for key in ("request_id", "run_id", "deployment_id", "source_path_id", "successor_path_id"))
        or any(not _int(binding[key], 1) for key in ("request_attempt", "deployment_epoch", "source_attempt",
                                                       "source_path_generation", "successor_attempt", "successor_path_generation"))
        or not _int(binding["source_path_attempt"], 0)
        or not _int(binding["successor_path_attempt"], 0)
        or binding["request_attempt"] != binding["successor_attempt"]
        or binding["successor_attempt"] <= binding["source_attempt"]
        or binding["successor_path_generation"] <= binding["source_path_generation"]
        or any(not isinstance(binding[key], str) or not DIGEST.fullmatch(binding[key])
               for key in ("source_digest", "candidate_digest", "spec_digest", "contract_digest",
                           "operator_plan_digest"))):
        raise ValueError("cleanup_v2_binding_invalid")
    value = _exact(doc["observation"], OBSERVATION_FIELDS)
    command = _exact(value["command_authority"], COMMAND_FIELDS)
    gateway = _exact(value["gateway_authority"], GATEWAY_FIELDS)
    if (value["case_id"] != CASE or value["executed"] is not True
        or value["ordinary_product_path"] is not True or value["private_material_exposed"] is not False
        or value["rejection_observed"] is not True or value["fail_closed"] is not True
        or value["terminal_state"] != "TERMINAL_BLOCKED"
        or value["unpublishable_suffix_discarded"] is not True
        or value["process_cleanup_complete"] is not True
        or not _int(value["stale_reservation_count"], 0) or value["stale_reservation_count"] != 0):
        raise ValueError("cleanup_v2_case_invalid")
    for authority in (command, gateway):
        if (authority["request_id"] != binding["request_id"]
            or authority["request_attempt"] != binding["request_attempt"]):
            raise ValueError("cleanup_v2_authority_binding_mismatch")
    if (not _int(command["registered_command_count"], 1)
        or not _int(command["terminal_cas_count"], 0)
        or command["terminal_cas_count"] != 0):
        raise ValueError("cleanup_v2_command_terminal_invalid")
    if (not _int(gateway["terminal_event_count"], 0)
        or gateway["terminal_event_count"] != 1
        or gateway["terminal_kind"] != "failed"
        or gateway["terminal_code"] != "request_cleanup_unproven"):
        raise ValueError("cleanup_v2_gateway_notice_invalid")
    return value


def collect_local_authorities(controller: Any, service: Any, request_id: str,
                              request_attempt: int, router: Any = None) -> dict[str, dict[str, Any]]:
    """Local only. Router identity joins the independently queried authorities.

    This private in-process seam cannot establish physical durability or survive
    a retired command controller; the caller must never substitute an empty tuple.
    """
    if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id) or not _int(request_attempt, 1):
        raise ValueError("request_attempt_binding_invalid")
    try:
        pending = router._pending[request_id]
        identity = pending.command_identity
        if identity is None or identity.request_id != request_id or identity.request_attempt != request_attempt:
            raise ValueError("command_authority_binding_mismatch")
        snapshots = controller.snapshot(request_id, request_attempt=request_attempt)
        if not snapshots:
            raise ValueError("command_authority_unavailable")
        if any(s.identity.request_id != request_id or s.identity.request_attempt != request_attempt
               or s.identity.path_digest != identity.path_digest for s in snapshots):
            raise ValueError("command_authority_binding_mismatch")
        command = {"request_id": request_id, "request_attempt": request_attempt,
                   "registered_command_count": len(snapshots),
                   "terminal_cas_count": sum(s.terminal_revision for s in snapshots)}
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("command_authority_unavailable") from exc
    try:
        session = service._get_session(request_id)
        with session.condition:
            terminal = session.terminal_event
            count = session.terminal_count
            if terminal is None or terminal.request_id != request_id:
                raise ValueError("gateway_authority_unavailable")
            # This is the gateway's receipt written at the actual notice
            # publication, not a later read of router's current attempt.
            receipt = session.terminal_provenance
            if (not isinstance(receipt, tuple) or len(receipt) != 4
                or receipt != (identity.request_id, identity.request_attempt,
                               identity.path_digest, identity.command_id)
                or count != 1 or terminal.kind != "failed"
                or terminal.code != "request_cleanup_unproven"):
                raise ValueError("gateway_authority_unavailable")
            gateway = {"request_id": receipt[0], "request_attempt": receipt[1],
                       "terminal_event_count": count, "terminal_kind": terminal.kind,
                       "terminal_code": terminal.code}
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("gateway_authority_unavailable") from exc
    return {"command_authority": command, "gateway_authority": gateway}
