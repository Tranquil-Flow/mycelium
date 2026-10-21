# SPDX-License-Identifier: AGPL-3.0-only
"""``mycelium.membership_contract_migration.v1`` — the one-time v1 -> v2 migration.

Spec §6, §4.5, §9.  This module authors the *enumerated* migration decisions the
plan requires and explicitly forbids the blanket ``.v1`` -> ``.v2`` suffix
rewrite blocker B1 named:

* an explicit per-protocol conversion map — exactly the protocols the A9
  contracts replace, never a suffix rewrite; an unmapped membership protocol is
  rejected, not renamed;
* an explicit decomposition for every legacy compound peer class into separate
  identity-kind / platform-family / transport / runtime declarations, with
  ``unknown`` where the label does not determine a fact (which stays visible and
  ineligible);
* cryptographic identity continuity: node ID and verification key are preserved
  only after key continuity on the current, non-revoked generation, and the
  generation advances exactly once;
* eligibility carry-over rejection: legacy activation eligibility is never
  copied into v2;
* the recorded cutover gate: after cutover every v1 write fails with
  ``membership_contract_upgrade_required``.

The local private filesystem executor is available only with an explicit typed
storage/request pair and verified signed profiles. It journals, restarts from
each checkpoint, serializes concurrent resume, rolls back before commit,
records the cutover (the migration record below) and removes the
migration-only reader. A live cutover is a physical A9 gate step.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mycelium_membership.contracts import PEER_CLASSES

from .primitives import (
    CAPABILITY_REPORT_PROTOCOL,
    MIGRATION_PROTOCOL,
    PEER_PROFILE_PROTOCOL,
    bounded_int,
    canonical_bytes,
    closed_fields,
    digest,
    enum_value,
    finite,
    record_digest,
    require,
    segment,
    text,
    unique_names,
)

PROTOCOL = MIGRATION_PROTOCOL

UPGRADE_REQUIRED = "membership_contract_upgrade_required"

#: The A9 frozen contracts that replace or introduce v2 surfaces (spec §4).
V2_INTRODUCED_CONTRACTS = (PEER_PROFILE_PROTOCOL, CAPABILITY_REPORT_PROTOCOL)

#: The explicit, enumerated per-protocol conversion map.  Exactly one v1
#: membership protocol has a v2 counterpart; every other v1 protocol keeps its
#: name and version.  This table is the antidote to a blanket suffix rewrite.
CONTRACT_CONVERSION_MAP = {
    "mycelium.membership.capability_report.v1": CAPABILITY_REPORT_PROTOCOL,
}

#: v1 protocols whose declaration payload (compound ``peer_class`` plus
#: ``runtime_capability``) is replaced by a signed ``peer_profile.v2`` while the
#: envelope protocol keeps its own name and version (spec §6.4).
DECLARATION_CONVERTING_PROTOCOLS = (
    "mycelium.membership.join_request.v1",
    "mycelium.membership.resume_request.v1",
)

#: Every remaining v1 membership protocol that migration must leave at v1.
UNCHANGED_PROTOCOLS = (
    "mycelium.membership.join_acceptance.v1",
    "mycelium.membership.resume_acceptance.v1",
    "mycelium.membership.heartbeat.v1",
    "mycelium.membership.lease_renewal.v1",
    "mycelium.membership.assignment_offer.v1",
    "mycelium.membership.assignment_result.v1",
    "mycelium.membership.drain_ack.v1",
    "mycelium.membership.seed_rotation_ack.v1",
    "mycelium.membership.signed_message.v1",
    "mycelium.membership.link_probe_report.v1",
)

#: The bounded v1 transition window may only authenticate the transition.
V1_TRANSITION_OPERATIONS = frozenset({"join_request", "resume_request"})
V1_FORBIDDEN_OPERATIONS = (
    "lease_renewal",
    "capability_change",
    "assignment_request",
    "readiness_input",
)

#: Legacy compound class -> separate v2 declarations (spec §6.4).  ``unknown``
#: marks a fact the compound label does not determine; it stays visible and
#: ineligible rather than being guessed from brand text.
LEGACY_PEER_CLASS_DECOMPOSITION = {
    "mac_mlx_iroh": {
        "identity_kind": "native_peer",
        "platform_family": "macos",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": ["mlx"],
        "declared_ineligible": False,
    },
    "linux_numpy_iroh": {
        "identity_kind": "native_peer",
        "platform_family": "linux",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": ["numpy"],
        "declared_ineligible": True,
    },
    "browser_http": {
        "identity_kind": "browser_peer",
        "platform_family": "browser",
        "architecture": "unknown",
        "transports": ["browser_https"],
        "runtimes": [],
        "declared_ineligible": True,
    },
    "pixel_http": {
        "identity_kind": "native_peer",
        "platform_family": "android",
        "architecture": "unknown",
        "transports": ["https"],
        "runtimes": [],
        "declared_ineligible": True,
    },
    "android_termux_iroh": {
        "identity_kind": "native_peer",
        "platform_family": "android",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": [],
        "declared_ineligible": True,
    },
    "linux_tbd": {
        "identity_kind": "native_peer",
        "platform_family": "linux",
        "architecture": "unknown",
        "transports": [],
        "runtimes": [],
        "declared_ineligible": True,
    },
    "artifact_source_https": {
        "identity_kind": "artifact_source",
        "platform_family": "unknown",
        "architecture": "unknown",
        "transports": ["https"],
        "runtimes": [],
        "declared_ineligible": True,
    },
    # The v1 activation allow-set admits this class, but the v2 declaration
    # stays ineligible until class qualification evidence exists (the same
    # stance as ``linux_numpy_iroh``); its runtime is not a registered v2
    # backend, so none is guessed.
    "android_native_iroh": {
        "identity_kind": "native_peer",
        "platform_family": "android",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": [],
        "declared_ineligible": True,
    },
    # Q2 phone ruling: the Qwen2 Android app is a separate v1 class so
    # reference-phone leases survive.  Same v2 stance as ``android_native_iroh``:
    # no registered v2 backend is guessed and it stays declared ineligible
    # until class qualification evidence exists.
    "android_native_qwen2_iroh": {
        "identity_kind": "native_peer",
        "platform_family": "android",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": [],
        "declared_ineligible": True,
    },
    "windows_native_iroh": {
        "identity_kind": "native_peer",
        "platform_family": "windows",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": ["numpy"],
        "declared_ineligible": True,
    },
    # The A13 Linux product class binds the NumPy runtime (owner parity ruling
    # 2026-10-07); v2 eligibility still needs class qualification.
    "linux_native_iroh": {
        "identity_kind": "native_peer",
        "platform_family": "linux",
        "architecture": "unknown",
        "transports": ["iroh"],
        "runtimes": ["numpy"],
        "declared_ineligible": True,
    },
}

CONTINUITY_STATES = frozenset({"preserved", "fresh_reenrollment_required", "rejected"})
MIGRATION_STATUSES = frozenset({"planned", "in_progress", "cutover_recorded", "complete"})
RECORD_FIELDS = frozenset(
    {
        "protocol",
        "migration_id",
        "contract_digests",
        "inventory_digest",
        "pre_generations",
        "post_generations",
        "continuity_proof_digests",
        "profile_digests",
        "qualification_digests",
        "cutover_generation",
        "cutover_at",
        "status",
    }
)


class MigrationUnavailable(RuntimeError):
    """A migration step is unavailable: untyped input, absent checkpoint or wrong stage."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def convert_protocol(protocol: str) -> str:
    """Return the enumerated v2 counterpart, or reject — never rewrite a suffix."""

    require(isinstance(protocol, str), "migration_protocol_unusable")
    require(
        protocol in CONTRACT_CONVERSION_MAP
        or protocol in DECLARATION_CONVERTING_PROTOCOLS
        or protocol in UNCHANGED_PROTOCOLS,
        "contract_not_in_migration_map",
    )
    if protocol in CONTRACT_CONVERSION_MAP:
        return CONTRACT_CONVERSION_MAP[protocol]
    return protocol


