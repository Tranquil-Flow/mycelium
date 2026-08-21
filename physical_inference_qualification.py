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
from dataclasses import dataclass, field
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
from mycelium_qualification.evidence import (
    EvidenceValidationError,
    canonical_json_bytes,
)
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
_NODE_TRANSFERS_PROTOCOL = "mycelium.controller_node_transfer_manifests.v1"
_PREPOSITIONED_PROTOCOL = "mycelium.controller_prepositioned_artifacts.v1"
_PREPOSITIONED_MEMBER_PROTOCOL = (
    "mycelium.controller_prepositioned_member_artifacts.v1"
)
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
_MAX_TRANSFER_BYTES = 2 * 1024 * 1024 * 1024
_MAX_RUNNER_OUTPUT_BYTES = 1_048_576
_MAX_RUNNER_TIMEOUT_SECONDS = 6 * 60 * 60.0
NODE_COMMAND_TIMEOUT_SECONDS = 900.0
NODE_SESSION_TIMEOUT_SECONDS = 930.0
_MIN_STAGE_TIMEOUT_SECONDS = 120.0
_MAX_STAGE_TIMEOUT_SECONDS = _MAX_RUNNER_TIMEOUT_SECONDS
_STAGE_TIMEOUT_OVERHEAD_SECONDS = 60.0
_STAGE_MINIMUM_BYTES_PER_SECOND = 512 * 1024
_STAGE_ACK_PROTOCOL = "mycelium.controller_remote_stage_ack.v1"
_CLEANUP_ACK_PROTOCOL = "mycelium.controller_remote_cleanup_ack.v1"
_NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
_NODE_OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"
_RUN_PLAN_PROTOCOL = "mycelium.controller_run_plan.v1"
_REMOTE_STAGE_SCRIPT = r'''import hashlib,json,os,shutil,stat,sys,tarfile
from pathlib import Path,PurePosixPath
root=Path(sys.argv[1]);node_id=sys.argv[2];expected_digest=sys.argv[3];expected_size=int(sys.argv[4]);preposition_digest=sys.argv[5] if len(sys.argv)>5 else None;preposition_size=int(sys.argv[6]) if len(sys.argv)>6 else 0;created=False
try:
    if not root.is_absolute() or str(root)!=sys.argv[1] or len(root.parts)<4 or not any(part.startswith("mycelium") for part in root.parts):raise ValueError("root")
    current=Path(root.anchor)
    for part in root.parts[1:-1]:
        current=current/part
        if current.exists() and current.is_symlink():raise ValueError("symlink")
    if root.exists():
        metadata=root.lstat();marker_path=root/".mycelium-stage.json"
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid!=os.geteuid():raise ValueError("root")
        marker_metadata=marker_path.lstat()
        if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink!=1 or marker_metadata.st_uid!=os.geteuid() or marker_metadata.st_size>1024:raise ValueError("marker")
        marker=json.loads(marker_path.read_text(encoding="utf-8"));keys=set(marker)
        if keys not in ({"archive_digest","node_id"},{"archive_digest","node_id","preposition_digest"}) or marker["node_id"]!=node_id:raise ValueError("marker")
        if not isinstance(marker["archive_digest"],str) or len(marker["archive_digest"])!=71 or not marker["archive_digest"].startswith("sha256:"):raise ValueError("marker")
        if "preposition_digest" in marker and (not isinstance(marker["preposition_digest"],str) or len(marker["preposition_digest"])!=71 or not marker["preposition_digest"].startswith("sha256:")):raise ValueError("marker")
        shutil.rmtree(root)
    root.mkdir(parents=True,mode=0o700,exist_ok=False);created=True
    journal={"archive_digest":expected_digest,"node_id":node_id}
    if preposition_digest is not None:journal["preposition_digest"]=preposition_digest
    journal_path=root/".mycelium-stage-in-progress.json"
    with journal_path.open("x",encoding="utf-8") as output:
        output.write(json.dumps(journal,sort_keys=True,separators=(",",":"))+"\n");output.flush();os.fsync(output.fileno())
    journal_path.chmod(0o600)
    archive_path=root/".incoming.tar";digest=hashlib.sha256();received=0
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
    with os.fdopen(os.open(archive_path,flags,0o600),"wb") as incoming:
        while received<expected_size:
            content=sys.stdin.buffer.read(min(1_048_576,expected_size-received))
            if not content:raise ValueError("size")
            incoming.write(content);digest.update(content);received+=len(content)
    archive_path.chmod(0o600);actual="sha256:"+digest.hexdigest()
    if actual!=expected_digest:raise ValueError("digest")
    with tarfile.open(archive_path,mode="r:") as archive:
        members=archive.getmembers();names=[member.name for member in members]
        if not members or len(members)>256 or names!=sorted(names) or len(names)!=len(set(names)):raise ValueError("members")
        for member in members:
            relative=PurePosixPath(member.name)
            if not member.isfile() or relative.is_absolute() or str(relative)!=member.name or any(part in ("",".","..") for part in relative.parts):raise ValueError("member")
            source=archive.extractfile(member)
            if source is None:raise ValueError("content")
            destination=root.joinpath(*relative.parts);destination.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            remaining=member.size
            with destination.open("xb") as output:
                while remaining:
                    content=source.read(min(1_048_576,remaining))
                    if not content:raise ValueError("content")
                    output.write(content);remaining-=len(content)
                if source.read(1):raise ValueError("content")
            destination.chmod(0o600)
    archive_path.unlink()
    actual_preposition=None
    if preposition_digest is not None:
        encoded=sys.stdin.buffer.read(preposition_size)
        if len(encoded)!=preposition_size:raise ValueError("preposition_size")
        actual_preposition="sha256:"+hashlib.sha256(encoded).hexdigest()
        if actual_preposition!=preposition_digest:raise ValueError("preposition_digest")
        document=json.loads(encoded)
        if set(document)!={"files","protocol"} or document["protocol"]!="mycelium.controller_prepositioned_member_artifacts.v1" or not isinstance(document["files"],list):raise ValueError("preposition_document")
        destinations=[]
        for record in document["files"]:
            if not isinstance(record,dict) or set(record)!={"content_digest","destination_path","size_bytes","source_path"}:raise ValueError("preposition_record")
            destination_value=record["destination_path"];source_value=record["source_path"];size=record["size_bytes"];content_digest=record["content_digest"]
            relative=PurePosixPath(destination_value)
            source_path=Path(source_value)
            if relative.is_absolute() or str(relative)!=destination_value or any(part in ("",".","..") for part in relative.parts):raise ValueError("preposition_destination")
            if not source_path.is_absolute() or str(source_path)!=source_value or any(part in ("",".","..") for part in source_path.parts):raise ValueError("preposition_source")
            if not isinstance(size,int) or isinstance(size,bool) or size<0 or not isinstance(content_digest,str) or len(content_digest)!=71 or not content_digest.startswith("sha256:"):raise ValueError("preposition_binding")
            destinations.append(destination_value)
            current=Path(source_path.anchor)
            for part in source_path.parts[1:]:
                current=current/part
                metadata=current.lstat()
                if stat.S_ISLNK(metadata.st_mode):raise ValueError("preposition_symlink")
            flags=os.O_RDONLY
            if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
            fd=os.open(source_path,flags)
            destination=root.joinpath(*relative.parts)
            try:
                metadata=os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_size!=size or destination.exists():raise ValueError("preposition_source")
                destination.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                output_flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
                if hasattr(os,"O_NOFOLLOW"):output_flags|=os.O_NOFOLLOW
                output_fd=os.open(destination,output_flags,0o600)
                copied=0;content_hash=hashlib.sha256()
                try:
                    while copied<size:
                        content=os.read(fd,min(1_048_576,size-copied))
                        if not content:raise ValueError("preposition_size")
                        os.write(output_fd,content);content_hash.update(content);copied+=len(content)
                    if os.read(fd,1):raise ValueError("preposition_size")
                    os.fsync(output_fd)
                finally:os.close(output_fd)
                if "sha256:"+content_hash.hexdigest()!=content_digest:raise ValueError("preposition_content")
            finally:os.close(fd)
        if destinations!=sorted(destinations) or len(destinations)!=len(set(destinations)):raise ValueError("preposition_order")
    if sys.stdin.buffer.read(1):raise ValueError("size")
    marker={"archive_digest":actual,"node_id":node_id}
    if actual_preposition is not None:marker["preposition_digest"]=actual_preposition
    marker_path=root/".mycelium-stage.json"
    with marker_path.open("x",encoding="utf-8") as output:
        output.write(json.dumps(marker,sort_keys=True,separators=(",",":"))+"\n");output.flush();os.fsync(output.fileno())
    marker_path.chmod(0o600)
    journal_path.unlink()
    directory=os.open(root,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)
    ack={"archive_digest":actual,"archive_size_bytes":received,"node_id":node_id,"protocol":"mycelium.controller_remote_stage_ack.v1","staging_root":str(root)}
    if actual_preposition is not None:ack.update({"preposition_digest":actual_preposition,"preposition_size_bytes":preposition_size})
    sys.stdout.write(json.dumps(ack,sort_keys=True,separators=(",",":"))+"\n");sys.stdout.flush()
except BaseException:
    if created:shutil.rmtree(root,ignore_errors=True)
    sys.stderr.write("remote_stage_rejected\n");raise SystemExit(2)
'''
_REMOTE_CLEANUP_SCRIPT = r'''import json,shutil,stat,sys
from pathlib import Path
root=Path(sys.argv[1]);node_id=sys.argv[2];archive_digest=sys.argv[3];preposition_digest=None if len(sys.argv)<5 or sys.argv[4]=="-" else sys.argv[4]
try:
    if not root.is_absolute() or str(root)!=sys.argv[1] or len(root.parts)<4 or not any(part.startswith("mycelium") for part in root.parts):raise ValueError("root")
    removed=False
    if root.exists():
        metadata=root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):raise ValueError("root")
        marker_path=root/".mycelium-stage.json"
        try:marker_metadata=marker_path.lstat()
        except FileNotFoundError:
            marker_path=root/".mycelium-stage-in-progress.json";marker_metadata=marker_path.lstat()
        if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink!=1 or marker_metadata.st_size>1024:raise ValueError("marker")
        marker=json.loads(marker_path.read_text(encoding="utf-8"));expected={"archive_digest":archive_digest,"node_id":node_id}
        if preposition_digest is not None:expected["preposition_digest"]=preposition_digest
        if marker!=expected:raise ValueError("marker")
        shutil.rmtree(root);removed=True
    ack={"node_id":node_id,"protocol":"mycelium.controller_remote_cleanup_ack.v1","removed":removed,"staging_root":str(root)}
    sys.stdout.write(json.dumps(ack,sort_keys=True,separators=(",",":"))+"\n");sys.stdout.flush()
except BaseException:
    sys.stderr.write("remote_cleanup_rejected\n");raise SystemExit(2)
'''


