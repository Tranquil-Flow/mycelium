"""Strict JSON-only operator-plan parsing for the physical runner."""
from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import RunnerError

OPERATOR_PLAN_PROTOCOL = "mycelium.physical_runner_operator_plan.v1"
MAX_PLAN_BYTES = 2 * 1024 * 1024
MAX_STRING_BYTES = 16 * 1024
MAX_DEPTH = 64
MAX_NODES = 100_000
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_LOCAL_ROOTS = (
    "/etc",
    "/private/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/dev",
    "/proc",
    "/sys",
    "/var/log",
    "/private/var/log",
)
_TOP_LEVEL_FIELDS = frozenset(
    {"protocol", "plan_id", "run_id", "now_unix_ms", "paths", "controller", "verification_keys"}
)
_PATH_FIELDS = frozenset({"evidence_output_dir", "lock_path", "state_path", "log_path"})
_CONTROLLER_FIELDS = frozenset(
    {
        "mode",
        "now",
        "source_root",
        "peers",
        "transfer_manifest",
        "node_transfer_manifests",
        "membership_snapshot",
        "run_plan",
        "authority_documents",
        "authority_profile",
    }
)
_CONTROLLER_PEER_FIELDS = frozenset(
    {
        "node_id",
        "ssh_target",
        "ssh_identity_file",
        "host_id",
        "boot_id",
        "staging_root",
        "process_transport",
    }
)
_PUBLIC_KEY_FIELDS = frozenset(
    {"algorithm", "encoding", "verification_key", "verification_key_digest", "endpoint_id"}
)
_CREDENTIAL_KEY_PARTS = (
    "private_key",
    "password",
    "passphrase",
    "api_key",
    "access_token",
    "refresh_token",
    "credential_value",
    "secret_value",
)
_CREDENTIAL_MATERIAL_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|\b(?:hf|sk)-[A-Za-z0-9_-]{16,}", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    protocol: str
    plan_id: str
    run_id: str
    now_unix_ms: int
    evidence_output_dir: str
    lock_path: str
    state_path: str
    log_path: str
    controller: Mapping[str, Any]
    gossip_verification_keys: tuple[Mapping[str, Any], ...]
    load_proof_verification_keys: tuple[Mapping[str, Any], ...]
    operator_plan_path: str | None = None


def _fail(code: str, detail: str = "") -> None:
    raise RunnerError(code, detail)


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        _fail(code, detail)


def _validate_json(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    _require(depth <= MAX_DEPTH and nodes[0] <= MAX_NODES, "plan_shape_invalid")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        _require(math.isfinite(value), "plan_non_finite_number")
        return
    if isinstance(value, str):
        _require(len(value.encode("utf-8")) <= MAX_STRING_BYTES, "plan_string_too_long")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require(isinstance(key, str), "plan_value_not_json")
            _validate_json(item, depth=depth + 1, nodes=nodes)
        return
    _fail("plan_value_not_json", type(value).__name__)


def _scan_credentials(value: Any, *, key: str = "") -> None:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            normalized = str(child_key).lower().replace("-", "_")
            path_reference = normalized.endswith(("_file", "_path"))
            public_key = normalized in _PUBLIC_KEY_FIELDS
            if not path_reference and not public_key and any(part in normalized for part in _CREDENTIAL_KEY_PARTS):
                _fail("plan_credential_field", normalized)
            _scan_credentials(child, key=normalized)
        return
    if isinstance(value, list):
        for child in value:
            _scan_credentials(child, key=key)
        return
    if isinstance(value, str) and _CREDENTIAL_MATERIAL_RE.search(value):
        _fail("plan_credential_material")


def _identifier(value: Any, field: str) -> str:
    _require(isinstance(value, str) and bool(value) and len(value) <= 128, "plan_field_invalid", field)
    _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None, "plan_field_invalid", field)
    return value


def _safe_local_path(value: Any, field: str, *, existing_directory: bool = False) -> str:
    _require(isinstance(value, str) and value and "\x00" not in value and not any(c in value for c in "\n\r\t"), "plan_path_unsafe", field)
    raw = Path(value)
    _require(raw.is_absolute() and ".." not in raw.parts and "." not in raw.parts, "plan_path_unsafe", field)
    raw_text = raw.as_posix()
    for prefix in _FORBIDDEN_LOCAL_ROOTS:
        _require(raw_text != prefix and not raw_text.startswith(prefix + "/"), "plan_path_unsafe", field)
    cursor = raw
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    try:
        while True:
            if cursor.is_symlink():
                _fail("plan_path_unsafe", field)
            if cursor == cursor.parent:
                break
            cursor = cursor.parent
        resolved = raw.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RunnerError("plan_path_unsafe", field) from exc
    _require(resolved.as_posix() == raw_text, "plan_path_unsafe", field)
    if existing_directory:
        _require(resolved.is_dir() and not resolved.is_symlink(), "plan_path_unsafe", field)
    return raw_text


