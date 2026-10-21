# SPDX-License-Identifier: AGPL-3.0-only
"""Typed migration lifecycle: restart, rollback, concurrent resume, cutover, reader removal.

Every call goes through the private filesystem adapter with signed v2 profiles;
raw ``declared_members`` / ``record`` keyword calls are refused as before.
"""

from __future__ import annotations

import json

import pytest

from mycelium_platform_neutral import migration
from mycelium_platform_neutral.migration_executor import (
    FileMembershipState,
    MigrationError,
    MigrationExecutor,
    MigrationRequest,
)
from mycelium_platform_neutral.peer_profile import PeerProfile, sign_peer_profile
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 1_000_000.0
BUILD = "sha256:" + "a" * 64
CONTRACT = "sha256:" + "c" * 64
INVENTORY = "sha256:" + "d" * 64
QUALIFICATION = "sha256:" + "e" * 64
STAGES = ("profiles_signed", "profiles_verified", "commit_pending", "committed")


def _members(tmp_path, generations=(("node-a", 5), ("node-b", 2))):
    store = FileMembershipState(tmp_path / "private")
    signers = {}
    baseline = {}
    for node, generation in generations:
        signer = generate_ed25519_signer(endpoint_id=f"endpoint-{node}")
        signers[node] = signer
        baseline[node] = {
            "generation": generation,
            "verification_key_digest": signer.verification_key_digest,
            "incarnation": "inc-1",
            "lease_expires_at": NOW + 600,
            "revoked": False,
            "software_build_digest": BUILD,
        }
    store.initialize(baseline)
    envelopes = []
    for node, generation in generations:
        profile = PeerProfile.from_mapping(
            {
                "protocol": "mycelium.membership.peer_profile.v2",
                "profile_id": f"profile-{node}",
                "swarm_id": "swarm-a",
                "node_id": node,
                "incarnation": "inc-2",
                "membership_generation": generation + 1,
                "identity_kind": "native_peer",
                "platform_family": "macos" if node == "node-a" else "linux",
                "platform_variant": None,
                "architecture": "arm64" if node == "node-a" else "x86_64",
                "software_build": {"version": "1.0", "digest": BUILD},
                "transports": [],
                "runtimes": [],
                "lease_expires_at": NOW + 600,
                "issued_at": NOW - 1,
                "expires_at": NOW + 600,
            }
        )
        envelopes.append(sign_peer_profile(signer=signers[node], profile=profile))
    return store, tuple(envelopes)


def _request(envelopes, migration_id="migration-001"):
    return MigrationRequest(migration_id=migration_id, profiles=tuple(envelopes))


def _cutover(store, now=NOW + 1):
    return migration.record_cutover(
        storage=store,
        contract_digests=(CONTRACT,),
        inventory_digest=INVENTORY,
        qualification_digests=(QUALIFICATION,),
        now=now,
    )


def test_raw_untyped_calls_stay_refused_without_success_documents():
    for name in (
        "execute_identity_preserving_migration",
        "restart_from_checkpoint",
        "rollback_before_commit",
        "resume_concurrent_migration",
        "record_cutover",
        "remove_migration_only_reader",
    ):
        function = getattr(migration, name)
        with pytest.raises(migration.MigrationUnavailable) as error:
            function(declared_members={"node-a": 5}, now=NOW)
        assert error.value.code == "verified_migration_request_required"


def test_executor_commits_generation_n_plus_one_for_every_member(tmp_path):
    store, envelopes = _members(tmp_path)
    assert (
        migration.execute_identity_preserving_migration(
            storage=store, request=_request(envelopes), now=NOW
        )
        == "committed"
    )
    members = store.read()
    assert {node: value["generation"] for node, value in members.items()} == {
        "node-a": 6,
        "node-b": 3,
    }
    assert {value["incarnation"] for value in members.values()} == {"inc-2"}


@pytest.mark.parametrize("stage", STAGES)
def test_restart_from_each_recorded_checkpoint_terminalizes(tmp_path, stage):
    store, envelopes = _members(tmp_path)
    executor = MigrationExecutor(store)
    with pytest.raises(migration.MigrationUnavailable):
        executor.execute(_request(envelopes), now=NOW, stop_after=stage)
    restarted = migration.restart_from_checkpoint(
        storage=FileMembershipState(store.root), request=_request(envelopes), now=NOW
    )
    assert restarted == {
        "migration_id": "migration-001",
        "checkpoint_before": stage,
        "checkpoint": "committed",
        "resumed": True,
    }
    assert store.read()["node-a"]["generation"] == 6


