#!/usr/bin/env python3
"""Fail-closed physical qualification controller skeleton.

This tranche validates exact transfer bytes and current seed-signed assignment
offers, then emits inert plans. It intentionally performs no physical launch,
SSH transfer, activation transport, evidence sealing, or readiness publication.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Any, NoReturn, Protocol

from mycelium_membership.contracts import (
    ASSIGNMENT_OFFER_PROTOCOL,
    MembershipContractError,
    verify_membership_message,
)

COMMANDS = frozenset(
    {"preflight", "prepare", "run", "cancel", "recover", "seal", "cleanup"}
)
MODES = frozenset({"dry-run", "fake", "local", "physical"})
_RESULT_PROTOCOL = "mycelium.physical_controller_result.v1"
_SNAPSHOT_PROTOCOL = "mycelium.controller_membership_snapshot.v1"
_TRANSFER_PROTOCOL = "mycelium.controller_transfer_manifest.v1"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SSH_TARGET_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_PATH_PARTS = frozenset(
    {".cache", ".git", ".gnupg", ".ssh", "models", "model-cache", "huggingface"}
)
_FORBIDDEN_NAME_RE = re.compile(
    r"(?:^|[._-])(?:api[-_]?key|credentials?|id[-_]?rsa|password|private[-_]?key|secrets?|tokens?)(?:[._-]|$)",
    re.IGNORECASE,
)
_MAX_DOCUMENT_BYTES = 1_048_576
_MAX_TRANSFER_BYTES = 256 * 1024 * 1024


class ControllerError(ValueError):
    """Stable fail-closed controller error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> NoReturn:
    raise ControllerError(code)


