# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh operator-plan template input for the Qwen live-route builder.

``scripts/build_qwen_live_route.py`` consumes a template plan document as the
seed of the operator plan it compiles.  Historically that template was a W6
fleet artifact.  This module builds a fresh, lane-owned template from only
two caller supplies:

* ``private_run_root`` -- the caller's private run root every output path is
  bound under; and
* ``public_keys`` -- explicitly supplied real ed25519 public-key records for
  gossip and load-proof verification.

The result is *uncompiled intent*: placeholder identifiers and time that the
existing builder replaces, an empty transfer manifest the builder fills with
the complete runtime closure, and pending peers/run-plan/membership snapshot
the builder recompiles from the supplied managed topology and hello
observations.  Nothing here imports W6/fleet state, fabricates observed
topology, or creates signer/seed authority: no private key is generated,
loaded, or accepted.  Construction performs no filesystem writes and no
network access; the only I/O is closed canonical validation.

A template from this module is NOT execution evidence and NOT a runnable or
qualified plan.  Canonical parse succeeds only after the builder (or a unit
fixture) fills the compiler-owned fields with real material.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import re
import stat as stat_module
from typing import Any

OPERATOR_PLAN_PROTOCOL = "mycelium.physical_runner_operator_plan.v1"

_TRANSFER_MANIFEST_PROTOCOL = "mycelium.controller_transfer_manifest.v1"
_PENDING_SNAPSHOT_PROTOCOL = "mycelium.controller_membership_snapshot.pending.v1"
_PENDING_RUN_PLAN_PROTOCOL = "mycelium.controller_run_plan.pending.v1"

# Mirrors the canonical parser's forbidden local roots
# (mycelium_physical_runner.config._FORBIDDEN_LOCAL_ROOTS).
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

