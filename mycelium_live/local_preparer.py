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
from .stage_pack_builder import StagePackBuildError, build_stage_pack_source


_STAGE_PACK_AUTHORITY_TTL_MS = 15 * 60 * 1_000
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


def _validate_preparation_authorization(
    bundle: Path, authorization: Mapping[str, Any]
) -> None:
    if authorization.get("protocol") != "mycelium.model_preparation_authorization.v1":
        raise ModelPreparationError("model_preparation_authorization_invalid")
    feasibility_digest = authorization.get("feasibility_digest")
    owner_decision_digest = authorization.get("owner_decision_digest")
    representation_digest = authorization.get("representation_digest")
    expected_binding = canonical_digest(
        {
            "feasibility_digest": feasibility_digest,
            "owner_decision_digest": owner_decision_digest,
            "representation_digest": representation_digest,
        }
    )
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
    if any(
        authorization.get(field) != frozen.get(field)
        for field in _REPRESENTATION_AUTHORITY_FIELDS
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


class LocalCandidatePreparer:
    """Build, verify, stage, and atomically publish one local model candidate."""

    def __init__(
        self,
        *,
        repo_root: Path,
        cache_root: Path,
        template_plan: Path,
        workspace_root: Path,
        candidate_root: Path,
        artifact_store: ArtifactAcquisitionStore,
        member_stage_pack_acquirer: MemberStagePackAcquirer | None = None,
        python_executable: str = sys.executable,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        self._repo = Path(repo_root).resolve(strict=True)
        self._cache = Path(cache_root).resolve(strict=True)
        self._template = Path(template_plan).resolve(strict=True)
        self._workspace = _private_directory(
            workspace_root, "model_preparation_root_unsafe", create=True
        )
        self._candidates = _private_directory(
            candidate_root, "candidate_plan_root_unsafe"
        )
        self._artifact_store = artifact_store
        self._member_stage_pack_acquirer = member_stage_pack_acquirer
        self._python = python_executable
        self._run = run
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))

    @staticmethod
    def _acquisition_policy(chunk_size_bytes: int) -> dict[str, Any]:
        return {
            "protocol": POLICY_PROTOCOL,
            "chunk_size_bytes": chunk_size_bytes,
            "maximum_sources": 3,
            "per_source_concurrency": 2,
            "aggregate_concurrency": 4,
            "maximum_retries_per_chunk": 2,
            "maximum_source_rotations": 4,
            "partial_state_ttl_seconds": 3_600,
            "disk_reserve_bytes": 1_073_741_824,
            "per_source_bytes_per_second": 500_000,
            "aggregate_bytes_per_second": 1_000_000,
            "serving_traffic_reserve_ratio": 0.4,
            "multi_source_threshold_bytes": 2 * chunk_size_bytes,
            "minimum_predicted_improvement_ratio": 0.2,
            "allow_redundant_hedging": False,
            "thermal_classes_allowed": ["fair", "nominal"],
            "power_classes_allowed": ["battery_ok", "external_power"],
        }

    def _acquire_stage_packs(
        self,
        *,
        attempt: Path,
        authorization: Mapping[str, Any],
        plan: dict[str, Any],
    ) -> None:
        controller = plan["controller"]
        bundle = Path(controller["source_root"]).resolve(strict=True)
        issued_at_unix_ms = self._clock()
        if type(issued_at_unix_ms) is not int or issued_at_unix_ms <= 0:
            raise ModelPreparationError("model_preparation_clock_invalid")
        evidence_valid_until_unix_ms = authorization.get("evidence_valid_until_unix_ms")
        if (
            type(evidence_valid_until_unix_ms) is not int
            or evidence_valid_until_unix_ms < issued_at_unix_ms
        ):
            raise ModelPreparationError("model_capacity_stale")
        stage_pack_expires_at_unix_ms = issued_at_unix_ms + _STAGE_PACK_AUTHORITY_TTL_MS
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
        chunk_size = 4 * 1024 * 1024
        policy = self._acquisition_policy(chunk_size)
        provisioner = SwarmArtifactProvisioner(self._artifact_store)
        peers = {peer["node_id"]: peer for peer in controller["peers"]}
        promoted: list[tuple[dict[str, Any], MemberStagePackPromotion]] = []
        source_root = attempt / "artifact-sources"
        source_root.mkdir(mode=0o700)
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
                    "origin_fallback_allowed": True,
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
                        origin=read_origin,
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

    def _execute(
        self,
        command: list[str],
        code: str,
        *,
        diagnostic_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        completed = self._run(
            command,
            cwd=self._repo,
            text=True,
            capture_output=True,
            check=False,
        )
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
        if completed.returncode != 0:
            if completed.returncode in {-9, 137}:
                raise ModelPreparationError("model_candidate_memory_pressure")
            raise ModelPreparationError(code)
        return completed

    def __call__(
        self,
        authorization: Mapping[str, Any],
        progress: Callable[[str, int | None, int | None], None],
    ) -> PreparationResult:
        model_id = str(authorization["model_id"])
        revision = str(authorization["revision"])
        snapshot, source_quantization = self._snapshot(model_id, revision)
        if source_quantization != authorization.get("source_quantization"):
            raise ModelPreparationError("model_source_representation_changed")
        attempt_name, route_label = _new_attempt_identity(revision)
        attempt = self._workspace / attempt_name
        attempt.mkdir(mode=0o700)
        authorization_path = attempt / "authorization.json"
        topology_path = attempt / "topology.json"
        authorization_path.write_bytes(_canonical(authorization))
        os.chmod(authorization_path, 0o600)
        template = json.loads(self._template.read_text("utf-8"))
        topology_path.write_bytes(
            _canonical(
                _topology_from_template(
                    template, authorization, route_label=route_label
                )
            )
        )
        os.chmod(topology_path, 0o600)
        output_root = attempt / "candidate"

        progress("compiling_assignments", None, None)
        completed = self._execute(
            [
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
                "--stage-sharded",
                "--output-root",
                str(output_root),
                "--route-label",
                route_label,
                "--topology",
                str(topology_path),
                "--model-preparation-authorization",
                str(authorization_path),
            ],
            "model_candidate_build_failed",
            diagnostic_path=attempt / "build-command.json",
        )
        try:
            build_result = json.loads(completed.stdout)
            plan_path = Path(build_result["operator_plan"])
            report = json.loads((output_root / "build-report.json").read_text("utf-8"))
            plan = json.loads(plan_path.read_text("utf-8"))
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelPreparationError("model_candidate_build_result_invalid") from exc
        verified_bytes = int(report["transfer_bytes"])
        progress("verifying_local_artifacts", None, verified_bytes)
        self._acquire_stage_packs(
            attempt=attempt,
            authorization=authorization,
            plan=plan,
        )
        plan_path.write_bytes(_canonical(plan))
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
        progress("publishing_candidate", int(report["transfer_bytes"]), verified_bytes)
        candidate_id = str(plan["controller"]["run_plan"]["deployment_id"])
        destination = self._candidates / f"{candidate_id}.json"
        temporary = self._candidates / f".{candidate_id}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(plan_path, temporary)
        os.chmod(temporary, 0o600)
        if destination.exists():
            if (
                destination.is_symlink()
                or hashlib.sha256(destination.read_bytes()).digest()
                != hashlib.sha256(temporary.read_bytes()).digest()
            ):
                temporary.unlink(missing_ok=True)
                raise ModelPreparationError("candidate_plan_conflict")
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, destination)
        return PreparationResult(
            candidate_id=candidate_id,
            topology_size=len(authorization["stages"]),
            transfer_bytes=int(report["transfer_bytes"]),
            verified_bytes=verified_bytes,
        )


__all__ = ["LocalCandidatePreparer"]