def assert_no_blanket_suffix_rewrite() -> None:
    """The map must not convert an unchanged protocol (the B1 forbidden pattern)."""

    for protocol in UNCHANGED_PROTOCOLS:
        if protocol in CONTRACT_CONVERSION_MAP:
            raise AssertionError(f"blanket rewrite of {protocol}")
        suffix_rewrite = protocol.replace(".v1", ".v2")
        if suffix_rewrite != protocol and any(
            suffix_rewrite in target for target in CONTRACT_CONVERSION_MAP.values()
        ):
            raise AssertionError(f"suffix rewrite of {protocol}")


@dataclass(frozen=True, slots=True)
class LegacyClassDecomposition:
    peer_class: str
    identity_kind: str
    platform_family: str
    architecture: str
    transports: tuple[str, ...]
    runtimes: tuple[str, ...]
    declared_ineligible: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "peer_class": self.peer_class,
            "identity_kind": self.identity_kind,
            "platform_family": self.platform_family,
            "architecture": self.architecture,
            "transports": list(self.transports),
            "runtimes": list(self.runtimes),
            "declared_ineligible": self.declared_ineligible,
        }


def decompose_legacy_peer_class(peer_class: str) -> LegacyClassDecomposition:
    require(isinstance(peer_class, str), "legacy_peer_class_unusable")
    require(peer_class in LEGACY_PEER_CLASS_DECOMPOSITION, "legacy_peer_class_unmapped")
    record = LEGACY_PEER_CLASS_DECOMPOSITION[peer_class]
    return LegacyClassDecomposition(
        peer_class=peer_class,
        identity_kind=record["identity_kind"],
        platform_family=record["platform_family"],
        architecture=record["architecture"],
        transports=tuple(record["transports"]),
        runtimes=tuple(record["runtimes"]),
        declared_ineligible=record["declared_ineligible"],
    )


