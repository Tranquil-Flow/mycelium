"""Operator-side physical candidate construction from a frozen preparation authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import uuid

from mycelium_model_catalog import scan_huggingface_cache
from mycelium_swarm_artifacts import (
    POLICY_PROTOCOL,
    SwarmArtifactContractError,
    canonical_digest,
    validate_acquisition_status,
)

from .artifact_provisioner import (
    ArtifactAcquisitionStore,
    ArtifactProvisioningError,
    SwarmArtifactProvisioner,
)
from .preparation import ModelPreparationError, PreparationResult
from .preparation import AUTHORIZATION_PROTOCOL, LEGACY_AUTHORIZATION_PROTOCOL
from .stage_pack_builder import StagePackBuildError, build_stage_pack_source


_STAGE_PACK_MIN_AUTHORITY_TTL_MS = 15 * 60 * 1_000
_STAGE_PACK_MAX_AUTHORITY_TTL_MS = 6 * 60 * 60 * 1_000
_STAGE_PACK_BASE_CHUNK_BYTES = 4 * 1024 * 1024
_STAGE_PACK_MAX_CHUNK_BYTES = 64 * 1024 * 1024
_STAGE_PACK_TARGET_MAX_CHUNKS = 1_024
_STORAGE_LINK_PROBE_BYTES = 8 * 1024 * 1024
_STORAGE_LINK_BLOCK_BYTES = 1024 * 1024
_STORAGE_WORK_PASSES = 8
_STORAGE_PREFLIGHT_PROTOCOL = "mycelium.private_storage_link_preflight.v1"
_ARTIFACT_DISK_RESERVE_BYTES = 1_073_741_824
_CHECKPOINT_PROTOCOL = "mycelium.private_model_preparation_checkpoint.v1"
_RECOVERY_AUTHORIZATION_PROTOCOL = (
    "mycelium.private_model_preparation_recovery_authorization.v1"
)
_CHECKPOINT_PHASES = (
    "authority_frozen",
    "candidate_challenged",
    "seed_bound",
    "artifacts_acquired",
    "peers_staged",
    "candidate_published",
)
_REPRESENTATION_AUTHORITY_FIELDS = (
    "model_id",
    "revision",
    "representation_digest",
    "source_quantization",
    "serving_dtype",
    "serving_quantization",
    "conversion_authorized",
    "owner_decision_digest",
    "download_authorized",
)
_V2_REPRESENTATION_AUTHORITY_FIELDS = (
    "source_artifact_digest",
    "quantizer",
)


def _representation_authority_fields(
    authorization: Mapping[str, Any],
) -> tuple[str, ...]:
    if authorization.get("protocol") == AUTHORIZATION_PROTOCOL:
        return (
            *_REPRESENTATION_AUTHORITY_FIELDS,
            *_V2_REPRESENTATION_AUTHORITY_FIELDS,
        )
    return _REPRESENTATION_AUTHORITY_FIELDS


def _validate_preparation_authorization(
    bundle: Path, authorization: Mapping[str, Any]
) -> None:
    if authorization.get("protocol") not in {
        AUTHORIZATION_PROTOCOL,
        LEGACY_AUTHORIZATION_PROTOCOL,
    }:
        raise ModelPreparationError("model_preparation_authorization_invalid")
    feasibility_digest = authorization.get("feasibility_digest")
    owner_decision_digest = authorization.get("owner_decision_digest")
    representation_digest = authorization.get("representation_digest")
    binding = {
        "feasibility_digest": feasibility_digest,
        "owner_decision_digest": owner_decision_digest,
        "representation_digest": representation_digest,
    }
    if authorization.get("protocol") == AUTHORIZATION_PROTOCOL:
        source_artifact_digest = authorization.get("source_artifact_digest")
        quantizer = authorization.get("quantizer")
        if (
            not isinstance(source_artifact_digest, str)
            or not isinstance(quantizer, str)
            or not quantizer
        ):
            raise ModelPreparationError("model_preparation_authorization_invalid")
        binding.update(
            {
                "source_artifact_digest": source_artifact_digest,
                "quantizer": quantizer,
            }
        )
    expected_binding = canonical_digest(binding)
    if authorization.get("preparation_binding_digest") != expected_binding:
        raise ModelPreparationError("model_preparation_authorization_invalid")
    try:
        frozen = json.loads(
            (bundle / "control" / "model-preparation-authorization.json").read_text(
                "utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelPreparationError("model_preparation_authorization_invalid") from exc
    if not isinstance(frozen, Mapping):
        raise ModelPreparationError("model_preparation_authorization_invalid")
    if authorization.get("protocol") != frozen.get("protocol") or any(
        authorization.get(field) != frozen.get(field)
        for field in _representation_authority_fields(authorization)
    ):
        raise ModelPreparationError("model_representation_drift")
    if authorization.get("stages") != frozen.get("stages"):
        raise ModelPreparationError("model_assignment_drift")


@dataclass(frozen=True)
class MemberStagePackPromotion:
    """One recipient-owned promoted pack returned by a member executor."""

    member_id: str
    files_root: str
    status: Mapping[str, Any]


MemberStagePackAcquirer = Callable[..., MemberStagePackPromotion]


def _bind_member_promotions(
    controller: dict[str, Any],
    promotions: list[tuple[dict[str, Any], MemberStagePackPromotion]],
) -> None:
    """Omit member-promoted bytes from archives and bind remote verification."""

    try:
        peers = {peer["node_id"]: peer for peer in controller["peers"]}
        transfer_records = {
            record["path"]: record
            for record in controller["transfer_manifest"]["files"]
        }
        manifests = controller["node_transfer_manifests"]["manifests"]
    except (KeyError, TypeError) as exc:
        raise ModelPreparationError(
            "member_artifact_controller_binding_invalid"
        ) from exc
    if set(manifests) != set(peers) or len(promotions) != len(peers):
        raise ModelPreparationError("member_artifact_controller_binding_invalid")
    members: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in peers}
    seen_members: set[str] = set()
    for manifest, promotion in promotions:
        member_id = promotion.member_id
        if (
            member_id in seen_members
            or member_id not in peers
            or manifest.get("recipient_member_id") != member_id
        ):
            raise ModelPreparationError("member_artifact_recipient_binding_invalid")
        seen_members.add(member_id)
        root = PurePosixPath(promotion.files_root)
        if (
            not root.is_absolute()
            or str(root) != promotion.files_root
            or any(part in {"", ".", ".."} for part in root.parts)
        ):
            raise ModelPreparationError("member_artifact_promotion_path_invalid")
        try:
            status = validate_acquisition_status(promotion.status)
        except SwarmArtifactContractError as exc:
            raise ModelPreparationError(exc.code) from exc
        if (
            status["state"] != "ready"
            or status["manifest_digest"] != manifest.get("manifest_digest")
            or status["assignment_digest"] != manifest.get("assignment_digest")
            or status["representation_digest"] != manifest.get("representation_digest")
            or status["promotion_digest"] is None
        ):
            raise ModelPreparationError("member_artifact_promotion_binding_invalid")
        destinations: set[str] = set()
        for file_record in manifest["files"]:
            destination = file_record["relative_path"]
            base = transfer_records.get(destination)
            if (
                not isinstance(base, Mapping)
                or base.get("size_bytes") != file_record["size_bytes"]
                or base.get("content_digest") != file_record["content_digest"]
            ):
                raise ModelPreparationError(
                    "member_artifact_controller_binding_invalid"
                )
            destinations.add(destination)
            source = root.joinpath(*PurePosixPath(destination).parts)
            members[member_id].append(
                {
                    "destination_path": destination,
                    "source_path": str(source),
                    "size_bytes": file_record["size_bytes"],
                    "content_digest": file_record["content_digest"],
                }
            )
        members[member_id].sort(key=lambda record: record["destination_path"])
        node_manifest = manifests[member_id]
        node_manifest["files"] = [
            record
            for record in node_manifest["files"]
            if record["path"] not in destinations
        ]
        if not any(
            record["path"] == "physical_inference_node.py"
            for record in node_manifest["files"]
        ):
            raise ModelPreparationError("member_artifact_controller_binding_invalid")
    if seen_members != set(peers):
        raise ModelPreparationError("member_artifact_controller_binding_invalid")
    controller["prepositioned_artifacts"] = {
        "protocol": "mycelium.controller_prepositioned_artifacts.v1",
        "members": members,
    }


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _file_digest(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise ModelPreparationError("model_preparation_checkpoint_invalid") from exc


def _write_durable_private_document(path: Path, document: object) -> None:
    """Atomically persist one private phase boundary, including its directory entry."""

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ModelPreparationError(
            "model_preparation_checkpoint_write_failed"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_private_document(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelPreparationError("model_preparation_checkpoint_invalid") from exc
    if not isinstance(document, dict):
        raise ModelPreparationError("model_preparation_checkpoint_invalid")
    return document


def _measure_storage_link(
    root: Path,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Measure durable write and read throughput on the bound artifact volume."""

    probe = root / f".storage-link-probe-{uuid.uuid4().hex}.tmp"
    block = b"\0" * _STORAGE_LINK_BLOCK_BYTES
    try:
        write_started = monotonic()
        descriptor = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            remaining = _STORAGE_LINK_PROBE_BYTES
            while remaining:
                payload = block[: min(len(block), remaining)]
                handle.write(payload)
                remaining -= len(payload)
            handle.flush()
            os.fsync(handle.fileno())
        write_seconds = monotonic() - write_started

        read_started = monotonic()
        read_bytes = 0
        with probe.open("rb") as handle:
            while payload := handle.read(_STORAGE_LINK_BLOCK_BYTES):
                read_bytes += len(payload)
        read_seconds = monotonic() - read_started
        if (
            read_bytes != _STORAGE_LINK_PROBE_BYTES
            or write_seconds <= 0
            or read_seconds <= 0
        ):
            raise ModelPreparationError("storage_link_probe_invalid")
        return {
            "probe_bytes": _STORAGE_LINK_PROBE_BYTES,
            "write_bytes_per_second": max(
                1, int(_STORAGE_LINK_PROBE_BYTES / write_seconds)
            ),
            "read_bytes_per_second": max(
                1, int(_STORAGE_LINK_PROBE_BYTES / read_seconds)
            ),
        }
    except ModelPreparationError:
        raise
    except OSError as exc:
        raise ModelPreparationError("storage_link_probe_failed") from exc
    finally:
        probe.unlink(missing_ok=True)


def _resumable_authority(authorization: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "protocol",
        "model_id",
        "revision",
        "catalog_generation",
        "operation_digest",
        "feasibility_digest",
        "representation_digest",
        "source_quantization",
        "serving_dtype",
        "serving_quantization",
        "conversion_authorized",
        "owner_decision_digest",
        "preparation_binding_digest",
        "evidence_generation",
        "stages",
        "download_authorized",
    )
    document = {field: authorization.get(field) for field in fields}
    if authorization.get("protocol") == AUTHORIZATION_PROTOCOL:
        document.update(
            {
                "source_artifact_digest": authorization.get("source_artifact_digest"),
                "quantizer": authorization.get("quantizer"),
            }
        )
    return document


