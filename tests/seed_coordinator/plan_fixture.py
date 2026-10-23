# SPDX-License-Identifier: AGPL-3.0-only
"""Pure plan fixtures; importing them never imports a model builder."""

def _two_member_plan(*, second_endpoint: str = "activation-b") -> dict:
    digest = "sha256:" + "a" * 64
    messages = []
    for recipient, peer, endpoint in (
        ("node-a", "node-b", second_endpoint),
        ("node-b", "node-a", "activation-a"),
    ):
        messages.append(
            {
                "message": {
                    "protocol": "mycelium.membership.assignment_offer.v1",
                    "message_id": f"old-{recipient}",
                    "swarm_id": "old-swarm",
                    "sender_node_id": "old-seed",
                    "sender_endpoint_id": "old-seed-endpoint",
                    "recipient_node_id": recipient,
                    "incarnation": "old-incarnation",
                    "generation": 1,
                    "issued_at": 1.0,
                    "expires_at": 2.0,
                    "deployment_id": "deployment-a",
                    "deployment_epoch": 1,
                    "assignment_id": f"assignment-{recipient}",
                    "assignment_digest": digest,
                    "stage_pack_digest": digest,
                    "graph_digest": digest,
                    "load_generation": 1,
                    "placement_provenance": "planner_v2",
                    "peer_endpoint_records": [
                        {
                            "node_id": peer,
                            "endpoint_id": endpoint,
                            "deployment_epoch": 1,
                            "membership_generation": 1,
                            "valid_from": 1.0,
                            "valid_until": 2.0,
                        }
                    ],
                }
            }
        )
    return {
        "controller": {
            "now": 1.0,
            "membership_snapshot": {
                "seed_key_digest": "sha256:" + "0" * 64,
                "swarm_id": "old-swarm",
                "assignment_offers": messages,
            },
        }
    }
