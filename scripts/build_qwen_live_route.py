#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build a pinned, authorization-bound dense Qwen live-route bundle."""

# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence
import contextlib
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence
import uuid

import mlx.core as mx
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.qwen_phone_builder import (
    BACKEND as PHONE_BACKEND, PhoneEvidence, RustStageExecutor, validate_build_receipt,
    authorized_membership_snapshot, bind_phone_graph, external_control_descriptor, preparation_proof_digest,
)

from layer_assignment import compile_layer_assignments
from mycelium_assignment_cache import ArtifactObjectKey, AssignmentArtifactCache
from mycelium_live.codec import Qwen2PromptCodec, Qwen3PromptCodec, Qwen35PromptCodec
from mycelium_layer_planner.gossip_adapter import (
    plan_signed_evidence,
    planner_snapshot_from_signed_evidence,
)
from mycelium_layer_planner.serialization import route_plan_to_dict
from mycelium_layer_planner.public_projection import build_m13_placement_projection
from mycelium_membership import ASSIGNMENT_OFFER_PROTOCOL, sign_membership_message
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    build_execution_graph,
    build_physical_device_states,
    build_replicated_execution_graph,
    prepare_assignment_artifacts,
)
from mycelium_qualification.safetensors_sharding import shard_qwen2_checkpoint
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.layer_builder import layer_load_proof_digest
from mycelium_router.serialization import execution_graph_to_dict
from runtime_loader import (
    canonical_json,
    execute_loaded_numpy_stage,
    execute_loaded_stage,
    load_assignment_stage,
)
from planner_assignment import (
    compile_bound_layer_assignments,
    compile_bound_replica_assignments,
)
from stage_pack import (
    artifact_report_for_loader,
    compile_stage_pack,
    verify_stage_pack,
)
from weight_provisioning import artifact_report_errors, provision_assignment


_MAX_STAGED_MODEL_FILE_BYTES = 1_900_000_000
MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_COMMIT = "7ae557604adf67be50417f59c2c2f167def9a775"
LOAD_GENERATION = 17
_STAGE_EVIDENCE_FIELDS = {
    "stage_pack",
    "stage_pack_manifest",
    "stage_pack_verification",
    "stage_pack_digest",
    "stage_pack_verification_digest",
}
_TOPOLOGY_PROTOCOL = "mycelium.qwen_live_topology.v1"
_TOPOLOGY_FIELDS = (
    "node_id",
    "process_transport",
    "ssh_target",
    "ssh_identity_file",
    "staging_root",
    "python_executable",
    "sidecar_binary",
    "endpoint_secret_file",
    "endpoint_id",
    "membership_generation",
    "runtime_backend",
)
_ROUTE_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_RUNTIME_PACKAGE_CLOSURE = (
    "mycelium_capacity_profiles",
    "mycelium_gossip",
    "mycelium_internet",
    "mycelium_invite",
    "mycelium_layer_planner",
    "mycelium_membership",
    "mycelium_mobile",
    "mycelium_node",
    "mycelium_physical_runner",
    "mycelium_qualification",
    "mycelium_router",
    "mycelium_seed",
)
_RUNTIME_MODULE_CLOSURE = (
    "mycelium_performance_budget.py",
    "mycelium_topology_evidence.py",
    "physical_inference_qualification.py",
    "two_process_runtime_qualification.py",
)
_PREPARATION_AUTHORIZATION_PROTOCOL = "mycelium.model_preparation_authorization.v2"
_LEGACY_PREPARATION_AUTHORIZATION_PROTOCOL = (
    "mycelium.model_preparation_authorization.v1"
)
_MANAGED_HELLO_OBSERVATIONS_PROTOCOL = "mycelium.managed_hello_observations.v1"
# Identities the native worker derives are run-scoped digests, never host
# aliases. Admission accepts only that measured shape.
_MANAGED_IDENTITY_RE = re.compile(r"^(host|boot)-[0-9a-f]{32}$")
_MANAGED_OBSERVATIONS_MAX_AGE_SECONDS = 10 * 60
_MANAGED_OBSERVATIONS_MAX_SKEW_SECONDS = 60


def _private_temporary_root(path: Path | None) -> Path | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        raise RuntimeError("model_preparation_temporary_root_unsafe")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise RuntimeError("model_preparation_temporary_root_unsafe") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError("model_preparation_temporary_root_unsafe")
    return candidate.resolve(strict=True)


def emitted_stage_shard_root(emit_root: Path) -> Path:
    """Where ``--emit-stage-shards <emit_root>`` writes the shard files."""

    return Path(emit_root) / "model"


@contextlib.contextmanager
def _stage_shard_destination(
    emit_root: Path | None,
    temporary_root: Path | None,
    *,
    prefix: str,
) -> Iterator[Path]:
    """Yield the destination for the derived stage-sharded representation.

    With ``--emit-stage-shards`` the representation is written to the exact
    durable ``<emit_root>/model`` path, and an existing target is refused so a
    prior emission is never silently reused; otherwise today's temporary
    directory is kept.
    """

    if emit_root is None:
        with tempfile.TemporaryDirectory(
            prefix=prefix,
            dir=_private_temporary_root(temporary_root),
        ) as temporary:
            yield Path(temporary) / "model"
        return
    sharded_root = emitted_stage_shard_root(emit_root)
    if sharded_root.exists():
        raise RuntimeError("emit_stage_shards_already_exists")
    sharded_root.parent.mkdir(parents=True, exist_ok=True)
    yield sharded_root



def _preparation_selection(
    m18_document: Mapping[str, Any] | None,
    *,
    node_ids: Sequence[str],
    planned_ranges: Sequence[range] | None,
    preparation_ranges: Sequence[range] | None,
    managed_ranges: Sequence[range] | None = None,
) -> tuple[tuple[str, ...], tuple[range, ...] | None]:
    """Nodes and layer ranges the artifact preparer (and stage sharding) covers.

    An M18 replica plan prepares its PRIMARY track only: a replica serves the same
    stage artifacts as the primary of its replica group, so it adds no stage.
    """
    node_ids = tuple(node_ids)
    if m18_document is not None:
        primary_track = m18_document["route_plan"]["legal_tracks"][0]["placement_ids"]
        placement_intent = {
            item["placement_id"]: item
            for item in m18_document["route_plan"]["placements"]
        }
        prepare_node_ids = tuple(
            placement_intent[item]["node_id"] for item in primary_track
        )
        prepare_ranges = tuple(
            range(
                placement_intent[item]["layer_range"]["start"],
                placement_intent[item]["layer_range"]["end"],
            )
            for item in primary_track
        )
        if len(prepare_node_ids) == 1:
            # The generic artifact preparer requires a multi-node route. For a
            # complete-model replica plan, use an inert balanced preparation only
            # to materialize the local model; replica-bound assignments below are
            # still compiled from the signed M18 route.
            return node_ids, None
        return prepare_node_ids, prepare_ranges
    if preparation_ranges is not None:
        return node_ids, tuple(preparation_ranges)
    if managed_ranges is not None:
        return node_ids, tuple(managed_ranges)
    return node_ids, None if planned_ranges is None else tuple(planned_ranges)


def _shard_stages(
    source_root: Path,
    destination: Path,
    prepare_node_ids: Sequence[str],
    prepare_ranges: Sequence[range] | None,
) -> dict[str, Any]:
    """Stage-shard one file set per PREPARED stage (never per topology node)."""
    return shard_qwen2_checkpoint(
        source_root,
        destination,
        shard_count=len(prepare_node_ids),
        layer_ranges=prepare_ranges,
        max_file_bytes=_MAX_STAGED_MODEL_FILE_BYTES,
    )

def _m18_runtime_kv_bytes(
    model_config: Mapping[str, Any],
    *,
    qualification_token_count: int,
    track_membership_count: int,
) -> int:
    """Size Router admission for the model context, not one startup prompt."""

    context_tokens = model_config.get("max_position_embeddings")
    if (
        not isinstance(context_tokens, int)
        or isinstance(context_tokens, bool)
        or not 1 <= context_tokens <= 1_048_576
        or not isinstance(qualification_token_count, int)
        or qualification_token_count < 1
        or not isinstance(track_membership_count, int)
        or track_membership_count < 1
    ):
        raise RuntimeError("m18_model_context_capacity_invalid")
    return max(context_tokens, qualification_token_count) * 32 * track_membership_count


def _route_label(args: argparse.Namespace) -> str:
    label = getattr(args, "route_label", "m7")
    if not isinstance(label, str) or not _ROUTE_LABEL_PATTERN.fullmatch(label):
        raise RuntimeError("route_label_invalid")
    return label


def _placement_provenance(
    *, m13_document: object, preparation_authorization: object
) -> str:
    if m13_document is not None or preparation_authorization is not None:
        return "planner_v2"
    return "target_local_physical_preload"


def _model_identity(args: argparse.Namespace) -> tuple[str, str, str]:
    model_id = getattr(args, "model_id", MODEL_ID)
    resolved_commit = getattr(args, "resolved_commit", MODEL_COMMIT)
    if (
        not isinstance(model_id, str)
        or model_id.count("/") != 1
        or not all(model_id.split("/"))
    ):
        raise RuntimeError("model_id_invalid")
    if (
        not isinstance(resolved_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", resolved_commit) is None
    ):
        raise RuntimeError("resolved_commit_invalid")
    model_slug = re.sub(r"[^a-z0-9]+", "-", model_id.rsplit("/", 1)[1].lower())
    model_slug = model_slug.strip("-")
    if not model_slug:
        raise RuntimeError("model_id_invalid")
    return model_id, resolved_commit, model_slug


def _preparation_authorization(
    path: Path | None,
    *,
    model_id: str,
    resolved_commit: str,
    topology: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, tuple[range, ...] | None]:
    if path is None:
        return None, None
    try:
        document = json.loads(Path(path).read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("model_preparation_authorization_invalid") from exc
    expected = {
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
        "evidence_valid_until_unix_ms",
        "stages",
        "download_authorized",
    }
    protocol = document.get("protocol") if isinstance(document, dict) else None
    if protocol == _PREPARATION_AUTHORIZATION_PROTOCOL:
        expected.update(
            {"authorized_at_unix_ms", "source_artifact_digest", "quantizer"}
        )
    stages = document.get("stages") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != expected
        or protocol
        not in {
            _PREPARATION_AUTHORIZATION_PROTOCOL,
            _LEGACY_PREPARATION_AUTHORIZATION_PROTOCOL,
        }
        or document.get("model_id") != model_id
        or document.get("revision") != resolved_commit
        or document.get("download_authorized") is not False
        or not isinstance(document.get("catalog_generation"), int)
        or not isinstance(document.get("evidence_generation"), int)
        or not isinstance(document.get("evidence_valid_until_unix_ms"), int)
        or (
            protocol == _PREPARATION_AUTHORIZATION_PROTOCOL
            and (
                not isinstance(document.get("authorized_at_unix_ms"), int)
                or document["authorized_at_unix_ms"] <= 0
                or document["authorized_at_unix_ms"] > int(time.time() * 1_000)
                or document["authorized_at_unix_ms"]
                > document["evidence_valid_until_unix_ms"]
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(document.get("source_artifact_digest", "")),
                )
                is None
                or not isinstance(document.get("quantizer"), str)
                or not document["quantizer"]
            )
        )
        or (
            protocol == _LEGACY_PREPARATION_AUTHORIZATION_PROTOCOL
            and document["evidence_valid_until_unix_ms"] < int(time.time() * 1_000)
        )
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(document.get("operation_digest", ""))
        )
        is None
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(document.get("feasibility_digest", ""))
        )
        is None
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(document.get("representation_digest", ""))
        )
        is None
        or not isinstance(document.get("source_quantization"), str)
        or not isinstance(document.get("serving_dtype"), str)
        or document.get("serving_dtype") not in {"float16", "float32"}
        or document.get("serving_quantization") not in {"none", "int8-weight-only"}
        or type(document.get("conversion_authorized")) is not bool
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(document.get("owner_decision_digest", ""))
        )
        is None
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(document.get("preparation_binding_digest", ""))
        )
        is None
        or (
            document.get("source_quantization") != document.get("serving_quantization")
            and document.get("conversion_authorized") is not True
        )
        or not isinstance(stages, list)
        or len(stages) != len(topology)
    ):
        raise RuntimeError("model_preparation_authorization_invalid")
    binding = {
        "feasibility_digest": document["feasibility_digest"],
        "owner_decision_digest": document["owner_decision_digest"],
        "representation_digest": document["representation_digest"],
    }
    if protocol == _PREPARATION_AUTHORIZATION_PROTOCOL:
        binding.update(
            {
                "source_artifact_digest": document["source_artifact_digest"],
                "quantizer": document["quantizer"],
            }
        )
    expected_binding_digest = (
        "sha256:" + hashlib.sha256(canonical_json(binding).encode("utf-8")).hexdigest()
    )
    if document["preparation_binding_digest"] != expected_binding_digest:
        raise RuntimeError("model_preparation_authorization_invalid")
    ranges: list[range] = []
    cursor = 0
    for index, (stage, node) in enumerate(zip(stages, topology, strict=True)):
        if not isinstance(stage, dict) or set(stage) != {
            "stage_index",
            "node_id",
            "start_layer",
            "end_layer_exclusive",
            "backend",
            "decode_mode",
            "assignment_files",
            "assignment_artifact_bytes",
        }:
            raise RuntimeError("model_preparation_authorization_invalid")
        start = stage.get("start_layer")
        end = stage.get("end_layer_exclusive")
        if (
            stage.get("stage_index") != index
            or stage.get("node_id") != node["node_id"]
            or stage.get("backend") != node["runtime_backend"]
            or not isinstance(stage.get("decode_mode"), str)
            or type(start) is not int
            or type(end) is not int
            or start != cursor
            or end <= start
            or not isinstance(stage.get("assignment_files"), list)
            or not all(
                isinstance(item, str) and item for item in stage["assignment_files"]
            )
            or type(stage.get("assignment_artifact_bytes")) is not int
            or stage["assignment_artifact_bytes"] < 0
        ):
            raise RuntimeError("model_preparation_authorization_invalid")
        ranges.append(range(start, end))
        cursor = end
    return document, tuple(ranges)


