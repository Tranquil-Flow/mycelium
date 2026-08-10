from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from mycelium_node import load_or_create_node_signer
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_seed import SqliteSeedState
from mycelium_seed.operator import (
    SeedOperatorError,
    backup_seed_state,
    begin_seed_key_rotation,
    complete_seed_key_rotation,
    revoke_seed_member,
    restore_seed_state,
    seed_key_rotation_status,
    seed_inventory,
    verify_seed_key_transition,
)


NOW = 2_000.0


def _seed_root(tmp_path: Path) -> Path:
    root = tmp_path / "seed"
    root.mkdir(mode=0o700)
    signer = load_or_create_node_signer(root / "identity" / "seed.key")
    state = SqliteSeedState(root / "state.sqlite3")
    state.bind_identity(
        swarm_id="swarm-a",
        seed_node_id="seed-node",
        seed_key_digest=signer.verification_key_digest,
    )
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
                "node-a",
                "endpoint-a",
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
                "incarnation-a",
                2,
                NOW + 300,
                4,
                NOW,
                NOW + 30,
                None,
                0,
                "RUNNING",
            ),
        )
    return root


def test_inventory_is_privacy_reduced_and_revoke_fences_generation(
    tmp_path: Path,
) -> None:
    root = _seed_root(tmp_path)

    before = seed_inventory(root, now=lambda: NOW)
    assert before["members"] == [
        {
            "node_id": "node-a",
            "peer_class": "mac_mlx_iroh",
            "generation": 2,
            "incarnation": "incarnation-a",
            "lifecycle_state": "RUNNING",
            "lease_freshness": "fresh",
            "activation_eligible": True,
            "revocation_state": "active",
        }
    ]
    assert "endpoint" not in repr(before).lower()

    revoked = revoke_seed_member(
        root,
        node_id="node-a",
        expected_generation=2,
        reason="operator_revoked",
        now=lambda: NOW + 1,
    )
    assert revoked["generation"] == 3
    assert revoked["lifecycle_state"] == "STOPPED"
    after = seed_inventory(root, now=lambda: NOW + 1)
    assert after["members"][0]["activation_eligible"] is False
    assert after["members"][0]["revocation_state"] == "revoked"

    with pytest.raises(SeedOperatorError, match="seed_member_generation_stale"):
        revoke_seed_member(
            root,
            node_id="node-a",
            expected_generation=2,
            reason="operator_revoked",
        )


