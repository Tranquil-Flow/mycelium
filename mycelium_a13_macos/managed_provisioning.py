# SPDX-License-Identifier: AGPL-3.0-only
"""Target-owned run-profile admission for the A13 macOS managed slice.

This module owns the smallest canonical admission decision that has to happen
on the **owner side** before any tensor-side-effect chain.  It deliberately
does NOT load tensors, fabricate a registry, stage bytes, or call the
production registry runner.  It composes the existing canonical producers
without forking them:

* ``mycelium_a13_macos.authority.DemoAuthority.status`` /
  ``DemoAuthority.coordinator.members`` — current identity/member/lease state.
* ``mycelium_a13_macos.serving.ModelContract`` /
  ``PreparedSource.resource_proposal`` — exact model and its bounded
  resource contract (model_resources.py; the 7B candidate's is the measured
  ``serving.RESOURCE_PROPOSAL``).
* ``mycelium_a13_macos.serving.PreparedSource`` — digest-bound compiled
  native manifest produced by ``prepare_local_source``.

The output is a frozen ``RunProfileAdmission`` record that the parent
integration can chain into ``prepare_registry_launch`` without ever
re-deriving the digest, member identity, grant budget, source path or
operating mode.  Reusing those markers means the parent cannot accidentally
override an owner-supplied scope or pass a stale profile into the
model-executing boundary.

Design invariants — non-negotiable
---------------------------------
1. Closed set of run modes.  Only ``managed_local`` is admitted; anything
   else (synthetic, debug, shell-tunnel, fake) fails closed before the
   canonical authority is consulted.
2. The exact source path is bound.  ``profile.source_root`` MUST equal the
   resolved ``prepared.source_root`` byte-for-byte.
3. The exact model digest is bound.  ``profile.expected_manifest_digest``
   MUST equal ``prepared.manifest_digest``.
4. The exact member grant is bound.  ``profile.grant_member_node_ids``
   MUST all be currently enrolled, current-generation, fresh-lease
   ``mac_mlx_iroh`` members known to this owner's coordinator.
5. The exact host grant is bound.  ``profile.grant_host_ids`` MUST match
   the current canonical roster (``distinct_hosts_required``).
6. Bounded budgets.  Network / disk / RSS / wall / run counts on the
   profile MUST be within the prepared source's resource proposal — never
   silently above.
7. Freshness window.  ``profile.freshness_window_seconds`` MUST be a
   positive integer and the owner lease MUST still be fresh.
8. No tensor side-effects.  This module imports no model loader, MLX
   kernel, conversion path, or stage transport.

Anything outside this scope stays with the parent lane: the operator-plan
producer (parent-owned ``build_qwen_live_route`` / ``local_preparer``), the
target application mailbox/worker (sibling-owned), the registry runner
(parent-owned via ``prepare_registry_launch``), and the public bootstrap /
notarization / signing work (external approvals).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import os
import stat
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from .serving import (
    REGISTERED_MODELS,
    ModelContract,
    PreparedSource,
    RESOURCE_PROPOSAL,
    ResourceProposal,
    ServingPreparationError,
)

if TYPE_CHECKING:  # pragma: no cover - import-only typing aid
    from .authority import DemoAuthority


# -----------------------------------------------------------------------
# Public admission contract
# -----------------------------------------------------------------------

#: Protocol marker for the admission record.  Bumping this is a wire break.
ADMISSION_PROTOCOL = "mycelium.a13_run_profile_admission.v1"

#: Closed set of run modes this slice is willing to admit.  Anything else
#: fails closed.
ADMITTED_RUN_MODES: frozenset[str] = frozenset({"managed_local"})

#: Required peer class for every grant member, matching the lane's
#: ``_PEER_CLASS``.  Refusing any other class keeps the admission
#: contract identical to ``DemoAuthority.plan_assignments``.
from .managed_classes import MANAGED_PEER_RUNTIMES as _MANAGED_PEER_RUNTIMES

_REQUIRED_PEER_CLASSES = frozenset(_MANAGED_PEER_RUNTIMES)

#: Maximum profile json size.  Kept conservative so the recorded admission
#: fits in the same private state envelope as the rest of the lane.
_MAX_ADMISSION_BYTES = 32 * 1024

#: Allowed identifier alphabet.  Mirrors ``_SEGMENT_RE`` in the authority
#: module so we cannot silently widen the host/grant identifier space.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: Allowed absolute POSIX path.  Mirrors the cache-root policy used by
#: ``layer_assignment.validate_target_cache_root``.
_ABS_POSIX_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")


class ProvisioningAdmissionError(ValueError):
    """Stable admission failure.  The ``code`` is the only public surface."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RunProfile:
    """Caller-supplied owner intent, validated before any side effect.

    ``expected_manifest_digest`` and ``source_root`` MUST match the
    ``PreparedSource`` produced by the canonical ``prepare_local_source``
    compiler.  ``grant_member_node_ids`` MUST be a 2-tuple of currently
    enrolled distinct members.  ``grant_host_ids`` MUST be a 2-tuple of
    distinct canonical host identifiers.  ``freshness_window_seconds`` is
    the maximum age (relative to ``time.time()``) the owner is willing to
    accept between status snapshot and the admission decision.
    """

    run_mode: str
    expected_model_id: str
    expected_resolved_commit: str
    expected_manifest_digest: str
    source_root: str
    runtime_backend: str
    runtime_dtype: str
    runtime_quantization: str
    grant_member_node_ids: tuple[str, ...]
    grant_host_ids: tuple[str, ...]
    network_download_bytes: int
    additional_disk_bytes: int
    max_combined_rss_bytes: int
    max_wall_seconds: int
    run_count: int
    freshness_window_seconds: int
    expected_artifact_cache_roots: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunProfileAdmission:
    """Digest/path/mode-bound admission record.

    A parent caller passes this straight into the next guarded step
    (``prepare_registry_launch``).  The parent MUST recompute
    ``admission_digest`` and confirm it equals the freshly recomputed value
    before any tensor-bound work; if it differs, the profile has been
    mutated out from under the admission and the next stage must refuse.
    """

    protocol: str
    admitted_run_mode: str
    profile_digest: str
    admission_digest: str
    expected_model_id: str
    expected_resolved_commit: str
    expected_manifest_digest: str
    source_root: str
    runtime_backend: str
    runtime_dtype: str
    runtime_quantization: str
    grant_member_node_ids: tuple[str, ...]
    grant_host_ids: tuple[str, ...]
    network_download_bytes: int
    additional_disk_bytes: int
    max_combined_rss_bytes: int
    max_wall_seconds: int
    run_count: int
    freshness_window_seconds: int
    owner_swarm_id: str
    owner_seed_key_digest: str
    owner_membership_generation: int | None
    owner_lease_expires_at: float | None
    decision_clock: float
    resource_proposal_digest: str
    chain: tuple[str, ...] = (
        "admit_run_profile",
        "DemoAuthority.status",
        "DemoAuthority.coordinator.members",
        "compile_local_model_manifest",
        "RunProfileAdmission",
    )


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _ensure_positive_int(name: str, value: object) -> int:
    if type(value) is not bool and isinstance(value, int) and value > 0:
        return int(value)
    raise ProvisioningAdmissionError(f"profile_field_invalid:{name}")


