"""Seed and node halves of the one-time A9 v1 -> v2 membership migration.

Spec §6.  The private executor (:mod:`.migration_executor`) owns the journal,
restart, cutover and reader removal.  This module connects it to the product:

* :func:`export_seed_baseline` reads the seed's durable v1 member rows (a copy of
  the seed SQLite database) into the executor's owner-private baseline.  It is
  the migration-only v1 reader on the seed side; it never writes the seed.
* :func:`profile_requests` turns each v1 member into a request, signed by the
  seed's own bound key, that tells the member which generation and lease it
  must bind.  The request carries the legacy compound class only as a
  decomposition hint.
* :func:`build_migration_profile` runs on the member host.  It first verifies
  the request against the seed key the member pinned at join and against the
  member's own durable membership state (node ID, swarm, incarnation,
  generation n+1); an unsigned, forged or foreign request is refused.  The
  platform family and architecture come from the host's own observation, never
  from the request or the legacy label; a conflict with the decomposed legacy
  class is refused.

Trust boundary: the seed database copy is accepted only when its durable
identity binding names the expected swarm and seed key; requests are accepted
by a member only under that seed key; and the executor accepts a profile only
under the member's pinned durable key (:mod:`.migration_executor`).
* :class:`SeedMembershipCutover` is the seed-side v1 write gate: once the
  cutover is recorded every v1 join, resume, member message and assignment
  offer fails with ``membership_contract_upgrade_required``.

Declarations made here are declarations only.  They earn no role; class
qualification and eligibility stay separate authorities.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import hashlib
from pathlib import Path
import platform as _platform
import threading
from typing import Any

from .migration import (
    MembershipContractUpgradeRequired,
    PlatformNeutralMigrationError,
    decompose_legacy_peer_class,
)
from .migration_executor import FileMembershipState, MigrationExecutor
from mycelium_qualification.signing import build_ed25519_verifier

from .peer_profile import PeerProfile, sign_peer_profile
from .primitives import (
    PEER_PROFILE_PROTOCOL,
    RUNTIME_BACKEND_STAGES,
    bounded_int,
    canonical_bytes,
    closed_fields,
    detached,
    digest,
    finite,
    require,
    segment,
    text,
)

PROFILE_REQUEST_PROTOCOL = "mycelium.a9_migration_profile_request.v1"
ROUTER_WIRE_PROTOCOL = "mycelium.router_wire.v1"
#: v1 lifecycle states the seed treats as revoked for resume (coordinator.accept_resume).
REVOKED_LIFECYCLE_STATES = frozenset({"STOPPING", "STOPPED"})
#: Declared transport bounds.  Declarations, not observations.
DECLARED_MAX_FRAME_BYTES = 16 * 1024 * 1024
DECLARED_MAX_CONCURRENCY = 1

SYSTEM_TO_PLATFORM = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}
MACHINE_TO_ARCHITECTURE = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "ARM64": "arm64",
    "x86_64": "x86_64",
    "AMD64": "x86_64",
    "amd64": "x86_64",
}

REQUEST_ENVELOPE_FIELDS = frozenset({"protocol", "request", "signature", "verification_key"})
REQUEST_SIGNATURE_FIELDS = frozenset(
    {
        "algorithm",
        "signer_endpoint_id",
        "verification_key_digest",
        "signed_statement_digest",
        "signature",
    }
)
MEMBER_STATE_FIELDS = ("node_id", "swarm_id", "incarnation", "membership_generation", "seed_key_digest")

REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "swarm_id",
        "node_id",
        "legacy_peer_class",
        "current_incarnation",
        "membership_generation",
        "lease_expires_at",
        "software_build",
        "transports",
        "runtimes",
    }
)


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_seed_binding(state: Any, *, swarm_id: str, seed_key_digest: str) -> None:
    """The seed database copy must be the expected seed's own durable state."""

    expected_key = digest(seed_key_digest, "migration_seed_key_invalid")
    expected_swarm = segment(swarm_id, "migration_swarm_id_invalid")
    try:
        binding = state.identity_binding()
    except Exception as exc:  # noqa: BLE001 - any unreadable binding fails closed
        raise PlatformNeutralMigrationError("seed_identity_binding_missing") from exc
    if binding.get("seed_key_digest") != expected_key or binding.get("swarm_id") != expected_swarm:
        raise PlatformNeutralMigrationError("seed_identity_binding_mismatch")


