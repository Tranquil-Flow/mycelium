#!/usr/bin/env python3
"""Assemble privacy-reduced M21 evidence from one live heterogeneous route."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_m21_heterogeneous import (  # noqa: E402
    build_heterogeneous_evidence,
    endpoint_identity_digest,
    pseudonymous_member_id,
)
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from mycelium_node.identity import load_node_signer  # noqa: E402
from mycelium_seed.authority import (  # noqa: E402
    SeedAuthorityError,
    derive_product_pseudonym_salt,
    load_product_pseudonym_salt,
)
from mycelium_seed.operator import seed_inventory  # noqa: E402


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"m21_input_invalid:{path.name}")
    return value


def _fetch(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("m21_live_status_invalid")
    return value


def _endpoint_ids(seed_root: Path) -> dict[str, str]:
    database = seed_root / "state.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute(
            "SELECT node_id, endpoint_id FROM seed_members ORDER BY node_id"
        ).fetchall()
    finally:
        connection.close()
    if not rows or any(not isinstance(node, str) or not isinstance(endpoint, str) for node, endpoint in rows):
        raise ValueError("m21_endpoint_inventory_invalid")
    return dict(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-state-root", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--transport-matrix", type=Path, required=True)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--before-status", type=Path, required=True)
    parser.add_argument("--live-base-url", required=True)
    parser.add_argument("--deployment-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    inventory = seed_inventory(args.seed_state_root)
    topology = _read(args.topology)
    matrix = _read(args.transport_matrix)
    operator_plan = _read(args.operator_plan)
    before = _read(args.before_status)
    after = _fetch(args.live_base_url.rstrip("/") + "/__mycelium/live-status")
    if (
        before.get("simulated") is not False
        or after.get("simulated") is not False
        or after.get("route_alive") is not True
        or before.get("deployment_id") != after.get("deployment_id")
    ):
        raise ValueError("m21_physical_live_route_required")
    before_counters = before.get("counters", {})
    after_counters = after.get("counters", {})
    frames_before = int(before_counters.get("frames_sent", -1))
    frames_after = int(after_counters.get("frames_sent", -1))
    if frames_before < 0 or frames_after <= frames_before or after_counters.get("fatal") is not None:
        raise ValueError("m21_live_frame_delta_required")

    route_nodes = {str(node["node_id"]): node for node in topology["nodes"]}
    identity_root = args.seed_state_root / "identity"
    try:
        salt = load_product_pseudonym_salt(identity_root).hex()
    except SeedAuthorityError as exc:
        if exc.code != "seed_product_pseudonym_key_missing":
            raise
        salt = derive_product_pseudonym_salt(
            load_node_signer(identity_root / "seed.key"),
            swarm_id=inventory["swarm_id"],
        ).hex()
    endpoints = _endpoint_ids(args.seed_state_root)
    members = []
    for member in inventory["members"]:
        node_id = str(member["node_id"])
        peer_class = str(member["peer_class"])
        route_node = route_nodes.get(node_id)
        runtime = (
            str(route_node["runtime_backend"])
            if route_node is not None
            else {
                "browser_http": "browser",
                "pixel_http": "android",
                "android_termux_iroh": "pixel-stdlib",
                "linux_tbd": "tbd",
            }.get(peer_class, "unknown")
        )
        participant = route_node is not None
        eligible = bool(member["activation_eligible"])
        members.append(
            {
                "member_id": pseudonymous_member_id(node_id, salt=salt),
                "peer_class": peer_class,
                "runtime_backend": runtime,
                "trust_state": "invited",
                "generation": int(member["generation"]),
                "incarnation": str(member["incarnation"]),
                "freshness": str(member["lease_freshness"]),
                "revocation_state": str(member["revocation_state"]),
                "activation_eligible": eligible,
                "route_participant": participant,
                "eligibility_reason": "eligible" if eligible else "activation_protocol_unavailable",
                "connectivity": "direct" if participant else "unknown",
                "external_network": route_node is not None and route_node.get("platform") == "linux",
                "endpoint_identity_digest": endpoint_identity_digest(endpoints[node_id]),
            }
        )
    pseudonyms = {
        node_id: pseudonymous_member_id(node_id, salt=salt) for node_id in endpoints
    }
    paths = [
        {
            "source_member_id": pseudonyms[item["local_node_id"]],
            "destination_member_id": pseudonyms[item["remote_node_id"]],
            "path_class": item["path_class"],
            "relay_region": item["relay_region"],
            "cold_rtt_ms": item["cold_rtt_ms"],
            "warm_rtt_ms": item["warm_rtt_ms"],
            "jitter_ms": item["jitter_ms"],
            "loss_ratio": item["loss_ratio"],
            "goodput_bytes_per_second": item["observed_goodput_Bps"],
            "reconnect_count": item["reconnect_count"],
            "connection_generation": item["connection_generation"],
            "selected_path_changes": item["selected_path_changes"],
            "sample_count": item["sample_count"],
        }
        for item in matrix["observations"]
        if item["local_node_id"] in route_nodes and item["remote_node_id"] in route_nodes
    ]
    recent = after.get("recent_inferences", [])
    latest_output = int(recent[-1]["output_tokens"]) if recent else 0
    runtime_classes = {str(node["runtime_backend"]) for node in route_nodes.values()}
    graph = operator_plan["controller"]["run_plan"]["nodes"][0]["configure"]["graph"]
    if graph["deployment_id"] != after["deployment_id"] or graph["model_id"] != after["model_id"]:
        raise ValueError("m21_operator_route_binding_mismatch")
    evidence = build_heterogeneous_evidence(
        generated_at_unix_ms=int(time.time() * 1_000),
        binding={
            "swarm_id": inventory["swarm_id"],
            "seed_key_digest": inventory["seed_key_digest"],
            "seed_node_id": inventory["seed_node_id"],
            "deployment_id": after["deployment_id"],
            "model_id": after["model_id"],
            "model_revision": graph["resolved_commit"],
            "membership_generation": max(int(member["generation"]) for member in inventory["members"]),
        },
        policy={
            "invitation_ownership": "owner_only",
            "operator_approval": "required",
            "maximum_invite_ttl_seconds": 600,
            "single_use": True,
            "request_quota_per_hour": 60,
            "byte_quota_per_hour": 1_073_741_824,
            "audit_retention_days": 30,
            "revocation_supported": True,
            "credential_rotation_supported": True,
            "abuse_response": "revoke_then_rotate",
            "permissionless_participation": False,
            "byzantine_resistance": False,
            "malicious_worker_confidentiality": False,
        },
        members=members,
        paths=paths,
        route={
            "physical": True,
            "route_alive": True,
            "heterogeneous": len(runtime_classes) >= 2,
            "participant_count": len(route_nodes),
            "runtime_class_count": len(runtime_classes),
            "frame_count_before": frames_before,
            "frame_count_after": frames_after,
            "latest_output_token_count": latest_output,
            "tailscale_product_dependency": False,
            "activation_transport": "endpointid_authenticated_iroh",
            "operator_staging_transport": "ssh_or_tailscale_optional",
        },
        exclusions=("path_transition_not_observed_within_budget",),
    )
    args.deployment_dir.mkdir(parents=True, exist_ok=True)
    (args.deployment_dir / "m21-heterogeneous.json").write_bytes(canonical_json_bytes(evidence))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "protocol": "mycelium.m21_physical_gate.v1",
        "gate_state": evidence["gate_state"],
        "evidence_digest": evidence["evidence_digest"],
        "participant_count": evidence["route"]["participant_count"],
        "runtime_class_count": evidence["route"]["runtime_class_count"],
        "frame_delta": frames_after - frames_before,
        "latest_output_token_count": latest_output,
        "tailscale_product_dependency": False,
        "activation_transport": "endpointid_authenticated_iroh",
        "network_download_performed": False,
    }
    args.output.write_bytes(canonical_json_bytes(report))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