def _m13_control_plane(
    path: Path | None,
    *,
    node_ids: tuple[str, ...],
    deployment_id: str,
) -> tuple[dict[str, Any] | None, tuple[range, ...] | None]:
    if path is None:
        return None, None
    try:
        document = json.loads(Path(path).read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("m13_control_plane_invalid") from exc
    expected = {
        "protocol",
        "signed_evidence_bundle",
        "planner_snapshot",
        "route_plan",
        "model",
        "workload",
        "policy",
        "quantization",
        "ab_deltas",
        "exclusions",
    }
    m14_expected = expected | {
        "transport_observations",
        "topology_decision",
        "topology_projection",
    }
    m18_expected = expected | {"admitted_node_ids", "replica_gain_evidence"}
    if not isinstance(document, dict) or frozenset(document) not in {
        frozenset(expected),
        frozenset(m14_expected),
        frozenset(m18_expected),
    }:
        raise RuntimeError("m13_control_plane_invalid")
    if document.get("protocol") not in {
        "mycelium.m13_physical_candidate.v1",
        "mycelium.m14_physical_candidate.v1",
        "mycelium.m18_physical_candidate.v1",
    }:
        raise RuntimeError("m13_control_plane_invalid")
    signed = document.get("signed_evidence_bundle")
    route = document.get("route_plan")
    if not isinstance(signed, dict) or not isinstance(route, dict):
        raise RuntimeError("m13_control_plane_invalid")
    if (
        signed.get("evidence_bundle", {}).get("deployment", {}).get("deployment_id")
        != deployment_id
    ):
        raise RuntimeError("m13_control_plane_deployment_mismatch")
    placement_records = route.get("placements", [])
    placements = {
        placement.get("placement_id"): placement
        for placement in placement_records
        if isinstance(placement, dict)
    }
    tracks = route.get("legal_tracks")
    if not isinstance(tracks, list) or not tracks:
        raise RuntimeError("m13_control_plane_route_invalid")
    if document["protocol"] == "mycelium.m18_physical_candidate.v1":
        by_node = {
            placement.get("node_id"): placement
            for placement in placement_records
            if isinstance(placement, dict)
        }
        if set(by_node) != set(node_ids) or len(by_node) != len(placement_records):
            raise RuntimeError("m13_control_plane_node_order_mismatch")
        ordered = [by_node[node_id] for node_id in node_ids]
        if len(tracks) < 2:
            raise RuntimeError("m13_control_plane_route_invalid")
    else:
        primary = {
            placement_id: placement
            for placement_id, placement in placements.items()
            if placement.get("primary") is True
        }
        ordered = [primary.get(item) for item in tracks[0].get("placement_ids", [])]
        if [
            item.get("node_id") if isinstance(item, dict) else None for item in ordered
        ] != list(node_ids):
            raise RuntimeError("m13_control_plane_node_order_mismatch")
    ranges = []
    for placement in ordered:
        layer_range = placement.get("layer_range")
        if not isinstance(layer_range, dict):
            raise RuntimeError("m13_control_plane_route_invalid")
        start, end = layer_range.get("start"), layer_range.get("end")
        if type(start) is not int or type(end) is not int:
            raise RuntimeError("m13_control_plane_route_invalid")
        ranges.append(range(start, end))
    return document, tuple(ranges)


def _validate_m13_control_plane(
    document: dict[str, Any],
    *,
    manifest: dict[str, Any],
) -> None:
    signed = document["signed_evidence_bundle"]
    trusted_digest = signed.get("verification_key", {}).get("verification_key_digest")
    captured = signed.get("statement", {}).get("captured_at_unix_ms")
    if not isinstance(trusted_digest, str) or type(captured) is not int:
        raise RuntimeError("m13_control_plane_invalid")
    admitted_raw = document.get("admitted_node_ids")
    admitted_node_ids = (
        None
        if admitted_raw is None
        else tuple(admitted_raw)
        if isinstance(admitted_raw, list)
        and admitted_raw
        and all(isinstance(item, str) and item for item in admitted_raw)
        else ()
    )
    if admitted_node_ids == ():
        raise RuntimeError("m13_control_plane_invalid")
    snapshot = planner_snapshot_from_signed_evidence(
        signed,
        expected_verification_key_digest=trusted_digest,
        now_unix_ms=captured + 1,
        model=document["model"],
        workload=document["workload"],
        policy=document["policy"],
        quantization=document["quantization"],
        admitted_node_ids=admitted_node_ids,
    )
    route = route_plan_to_dict(
        plan_signed_evidence(
            signed,
            expected_verification_key_digest=trusted_digest,
            now_unix_ms=captured + 1,
            model=document["model"],
            workload=document["workload"],
            policy=document["policy"],
            quantization=document["quantization"],
            admitted_node_ids=admitted_node_ids,
        )
    )
    normalized_route = json.loads(canonical_json(route))
    if (
        snapshot != document["planner_snapshot"]
        or normalized_route != document["route_plan"]
    ):
        raise RuntimeError("m13_control_plane_not_derived")
    expected_model = {
        "model_id": manifest["model_id"],
        "revision": manifest["resolved_commit"],
        "weight_digest": ("sha256:" + manifest["manifest_digest"]["value"]),
        "num_layers": manifest["num_layers"],
    }
    if any(
        document["model"].get(key) != value for key, value in expected_model.items()
    ):
        raise RuntimeError("m13_control_plane_model_mismatch")


def _bytes(document: Any) -> bytes:
    return canonical_json(document).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()


def _write_document(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_bytes(document))


def _write_durable_document(path: Path, document: Any) -> None:
    """Atomically checkpoint an expensive local result before remote work."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _local_challenge_checkpoint(
    *,
    model_id: str,
    resolved_commit: str,
    prepared: Any,
    assignments: list[dict[str, Any]],
    proofs: list[dict[str, Any]],
    challenge_prompt: tuple[int, ...],
    challenge_output: tuple[int, ...],
    challenge_output_text: str,
    runtime_backends: Mapping[str, str],
    preparation_authorization: Mapping[str, Any] | None,
    stage_sharding: Mapping[str, Any] | None,
    phone_challenges: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the completed local decode to its exact model and assignment inputs."""

    return {
        "protocol": "mycelium.local_candidate_challenge.v1",
        "state": "passed",
        "model_id": model_id,
        "resolved_commit": resolved_commit,
        "deployment_id": assignments[0]["deployment_id"],
        "source_artifact_digest": (
            None
            if preparation_authorization is None
            else preparation_authorization.get("source_artifact_digest")
        ),
        "representation_digest": (
            None
            if preparation_authorization is None
            else preparation_authorization.get("representation_digest")
        ),
        "preparation_binding_digest": (
            None
            if preparation_authorization is None
            else preparation_authorization.get("preparation_binding_digest")
        ),
        "model_manifest_digest": "sha256:"
        + prepared.manifest["manifest_digest"]["value"],
        "assignments": [
            {
                "node_id": assignment["node_id"],
                "assignment_digest": _digest(_bytes(assignment)),
                "load_proof_digest": preparation_proof_digest(proof),
            }
            for assignment, proof in zip(assignments, proofs, strict=True)
        ],
        **({"phone_challenges": dict(phone_challenges)} if phone_challenges else {}),
        "runtime_backends": dict(runtime_backends),
        "challenge_prompt_token_ids": list(challenge_prompt),
        "challenge_output_token_ids": list(challenge_output),
        "challenge_output_text": challenge_output_text,
        "stage_sharding": stage_sharding,
    }


def _target_report(
    base_report: dict[str, Any], assignment: dict[str, Any]
) -> dict[str, Any]:
    report = copy.deepcopy(base_report)
    for field in _STAGE_EVIDENCE_FIELDS:
        report.pop(field, None)
    for field in (
        "deployment_id",
        "deployment_epoch",
        "assignment_id",
        "node_id",
        "manifest_digest",
        "resolved_commit",
        "range",
        "artifact_cache_root",
    ):
        report[field] = copy.deepcopy(assignment[field])
    report["resolved_artifact_cache_root"] = assignment["artifact_cache_root"]
    for record in report["verified_files"]:
        record["local_path"] = str(
            Path(assignment["artifact_cache_root"]) / record["path"]
        )
    errors = artifact_report_errors(assignment, report)
    if errors:
        raise RuntimeError("target_artifact_report_invalid:" + ";".join(errors))
    return report


def _target_proof(
    base_proof: dict[str, Any],
    assignment: dict[str, Any],
    pack: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    proof = copy.deepcopy(base_proof)
    proof["assignment_id"] = assignment["assignment_id"]
    proof["node_id"] = assignment["node_id"]
    proof["control_plane_binding"] = copy.deepcopy(assignment["control_plane_binding"])
    proof["stage_pack_digest"] = pack["stage_pack_digest"]
    proof["stage_pack_verification_digest"] = verification[
        "stage_pack_verification_digest"
    ]
    return proof


def runtime_source_paths(repo: Path) -> tuple[str, ...]:
    """Derive project Python closure without importing or executing that source."""
    import ast
    repo=Path(repo).resolve()
    selected=set(_RUNTIME_MODULE_CLOSURE)|{'physical_inference_node.py','weight_quantization.py'}
    for package in _RUNTIME_PACKAGE_CLOSURE:
        folder=repo/package
        if not folder.is_dir():raise RuntimeError(f'runtime_closure_package_missing:{package}')
        selected.update(str(p.relative_to(repo)) for p in folder.rglob('*.py') if p.is_file())
    inspected=set()
    while selected-inspected:
        relative=min(selected-inspected);source=repo/relative
        if not source.is_file() or not source.resolve().is_relative_to(repo):
            raise RuntimeError(f'runtime_closure_file_missing:{relative}')
        tree=ast.parse(source.read_text(encoding='utf-8'),filename=relative)
        inspected.add(relative)
        modules=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):modules.extend(alias.name for alias in node.names)
            elif isinstance(node,ast.ImportFrom) and node.level==0 and node.module:
                modules.append(node.module)
        for module in modules:
            head=module.split('.')[0]
            if head=='tests' or head.startswith('test_'):continue
            path=repo/(head+'.py');folder=repo/head
            if path.is_file():selected.add(str(path.relative_to(repo)))
            elif folder.is_dir():
                selected.update(str(p.relative_to(repo)) for p in folder.rglob('*.py') if p.is_file() and '__pycache__' not in p.parts)
    return tuple(sorted(selected))


def _copy_runtime_closure(repo: Path, bundle: Path, template: dict[str, Any]) -> None:
    paths=set(runtime_source_paths(repo))
    paths.update(record['path'] for record in template['controller']['transfer_manifest']['files']
        if not record['path'].startswith(('control/','deployment/')))
    for relative in sorted(paths):
        source=repo/relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or not source.resolve().is_relative_to(repo.resolve()):
            raise RuntimeError('runtime_closure_path_invalid')
        if not source.is_file():raise RuntimeError(f'runtime_closure_file_missing:{relative}')
    if len(paths) > 220:
        # Preserve the bounded 256-file transfer contract, but run a verified
        # extracted filesystem tree rather than breaking __file__ with zipimport.
        import zipfile
        bundle.mkdir(parents=True, exist_ok=True)
        members = {}
        with zipfile.ZipFile(bundle / 'mycelium-runtime.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for relative in sorted(paths):
                source = repo / relative
                raw = source.read_bytes()
                mode = 0o700 if source.stat().st_mode & 0o111 else 0o600
                members[relative] = dict(size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),mode=mode)
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | mode) << 16
                archive.writestr(info, raw)
        # Explicit command-line resource paths remain reachable, too. The copy
        # inside the archive serves module-relative native resource consumers.
        for relative in sorted(p for p in paths if not p.endswith('.py')):
            target = bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / relative, target)
        for relative in sorted(p for p in paths if p.endswith('.py') and p != 'physical_inference_node.py'):
            stale = bundle / relative
            if stale.exists() or stale.is_symlink():
                if stale.is_symlink() or not stale.resolve().is_relative_to(bundle.resolve()):
                    raise RuntimeError('runtime_closure_path_invalid')
                stale.unlink()
        bootstrap = (repo/'scripts/runtime_archive_bootstrap.py').read_text(encoding='utf-8')
        archive_hash = hashlib.sha256((bundle/'mycelium-runtime.zip').read_bytes()).hexdigest()
        (bundle / 'physical_inference_node.py').write_text(bootstrap +
            '\nRUNTIME_ARCHIVE_SHA256 = ' + repr(archive_hash) +
            '\nRUNTIME_MEMBERS = ' + repr(members) +
            '\nif __name__ == "__main__":\n    main()\n', encoding='utf-8')
        return
    for relative in sorted(paths):
        target=bundle/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(repo/relative,target)


