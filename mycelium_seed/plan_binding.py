# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bind stored physical operator plans to current durable seed membership."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import math
from typing import Any

from mycelium_membership import peer_runtime_is_activation_eligible, sign_membership_message
from mycelium_qualification.signing import Ed25519EvidenceSigner


class PlanBindingError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def bind_operator_plan_document(
    plan: Mapping[str, Any],
    *,
    signer: Ed25519EvidenceSigner,
    swarm_id: str,
    seed_node_id: str,
    members: Sequence[Mapping[str, Any]],
    now: float,
) -> dict[str, Any]:
    """Return a detached plan with freshly signed coordinator-bound offers."""

    if not isinstance(plan, Mapping) or not math.isfinite(now) or now <= 0:
        raise PlanBindingError("operator_plan_binding_invalid")
    try:
        result = json.loads(json.dumps(dict(plan), allow_nan=False))
        snapshot = result["controller"]["membership_snapshot"]
        offers = snapshot["assignment_offers"]
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise PlanBindingError("operator_plan_binding_invalid") from exc
    if not isinstance(snapshot, dict) or not isinstance(offers, list) or not offers:
        raise PlanBindingError("operator_plan_binding_invalid")
    current = {
        member.get("node_id"): dict(member)
        for member in members
        if isinstance(member, Mapping) and isinstance(member.get("node_id"), str)
    }
    recipients: list[str] = []
    for envelope in offers:
        message = envelope.get("message") if isinstance(envelope, Mapping) else None
        recipient = message.get("recipient_node_id") if isinstance(message, Mapping) else None
        if not isinstance(recipient, str) or recipient in recipients:
            raise PlanBindingError("operator_plan_binding_invalid")
        recipients.append(recipient)
    selected: dict[str, dict[str, Any]] = {}
    for recipient in recipients:
        member = current.get(recipient)
        runtime = None if member is None else member.get("runtime_capability")
        if (
            member is None
            or not isinstance(runtime, Mapping)
            or not peer_runtime_is_activation_eligible(member.get("peer_class"), runtime)
            or not isinstance(member.get("lease_expires_at"), (int, float))
            or float(member["lease_expires_at"]) <= now
            or not isinstance(member.get("endpoint_id"), str)
            or not isinstance(member.get("generation"), int)
        ):
            raise PlanBindingError("operator_plan_member_ineligible")
        selected[recipient] = member
    valid_until = min(now + 3_600.0, *(float(item["lease_expires_at"]) for item in selected.values()))
    refreshed = []
    for index, envelope in enumerate(offers):
        message = dict(envelope["message"])
        recipient = message["recipient_node_id"]
        member = selected[recipient]
        message.update(
            message_id=f"bound-plan-offer-{index}-{int(now * 1_000)}",
            swarm_id=swarm_id,
            sender_node_id=seed_node_id,
            sender_endpoint_id=signer.endpoint_id,
            incarnation=f"bound-seed-{int(now * 1_000)}",
            generation=member["generation"],
            issued_at=now,
            expires_at=valid_until,
            peer_endpoint_records=[
                {
                    "node_id": node_id,
                    "endpoint_id": peer["endpoint_id"],
                    "deployment_epoch": message["deployment_epoch"],
                    "membership_generation": peer["generation"],
                    "valid_from": now,
                    "valid_until": valid_until,
                }
                for node_id, peer in sorted(selected.items())
                if node_id != recipient
            ],
        )
        refreshed.append(sign_membership_message(signer=signer, message=message))
    snapshot["seed_key_digest"] = signer.verification_key_digest
    snapshot["swarm_id"] = swarm_id
    snapshot["assignment_offers"] = refreshed
    result["controller"]["now"] = now
    return result


__all__ = ["PlanBindingError", "bind_operator_plan_document"]