@dataclass(frozen=True, slots=True)
class LegacyMemberIdentity:
    """The owner-private legacy facts continuity is decided against."""

    node_id: str
    verification_key_digest: str
    incarnation: str
    generation: int
    lease_expires_at: float
    revoked: bool
    software_build_digest: str | None


@dataclass(frozen=True, slots=True)
class ContinuityEvidence:
    """The v2 profile proof presented during the bounded migration window."""

    declared_node_id: str
    signer_key_digest: str
    signature_valid: bool
    new_incarnation: str
    profile_binds_generation: int
    software_build_digest: str | None
    operator_identity_mapping: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ContinuityDecision:
    state: str
    node_id: str | None
    committed_generation: int | None
    blockers: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "node_id": self.node_id,
            "committed_generation": self.committed_generation,
            "blockers": list(self.blockers),
        }


def evaluate_identity_continuity(
    legacy: LegacyMemberIdentity, evidence: ContinuityEvidence, *, now: float
) -> ContinuityDecision:
    """Preserve node ID and key only after cryptographic continuity succeeds."""

    finite(now, "migration_now_invalid", minimum=0.0)
    if not isinstance(legacy, LegacyMemberIdentity) or not isinstance(
        evidence, ContinuityEvidence
    ):
        raise PlatformNeutralMigrationError("migration_input_invalid")
    blockers: list[str] = []
    if evidence.operator_identity_mapping:
        return ContinuityDecision(
            state="rejected",
            node_id=None,
            committed_generation=None,
            blockers=("operator_identity_mapping_not_acceptable",),
        )
    if legacy.revoked:
        blockers.append("member_revoked")
    if now > legacy.lease_expires_at:
        blockers.append("legacy_generation_stale")
    if not legacy.software_build_digest or not evidence.software_build_digest:
        blockers.append("software_build_absent")
    if not evidence.signature_valid:
        blockers.append("key_continuity_proof_absent")
    if evidence.declared_node_id != legacy.node_id:
        blockers.append("node_id_mismatch")
    if evidence.signer_key_digest != legacy.verification_key_digest:
        blockers.append("changed_key")
    if evidence.new_incarnation == legacy.incarnation:
        blockers.append("incarnation_not_renewed")
    if evidence.profile_binds_generation != legacy.generation:
        blockers.append("migration_generation_mismatch")
    if blockers:
        return ContinuityDecision(
            state="fresh_reenrollment_required",
            node_id=None,
            committed_generation=None,
            blockers=tuple(blockers),
        )
    return ContinuityDecision(
        state="preserved",
        node_id=legacy.node_id,
        committed_generation=legacy.generation + 1,
        blockers=(),
    )


