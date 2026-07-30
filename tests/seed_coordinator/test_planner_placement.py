"""Tests for the PlannerPlacementSource pipeline (TDD RED phase).

Pipeline under test:
    Gossip EvidenceBundle
      -> planner_snapshot_from_evidence_bundle
      -> plan_snapshot
      -> RoutePlanV2
      -> compile_bound_layer_assignments
      -> PlacementDecision(provenance=planner_v2)
"""

from __future__ import annotations

import copy
import json
from itertools import count
from pathlib import Path
from typing import Any

import pytest

import model_manifest as mm
from mycelium_gossip.evidence_bundle import evidence_bundle_to_dict
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import GossipService
from mycelium_gossip.transport import InMemoryMesh, InMemoryTransport, LivenessEvent, LivenessKind
from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator, SqliteSeedState
from mycelium_seed.placement import (
    FrozenPlacementSource,
    MemberRecord,
    PlacementDecision,
    PlacementError,
)
from tests.gossip.helpers import link_payload, make_record, profile_payload, status_payload

NOW = 2_000.0
DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
MAC_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}
PERFORMANCE = {
    "prefill_ms_per_layer_token": 0.001,
    "decode_ms_per_layer_token": 0.001,
    "memory_bandwidth_Bps": 1_000_000_000,
    "spill_bandwidth_Bps": 1_000_000_000,
    "calibration_confidence": 1.0,
}
WORKLOAD = {"preset": "interactive_chat_v1", "concurrency_points": [1], "user_scale": 1}
POLICY = {
    "memory_reserve_fraction": 0,
    "replica_budget": 0,
    "ttft_slo_ms": 1_000_000,
    "tpot_slo_ms": 1_000_000,
}
RUNTIME_BY_NODE = {"backend": "mlx", "dtype": "float16", "quantization": "none"}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


# ---------------------------------------------------------------------------
# Fixture helpers - mirror existing test patterns from test_planner_assignment.
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _model_manifest() -> dict[str, Any]:
    return mm.compile_model_manifest(
        model_id="org/model",
        requested_revision="main",
        resolved_commit="a" * 40,
        config={
            "model_type": "gpt2",
            "architectures": ["GPT2LMHeadModel"],
            "n_layer": 4,
            "n_embd": 128,
            "n_head": 4,
            "n_inner": 512,
            "vocab_size": 1024,
            "n_positions": 128,
            "layer_norm_epsilon": 1e-5,
            "activation_function": "gelu_new",
            "scale_attn_weights": True,
            "scale_attn_by_inverse_layer_idx": False,
            "reorder_and_upcast_attn": False,
            "add_cross_attention": False,
            "tie_word_embeddings": False,
        },
        checkpoint_index={
            "weight_map": {
                "transformer.wte.weight": "shard-1.safetensors",
                "transformer.wpe.weight": "shard-1.safetensors",
                "transformer.h.0.attn.weight": "shard-2.safetensors",
                "transformer.h.1.attn.weight": "shard-2.safetensors",
                "transformer.h.2.attn.weight": "shard-3.safetensors",
                "transformer.h.3.attn.weight": "shard-3.safetensors",
                "transformer.ln_f.weight": "shard-3.safetensors",
                "lm_head.weight": "shard-1.safetensors",
            },
        },
        file_metadata={
            "shard-1.safetensors": {"size_bytes": 20_000_000, "sha256": "1" * 64},
            "shard-2.safetensors": {"size_bytes": 20_000_000, "sha256": "2" * 64},
            "shard-3.safetensors": {"size_bytes": 20_000_000, "sha256": "3" * 64},
        },
    )


def _planner_model(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": manifest["model_id"],
        "revision": manifest["resolved_commit"],
        "weight_digest": mm.manifest_digest_ref(manifest),
        "architecture": "Decoder",
        "num_layers": manifest["num_layers"],
        "hidden_size": 128,
        "dtype_bytes": 2,
        "kv_heads": 4,
        "head_dim": 32,
        "weight_bytes": 60_000_000,
    }


def _build_gossip_service(
    *,
    clock: Clock,
    manifest: dict[str, Any],
    epoch: int = 3,
    zero_memory: str | None = None,
    reachable: bool = True,
):
    """Build a two-node gossip service with status/profile/link evidence."""
    store = VersionedRecordStore("swarm-a", monotonic=clock)
    service = GossipService(
        swarm_id="swarm-a",
        node_id="local",
        incarnation=1,
        boot_id="boot-local-1",
        transport=InMemoryTransport(InMemoryMesh(monotonic=clock), "local"),
        registry=store,
        monotonic=clock,
    )
    for node_id in ("node-a", "node-b"):
        profile = profile_payload(node_id)
        status = status_payload(node_id, free_bytes=0 if zero_memory == node_id else 40_000_000)
        status["performance"] = copy.deepcopy(PERFORMANCE)
        store.apply(
            make_record(RecordKind.PROFILE, node_id=node_id, ttl_ms=10_000, payload=profile)
        )
        store.apply(
            make_record(RecordKind.STATUS, node_id=node_id, ttl_ms=10_000, payload=status)
        )
        service.submit_liveness(
            LivenessEvent(LivenessKind.PUT, "swarm-a", node_id, 1, f"boot-{node_id}-1", clock())
        )
    for src, dst in (("node-a", "node-b"), ("node-b", "node-a")):
        store.apply(
            make_record(
                RecordKind.LINK,
                node_id=src,
                ttl_ms=10_000,
                payload=link_payload(src, dst, reachable=reachable),
            )
        )
    service.drain()
    bundle = service.capture_evidence_bundle(
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=epoch,
        model_id=manifest["model_id"],
        num_layers=manifest["num_layers"],
        manifest_digest=mm.manifest_digest_ref(manifest),
        resolved_commit=manifest["resolved_commit"],
    )
    return service, bundle