def _transfer_manifest(bundle: Path) -> dict[str, Any]:
    records = []
    for path in sorted(
        candidate for candidate in bundle.rglob("*") if candidate.is_file()
    ):
        relative = str(path.relative_to(bundle))
        records.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "content_digest": _file_digest(path),
            }
        )
    return {
        "protocol": "mycelium.controller_transfer_manifest.v1",
        "files": records,
    }


def _node_transfer_manifests(
    transfer_manifest: dict[str, Any],
    packs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind each node to common files plus only its stage-owned tensors."""

    records = transfer_manifest["files"]
    common = {
        record["path"]
        for record in records
        if not (
            record["path"].startswith("deployment/")
            and record["path"].endswith(".safetensors")
        )
    }
    from physical_inference_qualification import node_transfer_manifests

    selections: dict[str, list[str]] = {}
    for pack in packs:
        if pack["node_id"] in selections:
            raise ValueError("node_transfer_duplicate_node")
        owned = {
            f"deployment/{artifact['upstream_path']}" for artifact in pack["artifacts"]
        }
        selections[pack["node_id"]] = sorted(common | owned)
    return node_transfer_manifests(transfer_manifest, selections)


def _endpoint_ids(template: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    offers = template["controller"]["membership_snapshot"]["assignment_offers"]
    for offer in offers:
        for record in offer["message"]["peer_endpoint_records"]:
            result[record["node_id"]] = record["endpoint_id"]
    return result


def _membership_generations(template: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    offers = template["controller"]["membership_snapshot"]["assignment_offers"]
    for offer in offers:
        message = offer["message"]
        records = [
            (message["recipient_node_id"], message["generation"]),
            *[
                (record["node_id"], record["membership_generation"])
                for record in message["peer_endpoint_records"]
            ],
        ]
        for node_id, generation in records:
            if type(generation) is not int or generation <= 0:
                raise RuntimeError("topology_membership_generation_invalid")
            prior = result.setdefault(node_id, generation)
            if prior != generation:
                raise RuntimeError("topology_membership_generation_inconsistent")
    return result


def _template_backend(runtime: Mapping[str, Any], *, index: int) -> str:
    """A template node's backend: its declared ``runtime_backend``, else LEGACY.

    The legacy rule (first node MLX, the rest NumPy) is the R7 two-host fleet's
    shape; a template that declares each node's backend never depends on it.
    """

    declared = runtime.get("runtime_backend")
    if declared is not None:
        return str(declared)
    return "mlx" if index == 0 else "numpy"


def check_topology_against_role_binding(
    topology: Sequence[Mapping[str, Any]], role_binding: Mapping[str, Any]
) -> None:
    """Refuse a route topology that is not exactly the bound members, in order.

    ``role_binding`` is a verified ``mycelium.gate_role_binding.v1`` document
    (``mycelium_gate_requirements``).  Its placements (role order) must equal
    the topology node order, and each node's runtime backend must be one the
    bound member's signed capability report offered.
    """

    from mycelium_gate_requirements.binding import verify_binding

    try:
        binding = verify_binding(role_binding)
    except ValueError as exc:
        raise RuntimeError("topology_role_binding_invalid") from exc
    if [node["node_id"] for node in topology] != list(binding["placements"]):
        raise RuntimeError("topology_role_binding_mismatch")
    backends: dict[str, set[str]] = {}
    for members in binding["roles"].values():
        for member in members:
            backends.setdefault(member["node_id"], set()).update(member["device_class"]["backends"])
    for node in topology:
        if node["runtime_backend"] not in backends.get(node["node_id"], set()):
            raise RuntimeError("topology_role_binding_backend_mismatch")


def _topology_nodes(
    template: dict[str, Any],
    topology_path: Path | None,
    *,
    managed_transport: bool = False,
) -> list[dict[str, Any]]:
    """Return a validated ordered physical topology.

    When no topology document is supplied, preserve the existing template-plan
    behavior. A topology document is the durable extension seam for a third or
    later peer; it intentionally contains public endpoint IDs and paths, never
    endpoint secret material.

    Managed preparation is an explicit opt-in: with ``managed_transport`` the
    document may express an all-managed ``managed_https`` peer set carrying no
    SSH target or identity, matching PeerIdentity's managed authority rules.
    Without the opt-in the accepted transport set is unchanged.
    """

    if topology_path is None:
        template_nodes = {
            node["node_id"]: node
            for node in template["controller"]["run_plan"]["nodes"]
        }
        endpoint_ids = _endpoint_ids(template)
        membership_generations = _membership_generations(template)
        nodes = []
        for index, peer in enumerate(template["controller"]["peers"]):
            node_id = peer["node_id"]
            runtime = template_nodes[node_id]
            nodes.append(
                {
                    **peer,
                    **({} if peer["process_transport"] == "external_iroh" else {
                        "python_executable": runtime["python_executable"],
                        "sidecar_binary": runtime["sidecar_binary"],
                        "endpoint_secret_file": runtime["endpoint_secret_file"],
                    }),
                    "endpoint_id": endpoint_ids[node_id],
                    "membership_generation": membership_generations[node_id],
                    "runtime_backend": (PHONE_BACKEND if peer["process_transport"] == "external_iroh"
                                        else _template_backend(runtime, index=index)),
                }
            )
    else:
        document = json.loads(topology_path.read_text(encoding="utf-8"))
        if document.get("protocol") != _TOPOLOGY_PROTOCOL:
            raise RuntimeError("topology_protocol_invalid")
        nodes = document.get("nodes")
        if not isinstance(nodes, list):
            raise RuntimeError("topology_nodes_invalid")

    if len(nodes) < 2:
        raise RuntimeError("topology_requires_two_or_more_nodes")
    if any(not isinstance(node, dict) for node in nodes):
        raise RuntimeError("topology_node_invalid")
    for node in nodes:
        optional = ({"python_executable", "sidecar_binary", "endpoint_secret_file",
                     "ssh_target", "ssh_identity_file"}
                    if node.get("process_transport") == "external_iroh" else set())
        missing = [field for field in _TOPOLOGY_FIELDS if field not in node and field not in optional]
        if missing:
            raise RuntimeError(f"topology_node_field_missing:{missing[0]}")
    node_ids = [node["node_id"] for node in nodes]
    if any(not isinstance(node_id, str) or not node_id for node_id in node_ids):
        raise RuntimeError("topology_node_id_invalid")
    if len(set(node_ids)) != len(node_ids):
        raise RuntimeError("topology_node_id_duplicate")
    if node_ids != sorted(node_ids) and (
        topology_path is None
        or document.get("placement_order_authority") != "m14_measured_cycle"
    ):
        raise RuntimeError("topology_order_must_match_node_ids")
    endpoint_ids = [node["endpoint_id"] for node in nodes]
    if any(
        not isinstance(endpoint_id, str) or not endpoint_id
        for endpoint_id in endpoint_ids
    ):
        raise RuntimeError("topology_endpoint_id_invalid")
    if len(set(endpoint_ids)) != len(endpoint_ids):
        raise RuntimeError("topology_endpoint_id_duplicate")
    generations = [node["membership_generation"] for node in nodes]
    if any(
        type(generation) is not int or generation <= 0 for generation in generations
    ):
        raise RuntimeError("topology_membership_generation_invalid")
    backends = [node["runtime_backend"] for node in nodes]
    if any(backend not in {"mlx", "numpy", PHONE_BACKEND} for backend in backends):
        raise RuntimeError("topology_runtime_backend_unsupported")
    for index, node in enumerate(nodes):
        phone = node["process_transport"] == "external_iroh"
        if phone != (node["runtime_backend"] == PHONE_BACKEND):
            raise RuntimeError("phone_backend_transport_mismatch")
        if phone:
            if index in (0, len(nodes) - 1):
                raise RuntimeError("external_node_stage_only")
            if any(node.get(k) is not None for k in
                   ("ssh_target", "ssh_identity_file", "python_executable", "sidecar_binary", "endpoint_secret_file")):
                raise RuntimeError("phone_process_authority_invalid")
    transports = [node["process_transport"] for node in nodes]
    if managed_transport:
        # The managed plan is all-managed, mirroring the runner's peer rule:
        # mixing managed control with unmanaged process authority is rejected.
        if any(transport != "managed_https" for transport in transports):
            raise RuntimeError("topology_managed_transport_mixed")
        for node in nodes:
            if (
                node["ssh_target"] is not None
                or node["ssh_identity_file"] is not None
            ):
                raise RuntimeError("managed_peer_ssh_authority_invalid")
    else:
        if any(transport not in {"local", "ssh", "external_iroh"} for transport in transports):
            raise RuntimeError("topology_process_transport_invalid")
    if any(
        node["process_transport"] not in {"local", "ssh", "managed_https", "external_iroh"}
        for node in nodes
    ):
        raise RuntimeError("topology_process_transport_invalid")
    local_count = sum(node["process_transport"] == "local" for node in nodes)
    if local_count != 1 and not (
        managed_transport
        and all(node["process_transport"] == "managed_https" for node in nodes)
    ):
        raise RuntimeError("topology_requires_one_local_node")
    return copy.deepcopy(nodes)


def _managed_layer_ranges(
    value: Any,
    *,
    node_ids: tuple[str, ...],
    managed_transport: bool,
) -> tuple[range, ...] | None:
    """Validate an owner-computed contiguous split for a managed topology.

    The A13 owner derives these ranges from member capability reports
    (``mycelium_a13_macos.layer_split``), one non-empty ``[start, end)`` per
    topology node in topology order, starting at layer 0. Coverage of the full
    model depth is re-checked by the canonical preparer against the manifest.
    """

    if value is None:
        return None
    if not managed_transport:
        raise RuntimeError("managed_layer_ranges_require_managed_transport")
    if not isinstance(value, (list, tuple)) or len(value) != len(node_ids):
        raise RuntimeError("managed_layer_ranges_invalid")
    ranges: list[range] = []
    expected = 0
    for item in value:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or any(type(bound) is not int for bound in item)
            or item[0] != expected
            or item[1] <= item[0]
        ):
            raise RuntimeError("managed_layer_ranges_invalid")
        ranges.append(range(item[0], item[1]))
        expected = item[1]
    return tuple(ranges)


def _placement_exclusions(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Keep topology-wide M14 exclusions out of M13's node exclusion field."""

    if document.get("protocol") == "mycelium.m14_physical_candidate.v1":
        return []
    exclusions = document.get("exclusions")
    if not isinstance(exclusions, list):
        raise ValueError("m13_exclusions_invalid")
    return list(exclusions)


def _remote_identity_program(run_id: str) -> str:
    """Render the controller's cross-platform physical identity derivation."""

    return """
import hashlib,json,platform,re,subprocess
from pathlib import Path
run_id={run_id!r}
host=''
if platform.system() == 'Darwin':
    completed=subprocess.run(('ioreg','-rd1','-c','IOPlatformExpertDevice'),capture_output=True,text=True,timeout=5.0)
    match=re.search(r'"IOPlatformUUID"\\s*=\\s*"([A-Fa-f0-9-]+)"',completed.stdout)
    if completed.returncode == 0 and match is not None:
        host=match.group(1).lower()
if not host:
    try:
        host=Path('/etc/machine-id').read_text(encoding='ascii').strip()
    except OSError:
        pass
if not host:
    host='host-'+hashlib.sha256(platform.node().encode()).hexdigest()[:32]
source=''
if platform.system() == 'Darwin':
    for name in ('kern.bootsessionuuid','kern.boottime'):
        completed=subprocess.run(('sysctl','-n',name),capture_output=True,text=True,timeout=5.0)
        if completed.returncode == 0 and completed.stdout.strip():
            source=completed.stdout.strip()
            break
else:
    try:
        source=Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    except OSError:
        pass
if not source:
    source='unknown'
boot='boot-'+hashlib.sha256((host+'\\0'+source).encode()).hexdigest()[:32]
host_digest=hashlib.sha256(b'mycelium.physical_runner.host_identity.v1\\0'+run_id.encode()+b'\\0'+host.encode()).hexdigest()
boot_digest=hashlib.sha256(b'mycelium.physical_runner.boot_identity.v1\\0'+run_id.encode()+b'\\0'+boot.encode()).hexdigest()
print(json.dumps(['host-'+host_digest[:32],'boot-'+boot_digest[:32]]))
""".format(run_id=run_id)


def _managed_hello_observations(document, node_ids, *, deployment_id):
    """Validate caller-admitted observation metadata; NOT signature/host proof.

    The application must authenticate native replies and membership before this
    source-preparation helper. A JSON file alone is never physical evidence.
    """
    fields={'protocol','deployment_id','run_id','captured_at_unix_ms','observations'}
    if not isinstance(document,dict) or document.get('protocol')!=_MANAGED_HELLO_OBSERVATIONS_PROTOCOL:
        raise RuntimeError('managed_observation_protocol_invalid')
    if set(document)!=fields:raise RuntimeError('managed_observation_fields_invalid')
    if document['deployment_id']!=deployment_id:raise RuntimeError('managed_observation_deployment_mismatch')
    captured=document['captured_at_unix_ms']; now=time.time()
    if type(captured) is not int or not -_MANAGED_OBSERVATIONS_MAX_SKEW_SECONDS<=now-captured/1000<=_MANAGED_OBSERVATIONS_MAX_AGE_SECONDS:
        raise RuntimeError('managed_observation_stale')
    run_id=document['run_id']
    if not isinstance(run_id,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,80}-qwen-[0-9]{8}T[0-9]{6}Z',run_id):
        raise RuntimeError('managed_observation_run_id_invalid')
    try:started=datetime.strptime(run_id.rsplit('-qwen-',1)[1],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp()
    except ValueError:raise RuntimeError('managed_observation_run_id_invalid') from None
    if not -_MANAGED_OBSERVATIONS_MAX_SKEW_SECONDS<=now-started<=_MANAGED_OBSERVATIONS_MAX_AGE_SECONDS:
        raise RuntimeError('managed_observation_run_id_stale')
    observations=document['observations']
    if not isinstance(observations,list) or len(observations)!=len(node_ids):
        raise RuntimeError('managed_observation_node_mismatch')
    result={}
    for item in observations:
        if not isinstance(item,dict) or set(item)!={'node_id','host_id','boot_id'}:
            raise RuntimeError('managed_observation_fields_invalid')
        node=item['node_id']
        if not isinstance(node,str) or node not in node_ids or node in result:
            raise RuntimeError('managed_observation_node_mismatch')
        host,boot=item['host_id'],item['boot_id']
        if (not isinstance(host,str) or not isinstance(boot,str)
            or re.fullmatch(r'host-[0-9a-f]{32}',host) is None
            or re.fullmatch(r'boot-[0-9a-f]{32}',boot) is None):
            raise RuntimeError('managed_observation_identity_synthetic')
        result[node]=(host,boot)
    if len({value[0] for value in result.values()})!=len(result):
        raise RuntimeError('managed_observation_hosts_not_distinct')
    return run_id,result


def _startup_challenge_request(route_label,prompt,output):
    return {
        'request_id':f'request-{route_label}-qwen-startup',
        'prompt_token_ids':list(prompt),'max_new_tokens':len(output),
        'expected_new_tokens':len(output),'qos_class':'interactive','admitted_at':0.0,
        'target_ttft_ms':120_000.0,'target_tpot_ms':120_000.0,'target_tokens_per_second':0.001,
        'sampling_seed':0,'generation_config_digest':_digest(_bytes({'max_new_tokens':len(output),'sampling_seed':0})),
    }


def refresh_peer_identities(
    peers: list[dict[str, Any]],
    run_id: str,
    *,
    python_executables_by_node: dict[str, str] | None = None,
    managed_observations: Mapping[str, tuple[str, str]] | None = None,
    phone_evidence: Mapping[str, PhoneEvidence] | None = None,
) -> list[dict[str, Any]]:
    """Measure and bind the run-scoped host identities used by node hello.

    Managed peers never execute remote SSH: their run-scoped identities are
    bound only from externally admitted fresh native hello observations.
    """

    remote_program = _remote_identity_program(run_id)
    refreshed = copy.deepcopy(peers)
    for peer in refreshed:
        if peer["process_transport"] == "local":
            host_id, boot_id = derive_local_run_scoped_identity(run_id)
        elif peer["process_transport"] == "external_iroh":
            evidence = (phone_evidence or {}).get(peer["node_id"])
            if evidence is None:
                raise RuntimeError("phone_authorized_evidence_required")
            host_id, boot_id = evidence.peer.host_id, evidence.peer.boot_id
        elif peer["process_transport"] == "managed_https":
            if managed_observations is None:
                raise RuntimeError("managed_peer_observations_missing")
            # PREFERENCE ORDER at the substitution site (p80r1, 2026-09-20): the LIVE hello observation
            # wins over whatever identity the supplied topology recorded, because a node's run-scoped
            # boot_id moves whenever it relaunches and topology.json carries NO boot_id at all -- so the
            # observation is the only MEASURED source there is. Only a node with no observation of its own
            # falls back to the topology, and the fallback is a value check, not an acceptance rule: an
            # absent or unmeasured identity is still refused, so the template's placeholders
            # ("pending-host-a" / "pending-boot-a", mycelium_a13_macos/compiler_inputs.py::_pending_peers)
            # can never be mistaken for a measured run-scoped identity.
            admitted = managed_observations.get(peer["node_id"])
            if admitted is None:
                identity = peer.get("host_id"), peer.get("boot_id")
                refusal = "managed_peer_observations_missing"
            else:
                identity = admitted
                refusal = "managed_observation_identity_synthetic"
            host_id, boot_id = identity
            if (
                _MANAGED_IDENTITY_RE.fullmatch(str(host_id)) is None
                or _MANAGED_IDENTITY_RE.fullmatch(str(boot_id)) is None
            ):
                raise RuntimeError(refusal)
        else:
            command = ["ssh"]
            identity = peer.get("ssh_identity_file")
            if identity:
                command.extend(("-i", identity))
            command.extend(
                (
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=15",
                    "-o",
                    "ServerAliveInterval=10",
                    "-o",
                    "ServerAliveCountMax=6",
                    peer["ssh_target"],
                    (python_executables_by_node or {}).get(
                        peer["node_id"], "/usr/bin/python3"
                    ),
                    "-c",
                    shlex.quote(remote_program),
                )
            )
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30.0,
            )
            host_id, boot_id = json.loads(completed.stdout)
        peer["host_id"] = host_id
        peer["boot_id"] = boot_id
    return refreshed


def preflight_remote_peers(topology: list[dict[str, Any]]) -> None:
    """Reject an unavailable required SSH peer before expensive local preparation.

    Managed peers are controlled by the owning application's canonical
    transport, never by this builder's SSH authority: skip them.
    """

    for peer in topology:
        if peer["process_transport"] in {"local", "managed_https", "external_iroh"}:
            continue
        command = ["ssh"]
        identity = peer.get("ssh_identity_file")
        if identity:
            command.extend(("-i", identity))
        command.extend(
            (
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "ServerAliveInterval=10",
                "-o",
                "ServerAliveCountMax=6",
                peer["ssh_target"],
                "true",
            )
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise RuntimeError("peer_preflight_unavailable") from exc


def _physical_graph_document(graph: Any) -> dict[str, Any]:
    """Render the strict node-process spelling of the execution graph."""

    document = execution_graph_to_dict(graph)
    for stage in document["stages"]:
        stage["layer_range"] = stage.pop("range")
    return document


def _membership_snapshot(
    *,
    assignments: list[dict[str, Any]],
    packs: list[dict[str, Any]],
    graph_document: dict[str, Any],
    endpoint_ids: dict[str, str],
    membership_generations: dict[str, int],
    now: float,
    route_label: str = "m7",
    placement_provenance: str = "target_local_physical_preload",
) -> dict[str, Any]:
    if not _ROUTE_LABEL_PATTERN.fullmatch(route_label):
        raise RuntimeError("route_label_invalid")
    signer = generate_ed25519_signer(endpoint_id=f"{route_label}-qwen-seed-endpoint")
    graph_digest = _digest(_bytes(graph_document))
    swarm_id = f"mycelium-{route_label}-qwen-multi-host"
    offers = []
    for index, (assignment, pack) in enumerate(zip(assignments, packs, strict=True)):
        peer_records = [
            {
                "node_id": other["node_id"],
                "endpoint_id": endpoint_ids[other["node_id"]],
                "deployment_epoch": assignment["deployment_epoch"],
                "membership_generation": membership_generations[other["node_id"]],
                "valid_from": now,
                "valid_until": now + 3_600.0,
            }
            for other in assignments
            if other["node_id"] != assignment["node_id"]
        ]
        peer_records.sort(key=lambda record: record["node_id"])
        message = {
            "protocol": ASSIGNMENT_OFFER_PROTOCOL,
            "message_id": f"{route_label}-qwen-offer-{index}",
            "swarm_id": swarm_id,
            "sender_node_id": "seed-node",
            "sender_endpoint_id": signer.endpoint_id,
            "recipient_node_id": assignment["node_id"],
            "incarnation": f"{route_label}-qwen-incarnation",
            "generation": membership_generations[assignment["node_id"]],
            "issued_at": now,
            "expires_at": now + 3_600.0,
            "deployment_id": assignment["deployment_id"],
            "deployment_epoch": assignment["deployment_epoch"],
            "assignment_id": assignment["assignment_id"],
            "assignment_digest": _digest(_bytes(assignment)),
            "stage_pack_digest": pack["stage_pack_digest"],
            "graph_digest": graph_digest,
            "load_generation": LOAD_GENERATION,
            "placement_provenance": placement_provenance,
            "peer_endpoint_records": peer_records,
        }
        offers.append(sign_membership_message(signer=signer, message=message))
    return {
        "protocol": "mycelium.controller_membership_snapshot.v1",
        "seed_key_digest": signer.verification_key_digest,
        "swarm_id": swarm_id,
        "deployment_id": assignments[0]["deployment_id"],
        "deployment_epoch": assignments[0]["deployment_epoch"],
        "assignment_offers": offers,
    }


def _deployment_prompt_codec(manifest: object, deployment_root: Path) -> Any:
    """The prompt codec of the manifest's architecture (Qwen3: thinking off)."""

    runtime_model = (
        manifest.get("runtime_model") if isinstance(manifest, Mapping) else None
    )
    architecture = (
        runtime_model.get("architecture") if isinstance(runtime_model, Mapping) else None
    )
    if architecture == "qwen3":
        return Qwen3PromptCodec.from_deployment(deployment_root)
    if architecture in {"qwen3_5", "qwen3_5_moe"}:
        return Qwen35PromptCodec.from_deployment(deployment_root)
    return Qwen2PromptCodec.from_deployment(deployment_root)


def _challenge(
    codec: Qwen2PromptCodec,
    loaded: list[Any],
    runtime_backends: list[str],
    *,
    generated_token_count: int = 4,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not loaded:
        raise RuntimeError("challenge_requires_one_or_more_stages")
    if len(runtime_backends) != len(loaded):
        raise RuntimeError("challenge_runtime_count_mismatch")
    context = codec.encode("Reply with exactly the word ready.")
    generated: list[int] = []
    for _ in range(generated_token_count):
        stage_output: Any = None
        for index, (stage, backend) in enumerate(
            zip(loaded, runtime_backends, strict=True)
        ):
            if backend == "mlx":
                stage_output = (
                    execute_loaded_stage(
                        stage,
                        token_ids=mx.array((context,), dtype=mx.int32),
                    )
                    if index == 0
                    else execute_loaded_stage(
                        stage,
                        hidden_states=mx.array(stage_output, dtype=mx.float32),
                    )
                )
                mx.eval(stage_output)
            elif backend == "numpy":
                stage_output = (
                    execute_loaded_numpy_stage(
                        stage,
                        token_ids=np.asarray((context,), dtype=np.int64),
                    )
                    if index == 0
                    else execute_loaded_numpy_stage(
                        stage,
                        hidden_states=np.asarray(stage_output),
                    )
                )
            elif backend == PHONE_BACKEND:
                if index in (0, len(loaded) - 1):
                    raise RuntimeError("external_node_stage_only")
                stage_output = stage.execute(stage_output)
            else:
                raise RuntimeError("challenge_runtime_backend_unsupported")
        logits = stage_output
        token_id = quantized_greedy_token_id(logits[0, -1, :].tolist())
        generated.append(token_id)
        context = (*context, token_id)
    return codec.encode("Reply with exactly the word ready."), tuple(generated)


def _release_runtime_memory() -> None:
    gc.collect()
    clear_cache = getattr(mx, "clear_cache", None)
    if callable(clear_cache):
        clear_cache()


def _streaming_challenge(
    codec: Qwen2PromptCodec,
    assignments: list[dict[str, Any]],
    reports: list[dict[str, Any]],
    runtime_backends: list[str],
    *,
    generated_token_count: int = 2,
    phone_executors: Mapping[str, RustStageExecutor] | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Challenge a large candidate with at most one stage's weights resident."""

    if (
        not assignments
        or len(assignments) != len(reports)
        or len(assignments) != len(runtime_backends)
        or not isinstance(generated_token_count, int)
        or isinstance(generated_token_count, bool)
        or not 2 <= generated_token_count <= 127
    ):
        raise RuntimeError("challenge_runtime_count_mismatch")
    prompt = codec.encode("Reply with exactly the word ready.")
    context = prompt
    generated: list[int] = []
    for _ in range(generated_token_count):
        stage_output: Any = None
        for index, (assignment, report, backend) in enumerate(
            zip(assignments, reports, runtime_backends, strict=True)
        ):
            if backend == PHONE_BACKEND:
                if index in (0, len(assignments) - 1):
                    raise RuntimeError("external_node_stage_only")
                executor = (phone_executors or {}).get(assignment["node_id"])
                if executor is None:
                    raise RuntimeError("phone_challenge_executor_required")
                stage_output = executor.execute(stage_output)
                continue
            loaded = load_assignment_stage(
                assignment,
                report,
                load_generation=LOAD_GENERATION,
            )
            try:
                if backend == "mlx":
                    stage_output = (
                        execute_loaded_stage(
                            loaded,
                            token_ids=mx.array((context,), dtype=mx.int32),
                        )
                        if index == 0
                        else execute_loaded_stage(
                            loaded,
                            hidden_states=mx.array(stage_output, dtype=mx.float32),
                        )
                    )
                    mx.eval(stage_output)
                elif backend == "numpy":
                    stage_output = (
                        execute_loaded_numpy_stage(
                            loaded,
                            token_ids=np.asarray((context,), dtype=np.int64),
                        )
                        if index == 0
                        else execute_loaded_numpy_stage(
                            loaded,
                            hidden_states=np.asarray(stage_output),
                        )
                    )
                else:
                    raise RuntimeError("challenge_runtime_backend_unsupported")
            finally:
                del loaded
                _release_runtime_memory()
        token_id = quantized_greedy_token_id(stage_output[0, -1, :].tolist())
        generated.append(token_id)
        context = (*context, token_id)
    return prompt, tuple(generated)


def _prepare_model_representation(
    args: argparse.Namespace,
    prepare: Any,
    *,
    shard_count: int,
    layer_ranges: Any,
) -> tuple[Any, dict[str, Any] | None]:
    """Prepare assignments from the source or its stage-sharded representation.

    The stage-sharded branch writes the deployment safetensors that members
    receive; ``stage_sharding["emitted_root"]`` names where they were emitted.
    """

    stage_sharding = None
    reusable_stage_root = getattr(args, "reuse_stage_sharded_root", None)
    emit_stage_shards = getattr(args, "emit_stage_shards", None)
    if getattr(args, "stage_sharded", False) and reusable_stage_root is not None:
        raise RuntimeError("stage_sharding_source_conflict")
    if emit_stage_shards is not None and not (
        getattr(args, "stage_sharded", False) or reusable_stage_root is not None
    ):
        raise RuntimeError("emit_stage_shards_requires_stage_branch")
    if reusable_stage_root is not None:
        candidate = Path(reusable_stage_root)
        try:
            reusable_root = candidate.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("stage_sharding_reuse_invalid") from exc
        if candidate.is_symlink() or not reusable_root.is_dir():
            raise RuntimeError("stage_sharding_reuse_invalid")
        # A prior immutable representation may have been file-sharded at a
        # different layer boundary. Feeding those files directly to a new
        # assignment can make one physical file contain tensors owned by two
        # peers. Repartition the already-created representation to the exact
        # current ranges before assignment compilation; this does not read the
        # source checkpoint or perform another quantization.
        with _stage_shard_destination(
            emit_stage_shards,
            getattr(args, "temporary_root", None),
            prefix="mycelium-reused-stage-shards-",
        ) as sharded_root:
            repartition = shard_qwen2_checkpoint(
                reusable_root,
                sharded_root,
                shard_count=shard_count,
                layer_ranges=layer_ranges,
                max_file_bytes=_MAX_STAGED_MODEL_FILE_BYTES,
            )
            prepared = prepare(sharded_root)
        stage_sharding = {
            "protocol": "mycelium.stage_sharding_reuse.v2",
            "manifest_digest": "sha256:"
            + prepared.manifest["manifest_digest"]["value"],
            "model_id": prepared.manifest["model_id"],
            "resolved_commit": prepared.manifest["resolved_commit"],
            "layer_ranges": repartition["layer_ranges"],
            "source_representation_reused": True,
            "source_requantized": False,
            "verified_by_builder": True,
        }
        if emit_stage_shards is not None:
            stage_sharding["emitted_root"] = str(sharded_root)
    elif getattr(args, "stage_sharded", False):
        with _stage_shard_destination(
            emit_stage_shards,
            getattr(args, "temporary_root", None),
            prefix="mycelium-stage-shards-",
        ) as sharded_root:
            stage_sharding = shard_qwen2_checkpoint(
                args.model_root,
                sharded_root,
                shard_count=shard_count,
                layer_ranges=layer_ranges,
                max_file_bytes=_MAX_STAGED_MODEL_FILE_BYTES,
            )
            prepared = prepare(sharded_root)
        if emit_stage_shards is not None:
            stage_sharding["emitted_root"] = str(sharded_root)
    else:
        prepared = prepare(args.model_root)
    return prepared, stage_sharding


def build(args: argparse.Namespace) -> dict[str, Any]:
    # Native helpers must be reaped even if later preparation or serialization fails.
    with contextlib.ExitStack() as resources:
        return _build(args, resources)


def _build(args: argparse.Namespace, resources: contextlib.ExitStack) -> dict[str, Any]:
    repo = ((Path(sys._MEIPASS)/'a13-runtime-source').resolve()
            if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1])
    route_label = _route_label(args)
    model_id, resolved_commit, model_slug = _model_identity(args)
    template = json.loads(args.template_plan.read_text("utf-8"))
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise RuntimeError("output_root_already_exists")
    topology = _topology_nodes(
        template,
        args.topology,
        managed_transport=bool(getattr(args, "managed_transport", False)),
    )
    role_binding_path = getattr(args, "role_binding", None)
    if role_binding_path is not None:
        check_topology_against_role_binding(
            topology, json.loads(Path(role_binding_path).read_text("utf-8"))
        )
    node_ids = tuple(node["node_id"] for node in topology)
    runtime_backends = {node["node_id"]: node["runtime_backend"] for node in topology}
    managed_ranges = _managed_layer_ranges(
        getattr(args, "managed_layer_ranges", None),
        node_ids=node_ids,
        managed_transport=bool(getattr(args, "managed_transport", False)),
    )
    deployment_id = getattr(args, "deployment_id", None) or str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "mycelium:" + ":".join((route_label, model_id, resolved_commit, *node_ids)),
        )
    )
    try:
        if str(uuid.UUID(deployment_id)) != deployment_id:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("deployment_id_invalid") from exc
    phone_evidence = {}
    phone_executors = {}
    phone_run_id = None
    phone_document = None
    prepare_only = bool(getattr(args, "phone_prepare_only", False))
    has_phone = PHONE_BACKEND in runtime_backends.values()
    if prepare_only and not has_phone:
        raise RuntimeError("phone_preparation_requires_phone")
    if has_phone and not prepare_only:
        path = getattr(args, "phone_evidence", None)
        seed_digest = getattr(args, "phone_seed_key_digest", None)
        if path is None or not seed_digest:
            raise RuntimeError("phone_authorized_evidence_required")
        phone_document = json.loads(Path(path).read_text("utf-8"))
        if phone_document.get("protocol") != "mycelium.phone_builder_evidence.v1":
            raise RuntimeError("phone_evidence_protocol_invalid")
        if any(getattr(args, field, None) is None for field in
               ("phone_executor", "phone_apk", "phone_source_root")):
            raise RuntimeError("phone_build_provenance_required")
        phone_run_id = phone_document["run_id"]
        phone_nodes = [n for n in topology if n["runtime_backend"] == PHONE_BACKEND]
        if set(phone_document["nodes"]) != {n["node_id"] for n in phone_nodes}:
            raise RuntimeError("phone_evidence_node_set_mismatch")
        for node in phone_nodes:
            evidence = PhoneEvidence(phone_document["nodes"][node["node_id"]],
                seed_key_digest=seed_digest, node=node, run_id=phone_run_id, deployment_id=deployment_id)
            validate_build_receipt(evidence.receipt, expected_digest=evidence.statement["build_receipt_digest"],
                binary=args.phone_executor, apk=args.phone_apk, source_root=args.phone_source_root)
            phone_evidence[node["node_id"]] = evidence
    managed_observations = None
    managed_run_id = None
    if getattr(args, "managed_transport", False):
        observations_path = getattr(args, "managed_hello_observations", None)
        if observations_path is None:
            raise RuntimeError("managed_peer_observations_missing")
        try:
            observations_document = json.loads(
                Path(observations_path).read_text("utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("managed_observations_invalid") from exc
        managed_run_id, managed_observations = _managed_hello_observations(
            observations_document, node_ids, deployment_id=deployment_id
        )
    preflight_remote_peers(topology)
    bundle = output_root / "transfer-bundle"
    deployment_root = bundle / "deployment"
    deployment_root.mkdir(parents=True)
    preparation_authorization, preparation_ranges = _preparation_authorization(
        getattr(args, "model_preparation_authorization", None),
        model_id=model_id,
        resolved_commit=resolved_commit,
        topology=topology,
    )
    runtime_dtype = (
        "float32"
        if preparation_authorization is None
        else preparation_authorization["serving_dtype"]
    )
    runtime_quantization = (
        "int8-weight-only"
        if preparation_authorization is None
        else preparation_authorization["serving_quantization"]
    )
    if phone_evidence and (runtime_dtype != "float32" or runtime_quantization != "int8-weight-only"):
        raise RuntimeError("phone_runtime_representation_invalid")
    m13_document, planned_ranges = _m13_control_plane(
        getattr(args, "m18_control_plane", None)
        or getattr(args, "m14_control_plane", None)
        or getattr(args, "m13_control_plane", None),
        node_ids=node_ids,
        deployment_id=deployment_id,
    )
    is_m18 = (
        m13_document is not None
        and m13_document["protocol"] == "mycelium.m18_physical_candidate.v1"
    )
    if is_m18 and phone_evidence:
        raise RuntimeError("phone_replica_plan_unsupported")
    if managed_ranges is not None and (
        m13_document is not None or preparation_ranges is not None
    ):
        # One placement authority per build: an owner capability split never
        # silently overrides a signed control-plane or preparation plan.
        raise RuntimeError("managed_layer_ranges_conflict")
    prepare_node_ids, prepare_ranges = _preparation_selection(
        m13_document if is_m18 else None,
        node_ids=node_ids,
        planned_ranges=planned_ranges,
        preparation_ranges=preparation_ranges,
        managed_ranges=managed_ranges,
    )

    def prepare(model_root: Path) -> Any:
        return prepare_assignment_artifacts(
            deployment_root,
            node_ids=prepare_node_ids,
            model_source=LocalModelSource(
                root=model_root,
                model_id=model_id,
                requested_revision="main",
                resolved_commit=resolved_commit,
            ),
            runtime_dtype=runtime_dtype,
            runtime_backends_by_node={
                node_id: runtime_backends[node_id] for node_id in prepare_node_ids
            },
            runtime_quantization=runtime_quantization,
            deployment_id=deployment_id,
            layer_ranges=prepare_ranges,
            control_plane_binding=(
                None
                if preparation_authorization is None
                else {
                    "protocol": "mycelium.control_plane_binding.v1",
                    "evidence_bundle_digest": preparation_authorization[
                        "operation_digest"
                    ],
                    "planner_snapshot_digest": preparation_authorization[
                        "preparation_binding_digest"
                    ],
                    "snapshot_generation": preparation_authorization[
                        "evidence_generation"
                    ],
                    "swarm_id": "live-qualified-swarm",
                    "deployment_id": deployment_id,
                    "deployment_epoch": 1,
                }
            ),
        )

    prepared, stage_sharding = _prepare_model_representation(
        args, prepare, shard_count=len(prepare_node_ids), layer_ranges=prepare_ranges
    )

    peers = [
        {
            field: node[field]
            for field in (
                "node_id",
                "ssh_target",
                "host_id",
                "boot_id",
                "staging_root",
                "process_transport",
                "ssh_identity_file",
            )
            if field in node
        }
        for node in topology
    ]
    for peer in peers:
        if peer["process_transport"] == "external_iroh":
            peer["ssh_target"] = None
            peer["ssh_identity_file"] = None
    staging_roots = {node["node_id"]: node["staging_root"] for node in topology}
    cache_roots = {
        node_id: f"{root}/deployment" for node_id, root in staging_roots.items()
    }
    runtime_by_node = {
        node_id: {
            "backend": backend,
            "dtype": runtime_dtype,
            "quantization": runtime_quantization,
        }
        for node_id, backend in runtime_backends.items()
    }
    if m13_document is None:
        assignments = compile_layer_assignments(
            route_plan=prepared.route,
            manifest=prepared.manifest,
            deployment_id=prepared.assignments[0]["deployment_id"],
            deployment_epoch=prepared.assignments[0]["deployment_epoch"],
            cache_roots=cache_roots,
            runtime_by_node=runtime_by_node,
            control_plane_binding=prepared.assignments[0]["control_plane_binding"],
        )
    else:
        _validate_m13_control_plane(m13_document, manifest=prepared.manifest)
        expected_runtime = m13_document["planner_snapshot"].get("node_runtime")
        expected_modes = (
            {
                value.get("decode_mode")
                for value in expected_runtime.values()
                if isinstance(value, dict)
            }
            if isinstance(expected_runtime, dict)
            else set()
        )
        required_mode = (
            "complete_context_replay"
            if {"numpy", PHONE_BACKEND} & set(runtime_backends.values())
            else "stage_local_kv"
        )
        accepted_modes = {required_mode}
        if is_m18 and set(runtime_backends.values()) == {"mlx"}:
            accepted_modes.add("complete_context_replay")
        if (
            not isinstance(expected_runtime, dict)
            or len(expected_modes) != 1
            or not expected_modes <= accepted_modes
            or any(
                expected_runtime.get(node_id, {}).get("backend") != backend
                for node_id, backend in runtime_backends.items()
            )
        ):
            raise RuntimeError("m13_control_plane_runtime_mismatch")
        if is_m18:
            assignments_by_placement = compile_bound_replica_assignments(
                route_plan=m13_document["route_plan"],
                planner_snapshot=m13_document["planner_snapshot"],
                evidence_bundle=m13_document["signed_evidence_bundle"][
                    "evidence_bundle"
                ],
                manifest=prepared.manifest,
                deployment_id=deployment_id,
                deployment_epoch=1,
                cache_roots=cache_roots,
                runtime_by_node=runtime_by_node,
            )
            placement_by_node = {
                assignment["node_id"]: placement_id
                for placement_id, assignment in assignments_by_placement.items()
            }
            if set(placement_by_node) != set(node_ids):
                raise RuntimeError("m18_control_plane_assignment_mismatch")
            placement_ids = [placement_by_node[node_id] for node_id in node_ids]
            assignments = [
                assignments_by_placement[placement_id] for placement_id in placement_ids
            ]
            local_assignments_by_placement = compile_bound_replica_assignments(
                route_plan=m13_document["route_plan"],
                planner_snapshot=m13_document["planner_snapshot"],
                evidence_bundle=m13_document["signed_evidence_bundle"][
                    "evidence_bundle"
                ],
                manifest=prepared.manifest,
                deployment_id=deployment_id,
                deployment_epoch=1,
                cache_roots={node_id: str(deployment_root) for node_id in node_ids},
                runtime_by_node=runtime_by_node,
            )
            local_assignments = [
                local_assignments_by_placement[placement_id]
                for placement_id in placement_ids
            ]
        else:
            assignments = compile_bound_layer_assignments(
                route_plan=m13_document["route_plan"],
                planner_snapshot=m13_document["planner_snapshot"],
                evidence_bundle=m13_document["signed_evidence_bundle"][
                    "evidence_bundle"
                ],
                manifest=prepared.manifest,
                deployment_id=deployment_id,
                deployment_epoch=1,
                cache_roots=cache_roots,
                runtime_by_node=runtime_by_node,
            )
    if prepare_only:
        preparation = dict(protocol="mycelium.phone_assignment_preparation.v1",
            deployment_id=deployment_id, assignments=assignments, manifest=prepared.manifest,
            route_ready=False, claim_boundary="Assignment inputs only; no load, seed offer, or route authority.")
        _write_document(output_root / "phone-preparation.json", preparation)
        return preparation
    if phone_evidence:
        external_control = external_control_descriptor(template["controller"].get("external_control"))
        if any(e.statement["controller_endpoint_id"] != external_control["controller_endpoint_id"]
               for e in phone_evidence.values()):
            raise RuntimeError("phone_controller_not_authorized")
    phone_proofs = {}
    for index, assignment in enumerate(assignments):
        evidence = phone_evidence.get(assignment["node_id"])
        if evidence is not None:
            phone_proofs[assignment["node_id"]] = evidence.bind_assignment(assignment,
                stage_id=f"stage-{index:03d}", snapshot_root=args.model_root,
                model_manifest=prepared.manifest)
            executor = resources.enter_context(RustStageExecutor(args.phone_executor, args.model_root,
                evidence.manifest["start_layer"], evidence.manifest["end_layer_exclusive"],
                hidden_size=evidence.manifest["architecture"]["hidden_size"]))
            from mycelium_mobile.qwen_stage_manifest import golden_input_f32
            golden = evidence.manifest["golden"]
            hidden = evidence.manifest["architecture"]["hidden_size"]
            probe = np.frombuffer(golden_input_f32(golden["seed"], golden["seq"], hidden), dtype="<f4")
            output = executor.execute(probe.reshape(1, golden["seq"], hidden))
            if _digest(output.astype("<f4").tobytes()) != golden["output_digest"]:
                raise RuntimeError("phone_host_golden_mismatch")
            phone_executors[assignment["node_id"]] = executor
    if is_m18:

        def local_fetch(
            model_id: str,
            revision: str,
            filename: str,
            cache_root: Path,
            *,
            local_files_only: bool,
        ) -> tuple[Path, bool]:
            del model_id, revision, cache_root, local_files_only
            return deployment_root / filename, True

        local_provisioning_reports = [
            provision_assignment(
                assignment,
                fetch_file=local_fetch,
                local_files_only=True,
            )
            for assignment in local_assignments
        ]
        local_packs = [
            compile_stage_pack(assignment, prepared.manifest, report)
            for assignment, report in zip(
                local_assignments, local_provisioning_reports, strict=True
            )
        ]
        local_verifications = [
            verify_stage_pack(
                pack,
                assignment=assignment,
                manifest=prepared.manifest,
            )
            for assignment, pack in zip(local_assignments, local_packs, strict=True)
        ]
        local_reports = [
            artifact_report_for_loader(
                pack,
                verification,
                assignment=assignment,
                manifest=prepared.manifest,
            )
            for assignment, pack, verification in zip(
                local_assignments,
                local_packs,
                local_verifications,
                strict=True,
            )
        ]
        base_loaded = [
            load_assignment_stage(assignment, report, load_generation=LOAD_GENERATION)
            for assignment, report in zip(local_assignments, local_reports, strict=True)
        ]
        report_sources = local_reports
    elif preparation_authorization is not None:
        base_loaded = []
        base_proof_documents = []
        for assignment, report in zip(
            prepared.assignments, prepared.reports, strict=True
        ):
            if assignment["node_id"] in phone_proofs:
                base_proof_documents.append(phone_proofs[assignment["node_id"]])
                continue
            loaded = load_assignment_stage(
                assignment,
                report,
                load_generation=LOAD_GENERATION,
            )
            base_proof_documents.append(json.loads(canonical_json(loaded.proof)))
            del loaded
            _release_runtime_memory()
        report_sources = prepared.reports
    else:
        base_loaded = [
            (phone_executors[assignment["node_id"]] if assignment["node_id"] in phone_executors
             else load_assignment_stage(assignment, report, load_generation=LOAD_GENERATION))
            for assignment, report in zip(
                prepared.assignments, prepared.reports, strict=True
            )
        ]
        report_sources = prepared.reports
    target_reports = [
        _target_report(report, assignment)
        for report, assignment in zip(report_sources, assignments, strict=True)
    ]
    packs = [
        compile_stage_pack(
            assignment,
            prepared.manifest,
            report,
            source_artifact_root=deployment_root,
        )
        for assignment, report in zip(assignments, target_reports, strict=True)
    ]
    verifications = [
        verify_stage_pack(
            pack,
            assignment=assignment,
            manifest=prepared.manifest,
            source_artifact_root=deployment_root,
        )
        for assignment, pack in zip(assignments, packs, strict=True)
    ]

    proof_documents = (
        base_proof_documents
        if preparation_authorization is not None
        else [phone_proofs[a["node_id"]] if a["node_id"] in phone_proofs
              else json.loads(canonical_json(loaded.proof))
              for a, loaded in zip(assignments, base_loaded, strict=True)]
    )
    proofs = [
        phone_proofs[assignment["node_id"]] if assignment["node_id"] in phone_proofs else _target_proof(
            proof_document,
            assignment,
            pack,
            verification,
        )
        for proof_document, assignment, pack, verification in zip(
            proof_documents, assignments, packs, verifications, strict=True
        )
    ]
    if (
        m13_document is not None
        and m13_document["protocol"] == "mycelium.m18_physical_candidate.v1"
    ):
        graph = build_replicated_execution_graph(
            dict(zip(placement_ids, assignments, strict=True)),
            dict(zip(placement_ids, proofs, strict=True)),
            m13_document["route_plan"],
            link_scheme="iroh",
            runtime_scheme="iroh",
        )
    else:
        graph = build_execution_graph(
            assignments, proofs, link_scheme="iroh", runtime_scheme="iroh"
        )
    graph = bind_phone_graph(graph, phone_evidence)
    graph_document = _physical_graph_document(graph)
    device_states = {
        node_id: asdict(state)
        for node_id, state in build_physical_device_states(graph).items()
    }
    codec = _deployment_prompt_codec(prepared.manifest, deployment_root)
    if (
        m13_document is not None
        and m13_document["protocol"] == "mycelium.m18_physical_candidate.v1"
    ):
        primary_track = m13_document["route_plan"]["legal_tracks"][0]["placement_ids"]
        loaded_by_node = {
            assignment["node_id"]: loaded
            for assignment, loaded in zip(assignments, base_loaded, strict=True)
        }
        assignment_by_placement = dict(zip(placement_ids, assignments, strict=True))
        challenge_loaded = [
            loaded_by_node[assignment_by_placement[item]["node_id"]]
            for item in primary_track
        ]
        challenge_backends = [
            assignment_by_placement[item]["runtime"]["backend"]
            for item in primary_track
        ]
    else:
        challenge_loaded = base_loaded
        challenge_backends = [
            assignment["runtime"]["backend"] for assignment in assignments
        ]
    if preparation_authorization is not None:
        challenge_prompt, challenge_output = _streaming_challenge(
            codec,
            list(prepared.assignments),
            list(prepared.reports),
            challenge_backends,
            phone_executors=phone_executors,
        )
    else:
        challenge_prompt, challenge_output = _challenge(
            codec,
            challenge_loaded,
            challenge_backends,
        )
    if is_m18:
        model_config = json.loads(
            (deployment_root / "config.json").read_text(encoding="utf-8")
        )
        if not isinstance(model_config, dict):
            raise RuntimeError("m18_model_context_capacity_invalid")
        qualification_token_count = len(challenge_prompt) + len(challenge_output)
        track_membership_count = {
            placement_id: sum(
                placement_id in track["placement_ids"]
                for track in m13_document["route_plan"]["legal_tracks"]
            )
            for placement_id in placement_ids
        }
        for placement_id, assignment in zip(placement_ids, assignments, strict=True):
            device_states[assignment["node_id"]]["available_kv_bytes"] = (
                _m18_runtime_kv_bytes(
                    model_config,
                    qualification_token_count=qualification_token_count,
                    track_membership_count=track_membership_count[placement_id],
                )
            )

    challenge_output_text = "".join(
        codec.decode_token(token_id) for token_id in challenge_output
    )
    _write_durable_document(
        output_root / "local-challenge-checkpoint.json",
        _local_challenge_checkpoint(
            model_id=model_id,
            resolved_commit=resolved_commit,
            prepared=prepared,
            assignments=assignments,
            proofs=proofs,
            challenge_prompt=challenge_prompt,
            challenge_output=challenge_output,
            challenge_output_text=challenge_output_text,
            runtime_backends=runtime_backends,
            preparation_authorization=preparation_authorization,
            stage_sharding=stage_sharding,
            phone_challenges={node: {"build_receipt": phone_evidence[node].receipt,
                                     "steps": executor.records}
                              for node, executor in phone_executors.items()},
        ),
    )

    control = bundle / "control"
    if preparation_authorization is not None:
        _write_document(
            control / "model-preparation-authorization.json",
            preparation_authorization,
        )
    _write_document(control / "model-manifest.json", prepared.manifest)
    _write_document(control / "execution-graph.json", graph_document)
    _write_document(control / "device-states.json", device_states)
    for index, (assignment, report, pack, verification, proof) in enumerate(
        zip(assignments, target_reports, packs, verifications, proofs, strict=True)
    ):
        _write_document(control / f"node-{index}-assignment.json", assignment)
        _write_document(control / f"node-{index}-artifact-report.json", report)
        _write_document(control / f"node-{index}-stage-pack.json", pack)
        _write_document(
            control / f"node-{index}-stage-pack-verification.json", verification
        )
        _write_document(control / f"node-{index}-load-proof.json", proof)
    materializations: dict[str, dict[str, Any]] = {}
    if m13_document is not None:
        milestone = (
            "m18"
            if m13_document["protocol"] == "mycelium.m18_physical_candidate.v1"
            else "m13"
        )
        _write_document(
            control / f"{milestone}-signed-evidence-bundle.json",
            m13_document["signed_evidence_bundle"],
        )
        _write_document(
            control / f"{milestone}-planner-snapshot.json",
            m13_document["planner_snapshot"],
        )
        _write_document(
            control / f"{milestone}-route-plan.json", m13_document["route_plan"]
        )
        cache_files = sorted(deployment_root.glob("*.safetensors"))
        cache = AssignmentArtifactCache(
            output_root / "artifact-cache",
            max_bytes=max(1, sum(path.stat().st_size for path in cache_files) * 2),
        )
        object_keys: dict[str, ArtifactObjectKey] = {}
        for path in cache_files:
            tensor_digest = _file_digest(path)
            key = ArtifactObjectKey(
                model_revision=prepared.manifest["resolved_commit"],
                manifest_digest="sha256:"
                + prepared.manifest["manifest_digest"]["value"],
                format=prepared.manifest["format"],
                quantization=runtime_quantization,
                tensor_digest=tensor_digest,
                size_bytes=path.stat().st_size,
            )
            cache.store(key, path)
            object_keys[path.name] = key
        for assignment, pack in zip(assignments, packs, strict=True):
            required = {
                artifact["upstream_path"]: object_keys[artifact["upstream_path"]]
                for artifact in pack["artifacts"]
            }
            materialization = cache.materialize_assignment(
                assignment_id=assignment["assignment_id"],
                required_objects=required,
                destination=output_root
                / "assignment-materializations"
                / assignment["node_id"],
            )
            materializations[assignment["node_id"]] = materialization
            _write_document(
                control / f"{assignment['node_id']}-assignment-materialization.json",
                materialization,
            )
        _write_document(
            control / f"{milestone}-assignment-cache-status.json", cache.status()
        )
        if m13_document["protocol"] == "mycelium.m18_physical_candidate.v1":
            _write_document(
                deployment_root / "m18-planner-route.json",
                m13_document["route_plan"],
            )
        else:
            projection = build_m13_placement_projection(
                planner_snapshot=m13_document["planner_snapshot"],
                route_plan=m13_document["route_plan"],
                assignments=assignments,
                materializations_by_node=materializations,
                load_proof_digests_by_node={
                    assignment["node_id"]: preparation_proof_digest(proof)
                    for assignment, proof in zip(assignments, proofs, strict=True)
                },
                promotion_report=None,
                exclusions=_placement_exclusions(m13_document),
                ab_deltas=m13_document["ab_deltas"],
            )
            _write_document(
                deployment_root / "m13-placement-projection.json", projection
            )
        if m13_document["protocol"] == "mycelium.m14_physical_candidate.v1":
            _write_document(
                control / "m14-transport-observations.json",
                m13_document["transport_observations"],
            )
            _write_document(
                control / "m14-topology-decision.json",
                m13_document["topology_decision"],
            )
            _write_document(
                deployment_root / "m14-topology-projection.json",
                m13_document["topology_projection"],
            )
    _copy_runtime_closure(repo, bundle, template)

    now = time.time()
    if any(now >= e.statement["expires_at"] for e in phone_evidence.values()):
        raise RuntimeError("phone_authority_stale")
    if phone_evidence and getattr(args, "phone_seed_state_root", None) is not None:
        from mycelium_seed.phone_preparation import preparation_membership_snapshot
        phone_document["membership_snapshot"] = preparation_membership_snapshot(
            args.phone_seed_state_root, seed_key_digest=args.phone_seed_key_digest,
            assignments=assignments, packs=packs, graph_document=graph_document,
            topology=topology, phone_evidence=phone_evidence,
            controller_endpoint_id=external_control["controller_endpoint_id"],
            placement_provenance=_placement_provenance(m13_document=m13_document,
                preparation_authorization=preparation_authorization))
    membership = authorized_membership_snapshot(
        phone_document["membership_snapshot"], seed_key_digest=args.phone_seed_key_digest,
        assignments=assignments, packs=packs, graph_document=graph_document,
        topology=topology, phone_evidence=phone_evidence, now=time.time(),
    ) if phone_evidence else _membership_snapshot(
        assignments=assignments,
        packs=packs,
        graph_document=graph_document,
        endpoint_ids={node["node_id"]: node["endpoint_id"] for node in topology},
        membership_generations={
            node["node_id"]: node["membership_generation"] for node in topology
        },
        now=now,
        route_label=route_label,
        placement_provenance=_placement_provenance(
            m13_document=m13_document,
            preparation_authorization=preparation_authorization,
        ),
    )
    request = _startup_challenge_request(route_label,challenge_prompt,challenge_output)
    topology_by_node = {node["node_id"]: node for node in topology}
    nodes = []
    for index, assignment in enumerate(assignments):
        node_id = assignment["node_id"]
        topology_node = topology_by_node[node_id]
        if node_id in phone_evidence:
            nodes.append({"node_id": node_id, "configure": phone_evidence[node_id].profile})
            continue
        nodes.append(
            {
                "node_id": node_id,
                "python_executable": topology_node["python_executable"],
                "socket_root": f"{staging_roots[node_id]}/socket",
                "sidecar_binary": topology_node["sidecar_binary"],
                "endpoint_secret_file": topology_node["endpoint_secret_file"],
                "configure": {
                    "assignment_file": f"control/node-{index}-assignment.json",
                    "manifest_file": "control/model-manifest.json",
                    "stage_pack_file": f"control/node-{index}-stage-pack.json",
                    "graph": graph_document,
                    "device_states": device_states,
                    "load_generation": LOAD_GENERATION,
                },
            }
        )
    run_id = (
        phone_run_id or managed_run_id
        or f"{route_label}-qwen-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    )
    peers = refresh_peer_identities(
        peers,
        run_id,
        python_executables_by_node={
            node["node_id"]: node["python_executable"] for node in topology
            if node["runtime_backend"] != PHONE_BACKEND
        },
        managed_observations=managed_observations,
        phone_evidence=phone_evidence,
    )
    run_plan = {
        "protocol": "mycelium.controller_run_plan.v1",
        "run_id": run_id,
        "deployment_id": assignments[0]["deployment_id"],
        "entry_node_id": assignments[0]["node_id"],
        "nodes": nodes,
        "request": request,
        "decode_count": len(challenge_output) - 1,
        "expected_token_ids": list(challenge_output),
        **({"decode_mode": "complete_context_replay"} if phone_evidence else {}),
    }
    transfer_manifest = _transfer_manifest(bundle)
    node_transfer_manifests = _node_transfer_manifests(transfer_manifest, packs)
    plan = copy.deepcopy(template)
    plan["run_id"] = run_id
    representation_slug = re.sub(
        r"[^a-z0-9]+", "-", runtime_quantization.lower()
    ).strip("-")
    plan["plan_id"] = (
        f"{route_label}-{model_slug}-{representation_slug}-{len(topology)}-host"
    )
    plan["now_unix_ms"] = int(now * 1_000)
    plan["controller"].update(
        {
            "source_root": str(bundle),
            "now": now,
            "peers": peers,
            "membership_snapshot": membership,
            "run_plan": run_plan,
            "transfer_manifest": transfer_manifest,
            "node_transfer_manifests": node_transfer_manifests,
        }
    )
    operator_plan = output_root / "operator-plan.json"
    _write_document(operator_plan, plan)
    report = {
        "protocol": f"mycelium.{route_label.replace('-', '_')}_qwen_plan.v1",
        "operator_plan": str(operator_plan),
        "run_id": run_id,
        "model_id": model_id,
        "resolved_commit": resolved_commit,
        "quantization": runtime_quantization,
        "source_artifact_digest": (
            None
            if preparation_authorization is None
            else preparation_authorization.get("source_artifact_digest")
        ),
        "source_quantization": (
            None
            if preparation_authorization is None
            else preparation_authorization["source_quantization"]
        ),
        "serving_dtype": (
            None
            if preparation_authorization is None
            else preparation_authorization["serving_dtype"]
        ),
        "quantizer": (
            None
            if preparation_authorization is None
            else preparation_authorization.get("quantizer")
        ),
        "representation_digest": (
            None
            if preparation_authorization is None
            else preparation_authorization["representation_digest"]
        ),
        "owner_decision_digest": (
            None
            if preparation_authorization is None
            else preparation_authorization["owner_decision_digest"]
        ),
        "preparation_binding_digest": (
            None
            if preparation_authorization is None
            else preparation_authorization["preparation_binding_digest"]
        ),
        "layer_ranges": [assignment["range"] for assignment in assignments],
        "runtime_backends": [
            assignment["runtime"]["backend"] for assignment in assignments
        ],
        "challenge_prompt_token_count": len(challenge_prompt),
        "challenge_output_token_ids": list(challenge_output),
        "challenge_output_text": challenge_output_text,
        "transfer_file_count": len(transfer_manifest["files"]),
        "transfer_bytes": sum(
            record["size_bytes"] for record in transfer_manifest["files"]
        ),
        "per_node_transfer_bytes": {
            node_id: sum(record["size_bytes"] for record in manifest["files"])
            for node_id, manifest in node_transfer_manifests["manifests"].items()
        },
        "stage_sharding": stage_sharding,
        "placement_provenance": _placement_provenance(
            m13_document=m13_document,
            preparation_authorization=preparation_authorization,
        ),
        "planner_snapshot_digest": (
            m13_document["route_plan"]["snapshot_digest"]
            if m13_document is not None
            else preparation_authorization["feasibility_digest"]
            if preparation_authorization is not None
            else None
        ),
        "assignment_materializations": materializations,
        "route_ready": False,
    }
    _write_document(output_root / "build-report.json", report)
    return report


def refresh_runtime_closure(args: argparse.Namespace) -> dict[str, Any]:
    """Refresh repository code in an existing bundle and repin its manifests."""

    repo = Path(__file__).resolve().parents[1]
    template = json.loads(args.template_plan.read_text("utf-8"))
    output_root = args.output_root.resolve()
    bundle = output_root / "transfer-bundle"
    if not bundle.is_dir():
        raise RuntimeError("runtime_refresh_bundle_missing")
    _copy_runtime_closure(repo, bundle, template)
    transfer_manifest = _transfer_manifest(bundle)
    packs = [
        json.loads(path.read_text("utf-8"))
        for path in sorted((bundle / "control").glob("node-*-stage-pack.json"))
    ]
    if not packs:
        raise RuntimeError("runtime_refresh_stage_packs_missing")
    node_transfer_manifests = _node_transfer_manifests(transfer_manifest, packs)
    plan = copy.deepcopy(template)
    plan["controller"]["source_root"] = str(bundle)
    plan["controller"]["transfer_manifest"] = transfer_manifest
    plan["controller"]["node_transfer_manifests"] = node_transfer_manifests
    operator_plan = output_root / "operator-plan.json"
    _write_document(operator_plan, plan)

    report_path = output_root / "build-report.json"
    report = json.loads(report_path.read_text("utf-8"))
    report["operator_plan"] = str(operator_plan)
    report["transfer_file_count"] = len(transfer_manifest["files"])
    report["transfer_bytes"] = sum(
        record["size_bytes"] for record in transfer_manifest["files"]
    )
    report["per_node_transfer_bytes"] = {
        node_id: sum(record["size_bytes"] for record in manifest["files"])
        for node_id, manifest in node_transfer_manifests["manifests"].items()
    }
    report["runtime_closure_refreshed"] = True
    _write_document(report_path, report)
    return {
        "protocol": "mycelium.runtime_closure_refresh.v1",
        "operator_plan": str(operator_plan),
        "transfer_file_count": report["transfer_file_count"],
        "transfer_bytes": report["transfer_bytes"],
        "route_ready": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-plan", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--resolved-commit", default=MODEL_COMMIT)
    parser.add_argument(
        "--deployment-id",
        help="reuse the deployment UUID bound into signed physical evidence",
    )
    parser.add_argument(
        "--stage-sharded",
        action="store_true",
        help="rewrite Qwen weights into one static and one layer-only file per host",
    )
    parser.add_argument(
        "--reuse-stage-sharded-root",
        type=Path,
        help=(
            "reuse a fully verified prior stage-sharded deployment as the local "
            "source; the builder revalidates every file and emits a new candidate"
        ),
    )
    parser.add_argument(
        "--emit-stage-shards",
        type=Path,
        help=(
            "write the derived stage-sharded representation to this durable "
            "directory instead of a temporary one; an existing target is refused"
        ),
    )
    parser.add_argument("--phone-prepare-only", action="store_true", help="Compile assignment inputs only, without load/route authority")
    parser.add_argument("--phone-seed-state-root", type=Path, help="Existing owner-local seed authority; revalidate and project final graph offers")
    parser.add_argument("--phone-evidence", type=Path)
    parser.add_argument("--phone-seed-key-digest")
    parser.add_argument("--phone-executor", type=Path)
    parser.add_argument("--phone-apk", type=Path)
    parser.add_argument("--phone-source-root", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--temporary-root",
        type=Path,
        help="owner-private directory for large ephemeral stage-sharding files",
    )
    parser.add_argument(
        "--route-label",
        default="m7",
        help="lowercase route/evidence label (default: m7)",
    )
    parser.add_argument(
        "--topology",
        type=Path,
        help="ordered public host/runtime topology (default: derive two hosts from template)",
    )
    parser.add_argument(
        "--role-binding",
        type=Path,
        help=(
            "verified gate role binding (mycelium_gate_requirements); the topology "
            "must be exactly its bound members, in role order, on offered backends"
        ),
    )
    parser.add_argument(
        "--m13-control-plane",
        type=Path,
        help="signed, derived M13 evidence/plan used as physical placement authority",
    )
    parser.add_argument(
        "--m14-control-plane",
        type=Path,
        help="signed M14 topology/placement authority extending the M13 control plane",
    )
    parser.add_argument(
        "--m18-control-plane",
        type=Path,
        help="signed M18 replica placement authority with two or more legal tracks",
    )
    parser.add_argument(
        "--model-preparation-authorization",
        type=Path,
        help="fresh local-only model feasibility authorization used as layer authority",
    )
    parser.add_argument(
        "--managed-transport",
        action="store_true",
        help=(
            "build for an all-managed_https topology whose peers are controlled "
            "by the owning application's canonical transport, never this "
            "builder's SSH authority"
        ),
    )
    parser.add_argument(
        "--managed-hello-observations",
        type=Path,
        help=(
            "fresh admitted managed hello observations binding each peer's "
            "run-scoped host/boot identity for this deployment (required with "
            "--managed-transport)"
        ),
    )
    parser.add_argument(
        "--refresh-runtime-closure-only",
        action="store_true",
        help="refresh code and repin manifests in an existing output root",
    )
    args = parser.parse_args()
    if args.refresh_runtime_closure_only:
        result = refresh_runtime_closure(args)
        print(json.dumps(result, sort_keys=True))
        return 0
    if (
        args.managed_hello_observations is not None
        and not args.managed_transport
    ):
        raise RuntimeError("managed_observations_require_managed_transport")
    if args.managed_transport and args.topology is None:
        raise RuntimeError("managed_transport_requires_topology")
    if args.managed_transport and args.managed_hello_observations is None:
        raise RuntimeError("managed_peer_observations_missing")
    result = build(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