def export_seed_baseline(
    state: Any, *, software_build_digest: str, swarm_id: str, seed_key_digest: str
) -> dict[str, dict[str, Any]]:
    """Owner-private executor baseline from the seed's durable v1 member rows.

    ``state`` is a :class:`mycelium_seed.state.SqliteSeedState` opened on a copy
    of the live seed database; the copy is refused unless its durable identity
    binding names ``swarm_id`` and ``seed_key_digest``.  The seed does not record
    a software build, so the caller binds the exact candidate build digest every
    member runs.
    """

    _require_seed_binding(state, swarm_id=swarm_id, seed_key_digest=seed_key_digest)
    build = digest(software_build_digest, "migration_build_digest_invalid")
    baseline: dict[str, dict[str, Any]] = {}
    for row in state.load_members():
        node = segment(row["node_id"], "migration_node_id_invalid")
        if node in baseline:
            raise PlatformNeutralMigrationError("duplicate_identity_rejected")
        baseline[node] = {
            "generation": bounded_int(row["generation"], "membership_generation_invalid", minimum=1),
            "verification_key_digest": digest(
                row["verification_key_digest"], "membership_key_invalid"
            ),
            "incarnation": segment(row["incarnation"], "membership_incarnation_invalid"),
            "lease_expires_at": finite(row["lease_expires_at"], "membership_lease_invalid"),
            "revoked": row["lifecycle_state"] in REVOKED_LIFECYCLE_STATES,
            "software_build_digest": build,
        }
    if not baseline:
        raise PlatformNeutralMigrationError("membership_inventory_invalid")
    return baseline


def _transport(family: str, *, build: str, endpoint_id: str) -> dict[str, Any]:
    return {
        "family": family,
        "build_digest": build,
        "wire_protocol_versions": [ROUTER_WIRE_PROTOCOL],
        # Digest only: raw EndpointIDs never enter a closed contract.
        "endpoint_id_digest": _sha256_text(endpoint_id),
        "connection_roles": ["initiator", "responder"],
        "direct_observed": None,
        "relay_observed": None,
        "persistent_reuse": None,
        "lifecycle_availability": "unknown",
        "max_frame_bytes": DECLARED_MAX_FRAME_BYTES,
        "max_concurrency": DECLARED_MAX_CONCURRENCY,
    }


def _runtime(backend_id: str, *, build: str) -> dict[str, Any]:
    return {
        "backend_id": backend_id,
        "build_digest": build,
        "model_families": ["qwen2"],
        "representations": ["int8-weight-only"],
        "precisions": ["float32"],
        "decode_operations": ["prefill", "decode"],
        "max_context_tokens": 4096,
        "max_concurrent_requests": 1,
        "kv_schema": "stage_local_kv",
        "artifact_formats": ["safetensors"],
        "cancellation_supported": True,
        "cleanup_supported": True,
    }