_PUBLIC_KEY_FIELDS = frozenset(
    {
        "algorithm",
        "encoding",
        "verification_key",
        "verification_key_digest",
        "endpoint_id",
    }
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
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|\b(?:hf|sk)-[A-Za-z0-9_-]{16,}",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENDPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KEY_PURPOSES = ("gossip", "load_proof")


class CompilerInputsError(ValueError):
    """Fail-closed template validation error with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise CompilerInputsError(code, detail)


def _reject_control_characters(value: str, code: str, field: str) -> None:
    _require(
        not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value),
        code,
        field,
    )


def _validated_run_root(private_run_root: Any) -> str:
    """Return a canonical absolute root path, rejecting unsafe locations.

    Validation only: no writes.  The root may not yet exist; every existing
    ancestor from the filesystem root down must be a real, non-symlink
    directory and the lexically-normalized text must equal its resolution.
    """

    if isinstance(private_run_root, (bytes, bytearray, memoryview)):
        raise CompilerInputsError("run_root_type_invalid")
    if isinstance(private_run_root, (int, float, bool)) or private_run_root is None:
        raise CompilerInputsError("run_root_type_invalid")
    try:
        raw_text = os.fspath(private_run_root)
    except TypeError as exc:
        raise CompilerInputsError("run_root_type_invalid") from exc
    _require(isinstance(raw_text, str) and bool(raw_text), "run_root_invalid")
    _reject_control_characters(raw_text, "run_root_unsafe", "private_run_root")

    raw = Path(raw_text)
    _require(
        raw.is_absolute() and str(raw) == raw_text,
        "run_root_not_absolute",
        raw_text,
    )
    _require(
        ".." not in raw.parts and "." not in raw.parts and len(raw.parts) > 1,
        "run_root_unsafe",
        raw_text,
    )
    posix_text = raw.as_posix()
    for prefix in _FORBIDDEN_LOCAL_ROOTS:
        _require(
            posix_text != prefix and not posix_text.startswith(prefix + "/"),
            "run_root_forbidden",
            posix_text,
        )

    cursor = raw
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    try:
        while True:
            if cursor.is_symlink():
                raise CompilerInputsError("run_root_symlink", posix_text)
            if cursor == cursor.parent:
                break
            cursor = cursor.parent
        resolved = raw.resolve(strict=False)
    except OSError as exc:
        raise CompilerInputsError("run_root_unsafe", posix_text) from exc
    _require(
        resolved.as_posix() == posix_text,
        "run_root_unsafe",
        posix_text,
    )
    if raw.exists():
        metadata = raw.lstat()
        _require(
            stat_module.S_ISDIR(metadata.st_mode),
            "run_root_not_directory",
            posix_text,
        )
    return posix_text


def _validated_public_keys(public_keys: Any) -> dict[str, list[dict[str, Any]]]:
    """Return deep-copied validated public-key records per purpose.

    Every key record must be a real caller-supplied ed25519 public key whose
    base64 text, decoded bytes and sha256 digest are mutually consistent.
    Private or credential-shaped fields and material are rejected outright.
    """

    _require(
        isinstance(public_keys, Mapping) and set(public_keys) == set(_KEY_PURPOSES),
        "public_keys_purposes_invalid",
        ",".join(sorted(public_keys)) if isinstance(public_keys, Mapping) else "",
    )
    validated: dict[str, list[dict[str, Any]]] = {}
    for purpose in _KEY_PURPOSES:
        records = public_keys[purpose]
        _require(
            isinstance(records, (list, tuple)) and bool(records),
            "public_keys_records_invalid",
            purpose,
        )
        seen_digests: set[str] = set()
        collected: list[dict[str, Any]] = []
        for record in records:
            _require(
                isinstance(record, Mapping),
                "public_keys_record_invalid",
                purpose,
            )
            fields = set(record)
            unknown = fields - _PUBLIC_KEY_FIELDS
            _require(not unknown, "public_keys_unknown_field", f"{purpose}:{sorted(unknown)[0] if unknown else ''}")
            for key in fields:
                normalized = str(key).lower().replace("-", "_")
                _require(
                    normalized in _PUBLIC_KEY_FIELDS
                    or not any(part in normalized for part in _CREDENTIAL_KEY_PARTS),
                    "public_keys_credential_field",
                    f"{purpose}:{normalized}",
                )
            _require(
                record.get("algorithm") == "ed25519",
                "public_keys_algorithm_invalid",
                purpose,
            )
            _require(
                record.get("encoding") == "base64",
                "public_keys_encoding_invalid",
                purpose,
            )
            key_text = record.get("verification_key")
            _require(
                isinstance(key_text, str) and bool(key_text),
                "public_keys_value_invalid",
                purpose,
            )
            try:
                key_bytes = base64.b64decode(key_text, validate=True)
            except (ValueError, TypeError) as exc:
                raise CompilerInputsError(
                    "public_keys_value_invalid", purpose
                ) from exc
            _require(
                len(key_bytes) == 32,
                "public_keys_length_invalid",
                purpose,
            )
            digest = record.get("verification_key_digest")
            _require(
                isinstance(digest, str) and _SHA256_RE.fullmatch(digest) is not None,
                "public_keys_digest_invalid",
                purpose,
            )
            expected = "sha256:" + hashlib.sha256(key_bytes).hexdigest()
            _require(
                digest == expected,
                "public_keys_digest_mismatch",
                purpose,
            )
            endpoint_id = record.get("endpoint_id")
            if endpoint_id is not None:
                _require(
                    isinstance(endpoint_id, str)
                    and _ENDPOINT_ID_RE.fullmatch(endpoint_id) is not None,
                    "public_keys_endpoint_id_invalid",
                    purpose,
                )
            snapshot = dict(record)
            for value in snapshot.values():
                if isinstance(value, str):
                    _require(
                        _CREDENTIAL_MATERIAL_RE.search(value) is None,
                        "public_keys_credential_material",
                        purpose,
                    )
            _require(
                digest not in seen_digests,
                "public_keys_duplicate",
                purpose,
            )
            seen_digests.add(digest)
            collected.append(snapshot)
        validated[purpose] = collected
    return validated


def _pending_peers(root_text: str) -> list[dict[str, Any]]:
    """Two placeholder managed peers carrying no process authority.

    The builder replaces these from the supplied managed topology and hello
    observations.  ``managed_https`` peers must never carry SSH targets or
    identity files, and the placeholder host/boot identities must never be
    mistaken for measured run-scoped identities.
    """

    peers = []
    for suffix in ("a", "b"):
        peers.append(
            {
                "node_id": f"pending-node-{suffix}",
                "ssh_target": None,
                "ssh_identity_file": None,
                "host_id": f"pending-host-{suffix}",
                "boot_id": f"pending-boot-{suffix}",
                "staging_root": f"{root_text}/staging/pending-node-{suffix}",
                "process_transport": "managed_https",
            }
        )
    return peers


def fresh_operator_template(
    private_run_root: str | os.PathLike[str],
    public_keys: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a fresh uncompiled operator-plan template.

    The four canonical output paths are unique and bound under the caller's
    private run root; identifiers and time are placeholders the existing
    builder replaces; ``controller.transfer_manifest.files`` is empty because
    the builder supplies the complete runtime closure; verification keys are
    the caller's validated real public records.  The template is intent, not
    execution evidence: it is deliberately NOT runnable until the builder
    fills peers, run plan, membership snapshot and manifests.
    """

    root_text = _validated_run_root(private_run_root)
    keys = _validated_public_keys(public_keys)
    return {
        "protocol": OPERATOR_PLAN_PROTOCOL,
        "plan_id": "pending-plan",
        "run_id": "pending-run",
        "now_unix_ms": 0,
        "paths": {
            "evidence_output_dir": f"{root_text}/evidence",
            "lock_path": f"{root_text}/run/runner.lock",
            "state_path": f"{root_text}/run/runner-state.json",
            "log_path": f"{root_text}/run/runner.log",
        },
        "controller": {
            "mode": "physical",
            "now": 0.0,
            "source_root": f"{root_text}/transfer-bundle",
            "peers": _pending_peers(root_text),
            "transfer_manifest": {
                "protocol": _TRANSFER_MANIFEST_PROTOCOL,
                "files": [],
            },
            "membership_snapshot": {
                "protocol": _PENDING_SNAPSHOT_PROTOCOL,
                "deployment_id": "pending-deployment",
                "state": "replaced_by_builder",
            },
            "run_plan": {
                "protocol": _PENDING_RUN_PLAN_PROTOCOL,
                "run_id": "pending-run",
                "deployment_id": "pending-deployment",
                "entry_node_id": "pending-node-a",
                "nodes": [],
            },
        },
        "verification_keys": {
            "gossip": [dict(record) for record in keys["gossip"]],
            "load_proof": [dict(record) for record in keys["load_proof"]],
        },
    }


__all__ = [
    "CompilerInputsError",
    "OPERATOR_PLAN_PROTOCOL",
    "fresh_operator_template",
]
