"""Legacy weak-RED placeholders, deselected by default.

Raw declared_members/record calls here cannot satisfy the real migration
contract. The typed, signed, fsynced local executor is tested with actual
filesystem transactions in tests/a9_acceptance/test_migration_executor.py.
These placeholders do not prove node/seed integration, reader removal or a
live cutover; keep them deselected until those later owned seams are delivered.
"""

from __future__ import annotations

import pytest

from mycelium_platform_neutral import migration

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64

BOUNDARY_FUNCTIONS = {
    "execute_identity_preserving_migration": "migration_executor_not_implemented",
    "restart_from_checkpoint": "migration_checkpoint_not_implemented",
    "rollback_before_commit": "migration_rollback_not_implemented",
    "resume_concurrent_migration": "concurrent_resume_not_implemented",
    "remove_migration_only_reader": "migration_reader_removal_not_implemented",
}


def _declared_members() -> dict:
    return {"node-a": 5, "node-b": 2}


def _migration_record() -> dict:
    return {
        "protocol": migration.PROTOCOL,
        "migration_id": "migration-001",
        "contract_digests": [D1, D2],
        "inventory_digest": D3,
        "pre_generations": _declared_members(),
        "post_generations": {"node-a": 6, "node-b": 3},
        "continuity_proof_digests": [D1],
        "profile_digests": [D2],
        "qualification_digests": [D3],
        "cutover_generation": 6,
        "cutover_at": 1_000_000.0,
        "status": "cutover_recorded",
    }


@pytest.mark.parametrize("function_name,code", sorted(BOUNDARY_FUNCTIONS.items()))
def test_boundary_function_raises_closed_unavailable_code(function_name: str, code: str) -> None:
    function = getattr(migration, function_name)

    with pytest.raises(migration.MigrationUnavailable) as error:
        function()

    assert error.value.code == code


@pytest.mark.parametrize("function_name,code", sorted(BOUNDARY_FUNCTIONS.items()))
def test_boundary_function_never_returns_a_success_document(function_name: str, code: str) -> None:
    function = getattr(migration, function_name)
    raised = False

    try:
        result = function(record=_migration_record(), now=1_000_100.0)
    except migration.MigrationUnavailable as error:
        raised = True
        assert error.code == code
    else:  # pragma: no cover - a return here is exactly what this test forbids
        assert not isinstance(result, dict)


def test_executor_commits_generation_n_plus_one_atomically() -> None:
    record = migration.execute_identity_preserving_migration(
        declared_members=_declared_members(), now=1_000_100.0
    )
    assert record["post_generations"] == {"node-a": 6, "node-b": 3}
    assert record["status"] == "cutover_recorded"


def test_executor_restarts_at_each_recorded_checkpoint() -> None:
    for checkpoint in ("profiles_signed", "profiles_verified", "commit_pending", "committed"):
        restarted = migration.restart_from_checkpoint(
            migration_id="migration-001", checkpoint=checkpoint, now=1_000_100.0
        )
        assert restarted["checkpoint"] in {"profiles_signed", "profiles_verified", "commit_pending", "committed"}
    assert restarted["resumed"] is True


def test_executor_rejects_a_changed_retry_and_accepts_an_exact_retry() -> None:
    exact = migration.execute_identity_preserving_migration(
        declared_members=_declared_members(), now=1_000_100.0, retry_token=D1
    )
    assert exact["retry"] == "exact"
    with pytest.raises(migration.MigrationUnavailable) as error:
        migration.execute_identity_preserving_migration(
            declared_members={"node-a": 5, "node-b": 1}, now=1_000_100.0, retry_token=D2
        )
    assert error.value.code == "changed_retry_rejected"


def test_executor_rejects_duplicate_identity_concurrent_resume_and_rolls_back_before_commit() -> None:
    with pytest.raises(migration.MigrationUnavailable) as duplicate:
        migration.execute_identity_preserving_migration(
            declared_members={"node-a": 5, "node-a ": 6}, now=1_000_100.0
        )
    assert duplicate.value.code == "duplicate_identity_rejected"
    with pytest.raises(migration.MigrationUnavailable) as concurrent:
        migration.resume_concurrent_migration(
            migration_id="migration-001", holder="seed-a", now=1_000_100.0
        )
    assert concurrent.value.code == "concurrent_resume_serialized"
    with pytest.raises(migration.MigrationUnavailable) as rollback:
        migration.rollback_before_commit(migration_id="migration-001", now=1_000_100.0)
    assert rollback.value.code == "prior_database_preserved"


def test_migration_only_reader_is_removed_after_cutover() -> None:
    removal = migration.remove_migration_only_reader(
        cutover_generation=6, now=1_000_100.0
    )
    assert removal["v1_reader_removed"] is True
    assert removal["v1_write_after_cutover"] == "membership_contract_upgrade_required"