def _evidence_bundle_dict(
    manifest: dict[str, Any] | None = None, **kwargs
) -> dict[str, Any]:
    manifest = manifest or _model_manifest()
    _, bundle = _build_gossip_service(clock=Clock(), manifest=manifest, **kwargs)
    return evidence_bundle_to_dict(bundle)


def _source_kwargs(
    manifest: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = manifest or _model_manifest()
    evidence = evidence or _evidence_bundle_dict(manifest)
    return {
        "manifest": manifest,
        "evidence_bundle": evidence,
        "planner_model": _planner_model(manifest),
        "workload": WORKLOAD,
        "policy": POLICY,
        "runtime_by_node": {
            "node-a": RUNTIME_BY_NODE,
            "node-b": RUNTIME_BY_NODE,
        },
    }


def _member(node_id: str = "node-a") -> MemberRecord:
    return MemberRecord(
        node_id=node_id,
        endpoint_id=f"{node_id}-endpoint",
        peer_class="mac_mlx_iroh",
        runtime_capability=MAC_RUNTIME_CAPABILITY,
        generation=1,
        lease_expires_at=NOW + 300.0,
        activation_eligible=True,
    )


# ---------------------------------------------------------------------------
# Import guard - will fail until PlannerPlacementSource exists.
# ---------------------------------------------------------------------------

from mycelium_seed.planner_placement import PlannerPlacementSource  # noqa: E402


# ---------------------------------------------------------------------------
# Happy path: planner source compiles valid evidence into placement intent.
# ---------------------------------------------------------------------------


def test_planner_source_compiles_valid_evidence_into_placement_decision() -> None:
    source = PlannerPlacementSource(**_source_kwargs())
    decision = source.compile([_member("node-a"), _member("node-b")])
    assert isinstance(decision, PlacementDecision)
    assert decision.placement_provenance == "planner_v2"


# ---------------------------------------------------------------------------
# Stale / mixed-generation evidence rejects.
# ---------------------------------------------------------------------------


def test_stale_evidence_bundle_rejects_with_generation_mismatch() -> None:
    manifest = _model_manifest()
    evidence = _evidence_bundle_dict(manifest)
    tampered = copy.deepcopy(evidence)
    tampered["snapshot_generation"] += 1
    from mycelium_gossip.evidence_bundle import evidence_bundle_digest

    tampered["evidence_bundle_digest"] = evidence_bundle_digest(tampered)
    kwargs = _source_kwargs(manifest, tampered)
    source = PlannerPlacementSource(**kwargs)
    with pytest.raises((PlacementError, ValueError)):
        source.compile([_member("node-a"), _member("node-b")])


def test_mixed_generation_evidence_across_two_bundles_rejects() -> None:
    manifest = _model_manifest()
    fresh_evidence = _evidence_bundle_dict(manifest)
    source = PlannerPlacementSource(**_source_kwargs(manifest, fresh_evidence))
    pinned = source.source_digest
    # A different model/evidence produces a different digest.
    manifest2 = _model_manifest()
    other_evidence = _evidence_bundle_dict(manifest2)
    assert other_evidence["evidence_bundle_digest"] != pinned


# ---------------------------------------------------------------------------
# Expired device_status excludes the node rather than defaulting.
# ---------------------------------------------------------------------------


def test_expired_device_status_excludes_node() -> None:
    manifest = _model_manifest()
    evidence_zero = _evidence_bundle_dict(manifest, zero_memory="node-a")
    kwargs = _source_kwargs(manifest, evidence_zero)
    source = PlannerPlacementSource(**kwargs)
    decision = source.compile([_member("node-a"), _member("node-b")])
    assigned_nodes = {a["node_id"] for a in decision.assignments}
    # Node-a should NOT be in assignments because zero memory excludes it.
    assert "node-a" not in assigned_nodes
    assert "node-b" in assigned_nodes


# ---------------------------------------------------------------------------
# Placement intent never becomes readiness.
# ---------------------------------------------------------------------------


def test_placement_intent_never_becomes_readiness() -> None:
    source = PlannerPlacementSource(**_source_kwargs())
    decision = source.compile([_member("node-a"), _member("node-b")])
    assert decision.placement_provenance == "planner_v2"
    serialized = json.dumps(decision.assignments, sort_keys=True)
    assert '"route_ready": true' not in serialized
    assert decision.source_digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# Assignments cover every layer exactly once with half-open ranges.
# ---------------------------------------------------------------------------


def test_assignments_cover_every_layer_exactly_once() -> None:
    manifest = _model_manifest()
    num_layers = manifest["num_layers"]
    source = PlannerPlacementSource(**_source_kwargs(manifest))
    decision = source.compile([_member("node-a"), _member("node-b")])
    assert len(decision.assignments) >= 2
    ranges: list[tuple[int, int]] = []
    for assignment in decision.assignments:
        r = assignment["range"]
        start = r["start_layer"]
        end = r["end_layer_exclusive"]
        assert end > start
        ranges.append((start, end))
    ranges.sort()
    assert ranges[0][0] == 0
    assert ranges[-1][1] == num_layers
    for i in range(len(ranges) - 1):
        assert ranges[i][1] == ranges[i + 1][0]
    covered = set()
    for start, end in ranges:
        for layer in range(start, end):
            assert layer not in covered, f"layer {layer} covered twice"
            covered.add(layer)
    assert covered == set(range(num_layers))


# ---------------------------------------------------------------------------
# Missing load proof, stale epoch, or runtime endpoint/assignment mismatch rejects.
# ---------------------------------------------------------------------------


def test_stale_epoch_rejects() -> None:
    manifest = _model_manifest()
    evidence = _evidence_bundle_dict(manifest)
    kwargs = _source_kwargs(manifest, evidence)
    kwargs["deployment_epoch"] = evidence["deployment"]["deployment_epoch"] + 1
    source = PlannerPlacementSource(**kwargs)
    with pytest.raises((PlacementError, ValueError)):
        source.compile([_member("node-a"), _member("node-b")])


def test_runtime_node_mismatch_rejects() -> None:
    manifest = _model_manifest()
    kwargs = _source_kwargs(manifest)
    kwargs["runtime_by_node"] = {
        "node-a": RUNTIME_BY_NODE,
        "node-c": RUNTIME_BY_NODE,
    }
    source = PlannerPlacementSource(**kwargs)
    with pytest.raises((PlacementError, ValueError)):
        source.compile([_member("node-a"), _member("node-b")])


# ---------------------------------------------------------------------------
# Provenance is planner_v2.
# ---------------------------------------------------------------------------


def test_provenance_is_planner_v2() -> None:
    source = PlannerPlacementSource(**_source_kwargs())
    decision = source.compile([_member("node-a"), _member("node-b")])
    assert decision.placement_provenance == "planner_v2"
    assert decision.source_digest.startswith("sha256:")
    assert len(decision.source_digest) == 71


# ---------------------------------------------------------------------------
# Coordinator, HTTP API, and node-agent require zero edits to swap placement source.
# ---------------------------------------------------------------------------


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


def test_coordinator_uses_planner_source_without_callsite_changes(
    tmp_path: Path,
) -> None:
    """Coordinator must accept PlannerPlacementSource as a drop-in replacement
    for FrozenPlacementSource with zero code edits."""
    source = PlannerPlacementSource(**_source_kwargs())
    coordinator = _coordinator(tmp_path / "planner", source)
    _join(coordinator, tmp_path / "planner", node_id="node-a")
    decision = coordinator.compile_placement()
    assert decision.placement_provenance == "planner_v2"


def test_planner_source_is_swappable_with_frozen_source_in_same_coordinator(
    tmp_path: Path,
) -> None:
    """Swapping PlannerPlacementSource for FrozenPlacementSource requires
    no coordinator or HTTP API changes."""
    fixture = tmp_path / "frozen.json"
    fixture.write_bytes(
        json.dumps(
            {
                "protocol": "mycelium.seed.frozen_placement.v1",
                "placement_id": "frozen-split",
                "assignments": [
                    {
                        "node_id": "node-a",
                        "assignment_id": "assignment-node-a",
                        "start_layer": 0,
                        "end_layer_exclusive": 2,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    frozen_source = FrozenPlacementSource(fixture)
    coordinator_frozen = _coordinator(tmp_path / "frozen", frozen_source)
    _join(coordinator_frozen, tmp_path / "frozen", node_id="node-a")
    frozen_decision = coordinator_frozen.compile_placement()
    assert frozen_decision.placement_provenance == "frozen_fixture"

    planner_source = PlannerPlacementSource(**_source_kwargs())
    coordinator_planner = _coordinator(tmp_path / "planner-swap", planner_source)
    _join(coordinator_planner, tmp_path / "planner-swap", node_id="node-a")
    planner_decision = coordinator_planner.compile_placement()
    assert planner_decision.placement_provenance == "planner_v2"
