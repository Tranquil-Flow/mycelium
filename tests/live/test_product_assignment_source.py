from __future__ import annotations

from types import SimpleNamespace

from mycelium_live.route import PhysicalLiveRoute


def test_physical_route_projects_only_validated_assignment_and_load_bindings() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._membership_snapshot = {
        "assignment_offers": [
            {
                "message": {
                    "assignment_id": "assignment-a",
                    "recipient_node_id": "node-a",
                    "generation": 4,
                    "load_generation": 17,
                    "assignment_digest": "sha256:" + "a" * 64,
                    "stage_pack_digest": "sha256:" + "b" * 64,
                }
            }
        ]
    }
    route._graph = SimpleNamespace(
        stages=(
            SimpleNamespace(
                stage_id="stage-a",
                placements=(
                    SimpleNamespace(
                        assignment_id="assignment-a",
                        load_proof_digest="sha256:" + "c" * 64,
                    ),
                ),
            ),
        )
    )

    assert route.product_assignment_records() == (
        {
            "assignment_id": "assignment-a",
            "node_id": "node-a",
            "stage_id": "stage-a",
            "membership_generation": 4,
            "load_generation": 17,
            "assignment_digest": "sha256:" + "a" * 64,
            "stage_pack_digest": "sha256:" + "b" * 64,
            "load_proof_digest": "sha256:" + "c" * 64,
        },
    )
