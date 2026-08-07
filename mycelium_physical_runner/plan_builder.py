"""Deterministic redacted safe-plan construction from injected probe facts."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

SAFE_PLAN_PROTOCOL = "mycelium.physical_runner_safe_plan.v1"
_INVENTORY_PROTOCOL = "mycelium.physical_runner_inventory.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PUBLIC_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_RUN_SCOPED_HOST_RE = re.compile(r"^host-[0-9a-f]{32}$")
_RUN_SCOPED_BOOT_RE = re.compile(r"^boot-[0-9a-f]{32}$")


class PlanBuildError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise PlanBuildError(code)


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _sequence(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(code)
    return value


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _segment(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _public_alias(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _PUBLIC_ALIAS_RE.fullmatch(value) is None
        or value.startswith(("/", "~", "file:", "hf_", "sk-"))
        or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
        or "/.ssh/" in value
        or "/Library/Caches/" in value
    ):
        _fail("public_alias_invalid")
    return value


def _path_fact(probes: Any, path: Any, *, code: str, kind: str, owner: str | None = None) -> Any:
    if not isinstance(path, str) or not path.startswith("/") or ".." in PurePosixPath(path).parts:
        _fail("unsafe_path")
    fact = getattr(probes, "paths", {}).get(path)
    if fact is None:
        _fail(code)
    if (
        getattr(fact, "kind", None) != kind
        or getattr(fact, "symlink", None) is not False
        or getattr(fact, "canonical", None) is not True
    ):
        _fail(code)
    if owner is not None and (
        getattr(fact, "owner", None) != owner or getattr(fact, "mode", None) != 0o600
    ):
        _fail(code)
    return fact


def _source_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or ".." in PurePosixPath(value).parts
        or any(character in value for character in "*?[]{}\\\n\r\t")
        or str(PurePosixPath(value)) != value
    ):
        _fail("source_file_invalid")
    return value


def _validate_host_paths(host: Mapping[str, Any], probes: Any) -> None:
    ssh_user = _segment(host.get("ssh_user"), "host_invalid")
    ssh_identity_owner = _segment(host.get("ssh_identity_owner"), "host_invalid")
    _path_fact(probes, host.get("staging_root"), code="unsafe_path", kind="directory")
    _path_fact(probes, host.get("socket_root"), code="unsafe_path", kind="directory")
    _path_fact(probes, host.get("evidence_root"), code="unsafe_path", kind="directory")
    _path_fact(
        probes,
        host.get("credential_path"),
        code="credential_path_invalid",
        kind="regular",
        owner=ssh_user,
    )
    _path_fact(
        probes,
        host.get("ssh_identity_path"),
        code="ssh_identity_path_invalid",
        kind="regular",
        owner=ssh_identity_owner,
    )


def _canonical_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))


def build_safe_plan(inventory: Mapping[str, Any], *, probes: Any) -> dict[str, Any]:
    root = _mapping(inventory, "inventory_invalid")
    if root.get("protocol") != _INVENTORY_PROTOCOL:
        _fail("inventory_protocol_invalid")
    if getattr(probes, "git_dirty", None) is not False:
        _fail("git_dirty")
    if getattr(probes, "public_network_required", None) is not False:
        _fail("public_network_required")

    run_id = _segment(root.get("run_id"), "inventory_invalid")
    deployment_id = _segment(root.get("deployment_id"), "inventory_invalid")
    source_tree = _mapping(root.get("source_tree"), "source_tree_invalid")
    expected_commit = source_tree.get("expected_commit")
    if (
        not isinstance(expected_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None
        or getattr(probes, "resolved_commit", None) != expected_commit
    ):
        _fail("source_commit_mismatch")

    source_records: list[dict[str, Any]] = []
    observed_sources = getattr(probes, "source_files", {})
    for raw in _sequence(source_tree.get("files"), "source_tree_invalid"):
        record = _mapping(raw, "source_file_invalid")
        path = _source_path(record.get("path"))
        expected_digest = _digest(record.get("digest"), "source_file_invalid")
        observed = observed_sources.get(path)
        if not isinstance(observed, Mapping):
            _fail("source_file_invalid")
        if observed.get("kind") != "regular" or observed.get("symlink") is not False:
            _fail("source_file_invalid")
        if observed.get("digest") != expected_digest:
            _fail("source_digest_mismatch")
        source_records.append({"path": path, "digest": expected_digest})
    source_records.sort(key=lambda item: item["path"])

    model = _mapping(root.get("model"), "model_invalid")
    model_alias = _public_alias(model.get("public_alias"))
    model_commit = model.get("resolved_commit")
    if not isinstance(model_commit, str) or re.fullmatch(r"[0-9a-f]{40}", model_commit) is None:
        _fail("model_invalid")
    if getattr(probes, "model_manifest_digest", None) != _digest(model.get("manifest_digest"), "model_invalid"):
        _fail("model_digest_mismatch")
    model_assets: list[dict[str, Any]] = []
    observed_blobs = getattr(probes, "model_blobs", {})
    for raw in _sequence(model.get("blobs"), "model_invalid"):
        record = _mapping(raw, "model_invalid")
        alias = _segment(record.get("alias"), "model_invalid")
        expected_digest = _digest(record.get("digest"), "model_invalid")
        observed = observed_blobs.get(alias)
        if not isinstance(observed, Mapping) or observed.get("present") is not True:
            _fail("model_blob_missing")
        if observed.get("digest") != expected_digest:
            _fail("model_digest_mismatch")
        size = record.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            _fail("model_invalid")
        model_assets.append(
            {"public_alias": f"{model_alias}:{alias}", "digest": expected_digest, "size_bytes": size}
        )
    model_assets.sort(key=lambda item: item["public_alias"])

    tokenizer = _mapping(root.get("tokenizer"), "tokenizer_invalid")
    tokenizer_alias = _public_alias(tokenizer.get("public_alias"))
    tokenizer_digest = _digest(tokenizer.get("digest"), "tokenizer_invalid")
    if getattr(probes, "tokenizer_digest", None) != tokenizer_digest:
        _fail("tokenizer_digest_mismatch")
    dependency = _mapping(root.get("dependency_lock"), "dependency_invalid")
    dependency_alias = _public_alias(dependency.get("public_alias"))
    dependency_digest = _digest(dependency.get("digest"), "dependency_invalid")
    if getattr(probes, "dependency_digest", None) != dependency_digest:
        _fail("dependency_digest_mismatch")

    hosts_raw = _sequence(root.get("hosts"), "hosts_invalid")
    if len(hosts_raw) < 2:
        _fail("hosts_invalid")
    hosts: list[dict[str, Any]] = []
    host_ids: set[str] = set()
    boot_ids: set[str] = set()
    sidecar_digest: str | None = None
    for raw in hosts_raw:
        host = _mapping(raw, "host_invalid")
        if "credential_value" in host:
            _fail("credential_value_forbidden")
        _validate_host_paths(host, probes)
        alias = _segment(host.get("alias"), "host_invalid")
        node_id = _segment(host.get("node_id"), "host_invalid")
        ssh_user = _segment(host.get("ssh_user"), "host_invalid")
        ssh_identity_path_alias = _segment(
            PurePosixPath(host["ssh_identity_path"]).name,
            "ssh_identity_path_invalid",
        )
        probe_transport = host.get("probe_transport")
        if probe_transport not in {"local", "ssh"}:
            _fail("host_invalid")
        host_id = _segment(host.get("host_id"), "host_invalid")
        boot_id = _segment(host.get("boot_id"), "host_invalid")
        if _RUN_SCOPED_HOST_RE.fullmatch(host_id) is None:
            _fail("host_invalid")
        if _RUN_SCOPED_BOOT_RE.fullmatch(boot_id) is None:
            _fail("host_invalid")
        if host_id in host_ids:
            _fail("duplicate_host_id")
        if boot_id in boot_ids:
            _fail("duplicate_boot_id")
        host_ids.add(host_id)
        boot_ids.add(boot_id)
        runtime = host.get("runtime")
        if not isinstance(runtime, str) or getattr(probes, "supported_runtimes", {}).get(runtime) is not True:
            _fail("unsupported_runtime")
        port = host.get("coordinator_port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            _fail("host_invalid")
        if (alias, port) in getattr(probes, "port_conflicts", set()):
            _fail("port_conflict")
        if f"mycelium-node:{node_id}" in getattr(probes, "process_conflicts", set()):
            _fail("process_conflict")
        expected_sidecar = _digest(host.get("sidecar_digest"), "sidecar_invalid")
        observed_sidecar = getattr(probes, "sidecar", {}).get(host.get("sidecar_binary"))
        if not isinstance(observed_sidecar, Mapping) or observed_sidecar.get("digest") != expected_sidecar:
            _fail("sidecar_digest_mismatch")
        if observed_sidecar.get("identity") != "mycelium-iroh-sidecar":
            _fail("sidecar_identity_mismatch")
        if sidecar_digest is not None and sidecar_digest != expected_sidecar:
            _fail("sidecar_digest_mismatch")
        sidecar_digest = expected_sidecar
        hosts.append(
            {
                "alias": alias,
                "role": host.get("role"),
                "node_id": node_id,
                "ssh_target": host.get("ssh_target"),
                "ssh_user": ssh_user,
                "probe_transport": probe_transport,
                "host_id": host_id,
                "boot_id": boot_id,
                "runtime": runtime,
                "coordinator_port": port,
                "credential_path_alias": f"{node_id}-endpoint-key",
                "ssh_identity_path_alias": ssh_identity_path_alias,
            }
        )
    hosts.sort(key=lambda item: item["node_id"])

    request = _canonical_clone(_mapping(root.get("request"), "request_invalid"))
    controller_nodes = [
        {
            "node_id": host["node_id"],
            "host_alias": host["alias"],
            "runtime": host["runtime"],
            "credential_path_alias": host["credential_path_alias"],
        }
        for host in hosts
    ]
    run_matrix = {
        "cold": {
            "cache_precondition": "fresh_run_scoped_stage_cache",
            "local_files_only": True,
            "public_network_bytes": 0,
            "public_downloads": "forbidden",
        },
        "warm": {
            "cache_precondition": "verified_compatible_local_cache",
            "local_files_only": True,
            "public_network_bytes": 0,
            "public_downloads": "forbidden",
        },
    }
    return {
        "protocol": SAFE_PLAN_PROTOCOL,
        "run_id": run_id,
        "deployment_id": deployment_id,
        "expected_commit": expected_commit,
        "route_ready": False,
        "release_ready": False,
        "source_manifest": source_records,
        "model_assets": model_assets,
        "tokenizer": {
            "public_alias": tokenizer_alias,
            "digest": tokenizer_digest,
        },
        "sidecar": {
            "public_alias": "mycelium-iroh-sidecar",
            "digest": sidecar_digest,
        },
        "dependencies": {
            "public_alias": dependency_alias,
            "digest": dependency_digest,
        },
        "hosts": hosts,
        "operator_plan": {
            "source_alias": _public_alias(source_tree.get("public_alias")),
            "source_files": [record["path"] for record in source_records],
            "model_alias": model_alias,
            "model_commit": model_commit,
            "model_manifest_digest": model.get("manifest_digest"),
            "request": request,
            "decode_count": root.get("decode_count"),
            "expected_token_ids": _canonical_clone(root.get("expected_token_ids")),
            "run_matrix": run_matrix,
        },
        "controller_run_plan": {
            "protocol": "mycelium.controller_run_plan.safe.v1",
            "run_id": run_id,
            "deployment_id": deployment_id,
            "entry_node_id": hosts[0]["node_id"],
            "nodes": controller_nodes,
        },
    }


__all__ = ["PlanBuildError", "SAFE_PLAN_PROTOCOL", "build_safe_plan"]
