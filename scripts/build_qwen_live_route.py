#!/usr/bin/env python3
"""Build a pinned, int8 Qwen2.5 live-route bundle for two or more hosts."""

# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.

from __future__ import annotations

import argparse
from collections.abc import Mapping
import copy
from dataclasses import asdict
import gc
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

import mlx.core as mx
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from layer_assignment import compile_layer_assignments
from mycelium_assignment_cache import ArtifactObjectKey, AssignmentArtifactCache
from mycelium_live.codec import Qwen2PromptCodec
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
    "runtime_backend",
)
_ROUTE_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_RUNTIME_PACKAGE_CLOSURE = (
    "mycelium_capacity_profiles",
    "mycelium_gossip",
    "mycelium_layer_planner",
)
_PREPARATION_AUTHORIZATION_PROTOCOL = "mycelium.model_preparation_authorization.v1"


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
    return (
        max(context_tokens, qualification_token_count)
        * 32
        * track_membership_count
    )


def _route_label(args: argparse.Namespace) -> str:
    label = getattr(args, "route_label", "m7")
    if not isinstance(label, str) or not _ROUTE_LABEL_PATTERN.fullmatch(label):
        raise RuntimeError("route_label_invalid")
    return label


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
        "evidence_generation",
        "evidence_valid_until_unix_ms",
        "stages",
        "download_authorized",
    }
    stages = document.get("stages") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != expected
        or document.get("protocol") != _PREPARATION_AUTHORIZATION_PROTOCOL
        or document.get("model_id") != model_id
        or document.get("revision") != resolved_commit
        or document.get("download_authorized") is not False
        or not isinstance(document.get("catalog_generation"), int)
        or not isinstance(document.get("evidence_generation"), int)
        or not isinstance(document.get("evidence_valid_until_unix_ms"), int)
        or document["evidence_valid_until_unix_ms"] < int(time.time() * 1_000)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(document.get("operation_digest", ""))) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(document.get("feasibility_digest", ""))) is None
        or not isinstance(stages, list)
        or len(stages) != len(topology)
    ):
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
            or not all(isinstance(item, str) and item for item in stage["assignment_files"])
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


def _copy_runtime_closure(repo: Path, bundle: Path, template: dict[str, Any]) -> None:
    paths = {
        record["path"]
        for record in template["controller"]["transfer_manifest"]["files"]
        if not record["path"].startswith(("control/", "deployment/"))
    }
    paths.add("weight_quantization.py")
    for package in _RUNTIME_PACKAGE_CLOSURE:
        package_root = repo / package
        if not package_root.is_dir():
            raise RuntimeError(f"runtime_closure_package_missing:{package}")
        paths.update(
            str(path.relative_to(repo))
            for path in package_root.rglob("*.py")
            if path.is_file()
        )
    for relative in sorted(paths):
        source = repo / relative
        if not source.is_file():
            raise RuntimeError(f"runtime_closure_file_missing:{relative}")
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _transfer_manifest(bundle: Path) -> dict[str, Any]:
    records = []
    for path in sorted(
        candidate for candidate in bundle.rglob("*") if candidate.is_file()
    ):
        relative = str(path.relative_to(bundle))
        payload = path.read_bytes()
        records.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "content_digest": _digest(payload),
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
    manifests: dict[str, Any] = {}
    for pack in packs:
        owned = {
            f"deployment/{artifact['upstream_path']}" for artifact in pack["artifacts"]
        }
        allowed = common | owned
        manifests[pack["node_id"]] = {
            "protocol": "mycelium.controller_transfer_manifest.v1",
            "files": [record for record in records if record["path"] in allowed],
        }
    return {
        "protocol": "mycelium.controller_node_transfer_manifests.v1",
        "manifests": manifests,
    }


