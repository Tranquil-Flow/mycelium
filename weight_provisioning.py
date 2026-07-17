#!/usr/bin/env python3
"""Download and verify exact upstream shard allowlists for Mycelium assignments."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from layer_assignment import validate_assignment_identity, validate_target_cache_root
from route_contract import validate_route_plan_v2


_SHA256_REF_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_SAFETENSORS_HEADER_BYTES = 100 * 1024 * 1024
_SAFE_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _timestamp() -> str:
   return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path) -> str:
   digest = hashlib.sha256()
   with Path(path).open("rb") as handle:
      while True:
         chunk = handle.read(1024 * 1024)
         if not chunk:
            break
         digest.update(chunk)
   return digest.hexdigest()


def _safe_artifact_path(value: str) -> str:
   if (
      not isinstance(value, str)
      or not value
      or len(value) > 1024
      or not _SAFE_ARTIFACT_RE.fullmatch(value)
   ):
      raise ValueError(f"unsafe artifact path: {value!r}")
   path = PurePosixPath(value)
   if (
      path.is_absolute()
      or len(path.parts) > 16
      or any(part in ("", ".", "..") for part in path.parts)
   ):
      raise ValueError(f"unsafe artifact path: {value!r}")
   return value


def safetensors_tensor_names(path: str | Path) -> set[str]:
   artifact = Path(path)
   size = artifact.stat().st_size
   if size < 8:
      raise ValueError(f"invalid safetensors container: {artifact}")
   with artifact.open("rb") as handle:
      raw_length = handle.read(8)
      header_length = struct.unpack("<Q", raw_length)[0]
      if header_length <= 0 or header_length > _MAX_SAFETENSORS_HEADER_BYTES:
         raise ValueError(f"invalid safetensors header length: {artifact}")
      if 8 + header_length > size:
         raise ValueError(f"truncated safetensors header: {artifact}")
      try:
         header = json.loads(handle.read(header_length))
      except (UnicodeDecodeError, json.JSONDecodeError) as exc:
         raise ValueError(f"invalid safetensors JSON header: {artifact}") from exc
   if not isinstance(header, dict):
      raise ValueError(f"invalid safetensors header object: {artifact}")
   data_size = size - 8 - header_length
   names = set()
   for name, metadata in header.items():
      if name == "__metadata__":
         continue
      if not isinstance(name, str) or not isinstance(metadata, dict):
         raise ValueError(f"invalid safetensors tensor metadata: {artifact}")
      offsets = metadata.get("data_offsets")
      if (
         not isinstance(offsets, list)
         or len(offsets) != 2
         or not all(isinstance(value, int) and not isinstance(value, bool) for value in offsets)
         or offsets[0] < 0
         or offsets[1] < offsets[0]
         or offsets[1] > data_size
      ):
         raise ValueError(f"invalid safetensors data offsets for {name}: {artifact}")
      names.add(name)
   if not names:
      raise ValueError(f"empty safetensors tensor set: {artifact}")
   return names


def _validate_assignment(assignment: dict[str, Any]) -> None:
   if assignment.get("protocol") != "mycelium.layer_assignment.v1":
      raise ValueError("expected mycelium.layer_assignment.v1")
   validate_assignment_identity(assignment)
   if not _COMMIT_RE.fullmatch(str(assignment.get("resolved_commit", ""))):
      raise ValueError("assignment resolved_commit must be immutable 40-hex")
   if not _SHA256_REF_RE.fullmatch(str(assignment.get("manifest_digest", ""))):
      raise ValueError("assignment manifest_digest is invalid")
   cache_root = assignment.get("artifact_cache_root")
   validate_target_cache_root(cache_root, str(assignment.get("node_id", "peer")))
   layer_range = assignment.get("range") or {}
   start = layer_range.get("start_layer")
   end = layer_range.get("end_layer_exclusive")
   count = layer_range.get("layer_count")
   if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end, count)):
      raise ValueError("assignment range fields must be integers")
   if start < 0 or end <= start or end - start != count:
      raise ValueError("assignment range is invalid")
   files = assignment.get("files")
   if not isinstance(files, list) or not files:
      raise ValueError("assignment requires at least one artifact file")
   seen = set()
   for record in files:
      path = _safe_artifact_path(record.get("path"))
      if path in seen:
         raise ValueError(f"duplicate artifact path: {path}")
      seen.add(path)
      size = record.get("size_bytes")
      if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
         raise ValueError(f"invalid artifact size: {path}")
      if not _SHA256_REF_RE.fullmatch(str(record.get("content_digest", ""))):
         raise ValueError(f"invalid artifact digest: {path}")
   expected_keys = assignment.get("expected_tensor_keys")
   expected_prefixes = assignment.get("expected_tensor_prefixes")
   if not isinstance(expected_keys, list) or not expected_keys or not all(isinstance(item, str) for item in expected_keys):
      raise ValueError("assignment requires expected_tensor_keys")
   if not isinstance(expected_prefixes, list) or not expected_prefixes or not all(isinstance(item, str) for item in expected_prefixes):
      raise ValueError("assignment requires expected_tensor_prefixes")


def fetch_huggingface_file(
   model_id: str,
   revision: str,
   filename: str,
   cache_root: str | Path,
   local_files_only: bool = False,
) -> tuple[Path, bool]:
   try:
      from huggingface_hub import hf_hub_download, try_to_load_from_cache
   except ImportError as exc:
      raise RuntimeError("huggingface_hub is required for Hub downloads") from exc

   cache_dir = str(Path(cache_root).expanduser().resolve())
   cached = try_to_load_from_cache(
      repo_id=model_id,
      filename=filename,
      revision=revision,
      cache_dir=cache_dir,
   )
   cache_hit = isinstance(cached, str) and Path(cached).is_file()
   resolved = hf_hub_download(
      repo_id=model_id,
      filename=filename,
      revision=revision,
      cache_dir=cache_dir,
      local_files_only=local_files_only,
   )
   return Path(resolved), cache_hit


def provision_assignment(
   assignment: dict[str, Any],
   *,
   fetch_file: Callable[..., Any] = fetch_huggingface_file,
   local_files_only: bool = False,
) -> dict[str, Any]:
   _validate_assignment(assignment)
   assigned_cache_root = assignment["artifact_cache_root"]
   cache_root = Path(assigned_cache_root).resolve()
   cache_root.mkdir(parents=True, exist_ok=True)
   required_bytes = sum(record["size_bytes"] for record in assignment["files"])
   if not local_files_only:
      free_bytes = shutil.disk_usage(cache_root).free
      reserve_bytes = 64 * 1024 * 1024
      if free_bytes < required_bytes + reserve_bytes:
         raise ValueError(
            f"insufficient disk: need {required_bytes + reserve_bytes} bytes including reserve, have {free_bytes}"
         )

   verified_files = []
   available_tensors: set[str] = set()
   network_download_bytes = 0
   cache_hit_bytes = 0
   for record in assignment["files"]:
      filename = _safe_artifact_path(record["path"])
      fetched = fetch_file(
         assignment["model_id"],
         assignment["resolved_commit"],
         filename,
         cache_root,
         local_files_only=local_files_only,
      )
      if isinstance(fetched, tuple):
         local_path, cache_hit = fetched
      else:
         local_path, cache_hit = fetched, False
      local_path = Path(local_path)
      if not local_path.is_file():
         raise ValueError(f"download did not produce file: {filename}")
      actual_size = local_path.stat().st_size
      if actual_size != record["size_bytes"]:
         raise ValueError(
            f"size mismatch for {filename}: expected {record['size_bytes']}, got {actual_size}"
         )
      expected_digest = _SHA256_REF_RE.fullmatch(record["content_digest"]).group(1)
      actual_digest = sha256_file(local_path)
      if actual_digest != expected_digest:
         raise ValueError(
            f"checksum mismatch for {filename}: expected sha256:{expected_digest}, got sha256:{actual_digest}"
         )
      tensor_names = safetensors_tensor_names(local_path)
      available_tensors.update(tensor_names)
      if cache_hit:
         cache_hit_bytes += actual_size
      else:
         network_download_bytes += actual_size
      verified_files.append({
         "path": filename,
         "local_path": str(local_path.resolve()),
         "size_bytes": actual_size,
         "content_digest": f"sha256:{actual_digest}",
         "cache_hit": bool(cache_hit),
         "tensor_count": len(tensor_names),
      })

   missing_tensors = sorted(set(assignment["expected_tensor_keys"]) - available_tensors)
   if missing_tensors:
      preview = ", ".join(missing_tensors[:5])
      raise ValueError(f"missing assigned tensors: {preview}")
   missing_prefixes = sorted(
      prefix
      for prefix in assignment["expected_tensor_prefixes"]
      if not any(name.startswith(prefix) for name in available_tensors)
   )
   if missing_prefixes:
      raise ValueError(f"missing assigned tensor prefixes: {', '.join(missing_prefixes)}")

   report = {
      "protocol": "mycelium.artifact_verification_report.v1",
      "deployment_id": assignment["deployment_id"],
      "deployment_epoch": assignment["deployment_epoch"],
      "assignment_id": assignment["assignment_id"],
      "node_id": assignment["node_id"],
      "manifest_digest": assignment["manifest_digest"],
      "resolved_commit": assignment["resolved_commit"],
      "range": dict(assignment["range"]),
      "artifact_cache_root": assigned_cache_root,
      "resolved_artifact_cache_root": str(cache_root),
      "verified_files": verified_files,
      "verified_tensor_prefixes": list(assignment["expected_tensor_prefixes"]),
      "verified_tensor_count": len(set(assignment["expected_tensor_keys"])),
      "expected_bytes": required_bytes,
      "network_download_bytes": network_download_bytes,
      "cache_hit_bytes": cache_hit_bytes,
      "ready_for_load": True,
      "route_ready": False,
      "claim_boundary": "files, SHA-256 digests, Safetensors headers, and assigned tensor coverage verified; layers not loaded",
      "timestamp": _timestamp(),
   }
   return report


def artifact_report_errors(
   assignment: dict[str, Any],
   report: dict[str, Any],
) -> list[str]:
   """Validate one report against the exact immutable assignment evidence."""
   node_id = assignment.get("node_id", "<unknown>")
   errors: list[str] = []
   try:
      _validate_assignment(assignment)
   except (TypeError, ValueError) as exc:
      errors.append(f"invalid assignment: {exc}")
   if not isinstance(report, dict):
      errors.append("report must be a JSON object")
      return [f"{node_id}: {error}" for error in errors]

   integer_fields = ("deployment_epoch", "verified_tensor_count", "expected_bytes")
   for field in integer_fields:
      value = report.get(field)
      if not isinstance(value, int) or isinstance(value, bool) or value < 0:
         errors.append(f"invalid report field type: {field}")
   if report.get("protocol") != "mycelium.artifact_verification_report.v1":
      errors.append("wrong report protocol")
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
      if report.get(field) != assignment.get(field):
         errors.append(f"report mismatch: {field}")
   if report.get("ready_for_load") is not True:
      errors.append("report is not ready_for_load")
   if report.get("route_ready") is not False:
      errors.append("artifact report must keep route_ready false")

   assigned_files_raw = assignment.get("files")
   assigned_files: dict[str, dict[str, Any]] = {}
   if not isinstance(assigned_files_raw, list) or not assigned_files_raw:
      errors.append("assignment lacks artifact files")
   else:
      for item in assigned_files_raw:
         if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append("invalid assigned file record")
            continue
         path = item["path"]
         if path in assigned_files:
            errors.append(f"duplicate assigned file: {path}")
         assigned_files[path] = item

   verified_files_raw = report.get("verified_files")
   if not isinstance(verified_files_raw, list):
      errors.append("report lacks verified_files")
      verified_files_raw = []
   verified_files: dict[str, dict[str, Any]] = {}
   for item in verified_files_raw:
      if not isinstance(item, dict) or not isinstance(item.get("path"), str):
         errors.append("invalid verified file record")
         continue
      path = item["path"]
      if path in verified_files:
         errors.append(f"duplicate verified file: {path}")
      verified_files[path] = item
   if set(verified_files) != set(assigned_files):
      errors.append("verified files do not match assignment")
   for path, expected in assigned_files.items():
      actual = verified_files.get(path)
      if actual is None:
         continue
      actual_size = actual.get("size_bytes")
      if not isinstance(actual_size, int) or isinstance(actual_size, bool) or actual_size < 0:
         errors.append(f"invalid report field type: verified file size for {path}")
      elif actual_size != expected.get("size_bytes"):
         errors.append(f"verified size mismatch: {path}")
      if actual.get("content_digest") != expected.get("content_digest"):
         errors.append(f"verified digest mismatch: {path}")

   expected_prefixes = assignment.get("expected_tensor_prefixes")
   if not isinstance(expected_prefixes, list) or not expected_prefixes:
      errors.append("assignment lacks expected tensor prefixes")
   elif report.get("verified_tensor_prefixes") != expected_prefixes:
      errors.append("verified tensor prefixes do not match assignment")
   expected_keys = assignment.get("expected_tensor_keys")
   if not isinstance(expected_keys, list) or not expected_keys:
      errors.append("assignment lacks expected tensor keys")
   elif report.get("verified_tensor_count") != len(set(expected_keys)):
      errors.append("verified tensor count does not match assignment")

   sizes = [item.get("size_bytes") for item in assigned_files.values()]
   if any(not isinstance(size, int) or isinstance(size, bool) or size < 0 for size in sizes):
      errors.append("assignment has invalid artifact byte count")
      expected_bytes = None
   else:
      expected_bytes = sum(sizes)
      if report.get("expected_bytes") != expected_bytes:
         errors.append("expected byte count does not match assignment")
   network_bytes = report.get("network_download_bytes")
   cache_bytes = report.get("cache_hit_bytes")
   if (
      expected_bytes is None
      or not isinstance(network_bytes, int)
      or isinstance(network_bytes, bool)
      or network_bytes < 0
      or not isinstance(cache_bytes, int)
      or isinstance(cache_bytes, bool)
      or cache_bytes < 0
      or network_bytes + cache_bytes != expected_bytes
   ):
      errors.append("download byte accounting does not match assignment")
   return [f"{node_id}: {error}" for error in errors]


def audit_provisioning(
   route_plan: dict[str, Any],
   assignments: list[dict[str, Any]],
   reports: list[dict[str, Any]],
) -> dict[str, Any]:
   validate_route_plan_v2(route_plan)
   errors = []
   route_nodes = {stage["node_id"] for stage in route_plan["route"]}

   def index_evidence(items: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
      indexed = {}
      for item in items:
         node_id = item.get("node_id")
         if not isinstance(node_id, str) or not node_id:
            errors.append(f"{kind} lacks node_id")
            continue
         if node_id in indexed:
            errors.append(f"duplicate {kind} for {node_id}")
         indexed[node_id] = item
         if node_id not in route_nodes:
            errors.append(f"unexpected {kind} for {node_id}")
      return indexed

   assignments_by_node = index_evidence(assignments, "assignment")
   reports_by_node = index_evidence(reports, "report")

   deployment_identity = None
   for assignment in assignments:
      node_id = assignment.get("node_id", "<unknown>")
      if assignment.get("protocol") != "mycelium.layer_assignment.v1":
         errors.append(f"{node_id} wrong assignment protocol")
      identity = tuple(
         assignment.get(field)
         for field in ("deployment_id", "deployment_epoch", "manifest_digest", "resolved_commit")
      )
      if deployment_identity is None:
         deployment_identity = identity
      elif identity != deployment_identity:
         errors.append(f"{node_id} assignment deployment identity mismatch")

   verified_nodes = []
   for stage in route_plan["route"]:
      node_id = stage["node_id"]
      assignment = assignments_by_node.get(node_id)
      report = reports_by_node.get(node_id)
      if assignment is None:
         errors.append(f"missing assignment for {node_id}")
         continue
      if report is None:
         errors.append(f"missing report for {node_id}")
         continue
      before = len(errors)
      errors.extend(artifact_report_errors(assignment, report))
      if assignment.get("range") != stage.get("range"):
         errors.append(f"{node_id} assignment range does not match route")
      if len(errors) == before:
         verified_nodes.append(node_id)

   all_verified = not errors and len(verified_nodes) == len(route_plan["route"])
   return {
      "protocol": "mycelium.provisioning_audit.v1",
      "all_assignments_verified": all_verified,
      "ready_for_runtime_load": all_verified,
      "route_ready": False,
      "verified_nodes": verified_nodes,
      "errors": errors,
      "claim_boundary": "artifact provisioning only; runtime layer load, stage probe, and route challenge remain required",
      "timestamp": _timestamp(),
   }
