"""Operator-side physical candidate construction from a frozen preparation authority."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping
import uuid

from mycelium_model_catalog import scan_huggingface_cache

from .preparation import ModelPreparationError, PreparationResult


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
    template: Mapping[str, Any], authorization: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        controller = template["controller"]
        source_root = Path(controller["source_root"])
        peers = {item["node_id"]: item for item in controller["peers"]}
        runtime_nodes = {
            item["node_id"]: item for item in controller["run_plan"]["nodes"]
        }
        endpoint_ids: dict[str, str] = {}
        for offer in controller["membership_snapshot"]["assignment_offers"]:
            endpoint_ids[offer["message"]["recipient_node_id"]] = offer["message"].get(
                "recipient_endpoint_id", ""
            )
            for record in offer["message"]["peer_endpoint_records"]:
                endpoint_ids[record["node_id"]] = record["endpoint_id"]
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
                    "staging_root": peer["staging_root"],
                    "python_executable": runtime["python_executable"],
                    "sidecar_binary": runtime["sidecar_binary"],
                    "endpoint_secret_file": runtime["endpoint_secret_file"],
                    "endpoint_id": endpoint_id,
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
        python_executable: str = sys.executable,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
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
        self._python = python_executable
        self._run = run

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
        attempt = self._workspace / (revision[:12] + "-" + uuid.uuid4().hex[:12])
        attempt.mkdir(mode=0o700)
        authorization_path = attempt / "authorization.json"
        topology_path = attempt / "topology.json"
        authorization_path.write_bytes(_canonical(authorization))
        os.chmod(authorization_path, 0o600)
        template = json.loads(self._template.read_text("utf-8"))
        topology_path.write_bytes(
            _canonical(_topology_from_template(template, authorization))
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
                "modelprep",
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
