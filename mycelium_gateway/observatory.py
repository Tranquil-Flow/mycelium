"""Durable coherent snapshot publication for the read-only Observatory gateway."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import threading
from types import MappingProxyType
from typing import Any, Deque, Mapping, Optional, Sequence, Union
import uuid


OBSERVATORY_STREAM_PROTOCOL = "mycelium.observatory_stream.v1"
MAX_SAFE_GENERATION = 2**53 - 1
_REQUIRED_BUNDLE_KEYS = frozenset({"snapshot", "incidents", "provisioning"})
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN[ A-Z0-9_-]{0,48}PRIVATE KEY-----", re.IGNORECASE)
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(?:"
    r"\bbearer\s+[a-z0-9._~+/=-]{8,}|"
    r"\bsk-[a-z0-9_-]{12,}|"
    r"\beyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}|"
    r"\b(?:a3t|akia|asia|agpa|aida|aroa|aipa|anpa|anva|asca)[a-z0-9]{16}\b|"
    r"\bgh[pousr]_[a-z0-9]{20,}\b|"
    r"\bgithub_pat_[a-z0-9_]{20,}\b|"
    r"\bxox[abprs]-[a-z0-9-]{10,}\b|"
    r"\bglpat-[a-z0-9_-]{20,}\b|"
    r"\baiza[a-z0-9_-]{35}\b"
    r")"
)
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "activation",
        "activations",
        "completion",
        "completions",
        "generatedtext",
        "hiddenstate",
        "hiddenstates",
        "inputids",
        "logits",
        "messages",
        "modelweights",
        "outputids",
        "prompt",
        "prompts",
        "prompttext",
        "rawprompt",
        "rawtensor",
        "statedict",
        "systemprompt",
        "tensor",
        "tensors",
        "token",
        "tokenids",
        "tokens",
        "userprompt",
        "weights",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "accesstoken",
    "apikey",
    "apisecret",
    "authtoken",
    "bearertoken",
    "credential",
    "credentials",
    "password",
    "passwd",
    "passphrase",
    "privatekey",
    "refreshtoken",
    "secret",
)


class ObservatoryPublisherError(RuntimeError):
    """Base error for the durable Observatory publisher."""


class BundleValidationError(ObservatoryPublisherError, ValueError):
    """A candidate bundle is not safe JSON Observatory evidence."""


class PublisherStateError(ObservatoryPublisherError):
    """Persisted publisher state is unavailable or invalid."""


class GenerationExhaustedError(ObservatoryPublisherError):
    """No further JavaScript-safe generation can be allocated."""


class SubscriberLimitError(ObservatoryPublisherError):
    """The bounded subscriber limit has been reached."""


FrozenJSON = Union[
    None,
    bool,
    int,
    float,
    str,
    tuple["FrozenJSON", ...],
    Mapping[str, "FrozenJSON"],
]


@dataclass(frozen=True)
class Publication:
    """One immutable complete envelope and its canonical wire representation."""

    generation: int
    bundle: Mapping[str, FrozenJSON]
    envelope_json: bytes

    @property
    def protocol(self) -> str:
        return OBSERVATORY_STREAM_PROTOCOL

    def envelope(self) -> dict[str, Any]:
        """Return a detached JSON-compatible copy for non-wire consumers."""
        return json.loads(self.envelope_json)


class SnapshotSubscription:
    """Bounded subscription with replay captured atomically at registration."""

    def __init__(
        self,
        *,
        owner: "CoherentSnapshotPublisher",
        identifier: int,
        replay: Sequence[Publication],
        queue_size: int,
        minimum_generation: int,
    ) -> None:
        self._owner = owner
        self._identifier = identifier
        self._replay = tuple(replay)
        self._minimum_generation = minimum_generation
        self._queue: Deque[Publication] = deque()
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._closed = False
        self._disconnect_reason: Optional[str] = None

    @property
    def replay(self) -> tuple[Publication, ...]:
        return self._replay

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def disconnect_reason(self) -> Optional[str]:
        with self._lock:
            return self._disconnect_reason

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def get_nowait(self) -> Publication:
        with self._lock:
            if not self._queue:
                raise queue.Empty
            return self._queue.popleft()

    def _offer(self, publication: Publication) -> bool:
        """Offer under publisher lock; return false when subscriber must be removed."""
        with self._lock:
            if self._closed:
                return False
            if publication.generation < self._minimum_generation:
                return True
            if len(self._queue) >= self._queue_size:
                self._closed = True
                self._disconnect_reason = "slow_consumer"
                return False
            self._queue.append(publication)
            return True

    def _mark_closed(self, reason: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._disconnect_reason = reason
            return True

    def close(self) -> None:
        self._mark_closed("client_disconnect")
        self._owner._remove_subscription(self._identifier)

    def __enter__(self) -> "SnapshotSubscription":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _normalized_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    if normalized in _SENSITIVE_EXACT_KEYS:
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def _validate_json_value(
    value: Any,
    *,
    depth: int,
    max_nesting: int,
    active_containers: set[int],
) -> None:
    if depth > max_nesting:
        raise BundleValidationError("Observatory bundle exceeds maximum nesting")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            if _PRIVATE_KEY_RE.search(value):
                raise BundleValidationError("Observatory bundle contains private key material")
            if _CREDENTIAL_VALUE_RE.search(value):
                raise BundleValidationError("Observatory bundle contains credential-shaped material")
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_GENERATION:
            raise BundleValidationError("Observatory bundle integer is not JSON safe")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BundleValidationError("Observatory bundle contains NaN or Infinity")
        return
    if not isinstance(value, (dict, list)):
        raise BundleValidationError("Observatory bundle contains a non-JSON value")

    identity = id(value)
    if identity in active_containers:
        raise BundleValidationError("Observatory bundle contains a reference cycle")
    active_containers.add(identity)
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise BundleValidationError("Observatory bundle keys must be strings")
                if not key.isascii():
                    raise BundleValidationError("Observatory bundle keys must use ASCII")
                if _is_sensitive_key(key):
                    raise BundleValidationError("Observatory bundle contains a prohibited sensitive field")
                _validate_json_value(
                    item,
                    depth=depth + 1,
                    max_nesting=max_nesting,
                    active_containers=active_containers,
                )
        else:
            for item in value:
                _validate_json_value(
                    item,
                    depth=depth + 1,
                    max_nesting=max_nesting,
                    active_containers=active_containers,
                )
    finally:
        active_containers.remove(identity)


def _freeze_json(value: Any) -> FrozenJSON:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _canonical_bundle(bundle: Any, *, max_nesting: int) -> tuple[dict[str, Any], Mapping[str, FrozenJSON]]:
    if not isinstance(bundle, dict):
        raise BundleValidationError("Observatory publication must be one complete bundle object")
    if set(bundle) != _REQUIRED_BUNDLE_KEYS:
        raise BundleValidationError(
            "Observatory bundle must contain exactly snapshot, incidents, and provisioning"
        )
    if not isinstance(bundle["snapshot"], dict):
        raise BundleValidationError("Observatory snapshot must be an object")
    if not isinstance(bundle["incidents"], list):
        raise BundleValidationError("Observatory incidents must be an array")
    if not isinstance(bundle["provisioning"], dict):
        raise BundleValidationError("Observatory provisioning must be an object")
    _validate_json_value(
        bundle,
        depth=0,
        max_nesting=max_nesting,
        active_containers=set(),
    )
    try:
        copied = json.loads(
            json.dumps(
                bundle,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BundleValidationError("Observatory bundle cannot be encoded as strict JSON") from exc
    return copied, _freeze_json(copied)  # type: ignore[return-value]


def _canonical_envelope_bytes(generation: int, bundle: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            {
                "protocol": OBSERVATORY_STREAM_PROTOCOL,
                "generation": generation,
                "bundle": bundle,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BundleValidationError("Observatory envelope cannot be encoded as strict JSON") from exc


def _open_parent_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(os.fspath(path.parent), flags)
    except OSError as exc:
        raise PublisherStateError("publisher state directory is unavailable") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise PublisherStateError("publisher state parent is not a directory")
    return descriptor


def _regular_file_flags(*, writable: bool = False, create: bool = False) -> int:
    flags = os.O_RDWR if writable else os.O_RDONLY
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if not writable and hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _open_process_lock(parent_fd: int, name: str) -> int:
    """Open or create a stable lock inode without a concurrent O_CREAT race."""
    existing_flags = _regular_file_flags(writable=True)
    create_flags = existing_flags | os.O_CREAT | os.O_EXCL
    for _ in range(32):
        try:
            descriptor = os.open(name, existing_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            try:
                descriptor = os.open(name, create_flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except FileNotFoundError:
                # Darwin/APFS can report transient ENOENT when same-name creation races.
                continue
            except OSError as exc:
                raise PublisherStateError("publisher process lock is unavailable") from exc
        except OSError as exc:
            raise PublisherStateError("publisher process lock is unavailable") from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            os.close(descriptor)
            raise PublisherStateError("publisher process lock must be an owned regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    raise PublisherStateError("publisher process lock creation did not stabilize")


class CoherentSnapshotPublisher:
    """Publish complete durable Observatory bundles with bounded replay and fan-out."""

    def __init__(
        self,
        state_path: Union[str, os.PathLike[str]],
        *,
        max_payload_bytes: int = 2 * 1024 * 1024,
        max_nesting: int = 32,
        replay_capacity: int = 32,
        max_subscribers: int = 64,
        subscriber_queue_size: int = 8,
    ) -> None:
        for name, value in (
            ("max_payload_bytes", max_payload_bytes),
            ("max_nesting", max_nesting),
            ("replay_capacity", replay_capacity),
            ("max_subscribers", max_subscribers),
            ("subscriber_queue_size", subscriber_queue_size),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._state_path = Path(state_path).expanduser().absolute()
        self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_name = self._state_path.name + ".lock"
        self._max_payload_bytes = max_payload_bytes
        self._max_nesting = max_nesting
        self._replay: Deque[Publication] = deque(maxlen=replay_capacity)
        self._max_subscribers = max_subscribers
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: dict[int, SnapshotSubscription] = {}
        self._next_subscriber_id = 1
        self._mutex = threading.RLock()

        parent_fd = _open_parent_directory(self._state_path)
        try:
            self._current = self._read_persisted(parent_fd)
        finally:
            os.close(parent_fd)
        if self._current is not None:
            self._replay.append(self._current)

    @property
    def subscriber_count(self) -> int:
        with self._mutex:
            return len(self._subscribers)

    def _publication_from_document(self, document: Any) -> Publication:
        if not isinstance(document, dict) or set(document) != {"protocol", "generation", "bundle"}:
            raise PublisherStateError("persisted publisher state has an invalid envelope")
        if document.get("protocol") != OBSERVATORY_STREAM_PROTOCOL:
            raise PublisherStateError("persisted publisher state has an unsupported protocol")
        generation = document.get("generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or generation > MAX_SAFE_GENERATION
        ):
            raise PublisherStateError("persisted publisher state has an invalid generation")
        try:
            copied, frozen = _canonical_bundle(document.get("bundle"), max_nesting=self._max_nesting)
            payload = _canonical_envelope_bytes(generation, copied)
        except BundleValidationError as exc:
            raise PublisherStateError("persisted publisher state contains an invalid bundle") from exc
        if len(payload) > self._max_payload_bytes:
            raise PublisherStateError("persisted publisher state is oversized")
        return Publication(generation=generation, bundle=frozen, envelope_json=payload)

    def _read_persisted(self, parent_fd: int) -> Optional[Publication]:
        descriptor: Optional[int] = None
        try:
            try:
                descriptor = os.open(
                    self._state_path.name,
                    _regular_file_flags(),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PublisherStateError("publisher state must be a regular non-symlink file") from exc
                raise PublisherStateError("publisher state cannot be opened") from exc
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise PublisherStateError("publisher state must be a regular file")
            if metadata.st_size > self._max_payload_bytes:
                raise PublisherStateError("persisted publisher state is oversized")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(65_536, self._max_payload_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self._max_payload_bytes:
                    raise PublisherStateError("persisted publisher state is oversized")
            try:
                document = json.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PublisherStateError("persisted publisher state is not valid JSON") from exc
            return self._publication_from_document(document)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _atomic_replace(self, parent_fd: int, payload: bytes) -> None:
        temporary_name = f".{self._state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        descriptor: Optional[int] = None
        temporary_exists = False
        try:
            try:
                existing = os.stat(
                    self._state_path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise PublisherStateError("publisher state target must remain a regular file")

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            temporary_exists = True
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short write while persisting Observatory publication")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            written_identity = os.fstat(descriptor)
            named_identity = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                written_identity.st_dev != named_identity.st_dev
                or written_identity.st_ino != named_identity.st_ino
                or not stat.S_ISREG(named_identity.st_mode)
            ):
                raise PublisherStateError("publisher temporary state identity changed")
            os.replace(
                temporary_name,
                self._state_path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temporary_exists = False
            installed = os.stat(self._state_path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                written_identity.st_dev != installed.st_dev
                or written_identity.st_ino != installed.st_ino
                or not stat.S_ISREG(installed.st_mode)
            ):
                raise PublisherStateError("publisher state identity changed during atomic replace")
            os.fsync(parent_fd)
        except PublisherStateError:
            raise
        except OSError as exc:
            raise PublisherStateError("publisher state could not be persisted") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass

    def _append_replay_locked(self, publication: Publication) -> None:
        if self._replay and self._replay[-1].generation == publication.generation:
            self._replay[-1] = publication
            return
        if self._replay and self._replay[-1].generation > publication.generation:
            return
        self._replay.append(publication)

    def _refresh_from_disk_locked(self) -> None:
        parent_fd = _open_parent_directory(self._state_path)
        try:
            persisted = self._read_persisted(parent_fd)
        finally:
            os.close(parent_fd)
        if persisted is not None and (
            self._current is None or persisted.generation > self._current.generation
        ):
            self._current = persisted
            self._append_replay_locked(persisted)

    def publish(self, bundle: Any) -> Publication:
        """Validate, durably persist, then expose one complete immutable bundle."""
        copied_bundle, frozen_bundle = _canonical_bundle(bundle, max_nesting=self._max_nesting)
        with self._mutex:
            parent_fd = _open_parent_directory(self._state_path)
            lock_descriptor: Optional[int] = None
            try:
                lock_descriptor = _open_process_lock(parent_fd, self._lock_name)
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                persisted = self._read_persisted(parent_fd)
                if persisted is not None:
                    self._append_replay_locked(persisted)
                previous_generation = persisted.generation if persisted is not None else 0
                if previous_generation >= MAX_SAFE_GENERATION:
                    raise GenerationExhaustedError("Observatory generation space is exhausted")
                generation = previous_generation + 1
                envelope_json = _canonical_envelope_bytes(generation, copied_bundle)
                if len(envelope_json) > self._max_payload_bytes:
                    raise BundleValidationError("Observatory envelope exceeds maximum payload size")
                publication = Publication(
                    generation=generation,
                    bundle=frozen_bundle,
                    envelope_json=envelope_json,
                )
                self._atomic_replace(parent_fd, envelope_json)

                # Durability is complete before current state, replay, or subscribers can see it.
                self._current = publication
                self._append_replay_locked(publication)
                slow_subscribers = [
                    identifier
                    for identifier, subscriber in self._subscribers.items()
                    if not subscriber._offer(publication)
                ]
                for identifier in slow_subscribers:
                    self._subscribers.pop(identifier, None)
                return publication
            finally:
                if lock_descriptor is not None:
                    try:
                        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_descriptor)
                os.close(parent_fd)

    def current_publication(self) -> Optional[Publication]:
        with self._mutex:
            self._refresh_from_disk_locked()
            return self._current

    def current_envelope(self) -> Optional[dict[str, Any]]:
        publication = self.current_publication()
        return None if publication is None else publication.envelope()

    def snapshot_json(self) -> Optional[bytes]:
        publication = self.current_publication()
        return None if publication is None else publication.envelope_json

    def _captured_replay_locked(self, last_event_id: Optional[int]) -> tuple[Publication, ...]:
        current = self._current
        if current is None:
            return ()
        if last_event_id is None:
            return (current,)
        if last_event_id == current.generation:
            return ()
        if last_event_id > current.generation:
            return (current,)
        candidates = tuple(
            publication for publication in self._replay if publication.generation > last_event_id
        )
        if not candidates:
            return (current,)
        if (
            candidates[0].generation != last_event_id + 1
            or candidates[-1].generation != current.generation
            or any(
                later.generation != earlier.generation + 1
                for earlier, later in zip(candidates, candidates[1:])
            )
        ):
            return (current,)
        return candidates

    def subscribe(self, *, last_event_id: Optional[int] = None) -> SnapshotSubscription:
        if last_event_id is not None and (
            isinstance(last_event_id, bool)
            or not isinstance(last_event_id, int)
            or last_event_id < 0
            or last_event_id > MAX_SAFE_GENERATION
        ):
            raise ValueError("Last-Event-ID must be a non-negative safe integer")
        with self._mutex:
            self._refresh_from_disk_locked()
            if len(self._subscribers) >= self._max_subscribers:
                raise SubscriberLimitError("Observatory subscriber limit reached")
            identifier = self._next_subscriber_id
            self._next_subscriber_id += 1
            replay = self._captured_replay_locked(last_event_id)
            minimum_generation = (
                self._current.generation + 1
                if self._current is not None
                else (0 if last_event_id is None else last_event_id + 1)
            )
            subscription = SnapshotSubscription(
                owner=self,
                identifier=identifier,
                replay=replay,
                queue_size=self._subscriber_queue_size,
                minimum_generation=minimum_generation,
            )
            self._subscribers[identifier] = subscription
            return subscription

    def _remove_subscription(self, identifier: int) -> None:
        with self._mutex:
            self._subscribers.pop(identifier, None)
