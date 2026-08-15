from __future__ import annotations

import pytest

from mycelium_node.identity import load_or_create_node_signer
from mycelium_seed.plan_binding import PlanBindingError, bind_operator_plan_document
from scripts.bind_operator_plan_to_seed import _rotate_run_session


def test_operator_plan_run_rotation_preserves_deployment_authority() -> None:
    plan = {
        "run_id": "old-run",
        "controller": {
            "run_plan": {
                "run_id": "old-run",
                "deployment_id": "deployment-a",
                "nodes": [{"node_id": "node-a"}],
            },
            "peers": [
                {
                    "node_id": "node-a",
                    "host_id": "old-host",
                    "boot_id": "old-boot",
                }
            ],
            "membership_snapshot": {"deployment_id": "deployment-a"},
        },
    }

    def refresh(peers: list[dict], run_id: str) -> list[dict]:
        assert run_id == "fresh-run"
        peers[0]["host_id"] = "fresh-host"
        peers[0]["boot_id"] = "fresh-boot"
        return peers

    rotated = _rotate_run_session(
        plan,
        run_id="fresh-run",
        identity_refresher=refresh,
    )

    assert rotated["run_id"] == "fresh-run"
    assert rotated["controller"]["run_plan"]["run_id"] == "fresh-run"
    assert rotated["controller"]["peers"][0]["host_id"] == "fresh-host"
    assert rotated["controller"]["membership_snapshot"] == {
        "deployment_id": "deployment-a"
    }
    assert plan["run_id"] == "old-run"


@pytest.mark.parametrize("run_id", ["", "has a space", "x" * 129])
def test_operator_plan_run_rotation_rejects_invalid_run_id(run_id: str) -> None:
    with pytest.raises(PlanBindingError, match="operator_plan_run_id_invalid"):
        _rotate_run_session(
            {
                "run_id": "old-run",
                "controller": {"run_plan": {"run_id": "old-run"}, "peers": [{}]},
            },
            run_id=run_id,
            identity_refresher=lambda peers, _run_id: peers,
        )


def test_operator_plan_run_rotation_rejects_mismatched_existing_session() -> None:
    with pytest.raises(PlanBindingError, match="operator_plan_run_session_invalid"):
        _rotate_run_session(
            {
                "run_id": "root-run",
                "controller": {"run_plan": {"run_id": "other-run"}, "peers": [{}]},
            },
            run_id="fresh-run",
            identity_refresher=lambda peers, _run_id: peers,
        )


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


def _members() -> list[dict]:
    return [
        {
            "node_id": node_id,
            "endpoint_id": membership_endpoint,
            "peer_class": "linux_numpy_iroh",
            "runtime_capability": {
                "runtime_backend": "numpy",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
            "generation": generation,
            "lease_expires_at": 500.0,
        }
        for node_id, membership_endpoint, generation in (
            ("node-a", "membership-a", 7),
            ("node-b", "membership-b", 9),
        )
    ]


def test_plan_binding_preserves_planner_authorized_activation_endpoints(
    tmp_path,
) -> None:
    signer = load_or_create_node_signer(tmp_path / "seed" / "seed.key")

    bound = bind_operator_plan_document(
        _two_member_plan(),
        signer=signer,
        swarm_id="current-swarm",
        seed_node_id="current-seed",
        members=_members(),
        now=100.0,
    )

    offers = bound["controller"]["membership_snapshot"]["assignment_offers"]
    by_recipient = {
        offer["message"]["recipient_node_id"]: offer["message"] for offer in offers
    }
    assert by_recipient["node-a"]["generation"] == 7
    assert by_recipient["node-b"]["generation"] == 9
    assert (
        by_recipient["node-a"]["peer_endpoint_records"][0]["endpoint_id"]
        == "activation-b"
    )
    assert (
        by_recipient["node-b"]["peer_endpoint_records"][0]["endpoint_id"]
        == "activation-a"
    )


def test_plan_binding_rejects_conflicting_activation_endpoint_identity(
    tmp_path,
) -> None:
    signer = load_or_create_node_signer(tmp_path / "seed" / "seed.key")
    plan = _two_member_plan()
    plan["controller"]["membership_snapshot"]["assignment_offers"][0]["message"][
        "peer_endpoint_records"
    ].append(
        {
            "node_id": "node-a",
            "endpoint_id": "conflicting-activation-a",
            "deployment_epoch": 1,
            "membership_generation": 1,
            "valid_from": 1.0,
            "valid_until": 2.0,
        }
    )

    with pytest.raises(
        PlanBindingError,
        match="operator_plan_activation_endpoint_conflict",
    ):
        bind_operator_plan_document(
            plan,
            signer=signer,
            swarm_id="current-swarm",
            seed_node_id="current-seed",
            members=_members(),
            now=100.0,
        )