@dataclass(frozen=True, slots=True)
class RoleReEvaluation:
    """A legacy member's v2 role states before any exact re-evaluation."""

    node_id: str
    roles: Mapping[str, str]

    def to_mapping(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "roles": dict(self.roles)}


def re_evaluate_roles(
    *, node_id: str, legacy_activation_eligible: bool, roles: tuple[str, ...]
) -> RoleReEvaluation:
    """Legacy activation eligibility is never copied into v2 (spec §6.5)."""

    segment(node_id, "migration_node_id_invalid")
    states = {role: "ineligible" for role in unique_names(list(roles), "migration_role_invalid")}
    return RoleReEvaluation(node_id=node_id, roles=states)


@dataclass(frozen=True, slots=True)
class MigrationCutover:
    """The recorded cutover: while open, v1 is a bounded read-only transition input."""

    migration_id: str
    v1_transition_open: bool
    cutover_generation: int | None
    cutover_at: float | None
    v1_reader_removed: bool

    @classmethod
    def before_cutover(cls, *, migration_id: str) -> "MigrationCutover":
        return cls(
            migration_id=segment(migration_id, "migration_id_invalid"),
            v1_transition_open=True,
            cutover_generation=None,
            cutover_at=None,
            v1_reader_removed=False,
        )

    def at_cutover(self, *, generation: int, at: float) -> "MigrationCutover":
        require(self.v1_transition_open, "migration_cutover_already_recorded")
        return MigrationCutover(
            migration_id=self.migration_id,
            v1_transition_open=False,
            cutover_generation=bounded_int(
                generation, "migration_cutover_generation_invalid", minimum=1
            ),
            cutover_at=finite(at, "migration_cutover_at_invalid", minimum=0.0),
            v1_reader_removed=self.v1_reader_removed,
        )


class MembershipContractUpgradeRequired(RuntimeError):
    """Every v1 write after the recorded cutover fails with this stable code."""

    def __init__(self) -> None:
        self.code = UPGRADE_REQUIRED
        super().__init__(UPGRADE_REQUIRED)


def admit_v1_write(cutover: MigrationCutover, *, operation: str) -> None:
    """Gate one v1 write; raises without mutating any generation."""

    require(isinstance(cutover, MigrationCutover), "migration_cutover_invalid")
    enum_value(operation, V1_TRANSITION_OPERATIONS | set(V1_FORBIDDEN_OPERATIONS), "v1_operation_invalid")
    if not cutover.v1_transition_open:
        raise MembershipContractUpgradeRequired()
    require(
        operation in V1_TRANSITION_OPERATIONS,
        "v1_transition_read_only",
    )


