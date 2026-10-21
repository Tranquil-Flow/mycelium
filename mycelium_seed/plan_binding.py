# SPDX-License-Identifier: AGPL-3.0-only
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
    observed_boot_ids: Mapping[str, str] | None = None,
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
    activation_endpoint_by_node: dict[str, str] = {}
    for envelope in offers:
        message = envelope.get("message") if isinstance(envelope, Mapping) else None
        recipient = message.get("recipient_node_id") if isinstance(message, Mapping) else None
        if not isinstance(recipient, str) or recipient in recipients:
            raise PlanBindingError("operator_plan_binding_invalid")
        recipients.append(recipient)
        records = message.get("peer_endpoint_records")
        if not isinstance(records, list):
            raise PlanBindingError("operator_plan_binding_invalid")
        for record in records:
            if not isinstance(record, Mapping):
                raise PlanBindingError("operator_plan_binding_invalid")
            node_id = record.get("node_id")
            endpoint_id = record.get("endpoint_id")
            if (
                not isinstance(node_id, str)
                or not node_id
                or not isinstance(endpoint_id, str)
                or not endpoint_id
            ):
                raise PlanBindingError("operator_plan_activation_endpoint_invalid")
            prior = activation_endpoint_by_node.setdefault(node_id, endpoint_id)
            if prior != endpoint_id:
                raise PlanBindingError("operator_plan_activation_endpoint_conflict")
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
    if len(selected) > 1 and set(activation_endpoint_by_node) != set(selected):
        raise PlanBindingError("operator_plan_activation_endpoint_missing")
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
                    # Seed control-plane identity proves current membership. The
                    # separately authorized Iroh activation identity must survive
                    # a membership-generation renewal.
                    "endpoint_id": activation_endpoint_by_node[node_id],
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
    # --------------------------------------------------------------------------------------------
    # bind boot_id ONTO THE PEERS, which this function previously never touched.
    #
    # It refreshes the offers (generation, issued_at, expires_at, peer_endpoint_records) and re-signs
    # them, but `result["controller"]["peers"]` is what workflow.py turns into PeerIdentity(**peer) --
    # and so what the qualification's identity check compares against the node's LIVE hello. Left
    # alone, the plan keeps whatever boot_id the topology recorded, while the node reports its CURRENT
    # boot at qualification: one boot apart, which is the capture-vs-use race that makes qualification
    # a coin flip (measured 2026-09-19: soak03's plan carried boot-af00774d... while the node's hello
    # said boot-a0423521..., and every round relaunches the apps so every round the ids move).
    #
    # WHY THE OBSERVATION AND NOT THE MEMBER: boot_id is NOT a membership field -- grep shows it nowhere
    # in mycelium_membership -- so `members` cannot supply it. The node's own observation carries it and
    # is written every round (managed_observations.py:256).
    #
    # SAFETY SHAPE: additive and inert by default. With no observations supplied, the peers are left
    # EXACTLY as before, so no existing caller changes behaviour. When supplied, only boot_id is
    # replaced, and only for a node we have an observation for -- node_id, host_id and every other peer
    # field stay untouched, so a node presenting a different node_id or host_id is still refused by the
    # identity check exactly as before. The acceptance rule is not weakened anywhere.
    #
    # AND WHY NOTHING IS RECORDED IN THE PLAN: the controller's field set is CLOSED
    # (mycelium_physical_runner/config.py:40-54) and every set member is checked exactly
    # (_CONTROLLER_PEER_FIELDS at :55-65 includes "boot_id", so refreshing its VALUE is legal, while
    # adding a controller key is not). An earlier version of this block also wrote
    # result["controller"]["boot_id_bindings"], which made load_operator_plan refuse the ENTIRE bound
    # plan with `plan_unknown_field: controller.boot_id_bindings`, surfacing as
    # ServingPreparationError("operator_plan_canonical_invalid") and stopping serving -- measured
    # 2026-09-19, i.e. the fix's own audit trail would have blocked every round in which the fix
    # worked. The refresh is therefore reported by the CALLER (workflow.py prints `boot_id_refreshed`)
    # and never stored in the plan.
    if observed_boot_ids:
        peers = result["controller"].get("peers")
        if isinstance(peers, list):
            for peer in peers:
                if not isinstance(peer, dict):
                    continue
                node_id = peer.get("node_id")
                observed = observed_boot_ids.get(node_id) if isinstance(node_id, str) else None
                if isinstance(observed, str) and observed and peer.get("boot_id") != observed:
                    peer["boot_id"] = observed
    return result


__all__ = ["PlanBindingError", "bind_operator_plan_document"]