def profile_requests(
    state: Any,
    *,
    signer: Any,
    swarm_id: str,
    software_build_digest: str,
    software_version: str,
) -> list[dict[str, Any]]:
    """One seed-signed v2 profile request per durable v1 member (generation n+1).

    ``signer`` must be the seed key the database copy is bound to.
    """

    _require_seed_binding(
        state, swarm_id=swarm_id, seed_key_digest=getattr(signer, "verification_key_digest", None)
    )
    build = digest(software_build_digest, "migration_build_digest_invalid")
    requests = []
    for row in state.load_members():
        decomposition = decompose_legacy_peer_class(row["peer_class"])
        transports = [
            _transport(family, build=build, endpoint_id=row["endpoint_id"])
            for family in decomposition.transports
            if family in ("iroh", "https", "browser_https")
        ]
        runtimes = [
            _runtime(backend, build=build)
            for backend in decomposition.runtimes
            if backend in RUNTIME_BACKEND_STAGES
        ]
        statement = {
                "protocol": PROFILE_REQUEST_PROTOCOL,
                "swarm_id": segment(swarm_id, "migration_swarm_id_invalid"),
                "node_id": segment(row["node_id"], "migration_node_id_invalid"),
                "legacy_peer_class": row["peer_class"],
                "current_incarnation": row["incarnation"],
                "membership_generation": int(row["generation"]) + 1,
                "lease_expires_at": float(row["lease_expires_at"]),
                "software_build": {
                    "version": text(software_version, "migration_build_version_invalid"),
                    "digest": build,
                },
                "transports": transports,
                "runtimes": runtimes,
            }
        requests.append(
            {
                "protocol": PROFILE_REQUEST_PROTOCOL,
                "request": statement,
                "signature": signer.sign(statement),
                "verification_key": signer.public_key_record(),
            }
        )
    return requests


def verify_profile_request(
    envelope: Mapping[str, Any], *, member_state: Mapping[str, Any]
) -> dict[str, Any]:
    """Member-side check: seed-signed, and addressed to exactly this durable member.

    ``member_state`` is the member's own durable membership state
    (``mycelium_node.durable_membership``); its pinned ``seed_key_digest`` is the
    only key a request is accepted under.
    """

    if not isinstance(member_state, Mapping) or any(
        field not in member_state for field in MEMBER_STATE_FIELDS
    ):
        raise PlatformNeutralMigrationError("migration_member_state_invalid")
    pinned = digest(member_state["seed_key_digest"], "migration_seed_key_invalid")
    if not isinstance(envelope, Mapping):
        raise PlatformNeutralMigrationError("migration_profile_request_invalid")
    record = closed_fields(envelope, REQUEST_ENVELOPE_FIELDS, "migration_profile_request_invalid")
    require(record["protocol"] == PROFILE_REQUEST_PROTOCOL, "migration_profile_request_invalid")
    signature = closed_fields(
        record["signature"], REQUEST_SIGNATURE_FIELDS, "migration_profile_request_signature_invalid"
    )
    key = detached(record["verification_key"], "migration_profile_request_key_invalid")
    require(
        signature["verification_key_digest"] == pinned
        and key.get("verification_key_digest") == pinned,
        "migration_profile_request_not_seed_signed",
    )
    request = detached(record["request"], "migration_profile_request_invalid")
    if set(request) != REQUEST_FIELDS or request["protocol"] != PROFILE_REQUEST_PROTOCOL:
        raise PlatformNeutralMigrationError("migration_profile_request_invalid")
    verify = build_ed25519_verifier([key])
    require(
        verify(canonical_bytes(request, "migration_profile_request_noncanonical"), dict(signature)),
        "migration_profile_request_signature_invalid",
    )
    generation = member_state["membership_generation"]
    if (
        request["node_id"] != member_state["node_id"]
        or request["swarm_id"] != member_state["swarm_id"]
        or request["current_incarnation"] != member_state["incarnation"]
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or request["membership_generation"] != generation + 1
    ):
        raise PlatformNeutralMigrationError("migration_profile_request_not_for_this_member")
    return request


def observe_host_platform(
    *, system: str | None = None, machine: str | None = None
) -> tuple[str, str]:
    """The member host's own platform family and architecture observation."""

    observed_system = _platform.system() if system is None else system
    observed_machine = _platform.machine() if machine is None else machine
    return (
        SYSTEM_TO_PLATFORM.get(observed_system, "unknown"),
        MACHINE_TO_ARCHITECTURE.get(observed_machine, "unknown"),
    )