@dataclass(frozen=True, slots=True)
class ContractMigrationRecord:
    migration_id: str
    contract_digests: tuple[str, ...]
    inventory_digest: str
    pre_generations: Mapping[str, int]
    post_generations: Mapping[str, int]
    continuity_proof_digests: tuple[str, ...]
    profile_digests: tuple[str, ...]
    qualification_digests: tuple[str, ...]
    cutover_generation: int | None
    cutover_at: float | None
    status: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContractMigrationRecord":
        record = closed_fields(value, RECORD_FIELDS, "migration_record_invalid")
        require(record["protocol"] == PROTOCOL, "migration_record_protocol_invalid")
        status = enum_value(record["status"], MIGRATION_STATUSES, "migration_status_invalid")
        cutover_generation = record["cutover_generation"]
        cutover_at = record["cutover_at"]
        if status in {"cutover_recorded", "complete"}:
            require(
                cutover_generation is not None and cutover_at is not None,
                "migration_cutover_not_recorded",
            )
        require(
            record["inventory_digest"] is not None,
            "migration_inventory_digest_missing",
        )
        return cls(
            migration_id=segment(record["migration_id"], "migration_id_invalid"),
            contract_digests=tuple(
                digest(item, "migration_contract_digest_invalid")
                for item in unique_names(
                    record["contract_digests"], "migration_contract_digests_invalid"
                )
            ),
            inventory_digest=digest(
                record["inventory_digest"], "migration_inventory_digest_invalid"
            ),
            pre_generations=_generation_map(record["pre_generations"]),
            post_generations=_generation_map(record["post_generations"]),
            continuity_proof_digests=tuple(
                digest(item, "migration_continuity_digest_invalid")
                for item in unique_names(
                    record["continuity_proof_digests"],
                    "migration_continuity_digests_invalid",
                )
            ),
            profile_digests=tuple(
                digest(item, "migration_profile_digest_invalid")
                for item in unique_names(
                    record["profile_digests"], "migration_profile_digests_invalid"
                )
            ),
            qualification_digests=tuple(
                digest(item, "migration_qualification_digest_invalid")
                for item in unique_names(
                    record["qualification_digests"],
                    "migration_qualification_digests_invalid",
                )
            ),
            cutover_generation=(
                None
                if cutover_generation is None
                else bounded_int(
                    cutover_generation, "migration_cutover_generation_invalid", minimum=1
                )
            ),
            cutover_at=(
                None
                if cutover_at is None
                else finite(cutover_at, "migration_cutover_at_invalid", minimum=0.0)
            ),
            status=status,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "migration_id": self.migration_id,
            "contract_digests": list(self.contract_digests),
            "inventory_digest": self.inventory_digest,
            "pre_generations": dict(self.pre_generations),
            "post_generations": dict(self.post_generations),
            "continuity_proof_digests": list(self.continuity_proof_digests),
            "profile_digests": list(self.profile_digests),
            "qualification_digests": list(self.qualification_digests),
            "cutover_generation": self.cutover_generation,
            "cutover_at": self.cutover_at,
            "status": self.status,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_mapping(), "migration_record_noncanonical")

    @property
    def digest(self) -> str:
        return record_digest(self.to_mapping(), "migration_record_noncanonical")

    def assert_monotonic_generations(self) -> None:
        """Every migrated member advances exactly once (spec §6.2)."""

        require(
            set(self.pre_generations) == set(self.post_generations),
            "migration_member_inventory_mismatch",
        )
        for node_id, pre in self.pre_generations.items():
            require(
                self.post_generations[node_id] == pre + 1,
                "migration_generation_not_monotonic",
            )

    def status_projection(self) -> dict[str, Any]:
        """Privacy-reduced status projection; never replays membership credentials."""

        return {
            "protocol": PROTOCOL,
            "migration_id": self.migration_id,
            "status": self.status,
            "cutover_generation": self.cutover_generation,
            "member_count": len(self.pre_generations),
            "continuity_preserved_count": len(self.continuity_proof_digests),
            "profile_count": len(self.profile_digests),
            "qualification_count": len(self.qualification_digests),
        }


