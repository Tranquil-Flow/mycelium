from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest

from mycelium_live import route as route_module
from mycelium_node import load_or_create_node_signer
from mycelium_seed import SqliteSeedState
from mycelium_seed.operator import begin_seed_key_rotation, complete_seed_key_rotation


SWARM_ID = "mycelium-m12-test-swarm"
SEED_NODE_ID = "seed-node"


def _write_minimal_plan(path: Path, *, seed_key_digest: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "controller": {
                    "membership_snapshot": {
                        "seed_key_digest": seed_key_digest,
                        "swarm_id": SWARM_ID,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _initialize_state(root: Path) -> tuple[Path, str]:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    signer = load_or_create_node_signer(root / "identity" / "seed.key")
    state = SqliteSeedState(root / "state.sqlite3")
    state.bind_identity(
        swarm_id=SWARM_ID,
        seed_node_id=SEED_NODE_ID,
        seed_key_digest=signer.verification_key_digest,
    )
    return root, signer.verification_key_digest


def _load(plan: Path, root: Path):
    return route_module._load_live_seed_authority(
        seed_state_root=root,
        plan_membership_snapshot=json.loads(plan.read_text(encoding="utf-8"))[
            "controller"
        ]["membership_snapshot"],
    )


def test_live_route_rejects_absent_seed_state_root_before_plan_construction(
    tmp_path: Path,
) -> None:
    plan = _write_minimal_plan(
        tmp_path / "operator-plan.json",
        seed_key_digest="sha256:" + "a" * 64,
    )

    with pytest.raises(route_module.LiveSeedStateError) as failed:
        route_module.PhysicalLiveRoute.from_operator_plan(
            plan,
            seed_state_root=tmp_path / "absent-seed",
        )

    assert failed.value.code == "live_seed_state_missing"
    assert not (tmp_path / "absent-seed").exists()


def test_live_seed_state_rejects_key_only_and_database_only_restore(
    tmp_path: Path,
) -> None:
    key_only = tmp_path / "key-only"
    key_only.mkdir(mode=0o700)
    key_only.chmod(0o700)
    key_signer = load_or_create_node_signer(key_only / "identity" / "seed.key")
    key_plan = _write_minimal_plan(
        tmp_path / "key-plan.json",
        seed_key_digest=key_signer.verification_key_digest,
    )

    with pytest.raises(route_module.LiveSeedStateError) as key_failed:
        _load(key_plan, key_only)
    assert key_failed.value.code == "live_seed_database_missing"

    database_only = tmp_path / "database-only"
    database_only.mkdir(mode=0o700)
    database_only.chmod(0o700)
    detached_signer = load_or_create_node_signer(
        tmp_path / "detached-identity" / "seed.key"
    )
    state = SqliteSeedState(database_only / "state.sqlite3")
    state.bind_identity(
        swarm_id=SWARM_ID,
        seed_node_id=SEED_NODE_ID,
        seed_key_digest=detached_signer.verification_key_digest,
    )
    database_plan = _write_minimal_plan(
        tmp_path / "database-plan.json",
        seed_key_digest=detached_signer.verification_key_digest,
    )

    with pytest.raises(route_module.LiveSeedStateError) as database_failed:
        _load(database_plan, database_only)
    assert database_failed.value.code == "live_seed_identity_missing"


def test_live_seed_state_rejects_digest_mismatch_and_unsafe_modes(
    tmp_path: Path,
) -> None:
    root, _digest = _initialize_state(tmp_path / "seed")
    mismatch_plan = _write_minimal_plan(
        tmp_path / "mismatch-plan.json",
        seed_key_digest="sha256:" + "f" * 64,
    )

    with pytest.raises(route_module.LiveSeedStateError) as mismatch:
        _load(mismatch_plan, root)
    assert mismatch.value.code == "live_seed_plan_identity_mismatch"

    key = root / "identity" / "seed.key"
    key.chmod(0o644)
    with pytest.raises(route_module.LiveSeedStateError) as unsafe:
        _load(mismatch_plan, root)
    assert unsafe.value.code == "live_seed_identity_permissions_invalid"


def test_live_seed_state_load_is_stable_and_does_not_mutate_identity(
    tmp_path: Path,
) -> None:
    root, digest = _initialize_state(tmp_path / "seed")
    plan = _write_minimal_plan(
        tmp_path / "operator-plan.json",
        seed_key_digest=digest,
    )
    key = root / "identity" / "seed.key"
    database = root / "state.sqlite3"
    before = (key.stat().st_mtime_ns, database.stat().st_mtime_ns)

    first = _load(plan, root)
    second = _load(plan, root)

    assert first.signer.verification_key_digest == digest
    assert second.signer.verification_key_digest == digest
    assert second.signer.endpoint_id == first.signer.endpoint_id
    assert first.swarm_id == second.swarm_id == SWARM_ID
    assert (key.stat().st_mtime_ns, database.stat().st_mtime_ns) == before


def test_live_seed_members_derive_activation_eligibility_from_runtime_policy(
    tmp_path: Path,
) -> None:
    root, digest = _initialize_state(tmp_path / "seed")
    plan = _write_minimal_plan(
        tmp_path / "operator-plan.json",
        seed_key_digest=digest,
    )
    now = time.time()
    with sqlite3.connect(root / "state.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO seed_members (
                node_id, endpoint_id, endpoint_addrs_json,
                peer_class, runtime_capability_json,
                verification_key_digest, incarnation, generation,
                lease_expires_at, last_heartbeat_sequence,
                last_liveness_at, next_heartbeat_due_at,
                last_activity_receipt_at, active_requests, lifecycle_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "node-0",
                "endpoint-0",
                '["https://127.0.0.1"]',
                "mac_mlx_iroh",
                json.dumps(
                    {
                        "activation_protocol": "mycelium.router_wire.v1",
                        "runtime_backend": "mlx",
                        "transport": "iroh",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "sha256:" + "1" * 64,
                "incarnation-0",
                1,
                now + 300,
                0,
                now,
                now + 30,
                None,
                0,
                "NEW",
            ),
        )

    authority = _load(plan, root)
    try:
        assert authority.current_members()[0]["activation_eligible"] is True
    finally:
        authority.close()


def test_product_pseudonym_salt_survives_seed_key_rotation(tmp_path: Path) -> None:
    root, old_digest = _initialize_state(tmp_path / "seed")
    old_plan = _write_minimal_plan(
        tmp_path / "old-plan.json",
        seed_key_digest=old_digest,
    )
    before = _load(old_plan, root)
    try:
        salt_before = before.product_pseudonym_salt()
    finally:
        before.close()

    pending = begin_seed_key_rotation(
        root,
        reason="scheduled_rotation",
        overlap_seconds=600,
        now=lambda: 2_000.0,
    )
    complete_seed_key_rotation(
        root,
        authority_generation=2,
        now=lambda: 2_001.0,
    )
    new_plan = _write_minimal_plan(
        tmp_path / "new-plan.json",
        seed_key_digest=pending["new_seed_key_digest"],
    )
    after = _load(new_plan, root)
    try:
        assert after.authority_generation == 2
        assert after.product_pseudonym_salt() == salt_before
    finally:
        after.close()