def test_restart_without_a_journal_is_not_a_resume(tmp_path):
    store, envelopes = _members(tmp_path)
    with pytest.raises(migration.MigrationUnavailable) as error:
        migration.restart_from_checkpoint(storage=store, request=_request(envelopes), now=NOW)
    assert error.value.code == "migration_checkpoint_absent"
    assert not (store.root / "journal.json").exists()


def test_exact_retry_is_idempotent_and_changed_retry_is_rejected(tmp_path):
    store, envelopes = _members(tmp_path)
    request = _request(envelopes)
    assert migration.execute_identity_preserving_migration(storage=store, request=request, now=NOW) == "committed"
    published = (store.root / "members.json").read_bytes()
    assert migration.execute_identity_preserving_migration(storage=store, request=request, now=NOW) == "committed"
    assert (store.root / "members.json").read_bytes() == published
    with pytest.raises(MigrationError, match="changed_retry_rejected"):
        migration.execute_identity_preserving_migration(
            storage=store, request=_request(envelopes, "migration-002"), now=NOW
        )
    assert (store.root / "members.json").read_bytes() == published


def test_duplicate_identity_is_rejected_before_any_journal(tmp_path):
    store, envelopes = _members(tmp_path)
    with pytest.raises(MigrationError, match="duplicate_identity_rejected"):
        migration.execute_identity_preserving_migration(
            storage=store, request=_request((envelopes[0], envelopes[0])), now=NOW
        )
    assert not (store.root / "journal.json").exists()


def test_concurrent_resume_is_serialized_not_interleaved(tmp_path):
    store, envelopes = _members(tmp_path)
    with pytest.raises(migration.MigrationUnavailable):
        MigrationExecutor(store).execute(_request(envelopes), now=NOW, stop_after="profiles_verified")
    # Another holder of the transaction lock (flock on POSIX, LockFileEx on win32).
    with FileMembershipState(store.root).lock():
        with pytest.raises(migration.MigrationUnavailable) as busy:
            migration.resume_concurrent_migration(
                storage=FileMembershipState(store.root), request=_request(envelopes), now=NOW
            )
        assert busy.value.code == "concurrent_resume_serialized"
    assert MigrationExecutor(store).checkpoint() == "profiles_verified"
    resumed = migration.resume_concurrent_migration(
        storage=FileMembershipState(store.root), request=_request(envelopes), now=NOW
    )
    assert resumed["checkpoint"] == "committed"
    assert resumed["checkpoint_before"] == "profiles_verified"


def test_rollback_before_commit_preserves_the_prior_database(tmp_path):
    store, envelopes = _members(tmp_path)
    before = (store.root / "members.json").read_bytes()
    with pytest.raises(migration.MigrationUnavailable):
        MigrationExecutor(store).execute(_request(envelopes), now=NOW, stop_after="commit_pending")
    result = migration.rollback_before_commit(storage=store, now=NOW)
    assert result["checkpoint"] == "rolled_back"
    assert result["prior_database_preserved"] is True
    assert (store.root / "members.json").read_bytes() == before
    assert migration.execute_identity_preserving_migration(storage=store, request=_request(envelopes), now=NOW) == "committed"
    with pytest.raises(MigrationError, match="rollback_not_available"):
        migration.rollback_before_commit(storage=store, now=NOW)


def test_cutover_requires_a_committed_migration(tmp_path):
    store, envelopes = _members(tmp_path)
    with pytest.raises(migration.MigrationUnavailable) as error:
        _cutover(store)
    assert error.value.code == "migration_not_committed"
    with pytest.raises(migration.MigrationUnavailable):
        MigrationExecutor(store).execute(_request(envelopes), now=NOW, stop_after="commit_pending")
    with pytest.raises(migration.MigrationUnavailable) as pending:
        _cutover(store)
    assert pending.value.code == "migration_not_committed"


