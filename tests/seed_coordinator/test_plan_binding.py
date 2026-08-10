from __future__ import annotations

from mycelium_node.identity import load_or_create_node_signer
from mycelium_seed.plan_binding import bind_operator_plan_document


def test_plan_binding_reissues_offers_from_current_members(tmp_path) -> None:
    signer = load_or_create_node_signer(tmp_path / "seed" / "seed.key")
    digest = "sha256:" + "a" * 64
    plan = {
        "controller": {
            "now": 1.0,
            "membership_snapshot": {
                "seed_key_digest": "sha256:" + "0" * 64,
                "swarm_id": "old-swarm",
                "assignment_offers": [
                    {
                        "message": {
                            "protocol": "mycelium.membership.assignment_offer.v1",
                            "message_id": "old-offer",
                            "swarm_id": "old-swarm",
                            "sender_node_id": "old-seed",
                            "sender_endpoint_id": "old-endpoint",
                            "recipient_node_id": "node-a",
                            "incarnation": "old-incarnation",
                            "generation": 1,
                            "issued_at": 1.0,
                            "expires_at": 2.0,
                            "deployment_id": "deployment-a",
                            "deployment_epoch": 1,
                            "assignment_id": "assignment-a",
                            "assignment_digest": digest,
                            "stage_pack_digest": digest,
                            "graph_digest": digest,
                            "load_generation": 1,
                            "placement_provenance": "target_local_physical_preload",
                            "peer_endpoint_records": [],
                        }
                    }
                ],
            },
        }
    }
    members = [{
        "node_id": "node-a",
        "endpoint_id": "endpoint-a",
        "peer_class": "mac_mlx_iroh",
        "runtime_capability": {"runtime_backend": "mlx", "transport": "iroh", "activation_protocol": "mycelium.router_wire.v1"},
        "generation": 3,
        "lease_expires_at": 500.0,
    }]

    bound = bind_operator_plan_document(
        plan,
        signer=signer,
        swarm_id="current-swarm",
        seed_node_id="current-seed",
        members=members,
        now=100.0,
    )

    snapshot = bound["controller"]["membership_snapshot"]
    message = snapshot["assignment_offers"][0]["message"]
    assert snapshot["seed_key_digest"] == signer.verification_key_digest
    assert snapshot["swarm_id"] == "current-swarm"
    assert message["swarm_id"] == "current-swarm"
    assert message["generation"] == 3
    assert plan["controller"]["membership_snapshot"]["swarm_id"] == "old-swarm"
