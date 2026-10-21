# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations


def test_layer_planner_owns_closed_a6_recovery_intent_projection() -> None:
    from mycelium_layer_planner import project_recovery_intent

    intent = project_recovery_intent(
        {
            "deployment_id": "deployment-a6",
            "deployment_epoch": 16,
            "topology_generation": 7,
            "model_id": "org/model",
            "model_revision": "a" * 40,
            "representation_digest": "sha256:" + "b" * 64,
            "graph_digest": "sha256:" + "c" * 64,
        },
        retained_path_ids=("path-source",),
        candidate_path_ids=("path-successor",),
        equivalent_candidate_generations=3,
        candidate_first_seen_unix_ms=1_000,
        generated_at_unix_ms=12_000,
    )

    assert intent["protocol"] == "mycelium.full_context_recovery_intent.v1"
    assert intent["route_ready"] is False
    assert intent["hysteresis"]["state"] == "stable_candidate"
    assert intent["retained_path_ids"] == ["path-source"]
    assert intent["candidate_path_ids"] == ["path-successor"]