def _safe_remote_path(value: Any, field: str) -> str:
    _require(isinstance(value, str) and value and "\x00" not in value, "plan_path_unsafe", field)
    path = PurePosixPath(value)
    _require(path.is_absolute() and str(path) == value and ".." not in path.parts and "." not in path.parts, "plan_path_unsafe", field)
    return value


def _safe_private_file(value: Any, field: str) -> str:
    normalized = _safe_local_path(value, field)
    path = Path(normalized)
    current = Path(path.anchor)
    try:
        for part in (path.anchor, *path.parts[1:-1]):
            if part != path.anchor:
                current /= part
            ancestor = current.lstat()
            _require(
                stat.S_ISDIR(ancestor.st_mode)
                and not stat.S_ISLNK(ancestor.st_mode)
                and ancestor.st_uid in {0, os.geteuid()}
                and stat.S_IMODE(ancestor.st_mode) & 0o022 == 0,
                "plan_path_unsafe",
                field,
            )
        metadata = path.lstat()
    except OSError as exc:
        raise RunnerError("plan_path_unsafe", field) from exc
    _require(
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_nlink == 1
        and metadata.st_size > 0
        and metadata.st_uid == os.geteuid()
        and metadata.st_mode & 0o077 == 0,
        "plan_path_unsafe",
        field,
    )
    return normalized


def _verification_keys(value: Any, field: str) -> tuple[Mapping[str, Any], ...]:
    _require(isinstance(value, list) and value, "plan_field_invalid", field)
    records: list[Mapping[str, Any]] = []
    for record in value:
        _require(isinstance(record, Mapping), "plan_field_invalid", field)
        snapshot = dict(record)
        _require(set(snapshot).issubset(_PUBLIC_KEY_FIELDS), "plan_unknown_field", field)
        _require(snapshot.get("algorithm") == "ed25519", "plan_field_invalid", field)
        _require(isinstance(snapshot.get("verification_key"), str), "plan_field_invalid", field)
        digest = snapshot.get("verification_key_digest")
        _require(isinstance(digest, str) and _SHA256_RE.fullmatch(digest) is not None, "plan_field_invalid", field)
        records.append(json.loads(json.dumps(snapshot, sort_keys=True)))
    return tuple(records)


def _controller(value: Any) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), "plan_field_invalid", "controller")
    snapshot = dict(value)
    unknown = set(snapshot) - _CONTROLLER_FIELDS
    _require(not unknown, "plan_unknown_field", "controller." + ",".join(sorted(unknown)))
    required = _CONTROLLER_FIELDS - {
        "authority_documents",
        "authority_profile",
        "node_transfer_manifests",
    }
    missing = required - set(snapshot)
    _require(not missing, "plan_missing_field", "controller." + ",".join(sorted(missing)))
    _require(snapshot.get("mode") == "physical", "plan_field_invalid", "controller.mode")
    authority_profile = snapshot.get("authority_profile")
    _require(
        authority_profile is None
        or authority_profile == "physical_frozen_route_inference_v1",
        "plan_field_invalid",
        "controller.authority_profile",
    )
    _require(
        authority_profile is None or "authority_documents" not in snapshot,
        "plan_field_invalid",
        "controller.authority_documents",
    )
    now = snapshot.get("now")
    _require(isinstance(now, (int, float)) and not isinstance(now, bool) and math.isfinite(float(now)), "plan_field_invalid", "controller.now")
    snapshot["source_root"] = _safe_local_path(snapshot.get("source_root"), "controller.source_root", existing_directory=True)
    peers = snapshot.get("peers")
    _require(isinstance(peers, list) and len(peers) >= 2, "plan_field_invalid", "controller.peers")
    peers_by_node: dict[str, Mapping[str, Any]] = {}
    for peer in peers:
        _require(isinstance(peer, Mapping), "plan_field_invalid", "controller.peers")
        peer_fields = set(peer)
        unknown_peer_fields = peer_fields - _CONTROLLER_PEER_FIELDS
        missing_peer_fields = _CONTROLLER_PEER_FIELDS - peer_fields
        _require(
            not unknown_peer_fields,
            "plan_unknown_field",
            "controller.peers." + ",".join(sorted(unknown_peer_fields)),
        )
        _require(
            not missing_peer_fields,
            "plan_missing_field",
            "controller.peers." + ",".join(sorted(missing_peer_fields)),
        )
        node_id = peer.get("node_id")
        _require(
            isinstance(node_id, str) and bool(node_id) and node_id not in peers_by_node,
            "plan_field_invalid",
            "controller.peers.node_id",
        )
        peers_by_node[node_id] = peer
        process_transport = peer.get("process_transport")
        _require(
            process_transport in {"local", "ssh"},
            "plan_field_invalid",
            "controller.peers.process_transport",
        )
        ssh_identity_file = peer.get("ssh_identity_file")
        if process_transport == "ssh":
            _safe_private_file(
                ssh_identity_file,
                "controller.peers.ssh_identity_file",
            )
        else:
            _require(
                ssh_identity_file is None,
                "plan_field_invalid",
                "controller.peers.ssh_identity_file",
            )
        _safe_remote_path(peer.get("staging_root"), "controller.peers.staging_root")
    for field in ("transfer_manifest", "membership_snapshot", "run_plan"):
        _require(isinstance(snapshot.get(field), Mapping), "plan_field_invalid", f"controller.{field}")
    if "node_transfer_manifests" in snapshot:
        node_manifests = snapshot["node_transfer_manifests"]
        _require(
            isinstance(node_manifests, Mapping)
            and node_manifests.get("protocol")
            == "mycelium.controller_node_transfer_manifests.v1"
            and isinstance(node_manifests.get("manifests"), Mapping),
            "plan_field_invalid",
            "controller.node_transfer_manifests",
        )
    entry_node_id = snapshot["run_plan"].get("entry_node_id")
    _require(
        isinstance(entry_node_id, str) and entry_node_id in peers_by_node,
        "plan_field_invalid",
        "controller.run_plan.entry_node_id",
    )
    for node_id, peer in peers_by_node.items():
        expected_transport = "local" if node_id == entry_node_id else "ssh"
        _require(
            peer.get("process_transport") == expected_transport,
            "plan_field_invalid",
            "controller.peers.process_transport",
        )
    if "authority_documents" in snapshot:
        _require(isinstance(snapshot["authority_documents"], Mapping), "plan_field_invalid", "controller.authority_documents")
    return json.loads(json.dumps(snapshot, sort_keys=True))