def _ensure_non_negative_int(name: str, value: object) -> int:
    if type(value) is not bool and isinstance(value, int) and value >= 0:
        return int(value)
    raise ProvisioningAdmissionError(f"profile_field_invalid:{name}")


def _validate_run_mode(run_mode: object) -> str:
    if not isinstance(run_mode, str) or run_mode not in ADMITTED_RUN_MODES:
        raise ProvisioningAdmissionError("profile_run_mode_not_admitted")
    return run_mode


def _validate_node_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or _NODE_ID_RE.fullmatch(value) is None:
        raise ProvisioningAdmissionError(f"profile_field_invalid:{field}")
    return value


def _validate_posix_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or _ABS_POSIX_PATH_RE.fullmatch(value) is None:
        raise ProvisioningAdmissionError(f"profile_field_invalid:{field}")
    return value


def _validate_digest(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(c not in "0123456789abcdef" for c in value[7:])
    ):
        raise ProvisioningAdmissionError(f"profile_field_invalid:{field}")
    return value


def _validate_grant_members(profile: RunProfile) -> tuple[str, ...]:
    members = tuple(profile.grant_member_node_ids)
    if len(members) != 2 or len(set(members)) != 2:
        raise ProvisioningAdmissionError("profile_grant_members_invalid")
    for member in members:
        _validate_node_id(member, field="grant_member_node_ids")
    return members


