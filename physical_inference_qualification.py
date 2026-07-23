#!/usr/bin/env python3
"""Fail-closed physical qualification controller.

The controller validates exact transfer bytes and current seed-signed assignment
offers, stages explicit archives, and drives bounded node-control sessions. It
never publishes route or release readiness; only the evidence qualifier may do
that after complete physical evidence sealing.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import select
import shlex
import stat
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, Callable, NoReturn, Protocol

from mycelium_membership.contracts import (
    ASSIGNMENT_OFFER_PROTOCOL,
    MembershipContractError,
    verify_membership_message,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    EvidenceSigningError,
    build_ed25519_verifier,
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
_MAX_RUNNER_OUTPUT_BYTES = 1_048_576
_STAGE_ACK_PROTOCOL = "mycelium.controller_remote_stage_ack.v1"
_CLEANUP_ACK_PROTOCOL = "mycelium.controller_remote_cleanup_ack.v1"
_NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
_NODE_OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"
_RUN_PLAN_PROTOCOL = "mycelium.controller_run_plan.v1"
_REMOTE_STAGE_SCRIPT = r'''import hashlib,io,json,os,shutil,sys,tarfile
from pathlib import Path,PurePosixPath
root=Path(sys.argv[1]);node_id=sys.argv[2];expected_digest=sys.argv[3];expected_size=int(sys.argv[4]);created=False
try:
    if not root.is_absolute() or str(root)!=sys.argv[1] or len(root.parts)<4 or root.exists():raise ValueError("root")
    current=Path(root.anchor)
    for part in root.parts[1:-1]:
        current=current/part
        if current.exists() and current.is_symlink():raise ValueError("symlink")
    root.mkdir(parents=True,mode=0o700,exist_ok=False);created=True
    raw=sys.stdin.buffer.read(expected_size+1)
    if len(raw)!=expected_size:raise ValueError("size")
    actual="sha256:"+hashlib.sha256(raw).hexdigest()
    if actual!=expected_digest:raise ValueError("digest")
    with tarfile.open(fileobj=io.BytesIO(raw),mode="r:") as archive:
        members=archive.getmembers();names=[member.name for member in members]
        if not members or len(members)>256 or names!=sorted(names) or len(names)!=len(set(names)):raise ValueError("members")
        for member in members:
            relative=PurePosixPath(member.name)
            if not member.isfile() or relative.is_absolute() or str(relative)!=member.name or any(part in ("",".","..") for part in relative.parts):raise ValueError("member")
            source=archive.extractfile(member)
            if source is None:raise ValueError("content")
            content=source.read()
            if len(content)!=member.size:raise ValueError("content")
            destination=root.joinpath(*relative.parts);destination.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            with destination.open("xb") as output:output.write(content)
            destination.chmod(0o600)
    marker={"archive_digest":actual,"node_id":node_id};marker_path=root/".mycelium-stage.json"
    with marker_path.open("x",encoding="utf-8") as output:output.write(json.dumps(marker,sort_keys=True,separators=(",",":"))+"\n")
    marker_path.chmod(0o600)
    ack={"archive_digest":actual,"archive_size_bytes":len(raw),"node_id":node_id,"protocol":"mycelium.controller_remote_stage_ack.v1","staging_root":str(root)}
    sys.stdout.write(json.dumps(ack,sort_keys=True,separators=(",",":"))+"\n");sys.stdout.flush()
except BaseException:
    if created:shutil.rmtree(root,ignore_errors=True)
    sys.stderr.write("remote_stage_rejected\n");raise SystemExit(2)
'''
_REMOTE_CLEANUP_SCRIPT = r'''import json,shutil,stat,sys
from pathlib import Path
root=Path(sys.argv[1]);node_id=sys.argv[2];archive_digest=sys.argv[3]
try:
    if not root.is_absolute() or str(root)!=sys.argv[1] or len(root.parts)<4 or not any(part.startswith("mycelium") for part in root.parts):raise ValueError("root")
    removed=False
    if root.exists():
        metadata=root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):raise ValueError("root")
        marker_path=root/".mycelium-stage.json";marker_metadata=marker_path.lstat()
        if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink!=1 or marker_metadata.st_size>1024:raise ValueError("marker")
        marker=json.loads(marker_path.read_text(encoding="utf-8"))
        if marker!={"archive_digest":archive_digest,"node_id":node_id}:raise ValueError("marker")
        shutil.rmtree(root);removed=True
    ack={"node_id":node_id,"protocol":"mycelium.controller_remote_cleanup_ack.v1","removed":removed,"staging_root":str(root)}
    sys.stdout.write(json.dumps(ack,sort_keys=True,separators=(",",":"))+"\n");sys.stdout.flush()
except BaseException:
    sys.stderr.write("remote_cleanup_rejected\n");raise SystemExit(2)
'''


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
            or not any(part.startswith("mycelium") for part in path.parts)
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
        stdin_bytes: bytes | None = None,
    ) -> CommandCapture: ...


class SubprocessRunner:
    """Bounded argv-only runner; local shell expansion is never used."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_bytes: bytes | None = None,
    ) -> CommandCapture:
        if (
            not isinstance(argv, tuple)
            or not argv
            or not all(isinstance(value, str) and value for value in argv)
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or not 0.0 < float(timeout_seconds) <= 300.0
            or (stdin_bytes is not None and not isinstance(stdin_bytes, bytes))
        ):
            _reject("runner_arguments_invalid")
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                shell=False,
                input=stdin_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=float(timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            raise ControllerError("command_timeout") from exc
        if (
            len(completed.stdout) > _MAX_RUNNER_OUTPUT_BYTES
            or len(completed.stderr) > _MAX_RUNNER_OUTPUT_BYTES
        ):
            _reject("command_output_too_large")
        return CommandCapture(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class NodeProcessSession:
    """Bounded canonical JSON-lines session for one physical node process."""

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        node_id: str,
        run_id: str,
        deployment_id: str,
        timeout_seconds: float,
    ) -> None:
        if (
            not isinstance(argv, tuple)
            or not argv
            or not all(isinstance(value, str) and value for value in argv)
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or not 0.0 < float(timeout_seconds) <= 300.0
        ):
            _reject("node_process_arguments_invalid")
        _segment(node_id, "peer_node_id_invalid")
        _segment(run_id, "run_id_invalid")
        _segment(deployment_id, "deployment_id_invalid")
        self.argv = argv
        self.node_id = node_id
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.timeout_seconds = float(timeout_seconds)
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._stderr_overflow = False
        self._stderr_lock = threading.Lock()
        self._request_lock = threading.Lock()
        try:
            self._process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise ControllerError("node_process_launch_failed") from exc
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"node-stderr-{node_id}",
            daemon=True,
        )
        self._stderr_thread.start()

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    @property
    def stderr(self) -> bytes:
        with self._stderr_lock:
            return bytes(self._stderr_buffer)

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                return
            terminate = False
            with self._stderr_lock:
                remaining = _MAX_RUNNER_OUTPUT_BYTES - len(self._stderr_buffer)
                if remaining > 0:
                    self._stderr_buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._stderr_overflow = True
                    terminate = True
            if terminate:
                try:
                    self._process.terminate()
                except OSError:
                    pass
                return

    def _read_line(self) -> bytes:
        assert self._process.stdout is not None
        deadline = time.monotonic() + self.timeout_seconds
        while b"\n" not in self._stdout_buffer:
            if len(self._stdout_buffer) > _MAX_DOCUMENT_BYTES:
                _reject("node_response_too_large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _reject("node_response_timeout")
            readable, _, _ = select.select(
                [self._process.stdout.fileno()],
                [],
                [],
                remaining,
            )
            if not readable:
                _reject("node_response_timeout")
            chunk = os.read(self._process.stdout.fileno(), 65_536)
            if not chunk:
                _reject("node_process_exited")
            self._stdout_buffer.extend(chunk)
        boundary = self._stdout_buffer.index(b"\n") + 1
        line = bytes(self._stdout_buffer[:boundary])
        del self._stdout_buffer[:boundary]
        if len(line) > _MAX_DOCUMENT_BYTES:
            _reject("node_response_too_large")
        return line

    def send(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        _segment(command_id, "command_id_invalid")
        _segment(command, "node_command_invalid")
        if not isinstance(payload, Mapping):
            _reject("node_command_payload_invalid")
        envelope = {
            "protocol": _NODE_CONTROL_PROTOCOL,
            "command_id": command_id,
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "command": command,
            "payload": dict(payload),
        }
        encoded = _canonical_bytes(envelope)
        with self._request_lock:
            with self._stderr_lock:
                stderr_overflow = self._stderr_overflow
            if stderr_overflow:
                _reject("node_stderr_too_large")
            if self._process.poll() is not None:
                _reject("node_process_exited")
            stdin = self._process.stdin
            if stdin is None or stdin.closed:
                _reject("node_process_exited")
            try:
                stdin.write(encoded)
                stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise ControllerError("node_process_exited") from exc
            line = self._read_line()
        try:
            response = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_reject_duplicates,
                parse_constant=lambda _value: _reject("node_response_invalid"),
            )
        except ControllerError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ControllerError("node_response_invalid") from exc
        if not isinstance(response, dict) or line != _canonical_bytes(response):
            _reject("node_response_noncanonical")
        common_fields = {
            "protocol",
            "command_id",
            "node_id",
            "ok",
            "route_ready",
        }
        if set(response) not in (common_fields | {"result"}, common_fields | {"error"}):
            _reject("node_response_fields_invalid")
        if (
            response.get("protocol") != _NODE_CONTROL_PROTOCOL
            or response.get("command_id") != command_id
            or response.get("node_id") != self.node_id
        ):
            _reject("node_response_correlation_invalid")
        if response.get("route_ready") is not False:
            _reject("node_response_readiness_invalid")
        if not isinstance(response.get("ok"), bool):
            _reject("node_response_fields_invalid")
        if response["ok"] != ("result" in response):
            _reject("node_response_fields_invalid")
        return response

    def close(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass
        if self._process.poll() is None:
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2.0)
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        self._stderr_thread.join(timeout=2.0)


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


def _read_verified_transfer_file(
    source_root: Path,
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
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
    chunks: list[bytes] = []
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
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _artifact_fingerprint(before) != _artifact_fingerprint(after):
        _reject("transfer_file_changed_during_read")
    actual_digest = "sha256:" + digest.hexdigest()
    if actual_digest != expected_digest:
        _reject("transfer_digest_mismatch")
    return (
        {
            "path": str(relative),
            "size_bytes": expected_size,
            "content_digest": expected_digest,
        },
        b"".join(chunks),
    )


def _verify_transfer_file(
    source_root: Path,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    verified, _content = _read_verified_transfer_file(source_root, record)
    return verified


def build_transfer_archive(
    source_root: Path,
    transfer_manifest: Mapping[str, Any],
) -> bytes:
    """Build a deterministic tar containing exactly the verified manifest files."""

    if (
        not isinstance(transfer_manifest, Mapping)
        or set(transfer_manifest) != {"protocol", "files"}
        or transfer_manifest.get("protocol") != _TRANSFER_PROTOCOL
    ):
        _reject("transfer_manifest_invalid")
    records = transfer_manifest.get("files")
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
    try:
        root_metadata = source_root.lstat()
        resolved_root = source_root.resolve(strict=True)
    except OSError as exc:
        raise ControllerError("source_root_unavailable") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        _reject("source_root_invalid")
    verified = [
        _read_verified_transfer_file(resolved_root, record) for record in records
    ]
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for record, content in verified:
            member = tarfile.TarInfo(record["path"])
            member.size = len(content)
            member.mode = 0o600
            member.uid = 0
            member.gid = 0
            member.mtime = 0
            member.uname = ""
            member.gname = ""
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


class QualificationController:
    """Validate inputs, orchestrate bounded physical work, never self-promote."""

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
        run_plan: Mapping[str, Any] | None = None,
        session_factory: Callable[..., Any] | None = None,
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
        if run_plan is not None and not isinstance(run_plan, Mapping):
            _reject("controller_run_plan_invalid")
        if session_factory is not None and not callable(session_factory):
            _reject("controller_session_factory_invalid")
        self.mode = mode
        self.peers = tuple(peers)
        self.source_root = resolved_root
        self._transfer_manifest = dict(transfer_manifest)
        self._membership_snapshot = dict(membership_snapshot)
        self._run_plan = None if run_plan is None else dict(run_plan)
        self._now = float(now)
        self._runner = runner or SubprocessRunner()
        self._session_factory = session_factory or NodeProcessSession

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

    def _validate_run_plan(self) -> dict[str, Any]:
        plan = self._run_plan
        expected_fields = {
            "protocol",
            "run_id",
            "deployment_id",
            "entry_node_id",
            "nodes",
            "request",
            "decode_count",
            "expected_token_ids",
        }
        if plan is None or set(plan) != expected_fields:
            _reject("controller_run_plan_invalid")
        if plan.get("protocol") != _RUN_PLAN_PROTOCOL:
            _reject("controller_run_plan_invalid")
        run_id = _segment(plan.get("run_id"), "run_id_invalid")
        deployment_id = _segment(
            plan.get("deployment_id"), "deployment_id_invalid"
        )
        if deployment_id != self._membership_snapshot.get("deployment_id"):
            _reject("run_plan_deployment_mismatch")
        node_ids = [peer.node_id for peer in self.peers]
        if len(node_ids) != 2:
            _reject("physical_run_peer_count_invalid")
        entry_node_id = plan.get("entry_node_id")
        if entry_node_id not in node_ids:
            _reject("run_plan_entry_node_invalid")
        records = plan.get("nodes")
        if not isinstance(records, list) or len(records) != len(node_ids):
            _reject("run_plan_nodes_invalid")
        expected_node_fields = {
            "node_id",
            "socket_root",
            "sidecar_binary",
            "endpoint_secret_file",
            "configure",
        }
        normalized_nodes: list[dict[str, Any]] = []
        actual_node_ids: list[Any] = []
        for record in records:
            if not isinstance(record, Mapping) or set(record) != expected_node_fields:
                _reject("run_plan_node_invalid")
            node_id = record.get("node_id")
            actual_node_ids.append(node_id)
            socket_root = record.get("socket_root")
            sidecar_binary = record.get("sidecar_binary")
            for value, code, require_marker in (
                (socket_root, "run_plan_socket_root_invalid", True),
                (sidecar_binary, "run_plan_sidecar_binary_invalid", False),
            ):
                if not isinstance(value, str) or len(value) > 1024:
                    _reject(code)
                path = PurePosixPath(value)
                if (
                    not path.is_absolute()
                    or str(path) != value
                    or len(path.parts) < 3
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or any(character in value for character in "\n\r\t")
                    or (
                        require_marker
                        and not any(part.startswith("mycelium") for part in path.parts)
                    )
                ):
                    _reject(code)
            endpoint_secret_file = record.get("endpoint_secret_file")
            if not isinstance(endpoint_secret_file, str) or len(endpoint_secret_file) > 1024:
                _reject("run_plan_endpoint_secret_file_invalid")
            endpoint_secret_path = PurePosixPath(endpoint_secret_file)
            path_pairs = set(zip(endpoint_secret_path.parts, endpoint_secret_path.parts[1:]))
            if (
                not endpoint_secret_path.is_absolute()
                or str(endpoint_secret_path) != endpoint_secret_file
                or len(endpoint_secret_path.parts) < 5
                or any(part in {"", ".", ".."} for part in endpoint_secret_path.parts)
                or any(character in endpoint_secret_file for character in "\n\r\t")
                or ("mycelium", "identities") not in path_pairs
            ):
                _reject("run_plan_endpoint_secret_file_invalid")
            configure = record.get("configure")
            if not isinstance(configure, Mapping):
                _reject("run_plan_configure_invalid")
            normalized_nodes.append(
                {
                    "node_id": node_id,
                    "socket_root": socket_root,
                    "sidecar_binary": sidecar_binary,
                    "endpoint_secret_file": endpoint_secret_file,
                    "configure": dict(configure),
                }
            )
        if actual_node_ids != sorted(node_ids):
            _reject("run_plan_nodes_invalid")
        request = plan.get("request")
        request_fields = {
            "request_id",
            "prompt_token_ids",
            "max_new_tokens",
            "expected_new_tokens",
            "qos_class",
            "admitted_at",
            "target_ttft_ms",
            "target_tpot_ms",
            "target_tokens_per_second",
            "sampling_seed",
            "generation_config_digest",
        }
        if not isinstance(request, Mapping) or set(request) != request_fields:
            _reject("run_plan_request_invalid")
        _segment(request.get("request_id"), "run_plan_request_invalid")
        decode_count = plan.get("decode_count")
        expected_token_ids = plan.get("expected_token_ids")
        if (
            not isinstance(decode_count, int)
            or isinstance(decode_count, bool)
            or not 1 <= decode_count <= 127
            or not isinstance(expected_token_ids, list)
            or len(expected_token_ids) != decode_count + 1
            or not all(
                isinstance(token_id, int)
                and not isinstance(token_id, bool)
                and token_id >= 0
                for token_id in expected_token_ids
            )
        ):
            _reject("run_plan_expected_output_invalid")
        transferred_paths = {
            record.get("path")
            for record in self._transfer_manifest.get("files", [])
            if isinstance(record, Mapping)
        }
        if "physical_inference_node.py" not in transferred_paths:
            _reject("run_plan_node_script_missing")
        return {
            "protocol": _RUN_PLAN_PROTOCOL,
            "run_id": run_id,
            "deployment_id": deployment_id,
            "entry_node_id": entry_node_id,
            "nodes": normalized_nodes,
            "request": dict(request),
            "decode_count": decode_count,
            "expected_token_ids": list(expected_token_ids),
        }

    def _hello_identity(
        self,
        response: Mapping[str, Any],
        *,
        peer: PeerIdentity,
        run_id: str,
        deployment_id: str,
    ) -> dict[str, Any]:
        if response.get("ok") is not True or not isinstance(response.get("result"), Mapping):
            _reject("node_hello_rejected")
        identity = dict(response["result"])
        expected_fields = {
            "protocol",
            "run_id",
            "deployment_id",
            "node_id",
            "host_id",
            "process_id",
            "endpoint_id",
            "peer_generation",
            "state",
            "route_ready",
        }
        if (
            set(identity) != expected_fields
            or identity.get("protocol") != _NODE_CONTROL_PROTOCOL
            or identity.get("run_id") != run_id
            or identity.get("deployment_id") != deployment_id
            or identity.get("node_id") != peer.node_id
            or identity.get("host_id") != peer.host_id
            or not isinstance(identity.get("process_id"), int)
            or isinstance(identity.get("process_id"), bool)
            or identity["process_id"] <= 0
            or identity.get("endpoint_id") is not None
            or identity.get("peer_generation") != 0
            or identity.get("state") != "NEW"
            or identity.get("route_ready") is not False
        ):
            _reject("node_hello_identity_invalid")
        return identity

    def _verified_observation(
        self,
        response: Mapping[str, Any],
        *,
        event: str,
        peer: PeerIdentity,
        process_id: int,
        run_id: str,
        deployment_id: str,
        endpoint_id: str,
        expected_verification_key: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if response.get("ok") is not True or not isinstance(response.get("result"), Mapping):
            _reject("node_command_rejected")
        signed = response["result"]
        if set(signed) != {"observation", "signature", "verification_key"}:
            _reject("node_observation_invalid")
        observation = signed.get("observation")
        signature = signed.get("signature")
        verification_key = signed.get("verification_key")
        observation_fields = {
            "protocol",
            "event",
            "monotonic_ns",
            "run_id",
            "deployment_id",
            "node_id",
            "host_id",
            "process_id",
            "endpoint_id",
            "peer_generation",
            "state",
            "route_ready",
            "details",
        }
        if (
            not isinstance(observation, Mapping)
            or set(observation) != observation_fields
            or observation.get("protocol") != _NODE_OBSERVATION_PROTOCOL
            or observation.get("event") != event
            or not isinstance(observation.get("monotonic_ns"), int)
            or isinstance(observation.get("monotonic_ns"), bool)
            or observation["monotonic_ns"] < 0
            or observation.get("run_id") != run_id
            or observation.get("deployment_id") != deployment_id
            or observation.get("node_id") != peer.node_id
            or observation.get("host_id") != peer.host_id
            or observation.get("process_id") != process_id
            or observation.get("endpoint_id") != endpoint_id
            or observation.get("route_ready") is not False
            or not isinstance(observation.get("peer_generation"), int)
            or isinstance(observation.get("peer_generation"), bool)
            or not isinstance(observation.get("state"), str)
            or not isinstance(observation.get("details"), Mapping)
            or not isinstance(signature, Mapping)
            or not isinstance(verification_key, Mapping)
            or (
                expected_verification_key is not None
                and dict(verification_key) != dict(expected_verification_key)
            )
            or signature.get("signer_endpoint_id") != endpoint_id
        ):
            _reject("node_observation_invalid")
        try:
            verifier = build_ed25519_verifier([verification_key])
            valid = verifier(canonical_json_bytes(dict(observation)), dict(signature))
        except (EvidenceSigningError, TypeError, ValueError) as exc:
            raise ControllerError("node_observation_signature_invalid") from exc
        if valid is not True:
            _reject("node_observation_signature_invalid")
        return dict(observation)

    def _run_physical(
        self,
        endpoints: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        plan = self._validate_run_plan()
        archive = build_transfer_archive(self.source_root, self._transfer_manifest)
        archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
        peers_by_node = {peer.node_id: peer for peer in self.peers}
        plans_by_node = {record["node_id"]: record for record in plan["nodes"]}
        sessions: dict[str, Any] = {}
        identities: dict[str, dict[str, Any]] = {}
        observations: dict[str, dict[str, Any]] = {
            node_id: {} for node_id in peers_by_node
        }
        endpoint_addresses: dict[str, dict[str, Any]] = {}
        verification_keys: dict[str, dict[str, Any]] = {}
        stopped: set[str] = set()
        primary_error: BaseException | None = None
        output_token_ids: list[int] | None = None
        try:
            for node_id in sorted(peers_by_node):
                peer = peers_by_node[node_id]
                node_plan = plans_by_node[node_id]
                node_script = f"{peer.staging_root}/physical_inference_node.py"
                remote_command = shlex.join(
                    (
                        "python3.14",
                        node_script,
                        "--run-id",
                        plan["run_id"],
                        "--deployment-id",
                        plan["deployment_id"],
                        "--node-id",
                        node_id,
                        "--artifact-root",
                        peer.staging_root,
                        "--socket-root",
                        node_plan["socket_root"],
                        "--sidecar-binary",
                        node_plan["sidecar_binary"],
                        "--endpoint-secret-file",
                        node_plan["endpoint_secret_file"],
                        "--command-timeout",
                        "30",
                    )
                )
                session = self._session_factory(
                    argv=(
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=15",
                        "--",
                        peer.ssh_target,
                        remote_command,
                    ),
                    node_id=node_id,
                    run_id=plan["run_id"],
                    deployment_id=plan["deployment_id"],
                    timeout_seconds=45.0,
                )
                sessions[node_id] = session
                hello = session.send(
                    command_id=f"{node_id}-hello-1",
                    command="hello",
                    payload={},
                )
                identities[node_id] = self._hello_identity(
                    hello,
                    peer=peer,
                    run_id=plan["run_id"],
                    deployment_id=plan["deployment_id"],
                )
            for node_id in sorted(sessions):
                peer = peers_by_node[node_id]
                configured = sessions[node_id].send(
                    command_id=f"{node_id}-configure-1",
                    command="configure",
                    payload=plans_by_node[node_id]["configure"],
                )
                endpoint_id = endpoints[node_id]["endpoint_id"]
                observation = self._verified_observation(
                    configured,
                    event="configured",
                    peer=peer,
                    process_id=identities[node_id]["process_id"],
                    run_id=plan["run_id"],
                    deployment_id=plan["deployment_id"],
                    endpoint_id=endpoint_id,
                )
                endpoint_addr = observation["details"].get("endpoint_addr")
                if (
                    not isinstance(endpoint_addr, Mapping)
                    or endpoint_addr.get("id") != endpoint_id
                ):
                    _reject("node_endpoint_address_invalid")
                endpoint_addresses[node_id] = dict(endpoint_addr)
                verification_keys[node_id] = dict(
                    configured["result"]["verification_key"]
                )
                observations[node_id]["configured"] = observation
            for node_id in sorted(sessions):
                peer = peers_by_node[node_id]
                remote_node_id = next(
                    candidate for candidate in sorted(sessions) if candidate != node_id
                )
                remote_identity = endpoints[remote_node_id]
                started = sessions[node_id].send(
                    command_id=f"{node_id}-start-1",
                    command="start",
                    payload={
                        "peer": {
                            "node_id": remote_node_id,
                            "endpoint_id": remote_identity["endpoint_id"],
                            "endpoint_addr": endpoint_addresses[remote_node_id],
                            "generation": remote_identity["membership_generation"],
                        }
                    },
                )
                observations[node_id]["started"] = self._verified_observation(
                    started,
                    event="started",
                    peer=peer,
                    process_id=identities[node_id]["process_id"],
                    run_id=plan["run_id"],
                    deployment_id=plan["deployment_id"],
                    endpoint_id=endpoints[node_id]["endpoint_id"],
                    expected_verification_key=verification_keys[node_id],
                )
            entry_node_id = plan["entry_node_id"]
            entry_peer = peers_by_node[entry_node_id]
            inference_started = sessions[entry_node_id].send(
                command_id=f"{entry_node_id}-infer-start-1",
                command="infer_start",
                payload={"request": plan["request"]},
            )
            observations[entry_node_id]["inference_started"] = self._verified_observation(
                inference_started,
                event="inference_started",
                peer=entry_peer,
                process_id=identities[entry_node_id]["process_id"],
                run_id=plan["run_id"],
                deployment_id=plan["deployment_id"],
                endpoint_id=endpoints[entry_node_id]["endpoint_id"],
                expected_verification_key=verification_keys[entry_node_id],
            )
            inference_decoded = sessions[entry_node_id].send(
                command_id=f"{entry_node_id}-infer-decode-1",
                command="infer_decode",
                payload={
                    "request_id": plan["request"]["request_id"],
                    "count": plan["decode_count"],
                },
            )
            decoded_observation = self._verified_observation(
                inference_decoded,
                event="inference_decoded",
                peer=entry_peer,
                process_id=identities[entry_node_id]["process_id"],
                run_id=plan["run_id"],
                deployment_id=plan["deployment_id"],
                endpoint_id=endpoints[entry_node_id]["endpoint_id"],
                expected_verification_key=verification_keys[entry_node_id],
            )
            observations[entry_node_id]["inference_decoded"] = decoded_observation
            output = decoded_observation["details"].get("output")
            if not isinstance(output, Mapping) or not isinstance(
                output.get("token_ids"), list
            ):
                _reject("node_inference_output_invalid")
            output_token_ids = list(output["token_ids"])
            if output_token_ids != plan["expected_token_ids"]:
                _reject("node_inference_token_mismatch")
            for node_id in sorted(sessions):
                peer = peers_by_node[node_id]
                snapshot = sessions[node_id].send(
                    command_id=f"{node_id}-snapshot-1",
                    command="snapshot",
                    payload={},
                )
                observations[node_id]["snapshot"] = self._verified_observation(
                    snapshot,
                    event="snapshot",
                    peer=peer,
                    process_id=identities[node_id]["process_id"],
                    run_id=plan["run_id"],
                    deployment_id=plan["deployment_id"],
                    endpoint_id=endpoints[node_id]["endpoint_id"],
                    expected_verification_key=verification_keys[node_id],
                )
            for node_id in sorted(sessions):
                stopped_response = sessions[node_id].send(
                    command_id=f"{node_id}-stop-1",
                    command="stop",
                    payload={},
                )
                if (
                    stopped_response.get("ok") is not True
                    or not isinstance(stopped_response.get("result"), Mapping)
                    or stopped_response["result"].get("state") != "STOPPED"
                ):
                    _reject("node_stop_invalid")
                final_observation = stopped_response["result"].get("final_observation")
                self._verified_observation(
                    {
                        "ok": True,
                        "result": final_observation,
                    },
                    event="stopping",
                    peer=peers_by_node[node_id],
                    process_id=identities[node_id]["process_id"],
                    run_id=plan["run_id"],
                    deployment_id=plan["deployment_id"],
                    endpoint_id=endpoints[node_id]["endpoint_id"],
                    expected_verification_key=verification_keys[node_id],
                )
                stopped.add(node_id)
        except BaseException as exc:
            primary_error = exc

        cleanup_failed = False
        for node_id, session in sessions.items():
            if node_id not in stopped:
                try:
                    session.send(
                        command_id=f"{node_id}-stop-cleanup",
                        command="stop",
                        payload={},
                    )
                except BaseException:
                    cleanup_failed = True
            try:
                session.close()
            except BaseException:
                cleanup_failed = True
        cleanup_actions: list[dict[str, Any]] = []
        for peer in self.peers:
            try:
                cleanup_actions.append(
                    self._cleanup_peer(peer, archive_digest=archive_digest)
                )
            except ControllerError:
                cleanup_failed = True
        if cleanup_failed:
            if primary_error is not None:
                raise ControllerError("physical_run_cleanup_failed") from primary_error
            _reject("physical_run_cleanup_failed")
        if primary_error is not None:
            raise primary_error
        assert output_token_ids is not None
        return {
            "protocol": _RESULT_PROTOCOL,
            "command": "run",
            "mode": self.mode,
            "peer_count": len(self.peers),
            "physical_execution": True,
            "route_ready": False,
            "release_ready": False,
            "token_parity": True,
            "output_token_ids": output_token_ids,
            "expected_token_ids": plan["expected_token_ids"],
            "identities": identities,
            "observations": observations,
            "cleanup": cleanup_actions,
            "claim_boundary": (
                "physical node sessions executed and exact token output matched; "
                "evidence is unsealed and no route or release readiness is claimed"
            ),
        }

    def _parse_stage_ack(
        self,
        capture: CommandCapture,
        *,
        peer: PeerIdentity,
        archive_digest: str,
        archive_size: int,
    ) -> dict[str, Any]:
        if capture.returncode != 0:
            _reject("remote_stage_failed")
        if capture.stderr or not capture.stdout or len(capture.stdout) > _MAX_DOCUMENT_BYTES:
            _reject("remote_stage_ack_invalid")
        try:
            ack = json.loads(
                capture.stdout.decode("utf-8"),
                object_pairs_hook=_reject_duplicates,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ControllerError) as exc:
            raise ControllerError("remote_stage_ack_invalid") from exc
        expected = {
            "protocol": _STAGE_ACK_PROTOCOL,
            "node_id": peer.node_id,
            "staging_root": peer.staging_root,
            "archive_digest": archive_digest,
            "archive_size_bytes": archive_size,
        }
        if ack != expected or capture.stdout != _canonical_bytes(expected):
            _reject("remote_stage_ack_mismatch")
        return expected

    def _cleanup_peer(
        self,
        peer: PeerIdentity,
        *,
        archive_digest: str,
    ) -> dict[str, Any]:
        remote_command = shlex.join(
            (
                "python3.14",
                "-c",
                _REMOTE_CLEANUP_SCRIPT,
                peer.staging_root,
                peer.node_id,
                archive_digest,
            )
        )
        capture = self._runner.run(
            (
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                "--",
                peer.ssh_target,
                remote_command,
            ),
            timeout_seconds=30.0,
        )
        if capture.returncode != 0 or capture.stderr or not capture.stdout:
            _reject("remote_cleanup_failed")
        try:
            ack = json.loads(
                capture.stdout.decode("utf-8"),
                object_pairs_hook=_reject_duplicates,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ControllerError) as exc:
            raise ControllerError("remote_cleanup_failed") from exc
        if (
            not isinstance(ack, dict)
            or set(ack) != {"protocol", "node_id", "staging_root", "removed"}
            or ack.get("protocol") != _CLEANUP_ACK_PROTOCOL
            or ack.get("node_id") != peer.node_id
            or ack.get("staging_root") != peer.staging_root
            or not isinstance(ack.get("removed"), bool)
            or capture.stdout != _canonical_bytes(ack)
        ):
            _reject("remote_cleanup_failed")
        return ack

    def _prepare_physical(
        self,
        transfers: tuple[dict[str, Any], ...],
        endpoints: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        del transfers
        archive = build_transfer_archive(self.source_root, self._transfer_manifest)
        archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
        actions: list[dict[str, Any]] = []
        attempted: list[PeerIdentity] = []
        try:
            for peer in self.peers:
                attempted.append(peer)
                remote_command = shlex.join(
                    (
                        "python3.14",
                        "-c",
                        _REMOTE_STAGE_SCRIPT,
                        peer.staging_root,
                        peer.node_id,
                        archive_digest,
                        str(len(archive)),
                    )
                )
                argv = (
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=15",
                    "--",
                    peer.ssh_target,
                    remote_command,
                )
                capture = self._runner.run(
                    argv,
                    timeout_seconds=120.0,
                    stdin_bytes=archive,
                )
                ack = self._parse_stage_ack(
                    capture,
                    peer=peer,
                    archive_digest=archive_digest,
                    archive_size=len(archive),
                )
                actions.append(
                    {
                        "node_id": peer.node_id,
                        "command": "prepare",
                        "status": "staged",
                        "archive_digest": archive_digest,
                        "archive_size_bytes": len(archive),
                        "staging_root": peer.staging_root,
                        "acknowledgement": ack,
                    }
                )
        except ControllerError as stage_error:
            cleanup_failed = False
            for peer in attempted:
                try:
                    self._cleanup_peer(peer, archive_digest=archive_digest)
                except ControllerError:
                    cleanup_failed = True
            if cleanup_failed:
                raise ControllerError("remote_cleanup_failed") from stage_error
            raise
        peers = [
            {
                "node_id": peer.node_id,
                "host_id": peer.host_id,
                "boot_id": peer.boot_id,
                "signed_endpoint": endpoints.get(peer.node_id),
            }
            for peer in self.peers
        ]
        return {
            "protocol": _RESULT_PROTOCOL,
            "command": "prepare",
            "mode": self.mode,
            "peer_count": len(self.peers),
            "peers": peers,
            "actions": actions,
            "route_ready": False,
            "release_ready": False,
            "physical_execution": True,
            "claim_boundary": (
                "verified archive staged on distinct physical peers; no node launch, "
                "inference route, qualification evidence, or readiness claim"
            ),
        }

    def execute(self, command: str) -> dict[str, Any]:
        if command not in COMMANDS:
            _reject("controller_command_invalid")
        transfers = self._validate_transfers()
        endpoints = self._validate_membership()
        if self.mode == "physical":
            self._validate_physical_distinctness()
            if command == "prepare":
                return self._prepare_physical(transfers, endpoints)
            if command == "run":
                return self._run_physical(endpoints)
            if command in {"recover", "seal"}:
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
    parser.add_argument("--run-plan")
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
            run_plan=(
                None
                if args.run_plan is None
                else _read_document(Path(args.run_plan))
            ),
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