def _resumable_authority_digest(authorization: Mapping[str, Any]) -> str:
    return canonical_digest(_resumable_authority(authorization))


def _exact_candidate_authority_matches(
    frozen: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """Return whether fresh eligibility names the exact challenged candidate."""

    return (
        frozen.get("protocol") == AUTHORIZATION_PROTOCOL
        and current.get("protocol") == AUTHORIZATION_PROTOCOL
        and all(
            frozen.get(field) == current.get(field)
            for field in (
                *_REPRESENTATION_AUTHORITY_FIELDS,
                *_V2_REPRESENTATION_AUTHORITY_FIELDS,
            )
        )
        and frozen.get("stages") == current.get("stages")
    )


def _authorization_started_within_lease(authorization: Mapping[str, Any]) -> bool:
    authorized_at = authorization.get("authorized_at_unix_ms")
    valid_until = authorization.get("evidence_valid_until_unix_ms")
    return (
        authorization.get("protocol") == AUTHORIZATION_PROTOCOL
        and type(authorized_at) is int
        and type(valid_until) is int
        and authorized_at > 0
        and authorized_at <= valid_until
    )


def _new_attempt_identity(revision: str) -> tuple[str, str]:
    token = uuid.uuid4().hex[:12]
    return f"{revision[:12]}-{token}", f"modelprep-{token}"


def _private_directory(path: Path, code: str, *, create: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ModelPreparationError(code)
    if create:
        candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ModelPreparationError(code) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ModelPreparationError(code)
    return candidate


def _directory_identity(path: Path) -> tuple[int, int] | None:
    """Return a stable local binding for one already-validated directory."""

    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return None
    return metadata.st_dev, metadata.st_ino


def _topology_from_template(
    template: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    route_label: str,
) -> dict[str, Any]:
    try:
        controller = template["controller"]
        source_root = Path(controller["source_root"])
        peers = {item["node_id"]: item for item in controller["peers"]}
        runtime_nodes = {
            item["node_id"]: item for item in controller["run_plan"]["nodes"]
        }
        endpoint_ids: dict[str, str] = {}
        membership_generations: dict[str, int] = {}
        for offer in controller["membership_snapshot"]["assignment_offers"]:
            message = offer["message"]
            recipient_id = message["recipient_node_id"]
            generation = message["generation"]
            if type(generation) is not int or generation <= 0:
                raise ModelPreparationError("model_preparation_template_invalid")
            prior = membership_generations.setdefault(recipient_id, generation)
            if prior != generation:
                raise ModelPreparationError("model_preparation_template_invalid")
            endpoint_ids[recipient_id] = message.get("recipient_endpoint_id", "")
            for record in message["peer_endpoint_records"]:
                endpoint_ids[record["node_id"]] = record["endpoint_id"]
                peer_generation = record["membership_generation"]
                if type(peer_generation) is not int or peer_generation <= 0:
                    raise ModelPreparationError("model_preparation_template_invalid")
                prior = membership_generations.setdefault(
                    record["node_id"], peer_generation
                )
                if prior != peer_generation:
                    raise ModelPreparationError("model_preparation_template_invalid")
        nodes = []
        for stage in authorization["stages"]:
            node_id = stage["node_id"]
            peer = peers[node_id]
            runtime = runtime_nodes[node_id]
            assignment = json.loads(
                (source_root / runtime["configure"]["assignment_file"]).read_text(
                    "utf-8"
                )
            )
            endpoint_id = endpoint_ids.get(node_id)
            if not endpoint_id:
                endpoint_id = next(
                    record["endpoint_id"]
                    for offer in controller["membership_snapshot"]["assignment_offers"]
                    for record in offer["message"]["peer_endpoint_records"]
                    if record["node_id"] == node_id
                )
            backend = assignment["runtime"]["backend"]
            if backend != stage["backend"]:
                raise ModelPreparationError("model_preparation_backend_changed")
            nodes.append(
                {
                    "node_id": node_id,
                    "process_transport": peer["process_transport"],
                    "ssh_target": peer["ssh_target"],
                    "ssh_identity_file": peer.get("ssh_identity_file"),
                    "staging_root": (
                        str(peer["staging_root"]) + f"-candidate-{route_label}"
                    ),
                    "python_executable": runtime["python_executable"],
                    "sidecar_binary": runtime["sidecar_binary"],
                    "endpoint_secret_file": runtime["endpoint_secret_file"],
                    "endpoint_id": endpoint_id,
                    "membership_generation": membership_generations[node_id],
                    "runtime_backend": backend,
                }
            )
    except ModelPreparationError:
        raise
    except (KeyError, OSError, TypeError, ValueError, StopIteration) as exc:
        raise ModelPreparationError("model_preparation_template_invalid") from exc
    return {
        "protocol": "mycelium.qwen_live_topology.v1",
        "placement_order_authority": "model_feasibility",
        "nodes": nodes,
    }


def _topology_from_operation_file(
    document: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    route_label: str,
) -> dict[str, Any]:
    """Bind an explicit enrolled execution topology to the planner's stage order."""

    values = document.get("nodes")
    stages = authorization.get("stages")
    if (
        document.get("protocol") != "mycelium.qwen_live_topology.v1"
        or not isinstance(values, list)
        or not isinstance(stages, list)
        or len(values) != len(stages)
        or len(values) < 2
    ):
        raise ModelPreparationError("model_preparation_topology_invalid")
    nodes: list[dict[str, Any]] = []
    for value, stage in zip(values, stages, strict=True):
        if not isinstance(value, Mapping) or not isinstance(stage, Mapping):
            raise ModelPreparationError("model_preparation_topology_invalid")
        required = {
            "node_id",
            "process_transport",
            "ssh_target",
            "staging_root",
            "python_executable",
            "sidecar_binary",
            "endpoint_secret_file",
            "endpoint_id",
            "membership_generation",
            "runtime_backend",
        }
        if not required <= set(value):
            raise ModelPreparationError("model_preparation_topology_invalid")
        node_id = value.get("node_id")
        backend = value.get("runtime_backend")
        staging_root = value.get("staging_root")
        if (
            stage.get("node_id") != node_id
            or stage.get("backend") != backend
            or not isinstance(staging_root, str)
            or not staging_root.startswith("/")
            or value.get("process_transport") not in {"local", "ssh"}
            or type(value.get("membership_generation")) is not int
            or value["membership_generation"] <= 0
        ):
            raise ModelPreparationError("model_preparation_topology_invalid")
        node = dict(value)
        node["staging_root"] = staging_root + f"-candidate-{route_label}"
        nodes.append(node)
    if sum(node["process_transport"] == "local" for node in nodes) != 1:
        raise ModelPreparationError("model_preparation_topology_invalid")
    return {
        "protocol": "mycelium.qwen_live_topology.v1",
        "placement_order_authority": "m14_measured_cycle",
        "nodes": nodes,
    }


class LocalCandidatePreparer:
    """Build, verify, stage, and atomically publish one local model candidate."""

    def __init__(
        self,
        *,
        repo_root: Path,
        cache_root: Path,
        template_plan: Path,
        execution_topology_plan: Path | None = None,
        workspace_root: Path,
        temporary_root: Path | None = None,
        candidate_root: Path,
        seed_state_root: Path,
        artifact_store: ArtifactAcquisitionStore,
        member_stage_pack_acquirer: MemberStagePackAcquirer | None = None,
        artifact_transfer_bytes_per_second: int | None = None,
        python_executable: str = sys.executable,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock_unix_ms: Callable[[], int] | None = None,
        storage_link_probe: Callable[[Path], Mapping[str, int]] | None = None,
    ) -> None:
        self._repo = Path(repo_root).resolve(strict=True)
        self._cache = Path(cache_root).resolve(strict=True)
        self._template = Path(template_plan).resolve(strict=True)
        self._execution_topology = (
            None
            if execution_topology_plan is None
            else Path(execution_topology_plan).resolve(strict=True)
        )
        self._workspace = _private_directory(
            workspace_root, "model_preparation_root_unsafe", create=True
        )
        self._workspace_identity = _directory_identity(self._workspace)
        self._temporary_root = (
            None
            if temporary_root is None
            else _private_directory(
                temporary_root,
                "model_preparation_temporary_root_unsafe",
                create=True,
            )
        )
        self._temporary_root_identity = (
            None
            if self._temporary_root is None
            else _directory_identity(self._temporary_root)
        )
        self._candidates = _private_directory(
            candidate_root, "candidate_plan_root_unsafe"
        )
        self._seed_state_root = _private_directory(
            seed_state_root, "live_seed_root_unsafe"
        )
        self._artifact_store = artifact_store
        self._member_stage_pack_acquirer = member_stage_pack_acquirer
        if artifact_transfer_bytes_per_second is not None and (
            type(artifact_transfer_bytes_per_second) is not int
            or not 1 <= artifact_transfer_bytes_per_second <= 1_000_000_000
        ):
            raise ModelPreparationError("artifact_transfer_rate_invalid")
        self._artifact_transfer_rate_cap = artifact_transfer_bytes_per_second
        self._storage_link_probe = storage_link_probe or _measure_storage_link
        self._python = python_executable
        self._run = run
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))

    def _require_bound_storage(self) -> None:
        """Fail closed if removable preparation storage disappeared or changed."""

        temporary_root = getattr(self, "_temporary_root", None)
        workspace_identity = getattr(
            self,
            "_workspace_identity",
            _directory_identity(self._workspace),
        )
        temporary_identity = getattr(
            self,
            "_temporary_root_identity",
            (None if temporary_root is None else _directory_identity(temporary_root)),
        )
        if (
            workspace_identity is None
            or _directory_identity(self._workspace) != workspace_identity
            or (
                temporary_root is not None
                and (
                    temporary_identity is None
                    or _directory_identity(temporary_root) != temporary_identity
                )
            )
        ):
            raise ModelPreparationError("model_preparation_workspace_unavailable")

    def _checkpoint_base(
        self,
        *,
        operation: str,
        route_label: str,
        authorization: Mapping[str, Any],
        topology_digest: str,
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_identity = getattr(
            self, "_workspace_identity", _directory_identity(self._workspace)
        )
        if workspace_identity is None:
            raise ModelPreparationError("model_preparation_workspace_unavailable")
        return {
            "protocol": _CHECKPOINT_PROTOCOL,
            "operation": operation,
            "route_label": route_label,
            "authority_digest": _resumable_authority_digest(authorization),
            "workspace_identity": {
                "device": workspace_identity[0],
                "inode": workspace_identity[1],
            },
            "topology_digest": topology_digest,
            "candidate_id": candidate_id,
            "completed_phase": "authority_frozen",
            "phase_evidence": {},
        }

    def _write_checkpoint(
        self,
        attempt: Path,
        checkpoint: Mapping[str, Any],
        phase: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require_bound_storage()
        if phase not in _CHECKPOINT_PHASES:
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        prior_phase = checkpoint.get("completed_phase")
        if prior_phase not in _CHECKPOINT_PHASES:
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        if _CHECKPOINT_PHASES.index(phase) < _CHECKPOINT_PHASES.index(prior_phase):
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        phase_evidence = checkpoint.get("phase_evidence")
        if not isinstance(phase_evidence, Mapping):
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        updated = {
            **checkpoint,
            "completed_phase": phase,
            "phase_evidence": {**phase_evidence, phase: dict(evidence)},
        }
        _write_durable_private_document(attempt / "phase-checkpoint.json", updated)
        self._require_bound_storage()
        return updated

    def _validate_checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        *,
        operation: str,
        authorization: Mapping[str, Any],
        candidate_id: str | None = None,
    ) -> None:
        identity = checkpoint.get("workspace_identity")
        if (
            checkpoint.get("protocol") != _CHECKPOINT_PROTOCOL
            or checkpoint.get("operation") != operation
            or checkpoint.get("authority_digest")
            != _resumable_authority_digest(authorization)
            or checkpoint.get("completed_phase") not in _CHECKPOINT_PHASES
            or not isinstance(checkpoint.get("route_label"), str)
            or not checkpoint["route_label"].startswith("modelprep-")
            or not isinstance(identity, Mapping)
            or (identity.get("device"), identity.get("inode"))
            != getattr(
                self, "_workspace_identity", _directory_identity(self._workspace)
            )
            or checkpoint.get("candidate_id") != candidate_id
            or not isinstance(checkpoint.get("phase_evidence"), Mapping)
        ):
            raise ModelPreparationError("model_preparation_checkpoint_invalid")

    def _resume_attempt(
        self,
        authorization: Mapping[str, Any],
    ) -> tuple[Path, dict[str, Any]] | None:
        expected = _resumable_authority_digest(authorization)
        candidates: list[tuple[int, Path, dict[str, Any]]] = []
        for path in self._workspace.glob("*/phase-checkpoint.json"):
            if not path.parent.name.startswith(str(authorization["revision"])[:12]):
                continue
            try:
                checkpoint = _read_private_document(path)
                if (
                    checkpoint.get("protocol") == _CHECKPOINT_PROTOCOL
                    and checkpoint.get("operation") == "prepare"
                    and checkpoint.get("authority_digest") == expected
                    and checkpoint.get("candidate_id") is None
                    and checkpoint.get("completed_phase") != "candidate_published"
                ):
                    self._validate_checkpoint(
                        checkpoint,
                        operation="prepare",
                        authorization=authorization,
                    )
                    candidates.append(
                        (path.stat().st_mtime_ns, path.parent, checkpoint)
                    )
            except (ModelPreparationError, OSError) as exc:
                raise ModelPreparationError(
                    "model_preparation_checkpoint_invalid"
                ) from exc
        if not candidates:
            return None
        _, attempt, checkpoint = max(candidates, key=lambda item: item[0])
        return attempt, checkpoint

    def _resume_warm_attempt(
        self,
        authorization: Mapping[str, Any],
        *,
        candidate_id: str,
        topology_digest: str,
    ) -> tuple[Path, dict[str, Any]] | None:
        """Recover an acquired exact-candidate attempt without new receipts."""

        expected = _resumable_authority_digest(authorization)
        candidates: list[tuple[int, Path, dict[str, Any]]] = []
        for path in self._workspace.glob("warm-*/phase-checkpoint.json"):
            try:
                checkpoint = _read_private_document(path)
                completed_phase = checkpoint.get("completed_phase")
                if (
                    checkpoint.get("protocol") != _CHECKPOINT_PROTOCOL
                    or checkpoint.get("operation") != "warm_reacquire"
                    or checkpoint.get("authority_digest") != expected
                    or checkpoint.get("candidate_id") != candidate_id
                    or checkpoint.get("topology_digest") != topology_digest
                    or completed_phase not in _CHECKPOINT_PHASES
                    or _CHECKPOINT_PHASES.index(completed_phase)
                    < _CHECKPOINT_PHASES.index("artifacts_acquired")
                ):
                    continue
                self._validate_checkpoint(
                    checkpoint,
                    operation="warm_reacquire",
                    authorization=authorization,
                    candidate_id=candidate_id,
                )
                self._checkpoint_bound_plan(
                    path.parent,
                    checkpoint,
                    phase=(
                        "candidate_published"
                        if completed_phase == "candidate_published"
                        else (
                            "peers_staged"
                            if completed_phase == "peers_staged"
                            else "artifacts_acquired"
                        )
                    ),
                )
                candidates.append((path.stat().st_mtime_ns, path.parent, checkpoint))
            except ModelPreparationError:
                raise
            except OSError as exc:
                raise ModelPreparationError(
                    "model_preparation_checkpoint_invalid"
                ) from exc
        if not candidates:
            return None
        _, attempt, checkpoint = max(candidates, key=lambda item: item[0])
        return attempt, checkpoint

    def _recover_challenged_attempt(
        self,
        authorization: Mapping[str, Any],
    ) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
        """Adopt an exact challenged build under additive fresh eligibility."""

        if not _authorization_started_within_lease(authorization):
            return None
        candidates: list[tuple[int, Path, dict[str, Any], dict[str, Any]]] = []
        for path in self._workspace.glob("*/phase-checkpoint.json"):
            if not path.parent.name.startswith(str(authorization["revision"])[:12]):
                continue
            try:
                checkpoint = _read_private_document(path)
                completed_phase = checkpoint.get("completed_phase")
                if (
                    checkpoint.get("protocol") != _CHECKPOINT_PROTOCOL
                    or checkpoint.get("operation") != "prepare"
                    or completed_phase not in _CHECKPOINT_PHASES
                    or _CHECKPOINT_PHASES.index(completed_phase)
                    < _CHECKPOINT_PHASES.index("candidate_challenged")
                    or completed_phase == "candidate_published"
                ):
                    continue
                frozen = _read_private_document(path.parent / "authorization.json")
                if not _exact_candidate_authority_matches(frozen, authorization):
                    continue
                if not _authorization_started_within_lease(frozen):
                    raise ModelPreparationError(
                        "model_preparation_recovery_authority_invalid"
                    )
                self._validate_checkpoint(
                    checkpoint,
                    operation="prepare",
                    authorization=frozen,
                )
                output_root = path.parent / "candidate"
                generated_plan_path = output_root / "operator-plan.json"
                report_path = output_root / "build-report.json"
                challenge_path = output_root / "local-challenge-checkpoint.json"
                frozen_bundle_path = (
                    output_root
                    / "transfer-bundle"
                    / "control"
                    / "model-preparation-authorization.json"
                )
                if any(
                    candidate.is_symlink() or not candidate.is_file()
                    for candidate in (
                        generated_plan_path,
                        report_path,
                        challenge_path,
                        frozen_bundle_path,
                    )
                ):
                    raise ModelPreparationError("model_preparation_checkpoint_invalid")
                report = json.loads(report_path.read_text("utf-8"))
                challenge = json.loads(challenge_path.read_text("utf-8"))
                generated_plan = json.loads(generated_plan_path.read_text("utf-8"))
                evidence = checkpoint["phase_evidence"]["candidate_challenged"]
                candidate_id = generated_plan["controller"]["run_plan"]["deployment_id"]
                expected_ranges = [
                    {
                        "start_layer": stage["start_layer"],
                        "end_layer_exclusive": stage["end_layer_exclusive"],
                        "layer_count": stage["end_layer_exclusive"]
                        - stage["start_layer"],
                    }
                    for stage in frozen["stages"]
                ]
                challenge_assignments = challenge.get("assignments")
                if (
                    not isinstance(report, dict)
                    or not isinstance(challenge, dict)
                    or not isinstance(generated_plan, dict)
                    or report.get("model_id") != frozen.get("model_id")
                    or report.get("resolved_commit") != frozen.get("revision")
                    or report.get("source_artifact_digest")
                    != frozen.get("source_artifact_digest")
                    or report.get("representation_digest")
                    != frozen.get("representation_digest")
                    or report.get("quantizer") != frozen.get("quantizer")
                    or report.get("owner_decision_digest")
                    != frozen.get("owner_decision_digest")
                    or report.get("layer_ranges") != expected_ranges
                    or challenge.get("protocol")
                    != "mycelium.local_candidate_challenge.v1"
                    or challenge.get("state") != "passed"
                    or challenge.get("deployment_id") != candidate_id
                    or challenge.get("model_id") != frozen.get("model_id")
                    or challenge.get("resolved_commit") != frozen.get("revision")
                    or challenge.get("source_artifact_digest")
                    != frozen.get("source_artifact_digest")
                    or challenge.get("representation_digest")
                    != frozen.get("representation_digest")
                    or challenge.get("preparation_binding_digest")
                    != frozen.get("preparation_binding_digest")
                    or not isinstance(challenge.get("challenge_output_token_ids"), list)
                    or len(challenge["challenge_output_token_ids"]) < 2
                    or not isinstance(challenge_assignments, list)
                    or not all(
                        isinstance(item, Mapping) for item in challenge_assignments
                    )
                    or [item.get("node_id") for item in challenge_assignments]
                    != [stage.get("node_id") for stage in frozen["stages"]]
                    or evidence.get("build_report_digest") != _file_digest(report_path)
                    or evidence.get("challenge_digest") != _file_digest(challenge_path)
                    or evidence.get("generated_plan_digest")
                    != _file_digest(generated_plan_path)
                    or evidence.get("frozen_authorization_digest")
                    != _file_digest(path.parent / "authorization.json")
                ):
                    raise ModelPreparationError("model_preparation_checkpoint_invalid")
                _validate_preparation_authorization(
                    output_root / "transfer-bundle", frozen
                )
                candidates.append(
                    (path.stat().st_mtime_ns, path.parent, checkpoint, frozen)
                )
            except ModelPreparationError:
                raise
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise ModelPreparationError(
                    "model_preparation_checkpoint_invalid"
                ) from exc
        if not candidates:
            return None
        _, attempt, checkpoint, frozen = max(candidates, key=lambda item: item[0])
        recovered_at = self._clock()
        if type(recovered_at) is not int or recovered_at <= 0:
            raise ModelPreparationError("model_preparation_clock_invalid")
        recovery = {
            "protocol": _RECOVERY_AUTHORIZATION_PROTOCOL,
            "source_authority": _resumable_authority(frozen),
            "source_authority_digest": checkpoint["authority_digest"],
            "source_authorized_at_unix_ms": frozen["authorized_at_unix_ms"],
            "source_evidence_valid_until_unix_ms": frozen[
                "evidence_valid_until_unix_ms"
            ],
            "source_preparation_binding_digest": frozen["preparation_binding_digest"],
            "source_completed_phase": checkpoint["completed_phase"],
            "recovery_authority": _resumable_authority(authorization),
            "recovery_authority_digest": _resumable_authority_digest(authorization),
            "recovery_authorized_at_unix_ms": authorization["authorized_at_unix_ms"],
            "recovery_evidence_valid_until_unix_ms": authorization[
                "evidence_valid_until_unix_ms"
            ],
            "recovered_at_unix_ms": recovered_at,
            "resume_from_phase": checkpoint["completed_phase"],
        }
        recovery_digest = canonical_digest(recovery)
        recovery_path = attempt / (
            "recovery-authorization-"
            + recovery_digest.removeprefix("sha256:")[:24]
            + ".json"
        )
        if recovery_path.exists():
            if _read_private_document(recovery_path) != recovery:
                raise ModelPreparationError(
                    "model_preparation_recovery_authority_invalid"
                )
        else:
            _write_durable_private_document(recovery_path, recovery)
        history = checkpoint.get("recovery_authorizations", [])
        if not isinstance(history, list) or not all(
            isinstance(item, str) for item in history
        ):
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        if recovery_digest not in history:
            history = [*history, recovery_digest]
        challenged_evidence = checkpoint["phase_evidence"]["candidate_challenged"]
        checkpoint = {
            **checkpoint,
            "phase_evidence": {
                **checkpoint["phase_evidence"],
                "candidate_challenged": {
                    **challenged_evidence,
                    "latest_recovery_authorization_digest": recovery_digest,
                },
            },
            "recovery_authorizations": history,
        }
        _write_durable_private_document(attempt / "phase-checkpoint.json", checkpoint)
        return attempt, checkpoint, frozen

    @staticmethod
    def _acquisition_policy(
        chunk_size_bytes: int,
        transfer_bytes_per_second: int | None = None,
    ) -> dict[str, Any]:
        per_source_rate = (
            500_000 if transfer_bytes_per_second is None else transfer_bytes_per_second
        )
        aggregate_rate = (
            1_000_000
            if transfer_bytes_per_second is None
            else transfer_bytes_per_second
        )
        return {
            "protocol": POLICY_PROTOCOL,
            "chunk_size_bytes": chunk_size_bytes,
            "maximum_sources": 3,
            "per_source_concurrency": 2,
            "aggregate_concurrency": 4,
            "maximum_retries_per_chunk": 2,
            "maximum_source_rotations": 4,
            "partial_state_ttl_seconds": 3_600,
            "disk_reserve_bytes": _ARTIFACT_DISK_RESERVE_BYTES,
            "per_source_bytes_per_second": per_source_rate,
            "aggregate_bytes_per_second": aggregate_rate,
            "serving_traffic_reserve_ratio": 0.4,
            "multi_source_threshold_bytes": 2 * chunk_size_bytes,
            "minimum_predicted_improvement_ratio": 0.2,
            "allow_redundant_hedging": False,
            "thermal_classes_allowed": ["fair", "nominal"],
            "power_classes_allowed": ["battery_ok", "external_power"],
        }

    @staticmethod
    def _stage_pack_authority_ttl_ms(
        authorization: Mapping[str, Any],
        measured_bytes_per_second: int,
        *,
        required_transfer_bytes: int | None = None,
    ) -> int:
        """Size bounded authority from measured storage throughput."""

        stages = authorization.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ModelPreparationError("model_stage_plan_invalid")
        sizes = [
            stage.get("assignment_artifact_bytes")
            for stage in stages
            if isinstance(stage, Mapping)
        ]
        if len(sizes) != len(stages) or any(
            type(size) is not int or size <= 0 for size in sizes
        ):
            raise ModelPreparationError("model_stage_plan_invalid")
        if type(measured_bytes_per_second) is not int or measured_bytes_per_second <= 0:
            raise ModelPreparationError("storage_link_probe_invalid")
        transfer_bytes = (
            sum(sizes)
            if required_transfer_bytes is None
            else required_transfer_bytes
        )
        if type(transfer_bytes) is not int or transfer_bytes < 0:
            raise ModelPreparationError("model_stage_plan_invalid")
        # Digesting, source-object materialization, verified transfer, and promoted
        # file assembly each touch the assignment bytes. The additional factor keeps
        # remote serialization and slower removable media inside a bounded grant.
        estimated_ms = (
            transfer_bytes * _STORAGE_WORK_PASSES * 1_000
            + measured_bytes_per_second
            - 1
        ) // measured_bytes_per_second
        ttl_ms = max(
            _STAGE_PACK_MIN_AUTHORITY_TTL_MS,
            estimated_ms + _STAGE_PACK_MIN_AUTHORITY_TTL_MS,
        )
        if ttl_ms > _STAGE_PACK_MAX_AUTHORITY_TTL_MS:
            raise ModelPreparationError("artifact_transfer_exceeds_maximum_lease")
        return ttl_ms

    @staticmethod
    def _stage_pack_chunk_size_bytes(
        authorization: Mapping[str, Any],
    ) -> int:
        """Keep large Merkle manifests inside the canonical evidence bound."""

        stages = authorization.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ModelPreparationError("model_stage_plan_invalid")
        sizes = [
            stage.get("assignment_artifact_bytes")
            for stage in stages
            if isinstance(stage, Mapping)
        ]
        if len(sizes) != len(stages) or any(
            type(size) is not int or size <= 0 for size in sizes
        ):
            raise ModelPreparationError("model_stage_plan_invalid")
        required = (max(sizes) + _STAGE_PACK_TARGET_MAX_CHUNKS - 1) // (
            _STAGE_PACK_TARGET_MAX_CHUNKS
        )
        chunk_size = max(
            _STAGE_PACK_BASE_CHUNK_BYTES,
            (
                (required + _STAGE_PACK_BASE_CHUNK_BYTES - 1)
                // _STAGE_PACK_BASE_CHUNK_BYTES
            )
            * _STAGE_PACK_BASE_CHUNK_BYTES,
        )
        if chunk_size > _STAGE_PACK_MAX_CHUNK_BYTES:
            raise ModelPreparationError("stage_pack_manifest_too_large")
        return chunk_size

    def _preflight_local_acquisition_storage(
        self,
        authorization: Mapping[str, Any],
        topology: Mapping[str, Any],
        *,
        attempt: Path,
    ) -> dict[str, Any]:
        """Measure and reserve storage before an expensive model scan or transfer."""

        local_nodes = {
            node.get("node_id")
            for node in topology.get("nodes", [])
            if isinstance(node, Mapping) and node.get("process_transport") == "local"
        }
        local_stages = [
            stage
            for stage in authorization.get("stages", [])
            if isinstance(stage, Mapping) and stage.get("node_id") in local_nodes
        ]
        if len(local_nodes) != 1 or len(local_stages) != 1:
            raise ModelPreparationError("model_preparation_topology_invalid")
        stage = local_stages[0]
        artifact_bytes = stage.get("assignment_artifact_bytes")
        stages = authorization.get("stages")
        sizes = (
            [
                item.get("assignment_artifact_bytes")
                for item in stages
                if isinstance(item, Mapping)
            ]
            if isinstance(stages, list)
            else []
        )
        if (
            type(artifact_bytes) is not int
            or artifact_bytes <= 0
            or not isinstance(stages, list)
            or len(sizes) != len(stages)
            or any(type(size) is not int or size <= 0 for size in sizes)
        ):
            raise ModelPreparationError("model_stage_plan_invalid")
        required_transfer_bytes = sum(sizes)
        report_path = attempt / "storage-link-preflight.json"
        observed_at = self._clock()
        report: dict[str, Any] = {
            "protocol": _STORAGE_PREFLIGHT_PROTOCOL,
            "state": "measuring",
            "phase": "storage_link_preflight",
            "summary": "Measuring the bound artifact storage link",
            "authority_digest": _resumable_authority_digest(authorization),
            "required_transfer_bytes": required_transfer_bytes,
            "required_storage_bytes": None,
            "available_storage_bytes": None,
            "probe_bytes": None,
            "measured_write_bytes_per_second": None,
            "measured_read_bytes_per_second": None,
            "measured_effective_bytes_per_second": None,
            "estimated_work_ms": None,
            "lease_ttl_ms": None,
            "lease_scope": "stage_pack_acquisition",
            "lease_expires_at_unix_ms": None,
            "observed_at_unix_ms": observed_at,
            "last_progress_at_unix_ms": observed_at,
            "blocker": None,
            "recommended_recovery": None,
        }
        _write_durable_private_document(report_path, report)
        try:
            ledger = self._artifact_store.ledger()
        except ArtifactProvisioningError as exc:
            raise ModelPreparationError(exc.code) from exc
        warm = any(
            isinstance(status, Mapping)
            and status.get("state") == "ready"
            and status.get("model_id") == authorization.get("model_id")
            and status.get("model_revision") == authorization.get("revision")
            and status.get("representation_digest")
            == authorization.get("representation_digest")
            and status.get("feasibility_digest")
            == authorization.get("feasibility_digest")
            and status.get("evidence_generation")
            == authorization.get("evidence_generation")
            and status.get("layer_start") == stage.get("start_layer")
            and status.get("layer_end_exclusive") == stage.get("end_layer_exclusive")
            and status.get("total_bytes") == artifact_bytes
            and status.get("promotion_digest") is not None
            for status in ledger.get("history", [])
        )
        if warm:
            required_transfer_bytes -= artifact_bytes
            report["required_transfer_bytes"] = required_transfer_bytes
        required = 0 if warm else artifact_bytes * 2 + _ARTIFACT_DISK_RESERVE_BYTES
        report["required_storage_bytes"] = required
        try:
            available = shutil.disk_usage(self._artifact_store.root).free
        except OSError as exc:
            raise ModelPreparationError("artifact_storage_failure") from exc
        report["available_storage_bytes"] = available
        if available < required:
            error = ModelPreparationError("insufficient_disk")
            report.update(
                state="failed",
                summary="Artifact storage capacity preflight blocked preparation",
                blocker=error.code,
                recommended_recovery=(
                    "Free space on the bound owner-approved artifact volume or "
                    "choose another approved volume."
                ),
                last_progress_at_unix_ms=max(self._clock(), observed_at),
            )
            _write_durable_private_document(report_path, report)
            raise error
        if required_transfer_bytes == 0:
            report.update(
                state="passed",
                summary="Exact promoted artifacts are available; no transfer is required",
                lease_ttl_ms=_STAGE_PACK_MIN_AUTHORITY_TTL_MS,
                last_progress_at_unix_ms=max(self._clock(), observed_at),
            )
            _write_durable_private_document(report_path, report)
            return report
        try:
            measurement = dict(self._storage_link_probe(self._artifact_store.root))
        except ModelPreparationError as exc:
            report.update(
                state="failed",
                summary="Artifact storage throughput measurement failed",
                blocker=exc.code,
                recommended_recovery=(
                    "Reconnect or repair the bound owner-approved artifact volume, "
                    "then retry preflight."
                ),
                last_progress_at_unix_ms=max(self._clock(), observed_at),
            )
            _write_durable_private_document(report_path, report)
            raise
        except (TypeError, ValueError) as exc:
            error = ModelPreparationError("storage_link_probe_invalid")
            report.update(
                state="failed",
                summary="Artifact storage throughput measurement was invalid",
                blocker=error.code,
                recommended_recovery=(
                    "Repeat the storage measurement on a stable owner-approved volume."
                ),
                last_progress_at_unix_ms=max(self._clock(), observed_at),
            )
            _write_durable_private_document(report_path, report)
            raise error from exc
        write_rate = measurement.get("write_bytes_per_second")
        read_rate = measurement.get("read_bytes_per_second")
        probe_bytes = measurement.get("probe_bytes")
        if (
            type(write_rate) is not int
            or write_rate <= 0
            or type(read_rate) is not int
            or read_rate <= 0
            or type(probe_bytes) is not int
            or probe_bytes <= 0
        ):
            error = ModelPreparationError("storage_link_probe_invalid")
            report.update(
                state="failed",
                summary="Artifact storage throughput measurement was invalid",
                blocker=error.code,
                recommended_recovery=(
                    "Repeat the storage measurement on a stable owner-approved volume."
                ),
                last_progress_at_unix_ms=max(self._clock(), observed_at),
            )
            _write_durable_private_document(report_path, report)
            raise error
        measured_rate = min(write_rate, read_rate)
        configured_cap = getattr(self, "_artifact_transfer_rate_cap", None)
        effective_rate = (
            measured_rate
            if configured_cap is None
            else min(measured_rate, configured_cap)
        )
        estimated_ms = (
            required_transfer_bytes * _STORAGE_WORK_PASSES * 1_000
            + effective_rate
            - 1
        ) // effective_rate
        report.update(
            probe_bytes=probe_bytes,
            measured_write_bytes_per_second=write_rate,
            measured_read_bytes_per_second=read_rate,
            measured_effective_bytes_per_second=effective_rate,
            estimated_work_ms=estimated_ms,
            last_progress_at_unix_ms=max(self._clock(), observed_at),
        )
        try:
            ttl_ms = self._stage_pack_authority_ttl_ms(
                authorization,
                effective_rate,
                required_transfer_bytes=required_transfer_bytes,
            )
        except ModelPreparationError as exc:
            if exc.code != "artifact_transfer_exceeds_maximum_lease":
                raise
            report.update(
                state="failed",
                summary="Measured storage throughput cannot fit the transfer in the maximum lease",
                blocker=exc.code,
                recommended_recovery=(
                    "Use a faster owner-approved storage link or reduce the "
                    "authorized assignment bytes."
                ),
            )
            _write_durable_private_document(report_path, report)
            raise
        report.update(
            state="passed",
            summary="Measured storage throughput fits the bounded acquisition lease",
            lease_ttl_ms=ttl_ms,
            blocker=None,
            recommended_recovery=None,
        )
        _write_durable_private_document(report_path, report)
        return report

    def _acquire_stage_packs(
        self,
        *,
        attempt: Path,
        authorization: Mapping[str, Any],
        plan: dict[str, Any],
        warm_only: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        controller = plan["controller"]
        bundle = Path(controller["source_root"]).resolve(strict=True)
        issued_at_unix_ms = self._clock()
        if type(issued_at_unix_ms) is not int or issued_at_unix_ms <= 0:
            raise ModelPreparationError("model_preparation_clock_invalid")
        evidence_valid_until_unix_ms = authorization.get("evidence_valid_until_unix_ms")
        authorized_at_unix_ms = authorization.get("authorized_at_unix_ms")
        if (
            type(evidence_valid_until_unix_ms) is not int
            or (
                authorization.get("protocol") == AUTHORIZATION_PROTOCOL
                and (
                    type(authorized_at_unix_ms) is not int
                    or authorized_at_unix_ms <= 0
                    or authorized_at_unix_ms > evidence_valid_until_unix_ms
                )
            )
            or (
                authorization.get("protocol") == LEGACY_AUTHORIZATION_PROTOCOL
                and evidence_valid_until_unix_ms < issued_at_unix_ms
            )
        ):
            raise ModelPreparationError("model_capacity_stale")
        if warm_only:
            measured_rate = 1_000_000
            lease_ttl_ms = _STAGE_PACK_MIN_AUTHORITY_TTL_MS
        else:
            preflight = _read_private_document(
                attempt / "storage-link-preflight.json"
            )
            measured_rate = preflight.get("measured_effective_bytes_per_second")
            lease_ttl_ms = preflight.get("lease_ttl_ms")
            required_transfer_bytes = preflight.get("required_transfer_bytes")
            if (
                preflight.get("protocol") != _STORAGE_PREFLIGHT_PROTOCOL
                or preflight.get("state") != "passed"
                or preflight.get("authority_digest")
                != _resumable_authority_digest(authorization)
                or type(measured_rate) is not int
                or measured_rate <= 0
                or type(lease_ttl_ms) is not int
                or type(required_transfer_bytes) is not int
                or required_transfer_bytes < 0
                or lease_ttl_ms
                != self._stage_pack_authority_ttl_ms(
                    authorization,
                    measured_rate,
                    required_transfer_bytes=required_transfer_bytes,
                )
            ):
                raise ModelPreparationError("storage_link_preflight_invalid")
        stage_pack_expires_at_unix_ms = issued_at_unix_ms + lease_ttl_ms
        if not warm_only:
            preflight.update(
                lease_expires_at_unix_ms=stage_pack_expires_at_unix_ms,
                last_progress_at_unix_ms=max(
                    issued_at_unix_ms,
                    int(preflight["last_progress_at_unix_ms"]),
                ),
            )
            _write_durable_private_document(
                attempt / "storage-link-preflight.json",
                preflight,
            )
        _validate_preparation_authorization(bundle, authorization)
        graph = controller["run_plan"]["nodes"][0]["configure"]["graph"]
        offers = {
            offer["message"]["assignment_id"]: offer["message"]
            for offer in controller["membership_snapshot"]["assignment_offers"]
        }
        packs: list[dict[str, Any]] = []
        for node in controller["run_plan"]["nodes"]:
            packs.append(
                json.loads(
                    (bundle / node["configure"]["stage_pack_file"]).read_text("utf-8")
                )
            )
        chunk_size = self._stage_pack_chunk_size_bytes(authorization)
        policy = self._acquisition_policy(
            chunk_size,
            measured_rate,
        )
        provisioner = SwarmArtifactProvisioner(self._artifact_store)
        peers = {peer["node_id"]: peer for peer in controller["peers"]}
        promoted: list[tuple[dict[str, Any], MemberStagePackPromotion]] = []
        # Source chunks are expensive to materialize for multi-gigabyte stages.
        # Keep one attempt-local, deterministic checkpoint so a process or storage
        # interruption can verify and reuse every completed content-addressed
        # object.  Older interrupted attempts used a random suffix; adopt exactly
        # one such private directory, but fail closed if recovery is ambiguous.
        source_root = attempt / "artifact-sources"
        if not source_root.exists():
            legacy_roots = [
                path
                for path in attempt.glob("artifact-sources-*")
                if path.name != source_root.name
            ]
            if len(legacy_roots) > 1:
                raise ModelPreparationError(
                    "stage_pack_source_checkpoint_ambiguous"
                )
            if legacy_roots:
                legacy = _private_directory(
                    legacy_roots[0], "stage_pack_source_checkpoint_invalid"
                )
                try:
                    os.replace(legacy, source_root)
                    directory = os.open(attempt, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError as exc:
                    raise ModelPreparationError(
                        "stage_pack_source_checkpoint_invalid"
                    ) from exc
            else:
                source_root.mkdir(mode=0o700)
        source_root = _private_directory(
            source_root, "stage_pack_source_checkpoint_invalid"
        )
        for pack in packs:
            offer = offers.get(pack.get("assignment_id"))
            if not isinstance(offer, Mapping):
                raise ModelPreparationError("stage_pack_assignment_offer_missing")
            stage_source = source_root / str(pack["node_id"])
            try:
                manifest, binding = build_stage_pack_source(
                    transfer_bundle=bundle,
                    pack=pack,
                    assignment_offer=offer,
                    graph=graph,
                    authorization=authorization,
                    output_root=stage_source,
                    chunk_size_bytes=chunk_size,
                    issued_at_unix_ms=issued_at_unix_ms,
                    expires_at_unix_ms=stage_pack_expires_at_unix_ms,
                    materialize_objects=not warm_only,
                    resume_existing=stage_source.exists(),
                )
                allowed = sorted(item["content_digest"] for item in manifest["chunks"])
                grant = {
                    "manifest_digest": manifest["manifest_digest"],
                    "assignment_digest": manifest["assignment_digest"],
                    "representation_digest": manifest["representation_digest"],
                    "feasibility_digest": manifest["feasibility_digest"],
                    "recipient_member_id": manifest["recipient_member_id"],
                    "recipient_membership_generation": manifest[
                        "recipient_membership_generation"
                    ],
                    "allowed_chunk_digests": allowed,
                    "authorized_source_member_ids": [],
                    "origin_fallback_allowed": not warm_only,
                    "maximum_total_bytes": manifest["total_size_bytes"],
                    "maximum_concurrency": policy["aggregate_concurrency"],
                    "maximum_bytes_per_second": policy["aggregate_bytes_per_second"],
                }

                def read_origin(
                    digest: str,
                    offset: int,
                    length: int,
                    *,
                    root: Path = stage_source / "objects",
                ):
                    with (root / digest.removeprefix("sha256:")).open("rb") as handle:
                        handle.seek(offset)
                        remaining = length
                        while remaining:
                            block = handle.read(min(4 * 1024 * 1024, remaining))
                            if not block:
                                break
                            remaining -= len(block)
                            yield block

                peer = peers.get(str(pack["node_id"]))
                if not isinstance(peer, Mapping):
                    raise ModelPreparationError("stage_pack_recipient_missing")
                if peer.get("process_transport") == "local":
                    status = provisioner.acquire(
                        manifest=manifest,
                        expected_binding=binding,
                        grant=grant,
                        advertisements=[],
                        policy=policy,
                        reader=lambda *_args, **_kwargs: (),
                        origin=None if warm_only else read_origin,
                    )
                    promotion = MemberStagePackPromotion(
                        member_id=str(pack["node_id"]),
                        files_root=str(
                            self._artifact_store.root
                            / "promoted"
                            / manifest["manifest_id"]
                            / "files"
                        ),
                        status=status,
                    )
                else:
                    if self._member_stage_pack_acquirer is None:
                        raise ModelPreparationError(
                            "member_artifact_transport_unconfigured"
                        )
                    promotion = self._member_stage_pack_acquirer(
                        manifest=manifest,
                        expected_binding=binding,
                        policy=policy,
                        stage_source=stage_source,
                        peer=dict(peer),
                        warm_only=warm_only,
                    )
                    if not isinstance(promotion, MemberStagePackPromotion):
                        raise ModelPreparationError(
                            "member_artifact_transport_result_invalid"
                        )
                    status = self._artifact_store.import_member_terminal(
                        promotion.status
                    )
                    promotion = MemberStagePackPromotion(
                        member_id=promotion.member_id,
                        files_root=promotion.files_root,
                        status=status,
                    )
            except (
                ArtifactProvisioningError,
                StagePackBuildError,
                SwarmArtifactContractError,
            ) as exc:
                raise ModelPreparationError(exc.code) from exc
            if status["state"] != "ready":
                raise ModelPreparationError(
                    status["reason_code"] or "stage_pack_acquisition_failed"
                )
            promoted.append((manifest, promotion))
        _bind_member_promotions(controller, promoted)
        shutil.rmtree(source_root)
        return tuple(dict(promotion.status) for _, promotion in promoted)

    def _snapshot(self, model_id: str, revision: str) -> tuple[Path, str]:
        matches = [
            entry
            for entry in scan_huggingface_cache(self._cache)
            if entry.model_id == model_id
            and entry.revision == revision
            and entry.compatible
        ]
        if len(matches) != 1:
            raise ModelPreparationError("local_model_snapshot_unavailable")
        return matches[0].snapshot_path.resolve(strict=True), matches[0].quantization

    def _reusable_stage_sharded_root(
        self,
        authorization: Mapping[str, Any],
    ) -> Path | None:
        """Find a complete private build of the exact immutable representation."""

        expected_ranges = [
            {
                "start_layer": stage["start_layer"],
                "end_layer_exclusive": stage["end_layer_exclusive"],
                "layer_count": stage["end_layer_exclusive"] - stage["start_layer"],
            }
            for stage in authorization["stages"]
        ]
        candidates: list[tuple[int, Path]] = []
        for report_path in self._workspace.glob("*/candidate/build-report.json"):
            try:
                if report_path.is_symlink() or not report_path.is_file():
                    continue
                report = json.loads(report_path.read_text("utf-8"))
                bundle = report_path.parent / "transfer-bundle"
                frozen = json.loads(
                    (
                        bundle / "control" / "model-preparation-authorization.json"
                    ).read_text("utf-8")
                )
                deployment = bundle / "deployment"
                if (
                    not isinstance(report, dict)
                    or not isinstance(frozen, dict)
                    or report.get("model_id") != authorization.get("model_id")
                    or report.get("resolved_commit") != authorization.get("revision")
                    or report.get("quantization")
                    != authorization.get("serving_quantization")
                    or report.get("representation_digest")
                    != authorization.get("representation_digest")
                    or report.get("stage_sharding") is None
                    or (
                        frozen.get("protocol") == LEGACY_AUTHORIZATION_PROTOCOL
                        and (
                            report.get("layer_ranges") != expected_ranges
                            or frozen.get("stages") != authorization.get("stages")
                        )
                    )
                    or any(
                        frozen.get(field) != authorization.get(field)
                        for field in _REPRESENTATION_AUTHORITY_FIELDS
                    )
                    or (
                        frozen.get("protocol") == AUTHORIZATION_PROTOCOL
                        and any(
                            frozen.get(field) != authorization.get(field)
                            for field in _V2_REPRESENTATION_AUTHORITY_FIELDS
                        )
                    )
                    or frozen.get("protocol")
                    not in {AUTHORIZATION_PROTOCOL, LEGACY_AUTHORIZATION_PROTOCOL}
                    or deployment.is_symlink()
                    or not deployment.is_dir()
                    or not (deployment / "config.json").is_file()
                    or not (deployment / "model.safetensors.index.json").is_file()
                    or not tuple(deployment.glob("*.safetensors"))
                ):
                    continue
                candidates.append((report_path.stat().st_mtime_ns, deployment))
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        # A late peer-identity or publication failure can occur after the exact local
        # deployment and authorization have been fully materialized but before the
        # builder writes its report. A v2 source/quantizer/representation binding also
        # makes the complete immutable model reusable after a fresh planner changes
        # layer boundaries. The next builder compiles the new assignments, revalidates
        # every file, and reruns its startup challenge under the new authority.
        for frozen_path in self._workspace.glob(
            "*/candidate/transfer-bundle/control/model-preparation-authorization.json"
        ):
            try:
                if frozen_path.is_symlink() or not frozen_path.is_file():
                    continue
                frozen = json.loads(frozen_path.read_text("utf-8"))
                deployment = frozen_path.parents[1] / "deployment"
                if (
                    not isinstance(frozen, dict)
                    or frozen.get("protocol") != AUTHORIZATION_PROTOCOL
                    or frozen.get("model_id") != authorization.get("model_id")
                    or frozen.get("revision") != authorization.get("revision")
                    or frozen.get("serving_quantization")
                    != authorization.get("serving_quantization")
                    or any(
                        frozen.get(field) != authorization.get(field)
                        for field in (
                            *_REPRESENTATION_AUTHORITY_FIELDS,
                            *_V2_REPRESENTATION_AUTHORITY_FIELDS,
                        )
                    )
                    or deployment.is_symlink()
                    or not deployment.is_dir()
                    or not (deployment / "config.json").is_file()
                    or not (deployment / "model.safetensors.index.json").is_file()
                    or not tuple(deployment.glob("*.safetensors"))
                ):
                    continue
                candidates.append((frozen_path.stat().st_mtime_ns, deployment))
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1].resolve(strict=True)

    def _execute(
        self,
        command: list[str],
        code: str,
        *,
        diagnostic_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        self._require_bound_storage()
        try:
            completed = self._run(
                command,
                cwd=self._repo,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            self._require_bound_storage()
            raise ModelPreparationError(code) from exc
        self._require_bound_storage()
        try:
            diagnostic_path.write_bytes(
                _canonical(
                    {
                        "protocol": "mycelium.private_model_preparation_command.v1",
                        "returncode": completed.returncode,
                        "stdout": completed.stdout[-32_768:],
                        "stderr": completed.stderr[-32_768:],
                    }
                )
            )
            os.chmod(diagnostic_path, 0o600)
        except OSError as exc:
            self._require_bound_storage()
            raise ModelPreparationError(
                "model_preparation_diagnostic_write_failed"
            ) from exc
        if completed.returncode != 0:
            if completed.returncode in {-9, 137}:
                raise ModelPreparationError("model_candidate_memory_pressure")
            raise ModelPreparationError(code)
        return completed

    def _bind_candidate_to_seed(
        self,
        generated_plan_path: Path,
        *,
        attempt: Path,
    ) -> tuple[Path, dict[str, Any]]:
        """Reissue candidate offers from the live durable seed before staging."""

        binding_id = uuid.uuid4().hex
        plan_path = attempt / f"bound-operator-plan-{binding_id}.json"
        self._execute(
            [
                self._python,
                "scripts/bind_operator_plan_to_seed.py",
                "--operator-plan",
                str(generated_plan_path),
                "--seed-state-root",
                str(self._seed_state_root),
                "--output",
                str(plan_path),
            ],
            "model_candidate_seed_binding_failed",
            diagnostic_path=attempt / f"seed-binding-command-{binding_id}.json",
        )
        try:
            plan = json.loads(plan_path.read_text("utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelPreparationError("model_candidate_seed_binding_invalid") from exc
        if not isinstance(plan, dict):
            raise ModelPreparationError("model_candidate_seed_binding_invalid")
        return plan_path, plan

    def _checkpoint_bound_plan(
        self,
        attempt: Path,
        checkpoint: Mapping[str, Any],
        *,
        phase: str = "artifacts_acquired",
    ) -> tuple[Path, dict[str, Any]]:
        """Recover the exact plan named by a durable post-acquisition phase."""

        evidence = checkpoint.get("phase_evidence")
        phase_record = (
            evidence.get(phase) if isinstance(evidence, Mapping) else None
        )
        acquired = (
            evidence.get("artifacts_acquired")
            if isinstance(evidence, Mapping)
            else None
        )
        expected = (
            phase_record.get(
                "published_plan_digest"
                if phase == "candidate_published"
                else "bound_plan_digest"
            )
            if isinstance(phase_record, Mapping)
            else None
        )
        receipt_digests = (
            acquired.get("receipt_digests")
            if isinstance(acquired, Mapping)
            else None
        )
        if (
            not isinstance(expected, str)
            or not isinstance(receipt_digests, list)
            or not receipt_digests
            or not all(isinstance(item, str) for item in receipt_digests)
            or len(set(receipt_digests)) != len(receipt_digests)
        ):
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        try:
            history = self._artifact_store.ledger().get("history")
        except ArtifactProvisioningError as exc:
            raise ModelPreparationError(exc.code) from exc
        if not isinstance(history, list):
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        terminal_digests = {
            canonical_digest(item)
            for item in history
            if isinstance(item, Mapping) and item.get("state") == "ready"
        }
        if not set(receipt_digests) <= terminal_digests:
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        matches: list[tuple[int, Path, dict[str, Any]]] = []
        for candidate in attempt.glob("bound-operator-plan*.json"):
            try:
                if _file_digest(candidate) != expected:
                    continue
                document = _read_private_document(candidate)
                controller = document.get("controller")
                if (
                    not isinstance(controller, Mapping)
                    or not isinstance(
                        controller.get("prepositioned_artifacts"), Mapping
                    )
                ):
                    raise ModelPreparationError(
                        "model_preparation_checkpoint_invalid"
                    )
                matches.append((candidate.stat().st_mtime_ns, candidate, document))
            except (ModelPreparationError, OSError) as exc:
                raise ModelPreparationError(
                    "model_preparation_checkpoint_invalid"
                ) from exc
        if not matches:
            raise ModelPreparationError("model_preparation_checkpoint_invalid")
        _, path, plan = max(matches, key=lambda item: item[0])
        return path, plan

    def _publish_candidate_plan(
        self,
        plan_path: Path,
        *,
        candidate_id: str,
        allow_seed_rebind: bool,
    ) -> str:
        destination = self._candidates / f"{candidate_id}.json"
        temporary = self._candidates / f".{candidate_id}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(plan_path, temporary)
        os.chmod(temporary, 0o600)
        try:
            if destination.exists() and not allow_seed_rebind:
                if (
                    destination.is_symlink()
                    or hashlib.sha256(destination.read_bytes()).digest()
                    != hashlib.sha256(temporary.read_bytes()).digest()
                ):
                    raise ModelPreparationError("candidate_plan_conflict")
                return _file_digest(destination)
            if destination.is_symlink():
                raise ModelPreparationError("candidate_plan_conflict")
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return _file_digest(destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _warm_receipt_totals(
        receipts: tuple[dict[str, Any], ...],
        *,
        expected_stage_count: int,
        prior_acquisition_ids: set[str],
    ) -> dict[str, int]:
        if len(receipts) != expected_stage_count:
            raise ModelPreparationError("warm_reacquisition_receipts_incomplete")
        cached = 0
        total = 0
        for receipt in receipts:
            sources = receipt.get("sources")
            if (
                receipt.get("state") != "ready"
                or receipt.get("acquisition_id") in prior_acquisition_ids
                or receipt.get("cached_verified_bytes") != receipt.get("total_bytes")
                or receipt.get("transferred_verified_bytes") != 0
                or receipt.get("origin_bytes") != 0
                or receipt.get("missing_bytes") != 0
                or receipt.get("quarantined_bytes") != 0
                or not isinstance(sources, list)
                or any(
                    not isinstance(source, Mapping) or source.get("verified_bytes") != 0
                    for source in sources
                )
            ):
                raise ModelPreparationError("warm_reacquisition_not_zero_transfer")
            cached += int(receipt["cached_verified_bytes"])
            total += int(receipt["total_bytes"])
        if cached != total:
            raise ModelPreparationError("warm_reacquisition_not_zero_transfer")
        return {"cached_verified_bytes": cached, "total_bytes": total}

    def _verified_candidate(
        self,
        candidate_id: str,
        current_authorization: Mapping[str, Any],
    ) -> tuple[Path, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
        if (
            not isinstance(candidate_id, str)
            or not 1 <= len(candidate_id) <= 256
            or candidate_id in {".", ".."}
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                for character in candidate_id
            )
        ):
            raise ModelPreparationError("model_candidate_identity_invalid")
        published_path = self._candidates / f"{candidate_id}.json"
        try:
            if published_path.is_symlink() or not published_path.is_file():
                raise OSError
            published = json.loads(published_path.read_text("utf-8"))
            bundle = Path(published["controller"]["source_root"]).resolve(strict=True)
            output_root = bundle.parent
            generated_plan_path = output_root / "operator-plan.json"
            generated_plan = json.loads(generated_plan_path.read_text("utf-8"))
            report_path = output_root / "build-report.json"
            report = json.loads(report_path.read_text("utf-8"))
            challenge_path = output_root / "local-challenge-checkpoint.json"
            challenge = json.loads(challenge_path.read_text("utf-8"))
            frozen_path = bundle / "control" / "model-preparation-authorization.json"
            frozen = json.loads(frozen_path.read_text("utf-8"))
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelPreparationError("verified_candidate_unavailable") from exc
        workspace = self._workspace.resolve(strict=True)
        if (
            not bundle.is_relative_to(workspace)
            or bundle.name != "transfer-bundle"
            or any(
                path.is_symlink() or not path.is_file()
                for path in (
                    generated_plan_path,
                    report_path,
                    challenge_path,
                    frozen_path,
                )
            )
        ):
            raise ModelPreparationError("verified_candidate_unavailable")
        if not all(
            isinstance(document, dict)
            for document in (published, generated_plan, report, challenge, frozen)
        ):
            raise ModelPreparationError("verified_candidate_invalid")
        immutable_fields = (
            *_REPRESENTATION_AUTHORITY_FIELDS,
            *_V2_REPRESENTATION_AUTHORITY_FIELDS,
        )
        expected_ranges = [
            {
                "start_layer": stage["start_layer"],
                "end_layer_exclusive": stage["end_layer_exclusive"],
                "layer_count": stage["end_layer_exclusive"] - stage["start_layer"],
            }
            for stage in frozen.get("stages", [])
            if isinstance(stage, Mapping)
        ]
        challenge_assignments = challenge.get("assignments")
        if (
            frozen.get("protocol") != AUTHORIZATION_PROTOCOL
            or any(
                frozen.get(field) != current_authorization.get(field)
                for field in immutable_fields
            )
            or frozen.get("stages") != current_authorization.get("stages")
            or report.get("model_id") != frozen.get("model_id")
            or report.get("resolved_commit") != frozen.get("revision")
            or report.get("representation_digest")
            != frozen.get("representation_digest")
            or report.get("source_artifact_digest")
            != frozen.get("source_artifact_digest")
            or report.get("quantizer") != frozen.get("quantizer")
            or report.get("owner_decision_digest")
            != frozen.get("owner_decision_digest")
            or report.get("layer_ranges") != expected_ranges
            or challenge.get("protocol") != "mycelium.local_candidate_challenge.v1"
            or challenge.get("state") != "passed"
            or challenge.get("deployment_id") != candidate_id
            or challenge.get("model_id") != frozen.get("model_id")
            or challenge.get("resolved_commit") != frozen.get("revision")
            or challenge.get("source_artifact_digest")
            != frozen.get("source_artifact_digest")
            or challenge.get("representation_digest")
            != frozen.get("representation_digest")
            or challenge.get("preparation_binding_digest")
            != frozen.get("preparation_binding_digest")
            or not isinstance(challenge.get("challenge_output_token_ids"), list)
            or len(challenge["challenge_output_token_ids"]) < 2
            or not isinstance(challenge_assignments, list)
            or [item.get("node_id") for item in challenge_assignments]
            != [stage.get("node_id") for stage in frozen["stages"]]
            or generated_plan.get("controller", {})
            .get("run_plan", {})
            .get("deployment_id")
            != candidate_id
            or published.get("controller", {}).get("run_plan", {}).get("deployment_id")
            != candidate_id
        ):
            raise ModelPreparationError("verified_candidate_authority_mismatch")
        _validate_preparation_authorization(bundle, frozen)
        evidence = {
            "build_report_digest": _file_digest(report_path),
            "challenge_digest": _file_digest(challenge_path),
            "generated_plan_digest": _file_digest(generated_plan_path),
            "frozen_authorization_digest": _file_digest(frozen_path),
        }
        return generated_plan_path, output_root, report, frozen, evidence

    def reacquire(
        self,
        authorization: Mapping[str, Any],
        candidate_id: str,
        progress: Callable[[str, int | None, int | None], None],
    ) -> PreparationResult:
        """Reacquire one challenged candidate through fresh product authority."""

        self._require_bound_storage()
        generated_plan_path, _output_root, report, frozen, evidence = (
            self._verified_candidate(candidate_id, authorization)
        )
        topology = {
            "protocol": "mycelium.qwen_live_topology.v1",
            "placement_order_authority": "warm_exact_candidate",
            "nodes": [
                {
                    "node_id": stage["node_id"],
                    "backend": stage["backend"],
                    "start_layer": stage["start_layer"],
                    "end_layer_exclusive": stage["end_layer_exclusive"],
                }
                for stage in authorization["stages"]
            ],
        }
        topology_digest = canonical_digest(topology)
        verified_bytes = int(report["transfer_bytes"])
        resumed = self._resume_warm_attempt(
            authorization,
            candidate_id=candidate_id,
            topology_digest=topology_digest,
        )
        if resumed is None:
            attempt_name, route_label = _new_attempt_identity(
                str(authorization["revision"])
            )
            attempt = self._workspace / f"warm-{attempt_name}"
            attempt.mkdir(mode=0o700)
            checkpoint = self._checkpoint_base(
                operation="warm_reacquire",
                route_label=route_label,
                authorization=authorization,
                topology_digest=topology_digest,
                candidate_id=candidate_id,
            )
            _write_durable_private_document(
                attempt / "phase-checkpoint.json", checkpoint
            )
            checkpoint = self._write_checkpoint(
                attempt, checkpoint, "candidate_challenged", evidence
            )
            progress("verifying_local_artifacts", 0, verified_bytes)
            plan_path, plan = self._bind_candidate_to_seed(
                generated_plan_path,
                attempt=attempt,
            )
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "seed_bound",
                {"bound_plan_digest": _file_digest(plan_path)},
            )
            try:
                prior = self._artifact_store.ledger()
            except ArtifactProvisioningError as exc:
                raise ModelPreparationError(exc.code) from exc
            prior_ids = {
                item["acquisition_id"]
                for item in prior.get("history", [])
                if isinstance(item, Mapping)
                and isinstance(item.get("acquisition_id"), str)
            }
            receipts = self._acquire_stage_packs(
                attempt=attempt,
                authorization=frozen,
                plan=plan,
                warm_only=True,
            )
            totals = self._warm_receipt_totals(
                receipts,
                expected_stage_count=len(authorization["stages"]),
                prior_acquisition_ids=prior_ids,
            )
            # Acquisition can legitimately outlive the short membership offer
            # lease. Persist its promotion bindings, then obtain a fresh signed
            # seed view immediately before staging.
            _write_durable_private_document(plan_path, plan)
            plan_path, plan = self._bind_candidate_to_seed(
                plan_path, attempt=attempt
            )
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "artifacts_acquired",
                {
                    "receipt_digests": [
                        canonical_digest(receipt) for receipt in receipts
                    ],
                    "bound_plan_digest": _file_digest(plan_path),
                    **totals,
                },
            )
        else:
            attempt, checkpoint = resumed
            challenged = checkpoint["phase_evidence"].get("candidate_challenged")
            if not isinstance(challenged, Mapping) or any(
                challenged.get(key) != value for key, value in evidence.items()
            ):
                raise ModelPreparationError("model_preparation_checkpoint_invalid")
            acquired = checkpoint["phase_evidence"]["artifacts_acquired"]
            receipt_digests = acquired.get("receipt_digests")
            if (
                not isinstance(receipt_digests, list)
                or len(receipt_digests) != len(authorization["stages"])
                or type(acquired.get("cached_verified_bytes")) is not int
                or type(acquired.get("total_bytes")) is not int
                or acquired["cached_verified_bytes"] != acquired["total_bytes"]
            ):
                raise ModelPreparationError("model_preparation_checkpoint_invalid")
            totals = {
                "cached_verified_bytes": acquired["cached_verified_bytes"],
                "total_bytes": acquired["total_bytes"],
            }
            plan_path, plan = self._checkpoint_bound_plan(
                attempt,
                checkpoint,
                phase=(
                    "candidate_published"
                    if checkpoint["completed_phase"] == "candidate_published"
                    else (
                        "peers_staged"
                        if checkpoint["completed_phase"] == "peers_staged"
                        else "artifacts_acquired"
                    )
                ),
            )
            if checkpoint["completed_phase"] in {
                "artifacts_acquired",
                "candidate_published",
            }:
                plan_path, plan = self._bind_candidate_to_seed(
                    plan_path, attempt=attempt
                )
                if checkpoint["completed_phase"] == "artifacts_acquired":
                    checkpoint = self._write_checkpoint(
                        attempt,
                        checkpoint,
                        "artifacts_acquired",
                        {**acquired, "bound_plan_digest": _file_digest(plan_path)},
                    )
        if checkpoint["completed_phase"] == "artifacts_acquired":
            progress("staging_peers", 0, verified_bytes)
            self._execute(
                [
                    self._python,
                    "scripts/stage_live_route.py",
                    "--operator-plan",
                    str(plan_path),
                    "--python",
                    self._python,
                ],
                "model_candidate_staging_failed",
                diagnostic_path=attempt / "stage-command.json",
            )
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "peers_staged",
                {"staged_plan_digest": _file_digest(plan_path)},
            )
            plan_path, plan = self._bind_candidate_to_seed(
                plan_path, attempt=attempt
            )
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "peers_staged",
                {
                    "staged_plan_digest": checkpoint["phase_evidence"][
                        "peers_staged"
                    ]["staged_plan_digest"],
                    "bound_plan_digest": _file_digest(plan_path),
                },
            )
        progress("publishing_candidate", 0, verified_bytes)
        published_digest = self._publish_candidate_plan(
            plan_path,
            candidate_id=candidate_id,
            allow_seed_rebind=True,
        )
        self._write_checkpoint(
            attempt,
            checkpoint,
            "candidate_published",
            {"published_plan_digest": published_digest},
        )
        return PreparationResult(
            candidate_id=candidate_id,
            topology_size=len(authorization["stages"]),
            transfer_bytes=0,
            verified_bytes=verified_bytes,
            operation="warm_reacquire",
            cache_receipt_count=len(
                checkpoint["phase_evidence"]["artifacts_acquired"][
                    "receipt_digests"
                ]
            ),
            cached_verified_bytes=totals["cached_verified_bytes"],
            transferred_verified_bytes=0,
            origin_bytes=0,
        )

    def __call__(
        self,
        authorization: Mapping[str, Any],
        progress: Callable[[str, int | None, int | None], None],
    ) -> PreparationResult:
        self._require_bound_storage()
        model_id = str(authorization["model_id"])
        revision = str(authorization["revision"])
        resumed = self._resume_attempt(authorization)
        recovered = (
            None
            if resumed is not None
            else self._recover_challenged_attempt(authorization)
        )
        checkpoint: dict[str, Any]
        if resumed is None and recovered is None:
            attempt_name, route_label = _new_attempt_identity(revision)
            attempt = self._workspace / attempt_name
            attempt.mkdir(mode=0o700)
        elif recovered is not None:
            attempt, checkpoint, frozen_authorization = recovered
            route_label = str(checkpoint["route_label"])
        else:
            assert resumed is not None
            attempt, checkpoint = resumed
            route_label = str(checkpoint["route_label"])
        authorization_path = attempt / "authorization.json"
        topology_path = attempt / "topology.json"
        if resumed is None and recovered is None:
            authorization_path.write_bytes(_canonical(authorization))
            os.chmod(authorization_path, 0o600)
            template = json.loads(self._template.read_text("utf-8"))
            topology = (
                _topology_from_template(
                    template, authorization, route_label=route_label
                )
                if self._execution_topology is None
                else _topology_from_operation_file(
                    json.loads(self._execution_topology.read_text("utf-8")),
                    authorization,
                    route_label=route_label,
                )
            )
            topology_path.write_bytes(_canonical(topology))
            os.chmod(topology_path, 0o600)
            checkpoint = self._checkpoint_base(
                operation="prepare",
                route_label=route_label,
                authorization=authorization,
                topology_digest=canonical_digest(topology),
            )
            _write_durable_private_document(
                attempt / "phase-checkpoint.json", checkpoint
            )
            frozen_authorization = dict(authorization)
        else:
            if recovered is None:
                frozen_authorization = _read_private_document(authorization_path)
            if recovered is None and (
                _resumable_authority(frozen_authorization)
                != _resumable_authority(authorization)
            ):
                raise ModelPreparationError("model_preparation_checkpoint_invalid")
            topology = _read_private_document(topology_path)
            if canonical_digest(topology) != checkpoint.get("topology_digest"):
                raise ModelPreparationError("model_preparation_checkpoint_invalid")
        output_root = attempt / "candidate"

        phase_index = _CHECKPOINT_PHASES.index(str(checkpoint["completed_phase"]))
        if phase_index < _CHECKPOINT_PHASES.index("candidate_challenged"):
            self._preflight_local_acquisition_storage(
                frozen_authorization,
                topology,
                attempt=attempt,
            )
            snapshot, source_quantization = self._snapshot(model_id, revision)
            if source_quantization != frozen_authorization.get("source_quantization"):
                raise ModelPreparationError("model_source_representation_changed")
            progress("compiling_assignments", None, None)
            reusable_stage_root = self._reusable_stage_sharded_root(
                frozen_authorization
            )
            build_command = [
                self._python,
                "scripts/build_qwen_live_route.py",
                "--template-plan",
                str(self._template),
                "--model-root",
                str(snapshot),
                "--model-id",
                model_id,
                "--resolved-commit",
                revision,
                "--output-root",
                str(output_root),
                "--route-label",
                route_label,
                "--topology",
                str(topology_path),
                "--model-preparation-authorization",
                str(authorization_path),
            ]
            if reusable_stage_root is None:
                build_command.append("--stage-sharded")
            else:
                build_command.extend(
                    ("--reuse-stage-sharded-root", str(reusable_stage_root))
                )
            if self._temporary_root is not None:
                build_command.extend(("--temporary-root", str(self._temporary_root)))
            completed = self._execute(
                build_command,
                "model_candidate_build_failed",
                diagnostic_path=attempt / "build-command.json",
            )
            try:
                build_result = json.loads(completed.stdout)
                generated_plan_path = Path(build_result["operator_plan"])
                generated_plan = json.loads(generated_plan_path.read_text("utf-8"))
                report_path = output_root / "build-report.json"
                report = json.loads(report_path.read_text("utf-8"))
                challenge_path = output_root / "local-challenge-checkpoint.json"
                challenge = json.loads(challenge_path.read_text("utf-8"))
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise ModelPreparationError(
                    "model_candidate_build_result_invalid"
                ) from exc
            if (
                challenge.get("state") != "passed"
                or challenge.get("deployment_id")
                != generated_plan["controller"]["run_plan"]["deployment_id"]
            ):
                raise ModelPreparationError("model_candidate_challenge_invalid")
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "candidate_challenged",
                {
                    "build_report_digest": _file_digest(report_path),
                    "challenge_digest": _file_digest(challenge_path),
                    "generated_plan_digest": _file_digest(generated_plan_path),
                    "frozen_authorization_digest": _file_digest(authorization_path),
                },
            )
        else:
            generated_plan_path = output_root / "operator-plan.json"
            report_path = output_root / "build-report.json"
            challenge_path = output_root / "local-challenge-checkpoint.json"
            try:
                report = json.loads(report_path.read_text("utf-8"))
                challenge = json.loads(challenge_path.read_text("utf-8"))
                evidence = checkpoint["phase_evidence"]["candidate_challenged"]
            except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
                raise ModelPreparationError(
                    "model_preparation_checkpoint_invalid"
                ) from exc
            if (
                not isinstance(report, dict)
                or not isinstance(challenge, dict)
                or challenge.get("state") != "passed"
                or evidence.get("build_report_digest") != _file_digest(report_path)
                or evidence.get("challenge_digest") != _file_digest(challenge_path)
                or evidence.get("generated_plan_digest")
                != _file_digest(generated_plan_path)
                or evidence.get("frozen_authorization_digest")
                != _file_digest(authorization_path)
            ):
                raise ModelPreparationError("model_preparation_checkpoint_invalid")
            _validate_preparation_authorization(
                output_root / "transfer-bundle", frozen_authorization
            )
            progress("compiling_assignments", None, None)
        verified_bytes = int(report["transfer_bytes"])
        phase_index = _CHECKPOINT_PHASES.index(str(checkpoint["completed_phase"]))
        if phase_index < _CHECKPOINT_PHASES.index("artifacts_acquired"):
            plan_path, plan = self._bind_candidate_to_seed(
                generated_plan_path,
                attempt=attempt,
            )
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "seed_bound",
                {"bound_plan_digest": _file_digest(plan_path)},
            )
            progress("verifying_local_artifacts", None, verified_bytes)
            receipts = self._acquire_stage_packs(
                attempt=attempt,
                authorization=frozen_authorization,
                plan=plan,
            )
            # Stage only under a fresh signed membership view.  The artifact
            # authority is intentionally longer lived than member offers, so every
            # cold path must cross this second lease boundary after acquisition.
            _write_durable_private_document(plan_path, plan)
            plan_path, plan = self._bind_candidate_to_seed(plan_path, attempt=attempt)
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "artifacts_acquired",
                {
                    "receipt_digests": [
                        canonical_digest(receipt) for receipt in receipts
                    ],
                    "bound_plan_digest": _file_digest(plan_path),
                },
            )
        else:
            # The content-addressed store and its promotion records are already
            # terminal.  Resume from the fsynced acquired plan, then reissue only
            # the short-lived seed membership authority before staging.
            plan_path, plan = self._checkpoint_bound_plan(
                attempt,
                checkpoint,
                phase=(
                    "peers_staged"
                    if checkpoint["completed_phase"] == "peers_staged"
                    else "artifacts_acquired"
                ),
            )
            if checkpoint["completed_phase"] == "artifacts_acquired":
                plan_path, plan = self._bind_candidate_to_seed(
                    plan_path, attempt=attempt
                )
                acquired_evidence = checkpoint["phase_evidence"][
                    "artifacts_acquired"
                ]
                checkpoint = self._write_checkpoint(
                    attempt,
                    checkpoint,
                    "artifacts_acquired",
                    {
                        **acquired_evidence,
                        "bound_plan_digest": _file_digest(plan_path),
                    },
                )
        phase_index = _CHECKPOINT_PHASES.index(str(checkpoint["completed_phase"]))
        if phase_index < _CHECKPOINT_PHASES.index("peers_staged"):
            progress("staging_peers", int(report["transfer_bytes"]), verified_bytes)
            self._execute(
                [
                    self._python,
                    "scripts/stage_live_route.py",
                    "--operator-plan",
                    str(plan_path),
                    "--python",
                    self._python,
                ],
                "model_candidate_staging_failed",
                diagnostic_path=attempt / "stage-command.json",
            )
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "peers_staged",
                {"staged_plan_digest": _file_digest(plan_path)},
            )
            plan_path, plan = self._bind_candidate_to_seed(
                plan_path, attempt=attempt
            )
            checkpoint = self._write_checkpoint(
                attempt,
                checkpoint,
                "peers_staged",
                {
                    "staged_plan_digest": checkpoint["phase_evidence"][
                        "peers_staged"
                    ]["staged_plan_digest"],
                    "bound_plan_digest": _file_digest(plan_path),
                },
            )
        progress("publishing_candidate", int(report["transfer_bytes"]), verified_bytes)
        candidate_id = str(plan["controller"]["run_plan"]["deployment_id"])
        published_digest = self._publish_candidate_plan(
            plan_path,
            candidate_id=candidate_id,
            allow_seed_rebind=False,
        )
        self._write_checkpoint(
            attempt,
            checkpoint,
            "candidate_published",
            {"published_plan_digest": published_digest},
        )
        return PreparationResult(
            candidate_id=candidate_id,
            topology_size=len(authorization["stages"]),
            transfer_bytes=int(report["transfer_bytes"]),
            verified_bytes=verified_bytes,
        )


__all__ = ["LocalCandidatePreparer"]