class ControllerError(ValueError):
    """Stable fail-closed controller error."""

    def __init__(
        self,
        code: str,
        *,
        remote_code: str | None = None,
        diagnostic: str | None = None,
    ) -> None:
        self.code = code
        self.remote_code = remote_code
        # Operator-facing node evidence attached when a command was
        # rejected by a live node process: the node's own stderr tail.
        # Bounded at construction (see _session_stderr_tail); the public
        # reason code and str(exc) stay exactly the code, so nothing
        # downstream changes shape.
        self.diagnostic = diagnostic
        super().__init__(code)


_NODE_STDERR_TAIL_CHARS = 4_000
_NODE_DIAGNOSTIC_MAX_CHARS = 12_000


def _session_stderr_tail(sessions: Mapping[str, Any]) -> str | None:
    """Bounded stderr tails from live node sessions, for operator diagnosis.

    The rejection envelope only carries an error code; the node's own
    stderr carries the real reason (traceback, verification failure,
    sidecar error). Collect bounded tails so a rejection can be raised
    with its evidence without leaking unbounded process output.
    """
    parts: list[str] = []
    budget = _NODE_DIAGNOSTIC_MAX_CHARS
    for node_id in sorted(sessions):
        stderr = getattr(sessions[node_id], "stderr", None)
        if not isinstance(stderr, bytes) or not stderr:
            continue
        tail = (
            stderr[-_NODE_STDERR_TAIL_CHARS:]
            .decode("utf-8", errors="replace")
            .rstrip()
        )
        if not tail:
            continue
        entry = f"node {node_id} stderr (tail):\n{tail}"
        budget -= len(entry) + 2
        if budget <= 0 and parts:
            break
        parts.append(entry)
    if not parts:
        return None
    return "\n\n".join(parts)


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


_SshIdentityBinding = tuple[
    tuple[int, int, int, int, int, int, int],
    tuple[tuple[str, int, int, int, int], ...],
]


def _ssh_identity_file_binding(value: str) -> _SshIdentityBinding:
    path = Path(value)
    if (
        not path.is_absolute()
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
    ):
        _reject("peer_ssh_identity_file_invalid")
    current = Path(path.anchor)
    directory_binding: list[tuple[str, int, int, int, int]] = []
    for part in (path.anchor, *path.parts[1:-1]):
        if part != path.anchor:
            current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ControllerError("peer_ssh_identity_file_invalid") from exc
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or mode & 0o022
        ):
            _reject("peer_ssh_identity_file_invalid")
        directory_binding.append(
            (str(current), metadata.st_dev, metadata.st_ino, metadata.st_uid, mode)
        )
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControllerError("peer_ssh_identity_file_invalid") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077 != 0
    ):
        _reject("peer_ssh_identity_file_invalid")
    file_binding = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
    )
    return file_binding, tuple(directory_binding)