def build_migration_profile(
    envelope: Mapping[str, Any],
    *,
    member_state: Mapping[str, Any],
    new_incarnation: str,
    issued_at: float,
    observed_platform: tuple[str, str] | None = None,
) -> PeerProfile:
    """Build the member's v2 profile from a verified request and its own host observation."""

    request = verify_profile_request(envelope, member_state=member_state)
    platform_family, architecture = (
        observe_host_platform() if observed_platform is None else observed_platform
    )
    decomposition = decompose_legacy_peer_class(request["legacy_peer_class"])
    if decomposition.platform_family not in ("unknown", platform_family):
        raise PlatformNeutralMigrationError("platform_observation_conflicts_legacy_class")
    if segment(new_incarnation, "migration_incarnation_invalid") == request["current_incarnation"]:
        raise PlatformNeutralMigrationError("incarnation_not_renewed")
    lease = finite(request["lease_expires_at"], "migration_lease_invalid")
    issued = finite(issued_at, "migration_issued_at_invalid", minimum=0.0)
    if issued > lease:
        raise PlatformNeutralMigrationError("legacy_generation_stale")
    return PeerProfile.from_mapping(
        {
            "protocol": PEER_PROFILE_PROTOCOL,
            "profile_id": f"profile-{request['node_id']}-g{request['membership_generation']}",
            "swarm_id": request["swarm_id"],
            "node_id": request["node_id"],
            "incarnation": new_incarnation,
            "membership_generation": request["membership_generation"],
            "identity_kind": decomposition.identity_kind,
            "platform_family": platform_family,
            "platform_variant": None,
            "architecture": architecture,
            "software_build": dict(request["software_build"]),
            "transports": list(request["transports"]),
            "runtimes": list(request["runtimes"]),
            "lease_expires_at": lease,
            "issued_at": issued,
            "expires_at": lease,
        }
    )


def sign_migration_profile(
    envelope: Mapping[str, Any],
    *,
    signer: Any,
    member_state: Mapping[str, Any],
    new_incarnation: str,
    issued_at: float,
    observed_platform: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Sign the v2 profile with the member's existing durable node key."""

    profile = build_migration_profile(
        envelope,
        member_state=member_state,
        new_incarnation=new_incarnation,
        issued_at=issued_at,
        observed_platform=observed_platform,
    )
    return sign_peer_profile(signer=signer, profile=profile)


class SeedMembershipCutover:
    """Seed-side v1 write gate bound to one private migration storage root.

    :meth:`admit` holds the migration transaction lock for the whole seed write,
    so the cutover check and the v1 mutation are atomic with respect to
    ``record_cutover`` (which takes the same lock): a cutover can never be
    recorded between the check and the write.
    """

    def __init__(self, storage_root: str | Path) -> None:
        self._executor = MigrationExecutor(FileMembershipState(Path(storage_root)))
        self._held = threading.local()

    def recorded(self) -> bool:
        return self._executor.cutover_state() is not None

    @contextmanager
    def admit(self, operation: str) -> Iterator[None]:
        """Admit one v1 write while holding the migration lock; refuse after cutover."""

        text(operation, "v1_operation_invalid")
        if getattr(self._held, "depth", 0):
            # Re-entrant admission on the same thread: the lock is already held.
            self._held.depth += 1
            try:
                yield
            finally:
                self._held.depth -= 1
            return
        with self._executor.storage.lock():
            if self._executor._load_cutover() is not None:  # noqa: SLF001 - same lock held
                raise MembershipContractUpgradeRequired()
            self._held.depth = 1
            try:
                yield
            finally:
                self._held.depth = 0


__all__ = [
    "PROFILE_REQUEST_PROTOCOL",
    "SeedMembershipCutover",
    "build_migration_profile",
    "export_seed_baseline",
    "observe_host_platform",
    "profile_requests",
    "sign_migration_profile",
    "verify_profile_request",
]