def _segment(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SEGMENT_RE.fullmatch(value) is None:
        _reject(code)
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
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
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ControllerError("noncanonical_document") from exc


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _reject("duplicate_document_key")
        value[key] = item
    return value


def _read_document(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControllerError("document_unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _reject("document_not_regular")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_DOCUMENT_BYTES:
        _reject("document_size_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ControllerError("document_open_failed") from exc
    try:
        before = os.fstat(fd)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65_536, _MAX_DOCUMENT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_DOCUMENT_BYTES:
                _reject("document_size_invalid")
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        _reject("document_changed_during_read")
    encoded = b"".join(chunks)
    try:
        value = json.loads(
            encoded,
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda _value: _reject("invalid_document_json"),
        )
    except ControllerError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ControllerError("invalid_document_json") from exc
    if not isinstance(value, dict) or encoded != _canonical_bytes(value):
        _reject("noncanonical_document")
    return value


@dataclass(frozen=True)
class PeerIdentity:
    node_id: str
    ssh_target: str
    host_id: str
    boot_id: str
    staging_root: str

    def __post_init__(self) -> None:
        _segment(self.node_id, "peer_node_id_invalid")
        _segment(self.host_id, "peer_host_id_invalid")
        _segment(self.boot_id, "peer_boot_id_invalid")
        if not isinstance(self.ssh_target, str) or _SSH_TARGET_RE.fullmatch(
            self.ssh_target
        ) is None:
            _reject("peer_ssh_target_invalid")
        if any(character in self.ssh_target for character in " ;|&$`\\\n\r\t"):
            _reject("peer_ssh_target_invalid")
        if not isinstance(self.staging_root, str):
            _reject("peer_staging_root_invalid")
        path = PurePosixPath(self.staging_root)
        if (
            not path.is_absolute()
            or str(path) != self.staging_root
            or any(part in {"", ".", ".."} for part in path.parts)
            or len(path.parts) < 4
        ):
            _reject("peer_staging_root_invalid")


@dataclass(frozen=True)
class CommandCapture:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> CommandCapture: ...


class SubprocessRunner:
    """Bounded argv-only runner reserved for later physical execution tranches."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> CommandCapture:
        if (
            not isinstance(argv, tuple)
            or not argv
            or not all(isinstance(value, str) and value for value in argv)
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or not 0.0 < float(timeout_seconds) <= 300.0
        ):
            _reject("runner_arguments_invalid")
        completed = subprocess.run(
            list(argv),
            check=False,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=float(timeout_seconds),
        )
        return CommandCapture(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _safe_transfer_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 512:
        _reject("unsafe_transfer_path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.lower() in _FORBIDDEN_PATH_PARTS for part in path.parts)
        or any(_FORBIDDEN_NAME_RE.search(part) for part in path.parts)
        or any(part.lower() == ".env" for part in path.parts)
    ):
        _reject("unsafe_transfer_path")
    return path


def _artifact_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _verify_transfer_file(
    source_root: Path,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if set(record) != {"path", "size_bytes", "content_digest"}:
        _reject("transfer_record_fields_invalid")
    relative = _safe_transfer_path(record.get("path"))
    expected_size = record.get("size_bytes")
    expected_digest = record.get("content_digest")
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or expected_size > _MAX_TRANSFER_BYTES
    ):
        _reject("transfer_size_invalid")
    if not isinstance(expected_digest, str) or _DIGEST_RE.fullmatch(expected_digest) is None:
        _reject("transfer_digest_invalid")
    candidate = source_root.joinpath(*relative.parts)
    current = source_root
    try:
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                _reject("unsafe_transfer_path")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(source_root)
    except ControllerError:
        raise
    except (OSError, ValueError) as exc:
        raise ControllerError("transfer_file_unavailable") from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise ControllerError("transfer_file_open_failed") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _reject("transfer_file_not_regular")
        if before.st_size != expected_size:
            _reject("transfer_size_mismatch")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1_048_576)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_TRANSFER_BYTES:
                _reject("transfer_size_invalid")
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _artifact_fingerprint(before) != _artifact_fingerprint(after):
        _reject("transfer_file_changed_during_read")
    actual_digest = "sha256:" + digest.hexdigest()
    if actual_digest != expected_digest:
        _reject("transfer_digest_mismatch")
    return {
        "path": str(relative),
        "size_bytes": expected_size,
        "content_digest": expected_digest,
    }


class QualificationController:
    """Validate controller inputs and emit inert command plans."""

    def __init__(
        self,
        *,
        mode: str,
        peers: Sequence[PeerIdentity],
        source_root: Path,
        transfer_manifest: Mapping[str, Any],
        membership_snapshot: Mapping[str, Any],
        now: float,
        runner: CommandRunner | None = None,
    ):
        if mode not in MODES:
            _reject("controller_mode_invalid")
        if (
            not isinstance(peers, Sequence)
            or isinstance(peers, (str, bytes))
            or not peers
            or not all(isinstance(peer, PeerIdentity) for peer in peers)
        ):
            _reject("controller_peers_invalid")
        node_ids = [peer.node_id for peer in peers]
        if len(node_ids) != len(set(node_ids)):
            _reject("controller_peer_duplicate")
        staging_roots = [peer.staging_root for peer in peers]
        if len(staging_roots) != len(set(staging_roots)):
            _reject("controller_staging_root_duplicate")
        try:
            root_metadata = source_root.lstat()
            resolved_root = source_root.resolve(strict=True)
        except OSError as exc:
            raise ControllerError("source_root_unavailable") from exc
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            _reject("source_root_invalid")
        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(float(now))
        ):
            _reject("controller_time_invalid")
        if not isinstance(transfer_manifest, Mapping) or not isinstance(
            membership_snapshot, Mapping
        ):
            _reject("controller_document_invalid")
        self.mode = mode
        self.peers = tuple(peers)
        self.source_root = resolved_root
        self._transfer_manifest = dict(transfer_manifest)
        self._membership_snapshot = dict(membership_snapshot)
        self._now = float(now)
        self._runner = runner or SubprocessRunner()

    def _validate_transfers(self) -> tuple[dict[str, Any], ...]:
        manifest = self._transfer_manifest
        if set(manifest) != {"protocol", "files"} or manifest.get(
            "protocol"
        ) != _TRANSFER_PROTOCOL:
            _reject("transfer_manifest_invalid")
        records = manifest.get("files")
        if (
            not isinstance(records, list)
            or not records
            or len(records) > 256
            or not all(isinstance(record, Mapping) for record in records)
        ):
            _reject("transfer_manifest_invalid")
        paths = [record.get("path") for record in records]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            _reject("transfer_manifest_order_invalid")
        return tuple(
            _verify_transfer_file(self.source_root, record) for record in records
        )

    def _validate_membership(self) -> dict[str, dict[str, Any]]:
        snapshot = self._membership_snapshot
        expected_fields = {
            "protocol",
            "seed_key_digest",
            "swarm_id",
            "deployment_id",
            "deployment_epoch",
            "assignment_offers",
        }
        if set(snapshot) != expected_fields or snapshot.get("protocol") != _SNAPSHOT_PROTOCOL:
            _reject("membership_snapshot_invalid")
        key_digest = snapshot.get("seed_key_digest")
        epoch = snapshot.get("deployment_epoch")
        if not isinstance(key_digest, str) or _DIGEST_RE.fullmatch(key_digest) is None:
            _reject("membership_seed_key_invalid")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            _reject("membership_epoch_invalid")
        offers = snapshot.get("assignment_offers")
        if not isinstance(offers, list) or len(offers) != len(self.peers):
            _reject("membership_offer_count_invalid")
        expected_nodes = {peer.node_id for peer in self.peers}
        validated: dict[str, dict[str, Any]] = {}
        endpoints: dict[str, dict[str, Any]] = {}
        for envelope in offers:
            recipient = (
                envelope.get("message", {}).get("recipient_node_id")
                if isinstance(envelope, Mapping)
                and isinstance(envelope.get("message"), Mapping)
                else None
            )
            if recipient not in expected_nodes or recipient in validated:
                _reject("membership_offer_recipient_invalid")
            try:
                message = verify_membership_message(
                    envelope,
                    now=self._now,
                    expected_key_digest=key_digest,
                    expected_protocol=ASSIGNMENT_OFFER_PROTOCOL,
                    expected_swarm_id=snapshot.get("swarm_id"),
                    expected_recipient_node_id=recipient,
                )
            except MembershipContractError as exc:
                raise ControllerError("membership_offer_invalid") from exc
            if message.get("deployment_id") != snapshot.get("deployment_id"):
                _reject("membership_deployment_mismatch")
            if message.get("deployment_epoch") != epoch:
                _reject("membership_epoch_mismatch")
            expected_records = expected_nodes - {recipient}
            actual_records = {
                record["node_id"] for record in message["peer_endpoint_records"]
            }
            if actual_records != expected_records:
                _reject("membership_peer_set_mismatch")
            for record in message["peer_endpoint_records"]:
                node_id = record["node_id"]
                identity = {
                    "endpoint_id": record["endpoint_id"],
                    "membership_generation": record["membership_generation"],
                }
                previous = endpoints.get(node_id)
                if previous is not None and previous != identity:
                    _reject("membership_peer_identity_conflict")
                endpoints[node_id] = identity
            validated[recipient] = message
        if set(validated) != expected_nodes:
            _reject("membership_offer_recipient_invalid")
        if len(self.peers) > 1 and set(endpoints) != expected_nodes:
            _reject("membership_peer_identity_incomplete")
        return endpoints

    def _validate_physical_distinctness(self) -> None:
        if len(self.peers) < 2:
            _reject("physical_peer_count_insufficient")
        hosts = [peer.host_id for peer in self.peers]
        boots = [peer.boot_id for peer in self.peers]
        pairs = list(zip(hosts, boots, strict=True))
        if (
            len(set(hosts)) != len(hosts)
            or len(set(boots)) != len(boots)
            or len(set(pairs)) != len(pairs)
        ):
            _reject("physical_host_identity_not_distinct")

    def execute(self, command: str) -> dict[str, Any]:
        if command not in COMMANDS:
            _reject("controller_command_invalid")
        transfers = self._validate_transfers()
        endpoints = self._validate_membership()
        if self.mode == "physical":
            self._validate_physical_distinctness()
            if command in {"prepare", "run", "recover", "seal"}:
                _reject("physical_execution_not_implemented")
        peers = [
            {
                "node_id": peer.node_id,
                "host_id": peer.host_id,
                "boot_id": peer.boot_id,
                "signed_endpoint": endpoints.get(peer.node_id),
            }
            for peer in self.peers
        ]
        actions = [
            {
                "node_id": peer.node_id,
                "command": command,
                "argv": None,
                "transfers": [dict(record) for record in transfers],
            }
            for peer in self.peers
        ]
        return {
            "protocol": _RESULT_PROTOCOL,
            "command": command,
            "mode": self.mode,
            "peer_count": len(self.peers),
            "peers": peers,
            "actions": actions,
            "route_ready": False,
            "release_ready": False,
            "physical_execution": False,
            "claim_boundary": (
                "validated inert controller plan; no SSH, process launch, activation, "
                "qualification evidence, or readiness claim"
            ),
        }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ControllerError("invalid_arguments")


def _peer_argument(value: str) -> PeerIdentity:
    parts = value.split(",")
    if len(parts) != 5:
        _reject("invalid_arguments")
    return PeerIdentity(
        node_id=parts[0],
        ssh_target=parts[1],
        host_id=parts[2],
        boot_id=parts[3],
        staging_root=parts[4],
    )


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(add_help=True, exit_on_error=False)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--mode", choices=sorted(MODES), default="physical")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--peers", nargs="+")
    parser.add_argument("--source-root")
    parser.add_argument("--transfer-manifest")
    parser.add_argument("--membership-snapshot")
    parser.add_argument("--now", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
        required = (
            args.peers,
            args.source_root,
            args.transfer_manifest,
            args.membership_snapshot,
            args.now,
        )
        if args.command == "preflight" and args.dry_run and all(
            value is None for value in required
        ):
            result = {
                "protocol": _RESULT_PROTOCOL,
                "command": "preflight",
                "mode": "dry-run",
                "peer_count": 0,
                "peers": [],
                "actions": [],
                "route_ready": False,
                "release_ready": False,
                "physical_execution": False,
                "claim_boundary": (
                    "inert preflight template; no SSH, process launch, activation, "
                    "qualification evidence, or readiness claim"
                ),
            }
            sys.stdout.buffer.write(_canonical_bytes(result))
            sys.stdout.buffer.flush()
            return 0
        if any(value is None for value in required):
            _reject("invalid_arguments")
        peers = tuple(_peer_argument(value) for value in args.peers)
        controller = QualificationController(
            mode="dry-run" if args.dry_run else args.mode,
            peers=peers,
            source_root=Path(args.source_root),
            transfer_manifest=_read_document(Path(args.transfer_manifest)),
            membership_snapshot=_read_document(Path(args.membership_snapshot)),
            now=args.now,
        )
        result = controller.execute(args.command)
        sys.stdout.buffer.write(_canonical_bytes(result))
        sys.stdout.buffer.flush()
        return 0
    except (ControllerError, argparse.ArgumentError, ValueError, TypeError):
        output = {
            "error": {"code": "invalid_arguments"},
            "ok": False,
            "route_ready": False,
        }
        if isinstance(sys.exc_info()[1], ControllerError):
            output["error"]["code"] = sys.exc_info()[1].code
        sys.stderr.buffer.write(_canonical_bytes(output))
        sys.stderr.buffer.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