def parse_operator_plan(value: Any, *, operator_plan_path: str | None = None) -> RunnerConfig:
    _require(isinstance(value, Mapping), "plan_type_invalid")
    _validate_json(value)
    _scan_credentials(value)
    snapshot = dict(value)
    unknown = set(snapshot) - _TOP_LEVEL_FIELDS
    missing = _TOP_LEVEL_FIELDS - set(snapshot)
    _require(not unknown, "plan_unknown_field", ",".join(sorted(unknown)))
    _require(not missing, "plan_missing_field", ",".join(sorted(missing)))
    _require(snapshot.get("protocol") == OPERATOR_PLAN_PROTOCOL, "plan_protocol_invalid")
    plan_id = _identifier(snapshot.get("plan_id"), "plan_id")
    run_id = _identifier(snapshot.get("run_id"), "run_id")
    now = snapshot.get("now_unix_ms")
    _require(isinstance(now, int) and not isinstance(now, bool) and now >= 0, "plan_field_invalid", "now_unix_ms")
    paths = snapshot.get("paths")
    _require(isinstance(paths, Mapping), "plan_field_invalid", "paths")
    _require(set(paths) == _PATH_FIELDS, "plan_unknown_field" if set(paths) - _PATH_FIELDS else "plan_missing_field", "paths")
    normalized_paths = {name: _safe_local_path(paths[name], f"paths.{name}") for name in sorted(_PATH_FIELDS)}
    keys = snapshot.get("verification_keys")
    _require(isinstance(keys, Mapping) and set(keys) == {"gossip", "load_proof"}, "plan_field_invalid", "verification_keys")
    return RunnerConfig(
        protocol=OPERATOR_PLAN_PROTOCOL,
        plan_id=plan_id,
        run_id=run_id,
        now_unix_ms=now,
        evidence_output_dir=normalized_paths["evidence_output_dir"],
        lock_path=normalized_paths["lock_path"],
        state_path=normalized_paths["state_path"],
        log_path=normalized_paths["log_path"],
        controller=_controller(snapshot["controller"]),
        gossip_verification_keys=_verification_keys(keys["gossip"], "verification_keys.gossip"),
        load_proof_verification_keys=_verification_keys(keys["load_proof"], "verification_keys.load_proof"),
        operator_plan_path=operator_plan_path,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("plan_duplicate_key", key)
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    _fail("plan_non_finite_number")


def load_operator_plan(path: str | os.PathLike[str]) -> RunnerConfig:
    candidate = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(candidate, flags)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            _fail("plan_unavailable")
        if metadata.st_size > MAX_PLAN_BYTES:
            _fail("plan_too_large")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            raw = handle.read(MAX_PLAN_BYTES + 1)
    except RunnerError:
        raise
    except OSError as exc:
        raise RunnerError("plan_unavailable") from exc
    if len(raw) > MAX_PLAN_BYTES:
        _fail("plan_too_large")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RunnerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerError("plan_invalid") from exc
    return parse_operator_plan(decoded, operator_plan_path=str(candidate.resolve()))


# Backward-compatible names are strict aliases, not a second schema.
PROTOCOL = OPERATOR_PLAN_PROTOCOL
parse_config = parse_operator_plan
load_config = load_operator_plan

__all__ = [
    "MAX_PLAN_BYTES",
    "OPERATOR_PLAN_PROTOCOL",
    "PROTOCOL",
    "RunnerConfig",
    "load_config",
    "load_operator_plan",
    "parse_config",
    "parse_operator_plan",
]