@dataclass(frozen=True)
class PeerIdentity:
    node_id: str
    ssh_target: str
    host_id: str
    boot_id: str
    staging_root: str
    process_transport: str
    ssh_identity_file: str | None = None
    _ssh_identity_binding: _SshIdentityBinding | None = field(
        default=None,
        init=False,
        repr=False,
    )

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
        if self.process_transport not in {"local", "ssh"}:
            _reject("peer_process_transport_invalid")
        if self.process_transport == "local":
            if self.ssh_identity_file is not None:
                _reject("peer_ssh_identity_file_invalid")
        elif (
            not isinstance(self.ssh_identity_file, str)
            or not self.ssh_identity_file
            or not PurePosixPath(self.ssh_identity_file).is_absolute()
            or any(character in self.ssh_identity_file for character in "\n\r\t")
        ):
            _reject("peer_ssh_identity_file_invalid")
        else:
            object.__setattr__(
                self,
                "_ssh_identity_binding",
                _ssh_identity_file_binding(self.ssh_identity_file),
            )


def _peer_process_argv(
    peer: PeerIdentity,
    command: tuple[str, ...],
) -> tuple[str, ...]:
    if not command or not all(isinstance(value, str) and value for value in command):
        _reject("peer_process_command_invalid")
    if peer.process_transport == "local":
        return command
    assert peer.ssh_identity_file is not None
    try:
        current_binding = _ssh_identity_file_binding(peer.ssh_identity_file)
    except ControllerError as exc:
        raise ControllerError("peer_ssh_identity_file_changed") from exc
    if current_binding != peer._ssh_identity_binding:
        _reject("peer_ssh_identity_file_changed")
    return (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-i",
        peer.ssh_identity_file,
        "--",
        peer.ssh_target,
        shlex.join(command),
    )


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
            or not 0.0 < float(timeout_seconds) <= _MAX_RUNNER_TIMEOUT_SECONDS
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
            or not 0.0 < float(timeout_seconds) <= _MAX_RUNNER_TIMEOUT_SECONDS
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


