# SPDX-License-Identifier: AGPL-3.0-only
"""Executable acceptance tests for the identity-preserving v2 migration decisions.

These are the deterministic halves of the four frozen acceptance cases in
``tests/a9_acceptance/inventory.v1.json`` — ``identity_continuity_v2_migration``,
``identity_discontinuity_migration``, ``eligibility_carry_over_rejection``, and
``v1_write_after_cutover`` — driven by the plan's **enumerated** per-protocol
conversion map rather than a blanket ``.v1`` -> ``.v2`` suffix rewrite (the B1
forbidden pattern).

What is GREEN here: the migration *decision core* (protocol enumeration,
cryptographic identity continuity, generation monotonicity, carry-over
rejection, cutover write gate).  The migration *executor* (journal,
checkpoint/restart, atomic commit, cutover record, migration-reader removal) is
covered by ``tests/a9_acceptance/test_migration_executor.py`` and the promoted
``tests/a9_migration_scaffold`` lifecycle suite.  The three physical cases among
the four remain physical rounds driven by ``scripts/run_a9_gate.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_membership import contracts as v1_contracts
from mycelium_platform_neutral import eligibility, migration, peer_profile
from mycelium_platform_neutral.primitives import PlatformNeutralContractError
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    build_ed25519_verifier,
    generate_ed25519_signer,
)

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = Path(__file__).with_name("inventory.v1.json")
NOW = 1_000_000.0
LATER = NOW + 600.0


def D(char: str) -> str:
    return "sha256:" + char * 64


#: The four frozen inventory cases this module discharges deterministically.
ACCEPTANCE_CASE_TESTS = {
    "identity_continuity_v2_migration": "test_identity_continuity_preserves_node_id_and_key",
    "identity_discontinuity_migration": "test_identity_discontinuity_requires_fresh_reenrollment",
    "eligibility_carry_over_rejection": "test_legacy_eligibility_is_not_carried_over",
    "v1_write_after_cutover": "test_v1_write_after_cutover_fails_with_upgrade_required",
}


def legacy_identity(**overrides) -> migration.LegacyMemberIdentity:
    base = dict(
        node_id="node-a",
        verification_key_digest=D("k"),
        incarnation="incarnation-001",
        generation=5,
        lease_expires_at=LATER,
        revoked=False,
        software_build_digest=D("a"),
    )
    base.update(overrides)
    return migration.LegacyMemberIdentity(**base)


def continuity_evidence(**overrides) -> migration.ContinuityEvidence:
    base = dict(
        declared_node_id="node-a",
        signer_key_digest=D("k"),
        signature_valid=True,
        new_incarnation="incarnation-002",
        profile_binds_generation=5,
        software_build_digest=D("a"),
        operator_identity_mapping=None,
    )
    base.update(overrides)
    return migration.ContinuityEvidence(**base)


def test_accepted_case_ids_are_registered_to_executable_tests() -> None:
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    cases = {case["case_id"]: case for case in inventory["acceptance_cases"]}
    module = globals()
    for case_id, test_name in ACCEPTANCE_CASE_TESTS.items():
        assert case_id in cases
        assert callable(module.get(test_name)), test_name
    assert cases["v1_write_after_cutover"]["gate_kind"] == "deterministic_negative"
    assert cases["identity_continuity_v2_migration"]["gate_kind"] == "physical_positive"


def test_identity_continuity_preserves_node_id_and_key() -> None:
    """Case ``identity_continuity_v2_migration`` (deterministic half)."""

    legacy = legacy_identity()
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    legacy = legacy_identity(verification_key_digest=signer.verification_key_digest)
    statement = {
        "protocol": "mycelium.migration.continuity_proof.v1",
        "node_id": legacy.node_id,
        "pre_generation": legacy.generation,
        "new_incarnation": "incarnation-002",
    }
    signature = signer.sign(statement)
    # Cryptographic continuity: the new profile is signed by the durable key the
    # legacy membership already pinned, verified against that pinned digest.
    verify = build_ed25519_verifier([signer.public_key_record()])
    assert verify(canonical_json_bytes(statement), signature) is True
    evidence = continuity_evidence(
        signer_key_digest=signer.verification_key_digest, signature_valid=True
    )
    decision = migration.evaluate_identity_continuity(legacy, evidence, now=NOW)
    assert decision.state == "preserved"
    assert decision.node_id == "node-a"
    assert decision.blockers == ()
    # The generation advances exactly once, and re-evaluating the same pre-state
    # cannot advance it twice.
    assert decision.committed_generation == legacy.generation + 1
    assert (
        migration.evaluate_identity_continuity(legacy, evidence, now=NOW)
        == decision
    )


def test_identity_discontinuity_requires_fresh_reenrollment() -> None:
    """Case ``identity_discontinuity_migration`` (deterministic half)."""

    legacy = legacy_identity()
    rejections = {
        "member_revoked": legacy_identity(revoked=True),
        "legacy_generation_stale": legacy_identity(lease_expires_at=NOW - 1.0),
        "software_build_absent": legacy_identity(software_build_digest=None),
        "node_id_mismatch": legacy_identity(node_id="node-b"),
        "changed_key": legacy_identity(verification_key_digest=D("x")),
        "incarnation_not_renewed": legacy_identity(incarnation="incarnation-002"),
        "migration_generation_mismatch": legacy_identity(generation=4),
    }
    for blocker, legacy in rejections.items():
        decision = migration.evaluate_identity_continuity(
            legacy, continuity_evidence(), now=NOW
        )
        assert decision.state == "fresh_reenrollment_required"
        assert blocker in decision.blockers, blocker
        # No partial v2 member is visible and no incumbent is displaced.
        assert decision.node_id is None
        assert decision.committed_generation is None
    absent_proof = migration.evaluate_identity_continuity(
        legacy_identity(), continuity_evidence(signature_valid=False), now=NOW
    )
    assert absent_proof.state == "fresh_reenrollment_required"
    assert "key_continuity_proof_absent" in absent_proof.blockers
    # A changed key can never be smoothed over by an operator-supplied mapping.
    mapped = migration.evaluate_identity_continuity(
        legacy_identity(),
        continuity_evidence(
            signer_key_digest=D("x"),
            operator_identity_mapping={"node-a": "node-b"},
        ),
        now=NOW,
    )
    assert mapped.state == "rejected"
    assert mapped.blockers == ("operator_identity_mapping_not_acceptable",)
    assert mapped.committed_generation is None


def test_legacy_eligibility_is_not_carried_over() -> None:
    """Case ``eligibility_carry_over_rejection`` (deterministic half)."""

    # The legacy label says this member was activation-eligible …
    assert v1_contracts.peer_runtime_is_activation_eligible(
        "mac_mlx_iroh",
        {
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": v1_contracts.ROUTER_ACTIVATION_PROTOCOL,
        },
    )
    # … and the v2 re-evaluation never copies that fact into any role.
    re_evaluated = migration.re_evaluate_roles(
        node_id="node-a",
        legacy_activation_eligible=True,
        roles=("probe_contributor", "artifact_source", "qualified_model_stage"),
    )
    assert set(re_evaluated.roles.values()) == {"ineligible"}
    # The same member with no exact v2 profile, evidence, or qualification is
    # visible and ineligible — no assignment, artifact, activation, or selection.
    subject = peer_profile.PeerProfile.from_mapping(
        {
            "protocol": peer_profile.PROTOCOL,
            "profile_id": "profile-001",
            "swarm_id": "swarm-demo",
            "node_id": "node-a",
            "incarnation": "incarnation-002",
            "membership_generation": 6,
            "identity_kind": "native_peer",
            "platform_family": "macos",
            "platform_variant": None,
            "architecture": "arm64",
            "software_build": {"version": "0.9.0", "digest": D("a")},
            "transports": [
                {
                    "family": "iroh",
                    "build_digest": D("b"),
                    "wire_protocol_versions": ["mycelium.router_wire.v1"],
                    "endpoint_id_digest": None,
                    "connection_roles": ["initiator"],
                    "direct_observed": None,
                    "relay_observed": None,
                    "persistent_reuse": None,
                    "lifecycle_availability": "service_managed",
                    "max_frame_bytes": 1 << 20,
                    "max_concurrency": 4,
                }
            ],
            "runtimes": [],
            "lease_expires_at": LATER,
            "issued_at": NOW - 60.0,
            "expires_at": LATER,
        }
    )
    role = eligibility.derive_eligibility(
        "qualified_model_stage",
        eligibility.LadderInputs(
            node_id=subject.node_id,
            membership_generation=subject.membership_generation,
            lease_expires_at=subject.lease_expires_at,
            revoked=False,
            identity_kind=subject.identity_kind,
            consent=None,
            profile=subject,
            now=NOW,
        ),
        now=NOW,
    )
    assert role.state == "ineligible"
    assert "class_qualification_absent" in role.blockers


def test_v1_write_after_cutover_fails_with_upgrade_required() -> None:
    """Case ``v1_write_after_cutover`` (the inventory's deterministic negative)."""

    cutover = migration.MigrationCutover.before_cutover(migration_id="migration-001")
    # During the bounded window, v1 may read only to authenticate the transition.
    migration.admit_v1_write(cutover, operation="join_request")
    migration.admit_v1_write(cutover, operation="resume_request")
    for operation in migration.V1_FORBIDDEN_OPERATIONS:
        with pytest.raises(PlatformNeutralContractError) as error:
            migration.admit_v1_write(cutover, operation=operation)
        assert error.value.code == "v1_transition_read_only"
    recorded = cutover.at_cutover(generation=6, at=NOW)
    assert recorded.v1_transition_open is False
    for operation in (*migration.V1_FORBIDDEN_OPERATIONS, "join_request", "resume_request"):
        with pytest.raises(migration.MembershipContractUpgradeRequired) as error:
            migration.admit_v1_write(recorded, operation=operation)
        assert error.value.code == "membership_contract_upgrade_required"
    # The gate is pure: no v2 generation changed, no dual write was created.
    assert recorded.cutover_generation == 6
    assert recorded.v1_transition_open is False
    assert recorded.v1_reader_removed is False
    with pytest.raises(PlatformNeutralContractError) as double:
        recorded.at_cutover(generation=7, at=NOW)
    assert double.value.code == "migration_cutover_already_recorded"


def test_enumerated_protocol_map_covers_every_v1_membership_protocol() -> None:
    v1_protocols = {
        value
        for name, value in vars(v1_contracts).items()
        if name.endswith("_PROTOCOL")
        and isinstance(value, str)
        and value.startswith("mycelium.membership.")
        and value.endswith(".v1")
    }
    assert len(v1_protocols) == 13
    enumerated = (
        set(migration.CONTRACT_CONVERSION_MAP)
        | set(migration.DECLARATION_CONVERTING_PROTOCOLS)
        | set(migration.UNCHANGED_PROTOCOLS)
    )
    assert enumerated == v1_protocols
    assert not (
        set(migration.CONTRACT_CONVERSION_MAP)
        & set(migration.DECLARATION_CONVERTING_PROTOCOLS)
    )
    # Only the two v2 contracts the spec introduces are reachable as targets.
    assert set(migration.CONTRACT_CONVERSION_MAP.values()) | set(
        migration.V2_INTRODUCED_CONTRACTS
    ) == {
        "mycelium.membership.capability_report.v2",
        "mycelium.membership.peer_profile.v2",
    }
    migration.assert_no_blanket_suffix_rewrite()
    for protocol in v1_protocols:
        if protocol in migration.UNCHANGED_PROTOCOLS:
            assert migration.convert_protocol(protocol) == protocol


def test_migration_record_requires_monotonic_generations_and_privacy_reduced_projection() -> None:
    record = migration.ContractMigrationRecord.from_mapping(
        {
            "protocol": migration.PROTOCOL,
            "migration_id": "migration-001",
            "contract_digests": [D("1"), D("2")],
            "inventory_digest": D("3"),
            "pre_generations": {"node-a": 5, "node-b": 2},
            "post_generations": {"node-a": 6, "node-b": 3},
            "continuity_proof_digests": [D("4")],
            "profile_digests": [D("5"), D("6")],
            "qualification_digests": [D("7")],
            "cutover_generation": 6,
            "cutover_at": NOW,
            "status": "cutover_recorded",
        }
    )
    record.assert_monotonic_generations()
    regressed = migration.ContractMigrationRecord.from_mapping(
        {
            **record.to_mapping(),
            "post_generations": {"node-a": 6, "node-b": 4},
        }
    )
    with pytest.raises(PlatformNeutralContractError) as error:
        regressed.assert_monotonic_generations()
    assert error.value.code == "migration_generation_not_monotonic"
    projection = record.status_projection()
    assert "node_id" not in json.dumps(projection)
    assert set(projection) == {
        "protocol",
        "migration_id",
        "status",
        "cutover_generation",
        "member_count",
        "continuity_preserved_count",
        "profile_count",
        "qualification_count",
    }
    not_recorded = record.to_mapping()
    not_recorded["status"] = "planned"
    not_recorded["cutover_generation"] = None
    not_recorded["cutover_at"] = None
    with pytest.raises(PlatformNeutralContractError) as missing:
        migration.ContractMigrationRecord.from_mapping(
            {**not_recorded, "status": "cutover_recorded", "cutover_at": None}
        )
    assert missing.value.code == "migration_cutover_not_recorded"