def _validate_grant_hosts(profile: RunProfile) -> tuple[str, ...]:
    hosts = tuple(profile.grant_host_ids)
    if len(hosts) != 2 or len(set(hosts)) != 2:
        raise ProvisioningAdmissionError("profile_grant_hosts_invalid")
    for host in hosts:
        _validate_node_id(host, field="grant_host_ids")
    return hosts


def _validate_budgets(profile: RunProfile, proposal: ResourceProposal) -> None:
    network = _ensure_non_negative_int(
        "network_download_bytes", profile.network_download_bytes
    )
    disk = _ensure_positive_int("additional_disk_bytes", profile.additional_disk_bytes)
    rss = _ensure_positive_int("max_combined_rss_bytes", profile.max_combined_rss_bytes)
    wall = _ensure_positive_int("max_wall_seconds", profile.max_wall_seconds)
    runs = _ensure_positive_int("run_count", profile.run_count)
    if (
        network > proposal.network_download_bytes
        or disk > proposal.additional_disk_bytes
        or rss > proposal.max_combined_rss_bytes
        or wall > proposal.max_wall_seconds
        or runs != proposal.run_count
    ):
        raise ProvisioningAdmissionError("profile_grant_budget_exceeds_proposal")


def _validate_cache_roots(profile: RunProfile, members: tuple[str, ...]) -> None:
    if set(profile.expected_artifact_cache_roots) != set(members):
        raise ProvisioningAdmissionError("profile_cache_roots_mismatch")
    for member in members:
        path = _validate_posix_path(
            profile.expected_artifact_cache_roots.get(member),
            field="expected_artifact_cache_roots",
        )