def _stage_timeout_seconds(archive_size_bytes: int) -> float:
    """Bound staging time while allowing large verified archives over slow links."""

    if (
        not isinstance(archive_size_bytes, int)
        or isinstance(archive_size_bytes, bool)
        or archive_size_bytes < 0
    ):
        _reject("transfer_size_invalid")
    estimated = (
        _STAGE_TIMEOUT_OVERHEAD_SECONDS
        + archive_size_bytes / _STAGE_MINIMUM_BYTES_PER_SECOND
    )
    return min(
        _MAX_STAGE_TIMEOUT_SECONDS,
        max(_MIN_STAGE_TIMEOUT_SECONDS, estimated),
    )


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
        node_transfer_manifests: Mapping[str, Any] | None = None,
        prepositioned_artifacts: Mapping[str, Any] | None = None,
        runner: CommandRunner | None = None,
        run_plan: Mapping[str, Any] | None = None,
        session_factory: Callable[..., Any] | None = None,
        seal_adapter: Callable[..., Mapping[str, Any]] | None = None,
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
        if seal_adapter is not None and not callable(seal_adapter):
            _reject("controller_seal_adapter_invalid")
        self.mode = mode
        self.peers = tuple(peers)
        self.source_root = resolved_root
        self._transfer_manifest = dict(transfer_manifest)
        self._node_transfer_manifests = (
            None
            if node_transfer_manifests is None
            else dict(node_transfer_manifests)
        )
        self._prepositioned_artifacts = (
            None
            if prepositioned_artifacts is None
            else dict(prepositioned_artifacts)
        )
        self._membership_snapshot = dict(membership_snapshot)
        self._run_plan = None if run_plan is None else dict(run_plan)
        self._now = float(now)
        self._runner = runner or SubprocessRunner()
        self._session_factory = session_factory or NodeProcessSession
        self._seal_adapter = seal_adapter

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
        verified = tuple(
            _verify_transfer_file(self.source_root, record) for record in records
        )
        node_manifests = self._node_transfer_manifests
        prepositioned = self._validate_prepositioned_artifacts(verified)
        if node_manifests is None:
            if any(prepositioned.values()):
                _reject("prepositioned_artifacts_require_node_manifests")
            return verified
        if (
            set(node_manifests) != {"protocol", "manifests"}
            or node_manifests.get("protocol") != _NODE_TRANSFERS_PROTOCOL
            or not isinstance(node_manifests.get("manifests"), Mapping)
        ):
            _reject("node_transfer_manifests_invalid")
        manifests = node_manifests["manifests"]
        expected_nodes = {peer.node_id for peer in self.peers}
        if set(manifests) != expected_nodes:
            _reject("node_transfer_manifests_invalid")
        base_records = {record["path"]: record for record in verified}
        covered_paths: set[str] = set()
        for node_id in sorted(expected_nodes):
            node_manifest = manifests[node_id]
            if (
                not isinstance(node_manifest, Mapping)
                or set(node_manifest) != {"protocol", "files"}
                or node_manifest.get("protocol") != _TRANSFER_PROTOCOL
                or not isinstance(node_manifest.get("files"), list)
                or not node_manifest["files"]
            ):
                _reject("node_transfer_manifest_invalid")
            node_paths = [record.get("path") for record in node_manifest["files"]]
            if node_paths != sorted(node_paths) or len(node_paths) != len(set(node_paths)):
                _reject("node_transfer_manifest_order_invalid")
            for record in node_manifest["files"]:
                if not isinstance(record, Mapping):
                    _reject("node_transfer_manifest_invalid")
                base = base_records.get(record.get("path"))
                if base is None or dict(record) != base:
                    _reject("node_transfer_manifest_not_base_subset")
            if "physical_inference_node.py" not in node_paths:
                _reject("node_transfer_manifest_node_script_missing")
            prepositioned_paths = {
                record["destination_path"] for record in prepositioned[node_id]
            }
            if set(node_paths) & prepositioned_paths:
                _reject("prepositioned_artifact_also_transferred")
            covered_paths.update(node_paths)
            covered_paths.update(prepositioned_paths)
        if covered_paths != set(base_records):
            _reject("node_transfer_manifests_incomplete")
        return verified

    def _validate_prepositioned_artifacts(
        self,
        verified: tuple[dict[str, Any], ...],
    ) -> dict[str, list[dict[str, Any]]]:
        expected_nodes = {peer.node_id for peer in self.peers}
        if self._prepositioned_artifacts is None:
            return {node_id: [] for node_id in expected_nodes}
        document = self._prepositioned_artifacts
        if (
            set(document) != {"protocol", "members"}
            or document.get("protocol") != _PREPOSITIONED_PROTOCOL
            or not isinstance(document.get("members"), Mapping)
            or set(document["members"]) != expected_nodes
        ):
            _reject("prepositioned_artifacts_invalid")
        base_records = {record["path"]: record for record in verified}
        result: dict[str, list[dict[str, Any]]] = {}
        for node_id in sorted(expected_nodes):
            records = document["members"][node_id]
            if (
                not isinstance(records, list)
                or len(records) > 256
                or not all(isinstance(record, Mapping) for record in records)
            ):
                _reject("prepositioned_artifacts_invalid")
            destinations = [record.get("destination_path") for record in records]
            if destinations != sorted(destinations) or len(destinations) != len(
                set(destinations)
            ):
                _reject("prepositioned_artifacts_order_invalid")
            normalized: list[dict[str, Any]] = []
            for record in records:
                if set(record) != {
                    "destination_path",
                    "source_path",
                    "size_bytes",
                    "content_digest",
                }:
                    _reject("prepositioned_artifact_invalid")
                destination = str(
                    _safe_transfer_path(record.get("destination_path"))
                )
                source_value = record.get("source_path")
                source_path = (
                    PurePosixPath(source_value)
                    if isinstance(source_value, str)
                    else None
                )
                if (
                    source_path is None
                    or not source_path.is_absolute()
                    or str(source_path) != source_value
                    or len(source_value) > 2048
                    or any(part in {"", ".", ".."} for part in source_path.parts)
                ):
                    _reject("prepositioned_artifact_source_invalid")
                size = record.get("size_bytes")
                digest = record.get("content_digest")
                base = base_records.get(destination)
                if (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or size > _MAX_TRANSFER_BYTES
                    or not isinstance(digest, str)
                    or _DIGEST_RE.fullmatch(digest) is None
                    or base is None
                    or size != base["size_bytes"]
                    or digest != base["content_digest"]
                ):
                    _reject("prepositioned_artifact_binding_invalid")
                normalized.append(
                    {
                        "destination_path": destination,
                        "source_path": source_value,
                        "size_bytes": size,
                        "content_digest": digest,
                    }
                )
            result[node_id] = normalized
        return result

    def _transfer_manifest_for_node(self, node_id: str) -> Mapping[str, Any]:
        if self._node_transfer_manifests is None:
            return self._transfer_manifest
        manifests = self._node_transfer_manifests.get("manifests")
        if not isinstance(manifests, Mapping):
            _reject("node_transfer_manifests_invalid")
        manifest = manifests.get(node_id)
        if not isinstance(manifest, Mapping):
            _reject("node_transfer_manifest_invalid")
        return manifest

    def _archive_identity_for_peer(self, peer: PeerIdentity) -> tuple[bytes, str]:
        archive = build_transfer_archive(
            self.source_root,
            self._transfer_manifest_for_node(peer.node_id),
        )
        return archive, "sha256:" + hashlib.sha256(archive).hexdigest()

    def _preposition_identity_for_peer(
        self,
        peer: PeerIdentity,
    ) -> tuple[bytes, str, int] | None:
        if self._prepositioned_artifacts is None:
            return None
        members = self._prepositioned_artifacts.get("members")
        if not isinstance(members, Mapping) or not isinstance(
            members.get(peer.node_id), list
        ):
            _reject("prepositioned_artifacts_invalid")
        document = {
            "protocol": _PREPOSITIONED_MEMBER_PROTOCOL,
            "files": members[peer.node_id],
        }
        encoded = _canonical_bytes(document)
        artifact_bytes = sum(
            int(record["size_bytes"]) for record in members[peer.node_id]
        )
        return (
            encoded,
            "sha256:" + hashlib.sha256(encoded).hexdigest(),
            artifact_bytes,
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
        optional_fields = {"qualification_operation", "recovery_fault", "decode_mode"}
        if plan is None or not expected_fields.issubset(plan) or set(plan) - expected_fields - optional_fields:
            _reject("controller_run_plan_invalid")
        if plan.get("protocol") != _RUN_PLAN_PROTOCOL:
            _reject("controller_run_plan_invalid")
        qualification_operation = plan.get("qualification_operation", "run")
        if qualification_operation not in {"run", "cancel"}:
            _reject("controller_run_plan_invalid")
        decode_mode = plan.get("decode_mode")
        if decode_mode not in {None, "complete_context_replay", "stage_local_kv"}:
            _reject("run_plan_decode_mode_invalid")
        run_id = _segment(plan.get("run_id"), "run_id_invalid")
        deployment_id = _segment(
            plan.get("deployment_id"), "deployment_id_invalid"
        )
        if deployment_id != self._membership_snapshot.get("deployment_id"):
            _reject("run_plan_deployment_mismatch")
        node_ids = [peer.node_id for peer in self.peers]
        if len(node_ids) < 2:
            _reject("physical_run_peer_count_invalid")
        entry_node_id = plan.get("entry_node_id")
        if entry_node_id not in node_ids:
            _reject("run_plan_entry_node_invalid")
        recovery_fault = None
        if "recovery_fault" in plan:
            fault = plan.get("recovery_fault")
            expected_fault_fields = {
                "kind",
                "node_id",
                "trigger",
                "mechanism",
                "claim_boundary",
            }
            if (
                not isinstance(fault, Mapping)
                or set(fault) != expected_fault_fields
                or fault.get("kind") != "physical_recovery_fault_interlock_v1"
                or fault.get("node_id") not in node_ids
                or fault.get("node_id") == entry_node_id
                or fault.get("trigger") != "before_snapshot"
                or fault.get("mechanism") != "controller_close_stdin_process_exit"
                or fault.get("claim_boundary")
                != "bounded controller interlock terminates a real node process before snapshot; transport success or latency is not synthesized"
            ):
                _reject("run_plan_recovery_fault_invalid")
            recovery_fault = dict(fault)
        records = plan.get("nodes")
        if not isinstance(records, list) or len(records) != len(node_ids):
            _reject("run_plan_nodes_invalid")
        expected_node_fields = {
            "node_id",
            "python_executable",
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
            python_executable = record.get("python_executable")
            socket_root = record.get("socket_root")
            sidecar_binary = record.get("sidecar_binary")
            for value, code, require_marker in (
                (python_executable, "run_plan_python_executable_invalid", False),
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
            if (
                not endpoint_secret_path.is_absolute()
                or str(endpoint_secret_path) != endpoint_secret_file
                or len(endpoint_secret_path.parts) < 5
                or any(part in {"", ".", ".."} for part in endpoint_secret_path.parts)
                or any(character in endpoint_secret_file for character in "\n\r\t")
                or endpoint_secret_path.parent.name not in {"identity", "identities"}
                or not any(
                    part.startswith("mycelium")
                    for part in endpoint_secret_path.parts[:-2]
                )
            ):
                _reject("run_plan_endpoint_secret_file_invalid")
            configure = record.get("configure")
            if not isinstance(configure, Mapping):
                _reject("run_plan_configure_invalid")
            normalized_nodes.append(
                {
                    "node_id": node_id,
                    "python_executable": python_executable,
                    "socket_root": socket_root,
                    "sidecar_binary": sidecar_binary,
                    "endpoint_secret_file": endpoint_secret_file,
                    "configure": dict(configure),
                }
            )
        if actual_node_ids != sorted(node_ids):
            graphs = [item["configure"].get("graph") for item in normalized_nodes]
            if (
                not graphs
                or not all(isinstance(graph, Mapping) for graph in graphs)
                or any(graph != graphs[0] for graph in graphs[1:])
            ):
                _reject("run_plan_nodes_invalid")
            stages = graphs[0].get("stages")
            if not isinstance(stages, list) or not stages:
                _reject("run_plan_nodes_invalid")
            graph_node_order: list[Any] = []
            for stage in stages:
                placements = (
                    stage.get("placements") if isinstance(stage, Mapping) else None
                )
                if not isinstance(placements, list) or len(placements) != 1:
                    _reject("run_plan_nodes_invalid")
                placement = placements[0]
                node_id = (
                    placement.get("node_id")
                    if isinstance(placement, Mapping)
                    else None
                )
                if node_id not in graph_node_order:
                    graph_node_order.append(node_id)
            if (
                graph_node_order != actual_node_ids
                or set(actual_node_ids) != set(node_ids)
                or entry_node_id != actual_node_ids[0]
            ):
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
            "qualification_operation": qualification_operation,
            "recovery_fault": recovery_fault,
            "decode_mode": decode_mode,
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
        signed_observation_sink: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if response.get("ok") is not True or not isinstance(response.get("result"), Mapping):
            remote_code: str | None = None
            remote_error = response.get("error")
            if isinstance(remote_error, Mapping):
                candidate = remote_error.get("code")
                if isinstance(candidate, str) and _SEGMENT_RE.fullmatch(candidate) is not None:
                    remote_code = candidate
            raise ControllerError("node_command_rejected", remote_code=remote_code)
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
        if signed_observation_sink is not None:
            signed_observation_sink.append(
                json.loads(
                    canonical_json_bytes(
                        {
                            "observation": dict(observation),
                            "signature": dict(signature),
                            "verification_key": dict(verification_key),
                        }
                    )
                )
            )
        return dict(observation)

    def _run_physical(
        self,
        endpoints: Mapping[str, Mapping[str, Any]],
        *,
        operation: str = "run",
    ) -> dict[str, Any]:
        if operation not in {"run", "cancel", "recover"}:
            _reject("physical_operation_invalid")
        plan = self._validate_run_plan()
        recovery_fault = plan["recovery_fault"] if operation == "recover" else None
        peers_by_node = {peer.node_id: peer for peer in self.peers}
        archive_digests = {
            node_id: self._archive_identity_for_peer(peer)[1]
            for node_id, peer in peers_by_node.items()
        }
        preposition_digests = {
            node_id: (
                None
                if (identity := self._preposition_identity_for_peer(peer)) is None
                else identity[1]
            )
            for node_id, peer in peers_by_node.items()
        }
        plans_by_node = {record["node_id"]: record for record in plan["nodes"]}
        sessions: dict[str, Any] = {}
        identities: dict[str, dict[str, Any]] = {}
        observations: dict[str, dict[str, Any]] = {
            node_id: {} for node_id in peers_by_node
        }
        signed_observations: list[dict[str, Any]] = []
        endpoint_addresses: dict[str, dict[str, Any]] = {}
        verification_keys: dict[str, dict[str, Any]] = {}
        created_sessions: list[tuple[str, Any]] = []
        stopped: set[int] = set()
        recovered_nodes: list[str] = []
        restart_attempts: dict[str, int] = {}
        recovery_fault_observed = False
        primary_error: BaseException | None = None
        output_token_ids: list[int] | None = None
        try:
            for node_id in sorted(peers_by_node):
                peer = peers_by_node[node_id]
                node_plan = plans_by_node[node_id]
                node_script = f"{peer.staging_root}/physical_inference_node.py"
                node_command = (
                    node_plan["python_executable"],
                    "-B",
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
                    str(int(NODE_COMMAND_TIMEOUT_SECONDS)),
                )
                if plan["decode_mode"] is not None:
                    node_command += ("--decode-mode", plan["decode_mode"])
                session = self._session_factory(
                    argv=_peer_process_argv(peer, node_command),
                    node_id=node_id,
                    run_id=plan["run_id"],
                    deployment_id=plan["deployment_id"],
                    timeout_seconds=NODE_SESSION_TIMEOUT_SECONDS,
                )
                sessions[node_id] = session
                created_sessions.append((node_id, session))
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
                    signed_observation_sink=signed_observations,
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
            ordered_node_ids = sorted(sessions)
            successors = {
                node_id: ordered_node_ids[(index + 1) % len(ordered_node_ids)]
                for index, node_id in enumerate(ordered_node_ids)
            }
            predecessors = {
                successor_id: node_id
                for node_id, successor_id in successors.items()
            }
            for node_id in ordered_node_ids:
                peer = peers_by_node[node_id]
                remote_node_id = successors[node_id]
                additional_node_ids = [
                    candidate
                    for candidate in ordered_node_ids
                    if candidate not in {node_id, remote_node_id}
                ]

                def peer_document(candidate: str) -> dict[str, Any]:
                    identity = endpoints[candidate]
                    return {
                        "node_id": candidate,
                        "endpoint_id": identity["endpoint_id"],
                        "endpoint_addr": endpoint_addresses[candidate],
                        "generation": identity["membership_generation"],
                    }

                started = sessions[node_id].send(
                    command_id=f"{node_id}-start-1",
                    command="start",
                    payload={
                        "peer": peer_document(remote_node_id),
                        "peers": [
                            peer_document(candidate)
                            for candidate in additional_node_ids
                        ],
                        "local_generation": endpoints[node_id]["membership_generation"],
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
                    signed_observation_sink=signed_observations,
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
                signed_observation_sink=signed_observations,
            )
            if operation == "cancel":
                cancelled = sessions[entry_node_id].send(
                    command_id=f"{entry_node_id}-cancel-1",
                    command="cancel",
                    payload={"request_id": plan["request"]["request_id"]},
                )
                observations[entry_node_id]["cancelled"] = (
                    self._verified_observation(
                        cancelled,
                        event="cancelled",
                        peer=entry_peer,
                        process_id=identities[entry_node_id]["process_id"],
                        run_id=plan["run_id"],
                        deployment_id=plan["deployment_id"],
                        endpoint_id=endpoints[entry_node_id]["endpoint_id"],
                        expected_verification_key=verification_keys[entry_node_id],
                        signed_observation_sink=signed_observations,
                    )
                )
                output_token_ids = []
            else:
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
                    signed_observation_sink=signed_observations,
                )
                observations[entry_node_id]["inference_decoded"] = (
                    decoded_observation
                )
                output = decoded_observation["details"].get("output")
                if not isinstance(output, Mapping) or not isinstance(
                    output.get("token_ids"), list
                ):
                    _reject("node_inference_output_invalid")
                output_token_ids = list(output["token_ids"])
                if output_token_ids != plan["expected_token_ids"]:
                    _reject("node_inference_token_mismatch")
            for node_id in ordered_node_ids:
                peer = peers_by_node[node_id]
                try:
                    snapshot_attempt = 1
                    cleanup_deadline = time.monotonic() + 5.0
                    while True:
                        if (
                            recovery_fault is not None
                            and not recovery_fault_observed
                            and node_id == recovery_fault["node_id"]
                        ):
                            sessions[node_id].close()
                            recovery_fault_observed = True
                        snapshot = sessions[node_id].send(
                            command_id=f"{node_id}-snapshot-{snapshot_attempt}",
                            command="snapshot",
                            payload={},
                        )
                        snapshot_observation = self._verified_observation(
                            snapshot,
                            event="snapshot",
                            peer=peer,
                            process_id=identities[node_id]["process_id"],
                            run_id=plan["run_id"],
                            deployment_id=plan["deployment_id"],
                            endpoint_id=endpoints[node_id]["endpoint_id"],
                            expected_verification_key=verification_keys[node_id],
                        )
                        snapshot_details = snapshot_observation.get("details")
                        cancellation_cleanup_complete = (
                            isinstance(snapshot_details, Mapping)
                            and snapshot_details.get("runtime", {}).get("active_state_count") == 0
                            and snapshot_details.get("transport_pending_delivery_count") == 0
                            and snapshot_details.get("transport_cancellation_cleanup_complete") is True
                        )
                        if operation != "cancel" or cancellation_cleanup_complete:
                            observations[node_id]["snapshot"] = self._verified_observation(
                                snapshot,
                                event="snapshot",
                                peer=peer,
                                process_id=identities[node_id]["process_id"],
                                run_id=plan["run_id"],
                                deployment_id=plan["deployment_id"],
                                endpoint_id=endpoints[node_id]["endpoint_id"],
                                expected_verification_key=verification_keys[node_id],
                                signed_observation_sink=signed_observations,
                            )
                            break
                        if time.monotonic() >= cleanup_deadline:
                            _reject("physical_cancellation_cleanup_incomplete")
                        snapshot_attempt += 1
                        time.sleep(0.05)
                except ControllerError as exc:
                    if operation != "recover" or exc.code != "node_process_exited":
                        raise
                    failed_session = sessions[node_id]
                    stopped.add(id(failed_session))
                    failed_session.close()
                    restart_attempts[node_id] = 1
                    node_plan = plans_by_node[node_id]
                    node_script = f"{peer.staging_root}/physical_inference_node.py"
                    node_command = (
                        node_plan["python_executable"],
                        "-B",
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
                        str(int(NODE_COMMAND_TIMEOUT_SECONDS)),
                    )
                    if plan["decode_mode"] is not None:
                        node_command += ("--decode-mode", plan["decode_mode"])
                    try:
                        replacement = self._session_factory(
                            argv=_peer_process_argv(peer, node_command),
                            node_id=node_id,
                            run_id=plan["run_id"],
                            deployment_id=plan["deployment_id"],
                            timeout_seconds=NODE_SESSION_TIMEOUT_SECONDS,
                        )
                        created_sessions.append((node_id, replacement))
                        sessions[node_id] = replacement
                        recovered_hello = replacement.send(
                            command_id=f"{node_id}-hello-recover-1",
                            command="hello",
                            payload={},
                        )
                        recovered_identity = self._hello_identity(
                            recovered_hello,
                            peer=peer,
                            run_id=plan["run_id"],
                            deployment_id=plan["deployment_id"],
                        )
                        configured = replacement.send(
                            command_id=f"{node_id}-configure-recover-1",
                            command="configure",
                            payload=node_plan["configure"],
                        )
                        endpoint_id = endpoints[node_id]["endpoint_id"]
                        configured_observation = self._verified_observation(
                            configured,
                            event="configured",
                            peer=peer,
                            process_id=recovered_identity["process_id"],
                            run_id=plan["run_id"],
                            deployment_id=plan["deployment_id"],
                            endpoint_id=endpoint_id,
                            signed_observation_sink=signed_observations,
                        )
                        endpoint_addr = configured_observation["details"].get(
                            "endpoint_addr"
                        )
                        if (
                            not isinstance(endpoint_addr, Mapping)
                            or endpoint_addr.get("id") != endpoint_id
                        ):
                            _reject("node_endpoint_address_invalid")
                        endpoint_addresses[node_id] = dict(endpoint_addr)
                        identities[node_id] = recovered_identity
                        verification_keys[node_id] = dict(
                            configured["result"]["verification_key"]
                        )
                        observations[node_id]["recovered_configured"] = (
                            configured_observation
                        )
                        successor_id = successors[node_id]
                        additional_node_ids = [
                            candidate
                            for candidate in ordered_node_ids
                            if candidate not in {node_id, successor_id}
                        ]

                        def recovery_peer_document(candidate: str) -> dict[str, Any]:
                            identity = endpoints[candidate]
                            return {
                                "node_id": candidate,
                                "endpoint_id": identity["endpoint_id"],
                                "endpoint_addr": endpoint_addresses[candidate],
                                "generation": identity["membership_generation"],
                            }

                        started = replacement.send(
                            command_id=f"{node_id}-start-recover-1",
                            command="start",
                            payload={
                                "peer": recovery_peer_document(successor_id),
                                "peers": [
                                    recovery_peer_document(candidate)
                                    for candidate in additional_node_ids
                                ],
                                "local_generation": endpoints[node_id][
                                    "membership_generation"
                                ],
                            },
                        )
                        observations[node_id]["recovered_started"] = (
                            self._verified_observation(
                                started,
                                event="started",
                                peer=peer,
                                process_id=recovered_identity["process_id"],
                                run_id=plan["run_id"],
                                deployment_id=plan["deployment_id"],
                                endpoint_id=endpoint_id,
                                expected_verification_key=verification_keys[node_id],
                                signed_observation_sink=signed_observations,
                            )
                        )
                        predecessor_id = predecessors[node_id]
                        recovery_generation = (
                            endpoints[node_id]["membership_generation"] + 1
                        )
                        rotated = sessions[predecessor_id].send(
                            command_id=f"{predecessor_id}-rotate-{node_id}-1",
                            command="rotate",
                            payload={
                                "peer": {
                                    "node_id": node_id,
                                    "endpoint_id": endpoint_id,
                                    "endpoint_addr": endpoint_addresses[node_id],
                                    "generation": recovery_generation,
                                }
                            },
                        )
                        rotated_observation = self._verified_observation(
                            rotated,
                            event="peer_rotated",
                            peer=peers_by_node[predecessor_id],
                            process_id=identities[predecessor_id]["process_id"],
                            run_id=plan["run_id"],
                            deployment_id=plan["deployment_id"],
                            endpoint_id=endpoints[predecessor_id]["endpoint_id"],
                            expected_verification_key=verification_keys[predecessor_id],
                            signed_observation_sink=signed_observations,
                        )
                        if (
                            rotated_observation["peer_generation"]
                            != recovery_generation
                        ):
                            _reject("recovery_generation_stale")
                        observations[predecessor_id][
                            f"peer_rotated_{node_id}"
                        ] = rotated_observation
                        recovered_snapshot = replacement.send(
                            command_id=f"{node_id}-snapshot-recover-1",
                            command="snapshot",
                            payload={},
                        )
                        observations[node_id]["snapshot"] = (
                            self._verified_observation(
                                recovered_snapshot,
                                event="snapshot",
                                peer=peer,
                                process_id=recovered_identity["process_id"],
                                run_id=plan["run_id"],
                                deployment_id=plan["deployment_id"],
                                endpoint_id=endpoint_id,
                                expected_verification_key=verification_keys[node_id],
                                signed_observation_sink=signed_observations,
                            )
                        )
                        recovered_nodes.append(node_id)
                    except ControllerError as recovery_exc:
                        if recovery_exc.code == "node_process_exited":
                            raise ControllerError(
                                "physical_recovery_exhausted"
                            ) from recovery_exc
                        raise
                    except BaseException as recovery_exc:
                        raise ControllerError(
                            "physical_recovery_exhausted"
                        ) from recovery_exc
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
                    signed_observation_sink=signed_observations,
                )
                stopped.add(id(sessions[node_id]))
        except BaseException as exc:
            primary_error = exc
            if isinstance(exc, ControllerError) and exc.diagnostic is None:
                # Attach the rejecting node's own stderr while every
                # session is still alive, so the reason for the
                # rejection survives the cleanup path that follows.
                exc.diagnostic = _session_stderr_tail(sessions)

        cleanup_failed = False
        for node_id, session in created_sessions:
            if id(session) not in stopped:
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
                    self._cleanup_peer(
                        peer,
                        archive_digest=archive_digests[peer.node_id],
                        preposition_digest=preposition_digests[peer.node_id],
                    )
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
            "command": operation,
            "mode": self.mode,
            "run_id": plan["run_id"],
            "peer_count": len(self.peers),
            "physical_execution": True,
            "route_ready": False,
            "release_ready": False,
            "cancelled": operation == "cancel",
            "token_parity": operation != "cancel",
            "output_token_ids": output_token_ids,
            "expected_token_ids": plan["expected_token_ids"],
            "identities": identities,
            "observations": observations,
            "signed_observations": signed_observations,
            "recovered_nodes": recovered_nodes,
            "restart_attempts": restart_attempts,
            "recovery_fault": (
                None
                if recovery_fault is None
                else {**recovery_fault, "observed": recovery_fault_observed}
            ),
            "cleanup": cleanup_actions,
            "claim_boundary": (
                "physical node sessions executed under bounded control; cancellation "
                "or recovery may have run, evidence is unsealed, and no route or "
                "release readiness is claimed"
            ),
        }

    def _seal_physical(
        self,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if self._seal_adapter is None:
            _reject("controller_evidence_adapter_missing")
        run_id = evidence.get("run_id")
        if not isinstance(run_id, str):
            _reject("sealed_run_invalid")
        sealed_value = self._seal_adapter(run_id=run_id, evidence=dict(evidence))
        if not isinstance(sealed_value, Mapping):
            _reject("sealed_descriptor_invalid")
        sealed = dict(sealed_value)
        if set(sealed) != {"run_id", "manifest_path", "manifest_digest"}:
            _reject("sealed_descriptor_invalid")
        manifest_path_value = sealed.get("manifest_path")
        manifest_digest = sealed.get("manifest_digest")
        if (
            sealed.get("run_id") != run_id
            or not isinstance(manifest_path_value, str)
            or not isinstance(manifest_digest, str)
            or _DIGEST_RE.fullmatch(manifest_digest) is None
        ):
            _reject("sealed_descriptor_invalid")
        manifest_path = Path(manifest_path_value)
        if (
            not manifest_path.is_absolute()
            or manifest_path.name != "evidence-manifest.json"
            or any(part in {"", ".", ".."} for part in manifest_path.parts)
        ):
            _reject("sealed_manifest_invalid")
        try:
            metadata = manifest_path.lstat()
            parent_metadata = manifest_path.parent.lstat()
            raw_manifest = manifest_path.read_bytes()
        except OSError as exc:
            raise ControllerError("sealed_manifest_invalid") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o222
            or stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_mode & 0o222
            or not raw_manifest
            or len(raw_manifest) > _MAX_DOCUMENT_BYTES
        ):
            _reject("sealed_manifest_mutable")
        try:
            manifest = json.loads(
                raw_manifest.decode("utf-8"),
                object_pairs_hook=_reject_duplicates,
            )
            canonical_manifest = canonical_json_bytes(manifest)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ControllerError,
            EvidenceValidationError,
        ) as exc:
            raise ControllerError("sealed_manifest_invalid") from exc
        actual_digest = "sha256:" + hashlib.sha256(canonical_manifest).hexdigest()
        if (
            not isinstance(manifest, dict)
            or raw_manifest != canonical_manifest
            or manifest.get("run_id") != run_id
            or manifest.get("evidence_class") != "physical_qualification"
            or actual_digest != manifest_digest
        ):
            _reject("sealed_manifest_invalid")
        return {
            **evidence,
            "command": "seal",
            "route_ready": False,
            "release_ready": False,
            "sealed_manifest": sealed,
            "qualifier_invocations": 0,
            "claim_boundary": (
                "writers stopped and immutable same-run evidence sealed once; "
                "qualification has not run and route and release readiness remain false"
            ),
        }

    def _seal_operation(self) -> str:
        plan = self._validate_run_plan()
        operation = plan["qualification_operation"]
        assert operation in {"run", "cancel"}
        return operation

    def seal_evidence(self) -> dict[str, Any]:
        """Run and seal physical evidence without invoking qualification authority."""

        self._validate_transfers()
        endpoints = self._validate_membership()
        if self.mode != "physical":
            _reject("physical_mode_required")
        self._validate_physical_distinctness()
        return self._seal_physical(
            self._run_physical(endpoints, operation=self._seal_operation())
        )

    def _parse_stage_ack(
        self,
        capture: CommandCapture,
        *,
        peer: PeerIdentity,
        archive_digest: str,
        archive_size: int,
        preposition_digest: str | None = None,
        preposition_size: int = 0,
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
        if preposition_digest is not None:
            expected.update(
                {
                    "preposition_digest": preposition_digest,
                    "preposition_size_bytes": preposition_size,
                }
            )
        if ack != expected or capture.stdout != _canonical_bytes(expected):
            _reject("remote_stage_ack_mismatch")
        return expected

    def _cleanup_peer(
        self,
        peer: PeerIdentity,
        *,
        archive_digest: str,
        preposition_digest: str | None = None,
    ) -> dict[str, Any]:
        argv = _peer_process_argv(
            peer,
            (
                "python3",
                "-c",
                _REMOTE_CLEANUP_SCRIPT,
                peer.staging_root,
                peer.node_id,
                archive_digest,
                preposition_digest or "-",
            ),
        )
        capture = self._runner.run(
            argv,
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

    def _cleanup_physical(self) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        failed = False
        for peer in self.peers:
            try:
                _archive, archive_digest = self._archive_identity_for_peer(peer)
                preposition = self._preposition_identity_for_peer(peer)
                actions.append(
                    self._cleanup_peer(
                        peer,
                        archive_digest=archive_digest,
                        preposition_digest=(
                            None if preposition is None else preposition[1]
                        ),
                    )
                )
            except ControllerError:
                failed = True
        if failed:
            _reject("physical_cleanup_failed")
        return {
            "protocol": _RESULT_PROTOCOL,
            "command": "cleanup",
            "mode": self.mode,
            "peer_count": len(self.peers),
            "physical_execution": True,
            "route_ready": False,
            "release_ready": False,
            "actions": actions,
            "claim_boundary": (
                "all declared staging roots received digest-bound cleanup; no route "
                "or release readiness is claimed"
            ),
        }

    def _prepare_physical(
        self,
        transfers: tuple[dict[str, Any], ...],
        endpoints: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        del transfers
        actions: list[dict[str, Any]] = []
        attempted: list[tuple[PeerIdentity, str, str | None]] = []
        try:
            for peer in self.peers:
                archive, archive_digest = self._archive_identity_for_peer(peer)
                preposition = self._preposition_identity_for_peer(peer)
                preposition_bytes = b"" if preposition is None else preposition[0]
                preposition_digest = None if preposition is None else preposition[1]
                preposition_artifact_bytes = (
                    0 if preposition is None else preposition[2]
                )
                attempted.append((peer, archive_digest, preposition_digest))
                stage_arguments = [
                    "python3",
                    "-c",
                    _REMOTE_STAGE_SCRIPT,
                    peer.staging_root,
                    peer.node_id,
                    archive_digest,
                    str(len(archive)),
                ]
                if preposition_digest is not None:
                    stage_arguments.extend(
                        [preposition_digest, str(len(preposition_bytes))]
                    )
                argv = _peer_process_argv(
                    peer,
                    tuple(stage_arguments),
                )
                capture = self._runner.run(
                    argv,
                    timeout_seconds=_stage_timeout_seconds(
                        len(archive)
                        + len(preposition_bytes)
                        + preposition_artifact_bytes
                    ),
                    stdin_bytes=archive + preposition_bytes,
                )
                ack = self._parse_stage_ack(
                    capture,
                    peer=peer,
                    archive_digest=archive_digest,
                    archive_size=len(archive),
                    preposition_digest=preposition_digest,
                    preposition_size=len(preposition_bytes),
                )
                actions.append(
                    {
                        "node_id": peer.node_id,
                        "command": "prepare",
                        "status": "staged",
                        "archive_digest": archive_digest,
                        "archive_size_bytes": len(archive),
                        "preposition_digest": preposition_digest,
                        "preposition_size_bytes": len(preposition_bytes),
                        "staging_root": peer.staging_root,
                        "acknowledgement": ack,
                    }
                )
        except ControllerError as stage_error:
            cleanup_failed = False
            for peer, archive_digest, preposition_digest in attempted:
                try:
                    self._cleanup_peer(
                        peer,
                        archive_digest=archive_digest,
                        preposition_digest=preposition_digest,
                    )
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
            if command == "cancel":
                return self._run_physical(endpoints, operation="cancel")
            if command == "recover":
                return self._run_physical(endpoints, operation="recover")
            if command == "seal":
                if self._seal_adapter is None:
                    _reject("controller_evidence_adapter_missing")
                return self._seal_physical(
                    self._run_physical(endpoints, operation=self._seal_operation())
                )
            if command == "cleanup":
                return self._cleanup_physical()
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
                "transfers": [
                    dict(record)
                    for record in self._transfer_manifest_for_node(peer.node_id).get(
                        "files", []
                    )
                ],
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
    if len(parts) != 7:
        _reject("invalid_arguments")
    process_transport = parts[5]
    identity_file = None if parts[6] == "-" else parts[6]
    if (process_transport == "local") != (identity_file is None):
        _reject("invalid_arguments")
    return PeerIdentity(
        node_id=parts[0],
        ssh_target=parts[1],
        host_id=parts[2],
        boot_id=parts[3],
        staging_root=parts[4],
        process_transport=process_transport,
        ssh_identity_file=identity_file,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(add_help=True, exit_on_error=False)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--mode", choices=sorted(MODES), default="physical")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--peers", nargs="+")
    parser.add_argument("--source-root")
    parser.add_argument("--transfer-manifest")
    parser.add_argument("--node-transfer-manifests")
    parser.add_argument("--prepositioned-artifacts")
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
            node_transfer_manifests=(
                None
                if args.node_transfer_manifests is None
                else _read_document(Path(args.node_transfer_manifests))
            ),
            prepositioned_artifacts=(
                None
                if args.prepositioned_artifacts is None
                else _read_document(Path(args.prepositioned_artifacts))
            ),
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
