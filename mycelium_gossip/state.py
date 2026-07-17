from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .schema import RecordEnvelope, RecordKind, build_record


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class NodeStateError(RuntimeError):
    pass


class NodeStateInUse(NodeStateError):
    pass


def _validate_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not _SEGMENT_RE.fullmatch(node_id):
        raise NodeStateError("node_id must be a safe non-empty key segment")


def _channel_key(kind: RecordKind, payload: Mapping[str, Any]) -> Tuple[str, ...]:
    if kind in {RecordKind.PROFILE, RecordKind.STATUS}:
        return (kind.value,)
    if kind is RecordKind.LINK:
        required = ("src_endpoint_id", "dst_node_id", "dst_endpoint_id")
        try:
            return (kind.value,) + tuple(str(payload[name]) for name in required)
        except KeyError as exc:
            raise NodeStateError(f"link sequence identity missing {exc.args[0]}") from exc
    if kind is RecordKind.OFFERING:
        required = ("deployment_id", "assignment_id", "inference_endpoint_id")
        try:
            return (kind.value,) + tuple(str(payload[name]) for name in required)
        except KeyError as exc:
            raise NodeStateError(f"offering sequence identity missing {exc.args[0]}") from exc
    if kind is RecordKind.MEMBERSHIP:
        try:
            return (kind.value, str(payload["subject_node_id"]))
        except KeyError as exc:
            raise NodeStateError("membership sequence identity missing subject_node_id") from exc
    raise NodeStateError(f"unsupported record kind: {kind!r}")


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(str(temporary), str(path))
        os.chmod(str(path), 0o600)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class NodeStateSession:
    """Exclusive process session for stable identity and per-record sequences."""

    def __init__(
        self,
        *,
        path: Path,
        lock_descriptor: int,
        node_id: str,
        incarnation: int,
        boot_id: str,
    ) -> None:
        self.path = path
        self.node_id = node_id
        self.incarnation = incarnation
        self.boot_id = boot_id
        self._lock_descriptor: Optional[int] = lock_descriptor
        self._sequences: Dict[Tuple[str, ...], int] = {}
        self._mutex = threading.RLock()

    @property
    def closed(self) -> bool:
        with self._mutex:
            return self._lock_descriptor is None

    def _require_open(self) -> None:
        if self._lock_descriptor is None:
            raise NodeStateError("node state session is closed")

    def next_sequence(self, kind: RecordKind, payload: Mapping[str, Any]) -> int:
        channel = _channel_key(kind, payload)
        with self._mutex:
            self._require_open()
            sequence = self._sequences.get(channel, 0)
            self._sequences[channel] = sequence + 1
            return sequence

    def build_record(
        self,
        *,
        swarm_id: str,
        kind: RecordKind,
        payload: Mapping[str, Any],
        ttl_ms: int,
        generated_at_unix_ms: int,
    ) -> RecordEnvelope:
        channel = _channel_key(kind, payload)
        with self._mutex:
            self._require_open()
            sequence = self._sequences.get(channel, 0)
            record = build_record(
                swarm_id=swarm_id,
                kind=kind,
                origin_node_id=self.node_id,
                incarnation=self.incarnation,
                sequence=sequence,
                boot_id=self.boot_id,
                generated_at_unix_ms=generated_at_unix_ms,
                ttl_ms=ttl_ms,
                payload=payload,
            )
            self._sequences[channel] = sequence + 1
            return record

    def close(self) -> None:
        with self._mutex:
            descriptor = self._lock_descriptor
            self._lock_descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> "NodeStateSession":
        self._require_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def open_node_state(
    path: Union[str, os.PathLike[str]], *, node_id: Optional[str] = None
) -> NodeStateSession:
    state_path = Path(path).expanduser()
    if node_id is not None:
        _validate_node_id(node_id)
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_path.parent.exists():
        # Tighten only the dedicated state directory; callers should not pass a shared directory.
        os.chmod(str(state_path.parent), 0o700)
    try:
        if state_path.is_symlink():
            raise NodeStateError("node state path must not be a symlink")
    except OSError as exc:
        raise NodeStateError(f"cannot inspect node state path: {exc}") from exc

    lock_path = state_path.with_name(state_path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(str(lock_path), flags, 0o600)
        os.chmod(str(lock_path), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise NodeStateInUse("node state is already active in another process") from exc
            raise

        if state_path.exists():
            read_flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                read_flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                read_flags |= os.O_NOFOLLOW
            read_descriptor = os.open(str(state_path), read_flags)
            try:
                chunks = []
                while True:
                    chunk = os.read(read_descriptor, 4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if sum(len(item) for item in chunks) > 65_536:
                        raise NodeStateError("corrupt node state: file is oversized")
            finally:
                os.close(read_descriptor)
            try:
                document = json.loads(b"".join(chunks).decode("utf-8"))
                if set(document) != {"version", "node_id", "incarnation"}:
                    raise ValueError("unexpected fields")
                if document["version"] != 1:
                    raise ValueError("unsupported version")
                stored_node_id = document["node_id"]
                stored_incarnation = document["incarnation"]
                _validate_node_id(stored_node_id)
                if (
                    isinstance(stored_incarnation, bool)
                    or not isinstance(stored_incarnation, int)
                    or stored_incarnation < 1
                    or stored_incarnation >= 2**63 - 1
                ):
                    raise ValueError("invalid incarnation")
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, NodeStateError) as exc:
                raise NodeStateError(f"corrupt node state: {exc}") from exc
            if node_id is not None and node_id != stored_node_id:
                raise NodeStateError("requested node_id does not match persisted identity")
            selected_node_id = stored_node_id
            incarnation = stored_incarnation + 1
        else:
            selected_node_id = node_id or f"node-{uuid.uuid4().hex}"
            _validate_node_id(selected_node_id)
            incarnation = 1

        _atomic_write_json(
            state_path,
            {"version": 1, "node_id": selected_node_id, "incarnation": incarnation},
        )
        session = NodeStateSession(
            path=state_path,
            lock_descriptor=descriptor,
            node_id=selected_node_id,
            incarnation=incarnation,
            boot_id=f"boot-{uuid.uuid4().hex}",
        )
        descriptor = None
        return session
    except Exception:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        raise