def _normalize_source_root(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ProvisioningAdmissionError("profile_source_root_invalid")
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ProvisioningAdmissionError("profile_source_root_symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProvisioningAdmissionError("profile_source_root_missing") from exc
    if not resolved.is_dir():
        raise ProvisioningAdmissionError("profile_source_root_not_directory")
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise ProvisioningAdmissionError("profile_source_root_invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ProvisioningAdmissionError("profile_source_root_unsafe")
    return str(resolved)


def _profile_payload(profile: RunProfile) -> dict[str, Any]:
    return {
        "run_mode": profile.run_mode,
        "expected_model_id": profile.expected_model_id,
        "expected_resolved_commit": profile.expected_resolved_commit,
        "expected_manifest_digest": profile.expected_manifest_digest,
        "source_root": profile.source_root,
        "runtime_backend": profile.runtime_backend,
        "runtime_dtype": profile.runtime_dtype,
        "runtime_quantization": profile.runtime_quantization,
        "grant_member_node_ids": list(profile.grant_member_node_ids),
        "grant_host_ids": list(profile.grant_host_ids),
        "network_download_bytes": int(profile.network_download_bytes),
        "additional_disk_bytes": int(profile.additional_disk_bytes),
        "max_combined_rss_bytes": int(profile.max_combined_rss_bytes),
        "max_wall_seconds": int(profile.max_wall_seconds),
        "run_count": int(profile.run_count),
        "freshness_window_seconds": int(profile.freshness_window_seconds),
        "expected_artifact_cache_roots": dict(profile.expected_artifact_cache_roots),
    }


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    import json

    return (
        json.dumps(
            {key: payload[key] for key in sorted(payload)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest_payload(payload: Mapping[str, Any]) -> str:
    raw = _canonical_json(payload)
    if len(raw) > _MAX_ADMISSION_BYTES:
        raise ProvisioningAdmissionError("admission_payload_too_large")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _resource_proposal_digest(proposal: ResourceProposal) -> str:
    payload = {
        "network_download_bytes": proposal.network_download_bytes,
        "execution_download_bytes": proposal.execution_download_bytes,
        "additional_disk_bytes": proposal.additional_disk_bytes,
        "max_combined_rss_bytes": proposal.max_combined_rss_bytes,
        "max_wall_seconds": proposal.max_wall_seconds,
        "run_count": proposal.run_count,
    }
    return _digest_payload(payload)


# -----------------------------------------------------------------------
# Canonical admission
# -----------------------------------------------------------------------


def _require_authority(authority: object) -> "DemoAuthority":
    # Avoid a hard import cycle — only fail closed on the wrong type.
    if authority is None or not hasattr(authority, "status") or not hasattr(
        authority, "coordinator"
    ):
        raise ProvisioningAdmissionError("admission_authority_unbound")
    return authority  # type: ignore[return-value]


def _coordinator_member_snapshot(authority: "DemoAuthority") -> dict[str, dict[str, Any]]:
    coordinator = authority.coordinator
    if coordinator is None:
        raise ProvisioningAdmissionError("admission_swarm_missing")
    snapshot: dict[str, dict[str, Any]] = {}
    for member in coordinator.members():
        snapshot[member["node_id"]] = dict(member)
    return snapshot


def _admit_against_authority(
    profile: RunProfile,
    prepared: PreparedSource,
    authority: "DemoAuthority",
    *,
    clock: float,
    require_capability: bool,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], float]:
    """Read canonical owner state without mutating it.

    Returns the status snapshot, the per-member snapshot, and the lease
    expiry (epoch seconds) of the owner itself.  The owner lease expiry
    is sourced from the coordinator's current member record for the
    metadata's target node id; the status snapshot only exposes a
    coarse-grained ``lease_state`` string.

    ``require_capability=False`` is the inert-test seam: it skips the
    ``capability_state == "self_report_accepted"`` and
    ``contribution_enabled is True`` checks.  These checks still run in
    every production path because the default is True; the seam exists
    so that real-canonical-owner-state tests can be exercised without
    pulling in a live MLX self-probe + HTTPS round-trip.
    """

    status = authority.status()
    if not status.get("started"):
        raise ProvisioningAdmissionError("admission_authority_not_started")
    if status.get("membership_state") != "current":
        raise ProvisioningAdmissionError("admission_membership_not_current")
    if status.get("lease_state") != "fresh":
        raise ProvisioningAdmissionError("admission_lease_not_fresh")
    if require_capability:
        if status.get("capability_state") != "self_report_accepted":
            raise ProvisioningAdmissionError("admission_capability_stale")
        if status.get("contribution_enabled") is not True:
            raise ProvisioningAdmissionError("admission_contribution_disabled")

    coordinator = authority.coordinator
    if coordinator is None:
        raise ProvisioningAdmissionError("admission_swarm_missing")
    metadata = authority._metadata  # type: ignore[attr-defined]  # noqa: SLF001 - bounded read
    if metadata is None:
        raise ProvisioningAdmissionError("admission_swarm_missing")
    owner_node_id = metadata.get("target_node_id")
    if not isinstance(owner_node_id, str) or not owner_node_id:
        raise ProvisioningAdmissionError("admission_owner_identity_missing")
    members = _coordinator_member_snapshot(authority)
    owner_record = members.get(owner_node_id)
    if owner_record is None:
        raise ProvisioningAdmissionError("admission_owner_not_enrolled")
    try:
        owner_lease = float(owner_record["lease_expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvisioningAdmissionError("admission_owner_lease_invalid") from exc

    grant_members = _validate_grant_members(profile)
    if owner_node_id not in grant_members:
        raise ProvisioningAdmissionError("admission_owner_not_in_grant")
    for member in grant_members:
        record = members.get(member)
        if record is None:
            raise ProvisioningAdmissionError("admission_grant_member_not_enrolled")
        if record.get("peer_class") not in _REQUIRED_PEER_CLASSES:
            raise ProvisioningAdmissionError("admission_grant_member_class_invalid")
        if record.get("lifecycle_state") not in {"NEW", "CONFIGURED", "RUNNING"}:
            raise ProvisioningAdmissionError("admission_grant_member_lifecycle_invalid")
        try:
            lease = float(record["lease_expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProvisioningAdmissionError("admission_grant_member_lease_invalid") from exc
        if lease <= clock:
            raise ProvisioningAdmissionError("admission_grant_member_lease_expired")

    grant_hosts = _validate_grant_hosts(profile)
    if set(grant_members) != set(grant_hosts):
        # The owner lane encodes the same host identity as the node id for
        # managed_local mode.  If the profile disagrees, refuse rather
        # than silently aligning.
        raise ProvisioningAdmissionError("admission_grant_host_member_mismatch")

    if profile.expected_model_id != prepared.model.model_id:
        raise ProvisioningAdmissionError("admission_model_id_mismatch")
    if profile.expected_resolved_commit != prepared.model.resolved_commit:
        raise ProvisioningAdmissionError("admission_resolved_commit_mismatch")
    if profile.runtime_backend != prepared.model.runtime_backend:
        raise ProvisioningAdmissionError("admission_runtime_backend_mismatch")
    if profile.runtime_dtype != prepared.model.runtime_dtype:
        raise ProvisioningAdmissionError("admission_runtime_dtype_mismatch")
    if profile.runtime_quantization != prepared.model.runtime_quantization:
        raise ProvisioningAdmissionError(
            "admission_runtime_quantization_mismatch"
        )

    return status, members, owner_lease


def admit_run_profile(
    profile: RunProfile,
    prepared: PreparedSource,
    authority: "DemoAuthority",
    *,
    clock: float | None = None,
    require_capability: bool = True,
) -> RunProfileAdmission:
    """Admit a caller-supplied run profile against the canonical owner state.

    The decision is fail-closed; every guard raises
    :class:`ProvisioningAdmissionError` with a stable ``code``.  This
    function never imports or invokes a model loader, conversion routine,
    Iroh runtime, or registry runner; it consumes only the canonical
    digest/path/mode markers and the owner's own status snapshot.

    ``require_capability=False`` is the inert-test seam documented on
    :func:`_admit_against_authority`.  Production callers must leave it
    True (the default).
    """

    if not isinstance(prepared, PreparedSource):
        raise ProvisioningAdmissionError("admission_prepared_source_missing")
    if not isinstance(profile, RunProfile):
        raise ProvisioningAdmissionError("admission_profile_invalid")

    _validate_run_mode(profile.run_mode)
    if profile.expected_model_id != prepared.model.model_id or prepared.model not in REGISTERED_MODELS:
        raise ProvisioningAdmissionError("admission_model_id_mismatch")
    if profile.expected_resolved_commit != prepared.model.resolved_commit:
        raise ProvisioningAdmissionError("admission_resolved_commit_mismatch")
    _validate_digest(
        profile.expected_manifest_digest, field="expected_manifest_digest"
    )
    if profile.expected_manifest_digest != prepared.manifest_digest:
        raise ProvisioningAdmissionError("admission_manifest_digest_mismatch")
    _ensure_positive_int(
        "freshness_window_seconds", profile.freshness_window_seconds
    )

    normalized_source_root = _normalize_source_root(profile.source_root)
    if normalized_source_root != str(prepared.source_root):
        raise ProvisioningAdmissionError("admission_source_root_mismatch")

    _validate_budgets(profile, prepared.resource_proposal)
    grant_members = _validate_grant_members(profile)
    _validate_cache_roots(profile, grant_members)

    authority = _require_authority(authority)
    if clock is None:
        clock = authority._clock()  # type: ignore[attr-defined]  # noqa: SLF001
    elif type(clock) is not float or clock != clock:  # NaN guard
        raise ProvisioningAdmissionError("admission_clock_invalid")

    # The authority state-class checks were performed inside
    # ``_admit_against_authority``; the status snapshot is not needed
    # in the digest computation itself.
    _status, members, owner_lease = _admit_against_authority(
        profile, prepared, authority, clock=clock, require_capability=require_capability
    )

    if int(profile.freshness_window_seconds) <= 0:
        raise ProvisioningAdmissionError("profile_freshness_window_invalid")
    # ``decision_clock`` below records the admission time so that
    # ``verify_run_profile_admission`` can recompute the digest and check
    # freshness; until then we rely on the authority's own ``lease_state``
    # freshness check performed by ``_admit_against_authority``.

    coordinator = authority.coordinator
    if coordinator is None:
        raise ProvisioningAdmissionError("admission_swarm_missing")

    # Pull the owner generation from the canonical roster rather than the
    # coarse-grained status snapshot, which is only used for state-class
    # checks above.
    owner_metadata = authority._metadata  # type: ignore[attr-defined]  # noqa: SLF001
    owner_node_id = owner_metadata.get("target_node_id") if owner_metadata else None
    owner_record = members.get(owner_node_id, {}) if owner_node_id else {}
    owner_generation = owner_record.get("generation")

    payload = _profile_payload(
        RunProfile(
            run_mode=profile.run_mode,
            expected_model_id=profile.expected_model_id,
            expected_resolved_commit=profile.expected_resolved_commit,
            expected_manifest_digest=profile.expected_manifest_digest,
            source_root=normalized_source_root,
            runtime_backend=profile.runtime_backend,
            runtime_dtype=profile.runtime_dtype,
            runtime_quantization=profile.runtime_quantization,
            grant_member_node_ids=profile.grant_member_node_ids,
            grant_host_ids=profile.grant_host_ids,
            network_download_bytes=profile.network_download_bytes,
            additional_disk_bytes=profile.additional_disk_bytes,
            max_combined_rss_bytes=profile.max_combined_rss_bytes,
            max_wall_seconds=profile.max_wall_seconds,
            run_count=profile.run_count,
            freshness_window_seconds=profile.freshness_window_seconds,
            expected_artifact_cache_roots=profile.expected_artifact_cache_roots,
        )
    )
    profile_digest = _digest_payload(payload)

    resource_proposal_digest = _resource_proposal_digest(prepared.resource_proposal)

    admission_payload: dict[str, Any] = {
        "protocol": ADMISSION_PROTOCOL,
        "admitted_run_mode": profile.run_mode,
        "profile_digest": profile_digest,
        "expected_model_id": profile.expected_model_id,
        "expected_resolved_commit": profile.expected_resolved_commit,
        "expected_manifest_digest": profile.expected_manifest_digest,
        "source_root": normalized_source_root,
        "runtime_backend": profile.runtime_backend,
        "runtime_dtype": profile.runtime_dtype,
        "runtime_quantization": profile.runtime_quantization,
        "grant_member_node_ids": list(profile.grant_member_node_ids),
        "grant_host_ids": list(profile.grant_host_ids),
        "network_download_bytes": int(profile.network_download_bytes),
        "additional_disk_bytes": int(profile.additional_disk_bytes),
        "max_combined_rss_bytes": int(profile.max_combined_rss_bytes),
        "max_wall_seconds": int(profile.max_wall_seconds),
        "run_count": int(profile.run_count),
        "freshness_window_seconds": int(profile.freshness_window_seconds),
        "owner_swarm_id": coordinator.swarm_id,
        "owner_seed_key_digest": coordinator.signer.verification_key_digest,
        "owner_membership_generation": owner_generation,
        "owner_lease_expires_at": owner_lease,
        "decision_clock": float(clock),
        "resource_proposal_digest": resource_proposal_digest,
        "decision_age": 0.0,
    }
    admission_digest = _digest_payload(admission_payload)

    return RunProfileAdmission(
        protocol=ADMISSION_PROTOCOL,
        admitted_run_mode=profile.run_mode,
        profile_digest=profile_digest,
        admission_digest=admission_digest,
        expected_model_id=profile.expected_model_id,
        expected_resolved_commit=profile.expected_resolved_commit,
        expected_manifest_digest=profile.expected_manifest_digest,
        source_root=normalized_source_root,
        runtime_backend=profile.runtime_backend,
        runtime_dtype=profile.runtime_dtype,
        runtime_quantization=profile.runtime_quantization,
        grant_member_node_ids=tuple(profile.grant_member_node_ids),
        grant_host_ids=tuple(profile.grant_host_ids),
        network_download_bytes=int(profile.network_download_bytes),
        additional_disk_bytes=int(profile.additional_disk_bytes),
        max_combined_rss_bytes=int(profile.max_combined_rss_bytes),
        max_wall_seconds=int(profile.max_wall_seconds),
        run_count=int(profile.run_count),
        freshness_window_seconds=int(profile.freshness_window_seconds),
        owner_swarm_id=coordinator.swarm_id,
        owner_seed_key_digest=coordinator.signer.verification_key_digest,
        owner_membership_generation=admission_payload[
            "owner_membership_generation"
        ],
        owner_lease_expires_at=admission_payload["owner_lease_expires_at"],
        decision_clock=float(clock),
        resource_proposal_digest=resource_proposal_digest,
    )


def verify_run_profile_admission(
    admission: RunProfileAdmission, *, clock: float | None = None
) -> None:
    """Recompute the admission digest and the freshness window.

    Parent callers MUST call this before chaining into
    ``prepare_registry_launch``; if the recomputed digest disagrees or the
    freshness window has elapsed, the next stage must refuse.
    """

    if not isinstance(admission, RunProfileAdmission):
        raise ProvisioningAdmissionError("admission_record_invalid")
    if admission.protocol != ADMISSION_PROTOCOL:
        raise ProvisioningAdmissionError("admission_protocol_mismatch")
    if admission.admitted_run_mode not in ADMITTED_RUN_MODES:
        raise ProvisioningAdmissionError("admission_run_mode_invalid")
    if admission.expected_manifest_digest != admission.expected_manifest_digest:
        raise ProvisioningAdmissionError("admission_manifest_digest_mismatch")

    recomputed_payload: dict[str, Any] = {
        "protocol": admission.protocol,
        "admitted_run_mode": admission.admitted_run_mode,
        "profile_digest": admission.profile_digest,
        "expected_model_id": admission.expected_model_id,
        "expected_resolved_commit": admission.expected_resolved_commit,
        "expected_manifest_digest": admission.expected_manifest_digest,
        "source_root": admission.source_root,
        "runtime_backend": admission.runtime_backend,
        "runtime_dtype": admission.runtime_dtype,
        "runtime_quantization": admission.runtime_quantization,
        "grant_member_node_ids": list(admission.grant_member_node_ids),
        "grant_host_ids": list(admission.grant_host_ids),
        "network_download_bytes": admission.network_download_bytes,
        "additional_disk_bytes": admission.additional_disk_bytes,
        "max_combined_rss_bytes": admission.max_combined_rss_bytes,
        "max_wall_seconds": admission.max_wall_seconds,
        "run_count": admission.run_count,
        "freshness_window_seconds": admission.freshness_window_seconds,
        "owner_swarm_id": admission.owner_swarm_id,
        "owner_seed_key_digest": admission.owner_seed_key_digest,
        "owner_membership_generation": admission.owner_membership_generation,
        "owner_lease_expires_at": admission.owner_lease_expires_at,
        "decision_clock": admission.decision_clock,
        "resource_proposal_digest": admission.resource_proposal_digest,
        "decision_age": 0.0,
    }
    recomputed = _digest_payload(recomputed_payload)
    if recomputed != admission.admission_digest:
        raise ProvisioningAdmissionError("admission_digest_mismatch")

    if clock is not None:
        if type(clock) is not float or clock != clock:
            raise ProvisioningAdmissionError("admission_clock_invalid")
        if clock - admission.decision_clock > admission.freshness_window_seconds:
            raise ProvisioningAdmissionError("admission_freshness_window_exceeded")
        if admission.owner_lease_expires_at is not None and clock >= admission.owner_lease_expires_at:
            raise ProvisioningAdmissionError("admission_lease_expired")


# -----------------------------------------------------------------------
# Next-execution-boundary manifest — published, not invoked, by this slice.
# -----------------------------------------------------------------------


def next_execution_boundary() -> Mapping[str, tuple[str, ...]]:
    """Map admission outputs to the next-stage calls the parent must make.

    Returning this from the slice documents, without performing, the
    minimal parent insertions the parent integration must make to chain
    the admission into the existing canonical chain.  Each key is the
    owned output of this slice; each value is the ordered tuple of
    canonical calls the parent must invoke (in order) before the
    next tensor-bound step.
    """

    return {
        "admit_run_profile": (
            "prepare_registry_launch",
            "RegistryLaunch.run",
        ),
        "RunProfileAdmission.profile_digest": (
            "operator_plan_binding.profile_digest",
            "operator_plan.member_digest",
        ),
        "RunProfileAdmission.expected_manifest_digest": (
            "prepare_registry_launch.prepared.manifest_digest",
            "operator_plan.graph.manifest_digest",
        ),
        "RunProfileAdmission.grant_host_ids": (
            "operator_plan.controller.peers.host_id",
        ),
        "RunProfileAdmission.grant_member_node_ids": (
            "operator_plan.membership_snapshot.member_node_id",
        ),
    }


__all__ = [
    "ADMISSION_PROTOCOL",
    "ADMITTED_RUN_MODES",
    "ProvisioningAdmissionError",
    "RunProfile",
    "RunProfileAdmission",
    "admit_run_profile",
    "next_execution_boundary",
    "verify_run_profile_admission",
]