def test_cutover_records_a_valid_migration_record_and_gates_v1_writes(tmp_path):
    store, envelopes = _members(tmp_path)
    migration.execute_identity_preserving_migration(storage=store, request=_request(envelopes), now=NOW)
    for operation in ("join_request", "resume_request"):
        migration.gate_v1_write(storage=store, operation=operation)
    with pytest.raises(ValueError, match="v1_transition_read_only"):
        migration.gate_v1_write(storage=store, operation="lease_renewal")
    record = _cutover(store)
    parsed = migration.ContractMigrationRecord.from_mapping(record)
    parsed.assert_monotonic_generations()
    assert parsed.status == "cutover_recorded"
    assert parsed.pre_generations == {"node-a": 5, "node-b": 2}
    assert parsed.post_generations == {"node-a": 6, "node-b": 3}
    assert parsed.cutover_generation == 6
    assert len(parsed.continuity_proof_digests) == 2
    assert len(parsed.profile_digests) == 2
    assert parsed.contract_digests == (CONTRACT,)
    assert parsed.inventory_digest == INVENTORY
    members_before = (store.root / "members.json").read_bytes()
    for operation in ("join_request", "resume_request", "lease_renewal", "capability_change", "assignment_request", "readiness_input"):
        with pytest.raises(migration.MembershipContractUpgradeRequired) as rejected:
            migration.gate_v1_write(storage=store, operation=operation)
        assert rejected.value.code == "membership_contract_upgrade_required"
    assert (store.root / "members.json").read_bytes() == members_before
    assert _cutover(store) == record  # exact re-record is idempotent
    with pytest.raises(MigrationError, match="cutover_already_recorded"):
        migration.record_cutover(
            storage=store,
            contract_digests=(CONTRACT, QUALIFICATION),
            inventory_digest=INVENTORY,
            qualification_digests=(),
            now=NOW + 2,
        )
    projection = parsed.status_projection()
    text = json.dumps(projection)
    for private in ("node-a", "verification_key", "inc-2", BUILD):
        assert private not in text


def test_v1_gate_before_any_migration_is_open_for_transition_only(tmp_path):
    store, _ = _members(tmp_path)
    migration.gate_v1_write(storage=store, operation="join_request")
    with pytest.raises(ValueError, match="v1_transition_read_only"):
        migration.gate_v1_write(storage=store, operation="readiness_input")
    with pytest.raises(ValueError, match="v1_operation_invalid"):
        migration.gate_v1_write(storage=store, operation="dual_write")


def test_migration_only_reader_is_removed_after_cutover(tmp_path):
    store, envelopes = _members(tmp_path)
    assert set(migration.read_v1_transition_input(storage=store)) == {"node-a", "node-b"}
    with pytest.raises(migration.MigrationUnavailable) as early:
        migration.remove_migration_only_reader(storage=store, now=NOW)
    assert early.value.code == "migration_cutover_not_recorded"
    migration.execute_identity_preserving_migration(storage=store, request=_request(envelopes), now=NOW)
    _cutover(store)
    removal = migration.remove_migration_only_reader(storage=store, now=NOW + 2)
    assert removal["v1_reader_removed"] is True
    assert removal["v1_write_after_cutover"] == "membership_contract_upgrade_required"
    record = migration.ContractMigrationRecord.from_mapping(removal["record"])
    assert record.status == "complete"
    with pytest.raises(migration.MembershipContractUpgradeRequired):
        migration.read_v1_transition_input(storage=store)
    assert migration.remove_migration_only_reader(storage=store, now=NOW + 3) == removal
    with pytest.raises(MigrationError):
        migration.rollback_before_commit(storage=store, now=NOW + 3)


def test_tampered_cutover_record_fails_closed(tmp_path):
    store, envelopes = _members(tmp_path)
    migration.execute_identity_preserving_migration(storage=store, request=_request(envelopes), now=NOW)
    _cutover(store)
    found = store._read("cutover.json")
    assert found is not None
    document = found[0]
    document["record"]["post_generations"]["node-a"] = 9
    store._write("cutover.json", document)
    with pytest.raises((MigrationError, ValueError)):
        migration.remove_migration_only_reader(storage=store, now=NOW + 2)
    with pytest.raises((MigrationError, ValueError)):
        migration.gate_v1_write(storage=store, operation="join_request")