def _endpoint_ids(template: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    offers = template["controller"]["membership_snapshot"]["assignment_offers"]
    for offer in offers:
        for record in offer["message"]["peer_endpoint_records"]:
            result[record["node_id"]] = record["endpoint_id"]
    return result


def _topology_nodes(
    template: dict[str, Any], topology_path: Path | None
) -> list[dict[str, Any]]:
    """Return a validated ordered physical topology.

    When no topology document is supplied, preserve the existing template-plan
    behavior. A topology document is the durable extension seam for a third or
    later peer; it intentionally contains public endpoint IDs and paths, never
    endpoint secret material.
    """

    if topology_path is None:
        template_nodes = {
            node["node_id"]: node
            for node in template["controller"]["run_plan"]["nodes"]
        }
        endpoint_ids = _endpoint_ids(template)
        nodes = []
        for index, peer in enumerate(template["controller"]["peers"]):
            node_id = peer["node_id"]
            runtime = template_nodes[node_id]
            nodes.append(
                {
                    **peer,
                    "python_executable": runtime["python_executable"],
                    "sidecar_binary": runtime["sidecar_binary"],
                    "endpoint_secret_file": runtime["endpoint_secret_file"],
                    "endpoint_id": endpoint_ids[node_id],
                    "runtime_backend": "mlx" if index == 0 else "numpy",
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
        missing = [field for field in _TOPOLOGY_FIELDS if field not in node]
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
    backends = [node["runtime_backend"] for node in nodes]
    if any(backend not in {"mlx", "numpy"} for backend in backends):
        raise RuntimeError("topology_runtime_backend_unsupported")
    if any(node["process_transport"] not in {"local", "ssh"} for node in nodes):
        raise RuntimeError("topology_process_transport_invalid")
    local_count = sum(node["process_transport"] == "local" for node in nodes)
    if local_count != 1:
        raise RuntimeError("topology_requires_one_local_node")
    return copy.deepcopy(nodes)


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
    completed=subprocess.run(('sysctl','-n','kern.boottime'),capture_output=True,text=True,timeout=5.0)
    if completed.returncode == 0:
        source=completed.stdout.strip()
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


def refresh_peer_identities(
    peers: list[dict[str, Any]],
    run_id: str,
    *,
    python_executables_by_node: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Measure and bind the run-scoped host identities used by node hello."""

    remote_program = _remote_identity_program(run_id)
    refreshed = copy.deepcopy(peers)
    for peer in refreshed:
        if peer["process_transport"] == "local":
            host_id, boot_id = derive_local_run_scoped_identity(run_id)
        else:
            command = ["ssh"]
            identity = peer.get("ssh_identity_file")
            if identity:
                command.extend(("-i", identity))
            command.extend(
                (
                    "-o",
                    "BatchMode=yes",
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
                "membership_generation": other_index + 1,
                "valid_from": now,
                "valid_until": now + 3_600.0,
            }
            for other_index, other in enumerate(assignments)
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
            "generation": 1,
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
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Challenge a large candidate with at most one stage's weights resident."""

    if not assignments or len(assignments) != len(reports) or len(assignments) != len(runtime_backends):
        raise RuntimeError("challenge_runtime_count_mismatch")
    prompt = codec.encode("Reply with exactly the word ready.")
    context = prompt
    stage_output: Any = None
    for index, (assignment, report, backend) in enumerate(
        zip(assignments, reports, runtime_backends, strict=True)
    ):
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
    return prompt, (token_id,)


def build(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[1]
    route_label = _route_label(args)
    model_id, resolved_commit, model_slug = _model_identity(args)
    template = json.loads(args.template_plan.read_text("utf-8"))
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise RuntimeError("output_root_already_exists")
    bundle = output_root / "transfer-bundle"
    deployment_root = bundle / "deployment"
    deployment_root.mkdir(parents=True)
    topology = _topology_nodes(template, args.topology)
    node_ids = tuple(node["node_id"] for node in topology)
    runtime_backends = {node["node_id"]: node["runtime_backend"] for node in topology}
    preparation_authorization, preparation_ranges = _preparation_authorization(
        getattr(args, "model_preparation_authorization", None),
        model_id=model_id,
        resolved_commit=resolved_commit,
        topology=topology,
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
    if is_m18:
        primary_track = m13_document["route_plan"]["legal_tracks"][0]["placement_ids"]
        placement_intent = {
            item["placement_id"]: item
            for item in m13_document["route_plan"]["placements"]
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
            prepare_node_ids = node_ids
            prepare_ranges = None
    elif preparation_ranges is not None:
        prepare_node_ids = node_ids
        prepare_ranges = preparation_ranges
    else:
        prepare_node_ids = node_ids
        prepare_ranges = planned_ranges

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
            runtime_dtype="float32",
            runtime_backends_by_node={
                node_id: runtime_backends[node_id] for node_id in prepare_node_ids
            },
            runtime_quantization="int8-weight-only",
            deployment_id=deployment_id,
            layer_ranges=prepare_ranges,
            control_plane_binding=(
                None
                if preparation_authorization is None
                else {
                    "protocol": "mycelium.control_plane_binding.v1",
                    "evidence_bundle_digest": preparation_authorization["operation_digest"],
                    "planner_snapshot_digest": preparation_authorization["feasibility_digest"],
                    "snapshot_generation": preparation_authorization["evidence_generation"],
                    "swarm_id": "live-qualified-swarm",
                    "deployment_id": deployment_id,
                    "deployment_epoch": 1,
                }
            ),
        )

    stage_sharding = None
    if getattr(args, "stage_sharded", False):
        with tempfile.TemporaryDirectory(prefix="mycelium-stage-shards-") as temporary:
            sharded_root = Path(temporary) / "model"
            stage_sharding = shard_qwen2_checkpoint(
                args.model_root,
                sharded_root,
                shard_count=len(node_ids),
                layer_ranges=planned_ranges,
                max_file_bytes=_MAX_STAGED_MODEL_FILE_BYTES,
            )
            prepared = prepare(sharded_root)
    else:
        prepared = prepare(args.model_root)

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
    staging_roots = {node["node_id"]: node["staging_root"] for node in topology}
    cache_roots = {
        node_id: f"{root}/deployment" for node_id, root in staging_roots.items()
    }
    runtime_by_node = {
        node_id: {
            "backend": backend,
            "dtype": "float32",
            "quantization": "int8-weight-only",
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
            if "numpy" in runtime_backends.values()
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
            load_assignment_stage(assignment, report, load_generation=LOAD_GENERATION)
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
        else [json.loads(canonical_json(loaded.proof)) for loaded in base_loaded]
    )
    proofs = [
        _target_proof(
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
    graph_document = _physical_graph_document(graph)
    device_states = {
        node_id: asdict(state)
        for node_id, state in build_physical_device_states(graph).items()
    }
    codec = Qwen2PromptCodec.from_deployment(deployment_root)
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
                quantization="int8-weight-only",
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
                    assignment["node_id"]: layer_load_proof_digest(proof)
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
    membership = _membership_snapshot(
        assignments=assignments,
        packs=packs,
        graph_document=graph_document,
        endpoint_ids={node["node_id"]: node["endpoint_id"] for node in topology},
        now=now,
        route_label=route_label,
        placement_provenance=(
            "planner_v2"
            if m13_document is not None
            else "capability_aware_contiguous_exact_weight_dp"
            if preparation_authorization is not None
            else "target_local_physical_preload"
        ),
    )
    request = {
        "request_id": f"request-{route_label}-qwen-startup",
        "prompt_token_ids": list(challenge_prompt),
        "max_new_tokens": len(challenge_output),
        "expected_new_tokens": len(challenge_output),
        "qos_class": "interactive",
        "admitted_at": 0.0,
        "target_ttft_ms": 120_000.0,
        "target_tpot_ms": 120_000.0,
        "target_tokens_per_second": 0.001,
        "sampling_seed": 17,
        "generation_config_digest": _digest(
            _bytes({"max_new_tokens": len(challenge_output), "sampling_seed": 17})
        ),
    }
    topology_by_node = {node["node_id"]: node for node in topology}
    nodes = []
    for index, assignment in enumerate(assignments):
        node_id = assignment["node_id"]
        topology_node = topology_by_node[node_id]
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
    run_id = f"{route_label}-qwen-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    peers = refresh_peer_identities(
        peers,
        run_id,
        python_executables_by_node={
            node["node_id"]: node["python_executable"] for node in topology
        },
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
    }
    transfer_manifest = _transfer_manifest(bundle)
    node_transfer_manifests = _node_transfer_manifests(transfer_manifest, packs)
    plan = copy.deepcopy(template)
    plan["run_id"] = run_id
    plan["plan_id"] = f"{route_label}-{model_slug}-int8-{len(topology)}-host"
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
        "quantization": "int8-weight-only",
        "layer_ranges": [assignment["range"] for assignment in assignments],
        "runtime_backends": [
            assignment["runtime"]["backend"] for assignment in assignments
        ],
        "challenge_prompt_token_count": len(challenge_prompt),
        "challenge_output_token_ids": list(challenge_output),
        "challenge_output_text": "".join(
            codec.decode_token(token_id) for token_id in challenge_output
        ),
        "transfer_file_count": len(transfer_manifest["files"]),
        "transfer_bytes": sum(
            record["size_bytes"] for record in transfer_manifest["files"]
        ),
        "per_node_transfer_bytes": {
            node_id: sum(record["size_bytes"] for record in manifest["files"])
            for node_id, manifest in node_transfer_manifests["manifests"].items()
        },
        "stage_sharding": stage_sharding,
        "placement_provenance": (
            "planner_v2"
            if m13_document is not None
            else "capability_aware_contiguous_exact_weight_dp"
            if preparation_authorization is not None
            else "target_local_physical_preload"
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
    parser.add_argument("--output-root", type=Path, required=True)
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
        "--refresh-runtime-closure-only",
        action="store_true",
        help="refresh code and repin manifests in an existing output root",
    )
    args = parser.parse_args()
    result = refresh_runtime_closure(args) if args.refresh_runtime_closure_only else build(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
