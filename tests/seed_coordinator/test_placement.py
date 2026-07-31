from __future__ import annotations

import hashlib
import json
from itertools import count
from pathlib import Path

import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SqliteSeedState
from mycelium_seed.placement import (
    FROZEN_PLACEMENT_PROTOCOL,
    FrozenPlacementSource,
    MemberRecord,
    PlacementDecision,
    PlacementError,
    PlannerPlacementSource,
)


NOW = 2_000.0
MAC_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _member(
    node_id: str = "node-a",
    *,
    activation_eligible: bool = True,
) -> MemberRecord:
    return MemberRecord(
        node_id=node_id,
        endpoint_id=f"{node_id}-endpoint",
        peer_class="mac_mlx_iroh" if activation_eligible else "browser_http",
        runtime_capability=(
            MAC_RUNTIME_CAPABILITY
            if activation_eligible
            else {
                "runtime_backend": "browser",
                "transport": "http",
                "activation_protocol": None,
            }
        ),
        generation=1,
        lease_expires_at=NOW + 300.0,
        activation_eligible=activation_eligible,
    )


def _fixture_bytes(*, node_id: str = "node-a", end_layer: int = 6) -> bytes:
    return (
        json.dumps(
            {
                "protocol": FROZEN_PLACEMENT_PROTOCOL,
                "placement_id": "dialogpt-small-two-node",
                "assignments": [
                    {
                        "node_id": node_id,
                        "assignment_id": f"assignment-{node_id}",
                        "start_layer": 0,
                        "end_layer_exclusive": end_layer,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _planner_snapshot() -> dict:
    return {
        "admitted_node_ids": ["node-a"],
        "model": {
            "model_id": "org/model",
            "revision": "immutable-revision",
            "weight_digest": "sha256:" + "a" * 64,
            "architecture": "Decoder",
            "num_layers": 4,
            "hidden_size": 128,
            "dtype_bytes": 2,
            "kv_heads": 2,
            "head_dim": 32,
            "weight_bytes": 4_000,
        },
        "nodes": [
            {
                "node_id": "node-a",
                "prefill_ms_per_layer_token": 0.001,
                "decode_ms_per_layer_token": 0.001,
                "fast_memory_bytes": 100_000_000,
                "total_memory_bytes": 200_000_000,
                "memory_bandwidth_Bps": 1_000_000_000,
                "spill_bandwidth_Bps": 1_000_000_000,
            }
        ],
        "links": [],
        "workload": {
            "preset": "interactive_chat_v1",
            "concurrency_points": [1],
        },
        "policy": {
            "memory_reserve_fraction": 0,
            "replica_budget": 0,
            "ttft_slo_ms": 1_000_000,
            "tpot_slo_ms": 1_000_000,
        },
    }


def _coordinator(root: Path, placement_source) -> SeedCoordinator:
    database = root / "seed-state" / "state.sqlite3"
    return SeedCoordinator(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_url="http://127.0.0.1:8788",
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
        invite_registry=SqliteInviteRegistry(database),
        state=SqliteSeedState(database),
        incarnation="seed-incarnation",
        placement_source=placement_source,
        clock=lambda: NOW,
        id_source=_ids("seed-message"),
        lease_seconds=300.0,
    )


def _join(coordinator: SeedCoordinator, root: Path, *, node_id: str) -> None:
    node = NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(root / "nodes" / f"{node_id}.key"),
        incarnation=f"{node_id}-incarnation",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability=MAC_RUNTIME_CAPABILITY,
        clock=lambda: NOW,
        id_source=_ids(f"{node_id}-message"),
    )
    bundle = coordinator.mint_invite(nonce=f"invite-{node_id}", ttl_seconds=120)
    verified = verify_invite_bundle(bundle, now=NOW)
    acceptance = coordinator.accept_join(
        invite_token=bundle["token"],
        join_envelope=node.join_request(
            invite_nonce=verified["payload"]["nonce"],
            endpoint_addrs=["https://100.117.33.124:9443/control"],
        ),
    )
    node.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])


def test_frozen_source_loads_fixture_and_binds_exact_bytes(tmp_path: Path) -> None:
    fixture = tmp_path / "placement.json"
    raw = _fixture_bytes()
    fixture.write_bytes(raw)

    decision = FrozenPlacementSource(fixture).compile([_member()])

    assert decision.placement_provenance == "frozen_fixture"
    assert decision.placement_id == "dialogpt-small-two-node"
    assert decision.source_digest == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert tuple(assignment["node_id"] for assignment in decision.assignments) == (
        "node-a",
    )


def test_frozen_source_detects_fixture_mutation_after_pin(tmp_path: Path) -> None:
    fixture = tmp_path / "placement.json"
    fixture.write_bytes(_fixture_bytes())
    source = FrozenPlacementSource(fixture)
    fixture.write_bytes(_fixture_bytes(end_layer=5))

    with pytest.raises(PlacementError) as raised:
        source.compile([_member()])

    assert raised.value.code == "placement_fixture_changed"


@pytest.mark.parametrize(
    ("members", "fixture_node_id", "expected_code"),
    [
        ([_member("node-a")], "node-unknown", "placement_member_unknown"),
        (
            [_member("node-browser", activation_eligible=False)],
            "node-browser",
            "placement_member_activation_ineligible",
        ),
    ],
)
def test_frozen_source_rejects_unusable_assignment_members(
    tmp_path: Path,
    members: list[MemberRecord],
    fixture_node_id: str,
    expected_code: str,
) -> None:
    fixture = tmp_path / "placement.json"
    fixture.write_bytes(_fixture_bytes(node_id=fixture_node_id))

    with pytest.raises(PlacementError) as raised:
        FrozenPlacementSource(fixture).compile(members)

    assert raised.value.code == expected_code


def test_seed_uses_constructor_injected_source_without_callsite_changes(
    tmp_path: Path,
) -> None:
    class StubPlannerPlacementSource:
        def __init__(self, end_layer: int) -> None:
            self.end_layer = end_layer
            self.seen: tuple[MemberRecord, ...] | None = None

        def compile(self, members):
            self.seen = tuple(members)
            return PlacementDecision(
                placement_provenance="planner_v2",
                placement_id=f"planner-split-{self.end_layer}",
                assignments=(
                    {
                        "node_id": "node-a",
                        "assignment_id": "assignment-node-a",
                        "start_layer": 0,
                        "end_layer_exclusive": self.end_layer,
                    },
                ),
                source_digest="sha256:" + "a" * 64,
            )

    first_source = StubPlannerPlacementSource(3)
    first = _coordinator(tmp_path / "first", first_source)
    _join(first, tmp_path / "first", node_id="node-a")

    second_source = StubPlannerPlacementSource(6)
    second = _coordinator(tmp_path / "second", second_source)
    _join(second, tmp_path / "second", node_id="node-a")

    assert first.compile_placement().placement_id == "planner-split-3"
    assert second.compile_placement().placement_id == "planner-split-6"
    assert first_source.seen is not None
    assert [member.node_id for member in first_source.seen] == ["node-a"]
    assert second_source.seen is not None
    assert [member.node_id for member in second_source.seen] == ["node-a"]


def test_seed_compiles_concrete_planner_source_without_callsite_changes(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(
        tmp_path / "planner",
        PlannerPlacementSource(_planner_snapshot()),
    )
    _join(coordinator, tmp_path / "planner", node_id="node-a")

    decision = coordinator.compile_placement()

    assert decision.placement_provenance == "planner_v2"
    assert tuple(item["node_id"] for item in decision.assignments) == ("node-a",)
    assert decision.assignments[0]["start_layer"] == 0
    assert decision.assignments[0]["end_layer_exclusive"] == 4