def test_operator_rejects_unsafe_or_mismatched_seed_state(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    key = root / "identity" / "seed.key"
    key.chmod(0o644)
    with pytest.raises(SeedOperatorError, match="seed_operator_identity_invalid"):
        seed_inventory(root, now=lambda: NOW)

    key.chmod(0o600)
    with sqlite3.connect(root / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE seed_metadata SET value = ? WHERE key = 'seed_key_digest'",
            ("sha256:" + "f" * 64,),
        )
    with pytest.raises(SeedOperatorError, match="seed_operator_identity_mismatch"):
        seed_inventory(root, now=lambda: NOW)


def test_backup_restore_preserves_complete_identity_and_membership(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    backup_output = tmp_path / "backups"
    status = backup_seed_state(
        root,
        output_root=backup_output,
        now=lambda: NOW,
        backup_id_source=lambda: "backup-test-generation",
    )
    backup_root = Path(status["backup_directory"])
    assert backup_root.stat().st_mode & 0o777 == 0o700
    assert (backup_root / "identity" / "seed.key").stat().st_mode & 0o777 == 0o600
    assert (backup_root / "state.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (backup_root / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert (
        backup_root / "identity" / "product.pseudonym.key"
    ).stat().st_mode & 0o777 == 0o600

    restored_root = tmp_path / "restored-seed"
    restored = restore_seed_state(backup_root, target_root=restored_root)
    assert restored["seed_key_digest"] == status["seed_key_digest"]
    assert restored["authority_generation"] == status["authority_generation"] == 1
    assert restored["member_count"] == 1
    inventory = seed_inventory(restored_root, now=lambda: NOW)
    assert inventory["seed_key_digest"] == status["seed_key_digest"]
    assert inventory["members"][0]["node_id"] == "node-a"
    assert (
        restored_root / "identity" / "product.pseudonym.key"
    ).read_bytes() == (
        backup_root / "identity" / "product.pseudonym.key"
    ).read_bytes()


def test_restore_rejects_tamper_partial_or_existing_target(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    status = backup_seed_state(
        root,
        output_root=tmp_path / "backups",
        backup_id_source=lambda: "backup-tamper-test",
    )
    backup_root = Path(status["backup_directory"])
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    with pytest.raises(SeedOperatorError, match="seed_operator_restore_target_exists"):
        restore_seed_state(backup_root, target_root=target)

    target.rmdir()
    database = backup_root / "state.sqlite3"
    database.chmod(0o644)
    with pytest.raises(SeedOperatorError, match="seed_operator_backup_invalid"):
        restore_seed_state(backup_root, target_root=target)
    assert not target.exists()


def test_restore_rejects_corrupt_database_even_when_manifest_digest_matches(
    tmp_path: Path,
) -> None:
    root = _seed_root(tmp_path)
    status = backup_seed_state(
        root,
        output_root=tmp_path / "backups",
        backup_id_source=lambda: "backup-corrupt-database",
    )
    backup_root = Path(status["backup_directory"])
    database = backup_root / "state.sqlite3"
    payload = bytearray(database.read_bytes())
    payload[:16] = b"not-a-sqlite-db!"
    database.write_bytes(payload)
    database.chmod(0o600)

    manifest_path = backup_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["database_digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    with pytest.raises(
        SeedOperatorError,
        match="seed_operator_backup_database_corrupt",
    ):
        restore_seed_state(backup_root, target_root=tmp_path / "restored-corrupt")


def test_rotation_begin_persists_dual_signed_monotonic_transition(
    tmp_path: Path,
) -> None:
    root = _seed_root(tmp_path)
    before = seed_key_rotation_status(root)
    assert before["event"] == "rotation_absent"
    assert before["authority_generation"] == 1

    pending = begin_seed_key_rotation(
        root,
        reason="scheduled_rotation",
        overlap_seconds=600,
        now=lambda: NOW,
    )
    assert pending["event"] == "rotation_pending"
    assert pending["previous_generation"] == 1
    assert pending["authority_generation"] == 2
    assert pending["old_seed_key_digest"] != pending["new_seed_key_digest"]
    assert (root / "identity" / "seed.next.key").stat().st_mode & 0o777 == 0o600

    envelope = json.loads((root / "identity" / "seed.rotation.json").read_text())
    transition = verify_seed_key_transition(
        envelope,
        now=NOW,
        expected_old_digest=pending["old_seed_key_digest"],
    )
    assert transition["authority_generation"] == 2
    after = seed_key_rotation_status(root)
    assert after["event"] == "rotation_pending"
    assert after["new_seed_key_digest"] == pending["new_seed_key_digest"]

    with pytest.raises(SeedOperatorError, match="seed_operator_rotation_pending"):
        begin_seed_key_rotation(
            root,
            reason="duplicate_rotation",
            overlap_seconds=600,
            now=lambda: NOW,
        )


def test_rotation_transition_rejects_tampered_new_key_proof(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    begin_seed_key_rotation(
        root,
        reason="scheduled_rotation",
        overlap_seconds=600,
        now=lambda: NOW,
    )
    envelope = json.loads((root / "identity" / "seed.rotation.json").read_text())
    envelope["transition"]["reason"] = "tampered_rotation"

    with pytest.raises(
        SeedOperatorError,
        match="seed_operator_rotation_old_signature_invalid",
    ):
        verify_seed_key_transition(envelope, now=NOW)


def test_rotation_completion_promotes_key_and_survives_reload(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    pending = begin_seed_key_rotation(
        root,
        reason="scheduled_rotation",
        overlap_seconds=600,
        now=lambda: NOW,
    )

    with pytest.raises(
        SeedOperatorError,
        match="seed_operator_rotation_acknowledgements_incomplete",
    ):
        complete_seed_key_rotation(
            root,
            authority_generation=2,
            now=lambda: NOW + 1,
        )
    state = SqliteSeedState(root / "state.sqlite3")
    transition = state.authority_state()["rotation"]["transition"]
    state.save_seed_rotation_acknowledgement(
        {
            "sender_node_id": "node-a",
            "generation": 2,
            "incarnation": "incarnation-a",
            "authority_generation": 2,
            "transition_digest": "sha256:"
            + hashlib.sha256(canonical_json_bytes(transition)).hexdigest(),
            "message_id": "rotation-ack-a",
            "issued_at": NOW + 0.5,
        }
    )

    completed = complete_seed_key_rotation(
        root,
        authority_generation=2,
        now=lambda: NOW + 1,
    )
    assert completed["event"] == "rotation_completed"
    assert completed["new_seed_key_digest"] == pending["new_seed_key_digest"]
    assert not (root / "identity" / "seed.next.key").exists()
    assert seed_key_rotation_status(root)["event"] == "rotation_completed"
    assert seed_inventory(root, now=lambda: NOW + 1)["seed_key_digest"] == pending[
        "new_seed_key_digest"
    ]

    repeated = complete_seed_key_rotation(
        root,
        authority_generation=2,
        now=lambda: NOW + 700,
    )
    assert repeated["event"] == "rotation_completed"


def test_rotation_completion_rejects_expired_pending_overlap(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    begin_seed_key_rotation(
        root,
        reason="scheduled_rotation",
        overlap_seconds=10,
        now=lambda: NOW,
    )

    with pytest.raises(
        SeedOperatorError,
        match="seed_operator_rotation_overlap_expired",
    ):
        complete_seed_key_rotation(
            root,
            authority_generation=2,
            now=lambda: NOW + 11,
        )
    assert seed_key_rotation_status(root)["event"] == "rotation_pending"


def test_rotation_loader_recovers_database_commit_before_key_rename(
    tmp_path: Path,
) -> None:
    root = _seed_root(tmp_path)
    pending = begin_seed_key_rotation(
        root,
        reason="scheduled_rotation",
        overlap_seconds=600,
        now=lambda: NOW,
    )
    SqliteSeedState(root / "state.sqlite3").complete_authority_rotation(
        authority_generation=2,
        new_seed_key_digest=pending["new_seed_key_digest"],
    )

    assert (root / "identity" / "seed.next.key").exists()
    recovered = seed_inventory(root, now=lambda: NOW + 1)
    assert recovered["seed_key_digest"] == pending["new_seed_key_digest"]
    completed = complete_seed_key_rotation(
        root,
        authority_generation=2,
        now=lambda: NOW + 1,
    )
    assert completed["event"] == "rotation_completed"
    assert not (root / "identity" / "seed.next.key").exists()


@pytest.mark.parametrize("overlap", [0, -1, float("inf"), 86_401])
def test_rotation_rejects_invalid_overlap(tmp_path: Path, overlap: float) -> None:
    with pytest.raises(
        SeedOperatorError,
        match="seed_operator_rotation_overlap_invalid",
    ):
        begin_seed_key_rotation(
            _seed_root(tmp_path),
            reason="scheduled_rotation",
            overlap_seconds=overlap,
            now=lambda: NOW,
        )
