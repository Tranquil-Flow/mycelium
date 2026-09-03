from __future__ import annotations

from types import SimpleNamespace

from scripts.qualify_a5_replica_track import (
    derive_track_pair,
    directed_link_qualified,
    runtime_clean,
    signed_memory_safe,
)


def _placement(placement_id: str, node_id: str, group: str, signature: str):
    return SimpleNamespace(
        placement_id=placement_id,
        node_id=node_id,
        replica_group_id=group,
        stage_signature=signature,
        load_proof_digest="sha256:" + "1" * 64,
        assignment_id=f"assignment-{placement_id}",
    )


def _graph():
    return SimpleNamespace(
        deployment_id="deployment-a5",
        deployment_epoch=1,
        topology_version=7,
        stages=(
            SimpleNamespace(
                stage_id="stage-0",
                placements=(
                    _placement("p0", "n0", "g0", "sig0"),
                    _placement("p2", "n2", "g0", "sig0"),
                ),
            ),
            SimpleNamespace(
                stage_id="stage-1",
                placements=(_placement("p1", "n1", "g1", "sig1"),),
            ),
        ),
    )


def test_derive_track_pair_binds_complete_ordered_tracks():
    pair = derive_track_pair(_graph())
    assert pair.incumbent_placement_ids == ("p0", "p1")
    assert pair.replica_placement_ids == ("p2", "p1")
    assert pair.replica_placement_id == "p2"
    assert pair.replica_group_id == "g0"
    assert pair.replica_node_ids == frozenset({"n2", "n1"})


def test_signed_memory_safe_fails_closed_when_selected_snapshot_missing():
    attestation = {
        "signed_observations": [
            {
                "observation": {
                    "event": "snapshot",
                    "node_id": "n2",
                    "details": {
                        "host_resources": {
                            "available_memory_bytes": 100,
                            "rss_bytes": 20,
                        }
                    },
                }
            }
        ]
    }
    assert signed_memory_safe(attestation, frozenset({"n2"})) is True
    assert signed_memory_safe(attestation, frozenset({"n2", "n1"})) is False


def test_runtime_clean_requires_zero_queue_reservations_and_kv():
    runtime = {
        "queue": {"depth": 0, "active_request_ids": []},
        "placements": [
            {"placement_id": "p2", "active_reservations": 0},
            {"placement_id": "p1", "active_reservations": 0},
        ],
    }
    live = {
        "route_alive": True,
        "peers": [
            {"node_id": "n2", "active_kv_state_count": 0, "transport_fatal": False},
            {"node_id": "n1", "active_kv_state_count": 0, "transport_fatal": False},
        ],
    }
    assert runtime_clean(runtime, live, ("p2", "p1"), frozenset({"n2", "n1"}))
    live["peers"][0]["active_kv_state_count"] = 1
    assert not runtime_clean(runtime, live, ("p2", "p1"), frozenset({"n2", "n1"}))


def test_directed_link_qualification_requires_participating_node_deltas():
    before = {
        "peers": [
            {"node_id": "n2", "frames_sent": 10, "frames_received": 5, "applied_operation_count": 3},
            {"node_id": "n1", "frames_sent": 8, "frames_received": 7, "applied_operation_count": 4},
        ]
    }
    after = {
        "peers": [
            {"node_id": "n2", "frames_sent": 12, "frames_received": 6, "applied_operation_count": 4},
            {"node_id": "n1", "frames_sent": 9, "frames_received": 9, "applied_operation_count": 5},
        ]
    }
    assert directed_link_qualified(before, after, frozenset({"n2", "n1"}))
    after["peers"][1]["frames_received"] = 7
    assert not directed_link_qualified(before, after, frozenset({"n2", "n1"}))