class PlatformNeutralMigrationError(ValueError):
    """Migration input error carrying one stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _typed(storage: Any, untrusted: Mapping[str, Any]) -> Any:
    from .migration_executor import FileMembershipState, MigrationExecutor

    if untrusted or not isinstance(storage, FileMembershipState):
        raise MigrationUnavailable("verified_migration_request_required")
    return MigrationExecutor(storage)


def _typed_request(request: Any, now: Any) -> None:
    from .migration_executor import MigrationRequest

    if not isinstance(request, MigrationRequest) or now is None:
        raise MigrationUnavailable("verified_migration_request_required")


def execute_identity_preserving_migration(*, storage: Any = None, request: Any = None, now: float | None = None, stop_after: str | None = None, **untrusted: Any) -> str:
    """Run only the local transaction with a typed adapter and signed inputs.

    Raw declared_members/record/booleans are not migration authority.
    """

    executor = _typed(storage, untrusted)
    _typed_request(request, now)
    return executor.execute(request, now=now, stop_after=stop_after)


def restart_from_checkpoint(*, storage: Any = None, request: Any = None, now: float | None = None, **untrusted: Any) -> dict[str, Any]:
    """Resume an interrupted migration from its private fsynced checkpoint."""

    executor = _typed(storage, untrusted)
    _typed_request(request, now)
    return executor.restart(request, now=now)


def rollback_before_commit(*, storage: Any = None, now: float | None = None, **untrusted: Any) -> dict[str, Any]:
    """Abandon an uncommitted migration; the prior durable database is untouched."""

    executor = _typed(storage, untrusted)
    finite(now, "migration_now_invalid", minimum=0.0)
    source = executor.storage._snapshot()[1]
    state = executor.rollback()
    preserved = executor.storage._snapshot()[1] == source
    return {"checkpoint": state, "prior_database_preserved": preserved}


def resume_concurrent_migration(*, storage: Any = None, request: Any = None, now: float | None = None, **untrusted: Any) -> dict[str, Any]:
    """Resume without waiting: a concurrently held transaction is reported, never interleaved."""

    executor = _typed(storage, untrusted)
    _typed_request(request, now)
    return executor.restart(request, now=now, blocking=False)


def record_cutover(*, storage: Any = None, contract_digests: Any = None, inventory_digest: Any = None, qualification_digests: Any = (), now: float | None = None, **untrusted: Any) -> dict[str, Any]:
    """Record the cutover after a committed migration; returns the migration record."""

    executor = _typed(storage, untrusted)
    if contract_digests is None or inventory_digest is None or now is None:
        raise MigrationUnavailable("verified_migration_request_required")
    return executor.record_cutover(
        contract_digests=tuple(contract_digests),
        inventory_digest=inventory_digest,
        qualification_digests=tuple(qualification_digests),
        now=now,
    )


def gate_v1_write(*, storage: Any = None, operation: str, **untrusted: Any) -> None:
    """Admit a v1 transition write before cutover; raise upgrade-required after it."""

    executor = _typed(storage, untrusted)
    document = executor.cutover_state()
    if document is None:
        cutover = MigrationCutover.before_cutover(migration_id="pending-cutover")
    else:
        record = document["record"]
        cutover = MigrationCutover.before_cutover(
            migration_id=document["migration_id"]
        ).at_cutover(generation=record["cutover_generation"], at=record["cutover_at"])
    admit_v1_write(cutover, operation=operation)


def remove_migration_only_reader(*, storage: Any = None, now: float | None = None, **untrusted: Any) -> dict[str, Any]:
    """Terminal step: after removal the v1 transition input cannot be read."""

    executor = _typed(storage, untrusted)
    if now is None:
        raise MigrationUnavailable("verified_migration_request_required")
    return executor.remove_migration_only_reader(now=now)


def read_v1_transition_input(*, storage: Any = None, **untrusted: Any) -> dict[str, Any]:
    """The bounded migration-only reader of the v1 member inventory."""

    return _typed(storage, untrusted).read_v1_transition_input()


def _generation_map(value: Any) -> dict[str, int]:
    require(isinstance(value, Mapping), "migration_generation_map_invalid")
    result: dict[str, int] = {}
    for node_id, generation in value.items():
        key = segment(node_id, "migration_node_id_invalid")
        result[key] = bounded_int(
            generation, "migration_generation_invalid", minimum=1, maximum=1 << 40
        )
    return result


def legacy_peer_classes() -> frozenset[str]:
    """Every legacy compound class the product ships at the tip."""

    return frozenset(PEER_CLASSES)
