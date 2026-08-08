from __future__ import annotations

import pytest

from mycelium_router.transports.iroh import PeerBinding, PeerSet


def _binding(
    node_id: str = "node-a",
    endpoint_id: str = "ep-a",
    generation: int = 1,
) -> PeerBinding:
    return PeerBinding(
        node_id=node_id,
        endpoint_id=endpoint_id,
        endpoint_addr={"id": endpoint_id, "addrs": []},
        generation=generation,
    )


def test_peer_set_accepts_and_lookups_by_node_id() -> None:
    ps = PeerSet()
    ps.upsert(_binding("node-a", "ep-a", 1))
    ps.upsert(_binding("node-b", "ep-b", 1))

    assert ps.lookup("node-a").node_id == "node-a"
    assert ps.lookup("node-b").node_id == "node-b"
    assert ps.count == 2


def test_peer_set_rejects_lookup_for_unknown_destination() -> None:
    ps = PeerSet()
    ps.upsert(_binding("node-a", "ep-a", 1))
    with pytest.raises(KeyError, match="node-c"):
        ps.lookup("node-c")


def test_peer_set_rejects_stale_generation_upsert() -> None:
    ps = PeerSet()
    ps.upsert(_binding("node-a", "ep-a", 5))
    with pytest.raises(ValueError, match="stale_peer_generation"):
        ps.upsert(_binding("node-a", "ep-a", 3))


def test_peer_set_accepts_monotonic_generation_upsert() -> None:
    ps = PeerSet()
    ps.upsert(_binding("node-a", "ep-a", 1))
    ps.upsert(_binding("node-a", "ep-a", 5))
    assert ps.lookup("node-a").generation == 5
    assert ps.count == 1


def test_peer_set_atomic_replace_validates_all_before_mutating() -> None:
    ps = PeerSet()
    ps.upsert(_binding("node-a", "ep-a", 1))
    ps.upsert(_binding("node-b", "ep-b", 1))

    replacements = [
        _binding("node-a", "ep-a2", 2),
        _binding("node-b", "ep-b2", 2),
    ]
    ps.atomic_replace(replacements)

    assert ps.lookup("node-a").endpoint_id == "ep-a2"
    assert ps.lookup("node-b").endpoint_id == "ep-b2"
    assert ps.count == 2


def test_peer_set_atomic_replace_rejects_partial_set_with_stale_generation() -> None:
    ps = PeerSet()
    ps.upsert(_binding("node-a", "ep-a", 10))
    ps.upsert(_binding("node-b", "ep-b", 10))

    bad_replacements = [
        _binding("node-a", "ep-a2", 11),
        _binding("node-b", "ep-b2", 5),  # stale
    ]
    with pytest.raises(ValueError, match="stale_peer_generation"):
        ps.atomic_replace(bad_replacements)

    # State unchanged after rejection
    assert ps.lookup("node-a").endpoint_id == "ep-a"
    assert ps.lookup("node-b").endpoint_id == "ep-b"


def test_peer_set_snapshot_is_immutable_view() -> None:
    ps = PeerSet()
    ps.upsert(_binding("node-a", "ep-a", 1))
    snapshot = ps.snapshot()
    assert set(snapshot.keys()) == {"node-a"}
    assert snapshot["node-a"].endpoint_id == "ep-a"
