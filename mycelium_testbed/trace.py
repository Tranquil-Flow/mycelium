# SPDX-License-Identifier: AGPL-3.0-only
"""Lifecycle trace export for the Mycelium core pilot testbed (PLAN 5.2 item 4).

One JSONL timeline per request and per command - ``enqueue``, ``delivered``,
``started``, ``finished``, ``receipt``, ``terminal`` - carrying a monotonic and a
wall stamp per host, plus a ``run.json`` header (host id, boot id, package
digest, git head, start wall time).

Cross-host ordering is *estimated*, never assumed:

* :func:`pair_round_trip_samples` derives NTP-style four-timestamp samples from
  the trace itself, pairing a sender's ``enqueue`` with the receiver's
  ``delivered`` of the same frame.  Pairing compares stamps only *within* one
  host, so a skewed clock cannot corrupt the pairing.
* :func:`estimate_clock_offset` returns the offset **and its uncertainty**; a
  one-way sample without a caller-supplied bound raises
  :class:`ClockOffsetUnavailable`.
* :func:`merge_timeline` refuses to interleave a host that has no estimated
  offset, so a timeline is never silently aligned.

This module is stdlib-only and imports no product code.  Write paths are chosen
by the caller; run directories belong outside the repository.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, NamedTuple, Sequence

try:  # POSIX advisory lock; absent on platforms without fcntl.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

TRACE_SCHEMA_VERSION = "mycelium.testbed.trace.v1"
EXCHANGE_SCHEMA_VERSION = "mycelium.testbed.exchange.v1"
DEFAULT_REFERENCE_HOST = "host-a"

REQUESTS_DIRNAME = "requests"
COMMANDS_DIRNAME = "commands"
EXCHANGES_FILENAME = "exchanges.jsonl"
RUN_HEADER_FILENAME = "run.json"

OK_STATUSES = frozenset({"ok", "completed", "accepted", "issued", "success"})


# --------------------------------------------------------------------------- #
# errors - nothing about a trace is dropped silently
# --------------------------------------------------------------------------- #


class TraceError(Exception):
    """Base class for testbed trace errors."""


class UnknownTraceEventKind(TraceError, ValueError):
    """An event kind outside the frozen lifecycle vocabulary was encountered."""


class TraceSchemaVersionError(TraceError, ValueError):
    """A document carries a schema version this reader does not implement."""


class ClockOffsetUnavailable(TraceError, RuntimeError):
    """No honest offset estimate exists for a host, so no alignment is done."""


class TraceDetailNotSerialisable(TraceError, TypeError):
    """Event detail contained a value that JSONL cannot represent."""


# --------------------------------------------------------------------------- #
# lifecycle vocabulary
# --------------------------------------------------------------------------- #


class EventKind(StrEnum):
    """The frozen lifecycle vocabulary (PLAN 5.2 item 4)."""

    ENQUEUE = "enqueue"
    DELIVERED = "delivered"
    STARTED = "started"
    FINISHED = "finished"
    RECEIPT = "receipt"
    TERMINAL = "terminal"

    @classmethod
    def parse(cls, value: Any) -> "EventKind":
        if isinstance(value, EventKind):
            return value
        if not isinstance(value, str):
            raise UnknownTraceEventKind(
                f"trace_event_kind_not_a_string:{type(value).__name__}"
            )
        try:
            return cls(value)
        except ValueError as error:
            raise UnknownTraceEventKind(f"unknown_trace_event_kind:{value}") from error


LIFECYCLE_ORDER: tuple[EventKind, ...] = (
    EventKind.ENQUEUE,
    EventKind.DELIVERED,
    EventKind.STARTED,
    EventKind.FINISHED,
    EventKind.RECEIPT,
    EventKind.TERMINAL,
)


def canonical_event_kinds() -> tuple[str, ...]:
    """The vocabulary a reader must accept, in lifecycle order."""

    return tuple(kind.value for kind in LIFECYCLE_ORDER)


# --------------------------------------------------------------------------- #
# host identity and clocks
# --------------------------------------------------------------------------- #


class ClockStamp(NamedTuple):
    monotonic_ns: int
    wall_ns: int


def _boot_id_from_platform() -> str:
    """Best-effort stable boot identity for this host (no third-party imports)."""

    proc_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        if proc_boot_id.is_file():
            value = proc_boot_id.read_text(encoding="ascii").strip()
            if value:
                return f"linux-boot-id:{value}"
    except OSError:
        pass
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        fields = completed.stdout.replace("{", " ").replace("}", " ").split(",")
        seconds = None
        microseconds = None
        for item in fields:
            name, _, raw = item.partition("=")
            name = name.strip()
            value = raw.strip()
            if not value.isdigit():
                continue
            if name == "sec":
                seconds = value
            elif name == "usec":
                microseconds = value
        if seconds is not None:
            return f"macos-boottime:{seconds}.{microseconds or '0'}"
    return "boot-id-unavailable"


_BOOT_ID_CACHE: dict[str, str] = {}


def read_boot_id() -> str:
    """Cached boot identity of the local host."""

    if "value" not in _BOOT_ID_CACHE:
        try:
            _BOOT_ID_CACHE["value"] = _boot_id_from_platform()
        except BaseException:  # pragma: no cover - defensive, never fail a run
            _BOOT_ID_CACHE["value"] = "boot-id-unavailable"
    return _BOOT_ID_CACHE["value"]


def read_hostname() -> str | None:
    try:
        return os.uname().nodename
    except (AttributeError, OSError):  # pragma: no cover - non-POSIX
        return None


class HostClock:
    """A host's own clock, optionally skewed to emulate a remote host.

    The offsets exist so a test can *emulate* two hosts with different clock
    epochs on one machine.  Production callers leave them at zero.
    """

    def __init__(
        self,
        *,
        monotonic_offset_ns: int = 0,
        wall_offset_ns: int = 0,
        monotonic_ns_source: Callable[[], int] | None = None,
        wall_ns_source: Callable[[], int] | None = None,
    ) -> None:
        self.monotonic_offset_ns = int(monotonic_offset_ns)
        self.wall_offset_ns = int(wall_offset_ns)
        self._monotonic_ns_source = monotonic_ns_source or time.monotonic_ns
        self._wall_ns_source = wall_ns_source or time.time_ns

    def monotonic_ns(self) -> int:
        return int(self._monotonic_ns_source()) + self.monotonic_offset_ns

    def wall_ns(self) -> int:
        return int(self._wall_ns_source()) + self.wall_offset_ns

    def stamp(self) -> ClockStamp:
        return ClockStamp(self.monotonic_ns(), self.wall_ns())


def iso_utc(wall_ns: int) -> str:
    seconds, nanoseconds = divmod(int(wall_ns), 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanoseconds // 1000:06d}Z"


# --------------------------------------------------------------------------- #
# trace events
# --------------------------------------------------------------------------- #


def _coerce_detail(value: Any, *, path: str = "detail") -> Any:
    """Return a JSON-safe copy of ``value`` or raise (never coerce silently)."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_detail(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        coerced: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TraceDetailNotSerialisable(f"{path}.key_not_a_string:{type(key).__name__}")
            coerced[key] = _coerce_detail(item, path=f"{path}.{key}")
        return coerced
    raise TraceDetailNotSerialisable(f"{path}.unsupported_type:{type(value).__name__}")


@dataclass(frozen=True)
class TraceEvent:
    """One (request, command) lifecycle transition on one host."""

    kind: EventKind
    host_id: str
    pid: int
    recorder_id: str
    monotonic_ns: int
    wall_ns: int
    sequence: int
    request_id: str | None = None
    command_id: str | None = None
    placement_id: str | None = None
    status: str = "ok"
    detail: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TRACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EventKind.parse(self.kind))
        if not self.request_id and not self.command_id:
            raise TraceError("trace_event_without_request_or_command")
        if self.sequence < 1:
            raise TraceError(f"trace_event_sequence_must_be_positive:{self.sequence}")

    @property
    def subject(self) -> str:
        return self.request_id or self.command_id or "unknown"

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": str(self.kind),
            "host_id": self.host_id,
            "pid": self.pid,
            "recorder_id": self.recorder_id,
            "sequence": self.sequence,
            "monotonic_ns": self.monotonic_ns,
            "wall_ns": self.wall_ns,
            "wall_iso": iso_utc(self.wall_ns),
            "request_id": self.request_id,
            "command_id": self.command_id,
            "placement_id": self.placement_id,
            "status": self.status,
            "detail": dict(self.detail),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_document(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "TraceEvent":
        if not isinstance(document, Mapping):
            raise TraceError(f"trace_event_document_not_a_mapping:{type(document).__name__}")
        version = document.get("schema_version")
        if version != TRACE_SCHEMA_VERSION:
            raise TraceSchemaVersionError(
                f"trace_schema_version_mismatch:expected={TRACE_SCHEMA_VERSION}:found={version!r}"
            )
        detail = document.get("detail") or {}
        if not isinstance(detail, Mapping):
            raise TraceError("trace_event_detail_not_a_mapping")
        return cls(
            kind=EventKind.parse(document.get("kind")),
            host_id=str(document.get("host_id")),
            pid=_required_int(document, "pid", default=-1),
            recorder_id=str(document.get("recorder_id", "unknown")),
            monotonic_ns=_required_int(document, "monotonic_ns"),
            wall_ns=_required_int(document, "wall_ns"),
            sequence=_required_int(document, "sequence"),
            request_id=_optional_str(document.get("request_id")),
            command_id=_optional_str(document.get("command_id")),
            placement_id=_optional_str(document.get("placement_id")),
            status=str(document.get("status", "ok")),
            detail=dict(detail),
            schema_version=str(version),
        )

    @classmethod
    def from_json_line(cls, line: str) -> "TraceEvent":
        try:
            document = json.loads(line)
        except json.JSONDecodeError as error:
            raise TraceError(f"trace_event_line_not_json:{error.msg}") from error
        return cls.from_document(document)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_int(
    document: Mapping[str, Any], key: str, *, default: int | None = None
) -> int:
    """Read an integer field, refusing a document that omits or mistypes it."""

    if key not in document:
        if default is None:
            raise TraceError(f"trace_field_missing:{key}")
        return default
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceError(f"trace_field_not_an_integer:{key}:{value!r}")
    return value


def event_sort_key(event: TraceEvent) -> tuple[str, int, int]:
    """Raw order *within* a host: the only order a skewed clock cannot break."""

    return (event.host_id, event.sequence, event.monotonic_ns)


# --------------------------------------------------------------------------- #
# JSONL writing
# --------------------------------------------------------------------------- #


class TraceFileWriter:
    """Append-only JSONL writer: one ``write`` per line, flush/sync on demand.

    ``O_APPEND`` keeps concurrent writers (two hosts in one run directory, or two
    child processes) from overwriting each other's lines.
    """

    def __init__(self, path: Path, *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        self._fsync = bool(fsync)
        self._closed = False

    def append_line(self, line: str) -> None:
        payload = (line.rstrip("\n") + "\n").encode("utf-8")
        with self._lock:
            if self._closed:
                raise TraceError(f"trace_writer_closed:{self.path}")
            os.write(self._fd, payload)
            if self._fsync:
                os.fsync(self._fd)

    def sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            os.fsync(self._fd)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._fsync:
                    os.fsync(self._fd)
            finally:
                os.close(self._fd)

    def __enter__(self) -> "TraceFileWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# run directory and header
# --------------------------------------------------------------------------- #


def compute_package_digest(root: Path | None = None) -> str:
    """Deterministic digest of a Python package's source files."""

    base = Path(root) if root is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in base.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for path in files:
        digest.update(path.relative_to(base).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def read_git_head(start: Path | None = None) -> str | None:
    """Read-only ``HEAD`` of the repository containing ``start`` (no writes)."""

    directory = Path(start) if start is not None else Path(__file__).resolve().parent
    candidate: Path | None = None
    for parent in [directory, *directory.parents]:
        if (parent / ".git").exists():
            candidate = parent
            break
    if candidate is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout.strip()
    return head or None


@dataclass(frozen=True)
class HostRecord:
    """One recorder registered with a run directory."""

    host_id: str
    boot_id: str
    pid: int
    recorder_id: str
    started_wall_ns: int
    hostname: str | None = None
    role: str | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "boot_id": self.boot_id,
            "pid": self.pid,
            "recorder_id": self.recorder_id,
            "hostname": self.hostname,
            "role": self.role,
            "started_wall_ns": self.started_wall_ns,
            "started_wall_iso": iso_utc(self.started_wall_ns),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "HostRecord":
        return cls(
            host_id=str(document.get("host_id")),
            boot_id=str(document.get("boot_id", "boot-id-unavailable")),
            pid=_required_int(document, "pid", default=-1),
            recorder_id=str(document.get("recorder_id", "unknown")),
            started_wall_ns=_required_int(document, "started_wall_ns", default=0),
            hostname=_optional_str(document.get("hostname")),
            role=_optional_str(document.get("role")),
        )


@dataclass(frozen=True)
class RunHeader:
    """The ``run.json`` document."""

    run_id: str
    run_dir: str
    host_id: str | None
    boot_id: str
    package_digest: str
    git_head: str | None
    start_wall_ns: int
    start_wall_iso: str
    hosts: tuple[HostRecord, ...] = ()
    reference_host: str | None = None
    product_package_digest: str | None = None
    product_roots: tuple[str, ...] = ()
    notes: str | None = None
    schema_version: str = TRACE_SCHEMA_VERSION

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "host_id": self.host_id,
            "boot_id": self.boot_id,
            "package_digest": self.package_digest,
            "product_package_digest": self.product_package_digest,
            "product_roots": list(self.product_roots),
            "git_head": self.git_head,
            "start_wall_ns": self.start_wall_ns,
            "start_wall_iso": self.start_wall_iso,
            "reference_host": self.reference_host,
            "hosts": [record.to_document() for record in self.hosts],
            "notes": self.notes,
            "event_kinds": list(canonical_event_kinds()),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "RunHeader":
        version = document.get("schema_version")
        if version != TRACE_SCHEMA_VERSION:
            raise TraceSchemaVersionError(
                f"run_header_schema_version_mismatch:expected={TRACE_SCHEMA_VERSION}:found={version!r}"
            )
        hosts = document.get("hosts") or ()
        if not isinstance(hosts, (list, tuple)):
            raise TraceError("run_header_hosts_not_a_list")
        return cls(
            run_id=str(document.get("run_id")),
            run_dir=str(document.get("run_dir", "")),
            host_id=_optional_str(document.get("host_id")),
            boot_id=str(document.get("boot_id", "boot-id-unavailable")),
            package_digest=str(document.get("package_digest", "")),
            git_head=_optional_str(document.get("git_head")),
            start_wall_ns=_required_int(document, "start_wall_ns", default=0),
            start_wall_iso=str(document.get("start_wall_iso", "")),
            hosts=tuple(HostRecord.from_document(item) for item in hosts),
            reference_host=_optional_str(document.get("reference_host")),
            product_package_digest=_optional_str(document.get("product_package_digest")),
            product_roots=tuple(
                str(item) for item in (document.get("product_roots") or ())
            ),
            notes=_optional_str(document.get("notes")),
            schema_version=str(version),
        )


class TraceRunDirectory:
    """A run directory outside the repository: header, per-request and per-command files."""

    def __init__(
        self,
        run_dir: str | os.PathLike[str],
        *,
        run_id: str | None = None,
        host_id: str | None = None,
        reference_host: str | None = None,
        fsync: bool = False,
        package_digest: str | None = None,
        product_package_digest: str | None = None,
        product_roots: Sequence[str | os.PathLike[str]] = (),
        notes: str | None = None,
        clock: HostClock | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.requests_dir = self.run_dir / REQUESTS_DIRNAME
        self.commands_dir = self.run_dir / COMMANDS_DIRNAME
        self.exchange_path = self.run_dir / EXCHANGES_FILENAME
        self.fsync = bool(fsync)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._writers: dict[Path, TraceFileWriter] = {}
        self._exchange_writer: TraceFileWriter | None = None
        self._records: list[HostRecord] = []
        start = (clock or HostClock()).stamp()
        self.header = RunHeader(
            run_id=run_id or self.run_dir.name,
            run_dir=str(self.run_dir),
            host_id=host_id,
            boot_id=read_boot_id(),
            package_digest=package_digest or compute_package_digest(),
            git_head=read_git_head(),
            start_wall_ns=start.wall_ns,
            start_wall_iso=iso_utc(start.wall_ns),
            hosts=(),
            reference_host=reference_host,
            product_package_digest=product_package_digest,
            product_roots=tuple(str(item) for item in product_roots),
            notes=notes,
        )
        self._write_header()

    # -- header ---------------------------------------------------------- #

    @classmethod
    def attach(
        cls,
        run_dir: str | os.PathLike[str],
        *,
        fsync: bool = False,
    ) -> "TraceRunDirectory":
        """Attach to an existing run directory (a second host or a child process)."""

        instance = cls.__new__(cls)
        instance.run_dir = Path(run_dir)
        instance.requests_dir = instance.run_dir / REQUESTS_DIRNAME
        instance.commands_dir = instance.run_dir / COMMANDS_DIRNAME
        instance.exchange_path = instance.run_dir / EXCHANGES_FILENAME
        instance.fsync = bool(fsync)
        header_path = instance.run_dir / RUN_HEADER_FILENAME
        if not header_path.is_file():
            raise TraceError(f"run_header_missing:{header_path}")
        instance.requests_dir.mkdir(parents=True, exist_ok=True)
        instance.commands_dir.mkdir(parents=True, exist_ok=True)
        instance._lock = threading.Lock()
        instance._writers = {}
        instance._exchange_writer = None
        instance._records = []
        instance.header = RunHeader.from_document(
            json.loads(header_path.read_text(encoding="utf-8"))
        )
        return instance

    def _write_header(self) -> None:
        self._persist_header(self.header)

    def _persist_header(self, header: RunHeader) -> None:
        payload = json.dumps(header.to_document(), indent=2, sort_keys=True) + "\n"
        target = self.run_dir / RUN_HEADER_FILENAME
        temporary = self.run_dir / f".{RUN_HEADER_FILENAME}.tmp.{os.getpid()}"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    @contextmanager
    def _header_guard(self) -> Iterator[None]:
        """Serialise header updates across the processes sharing a run directory."""

        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        lock_path = self.run_dir / f".{RUN_HEADER_FILENAME}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def register_recorder(self, record: HostRecord) -> None:
        with self._lock:
            if any(
                existing.recorder_id == record.recorder_id
                for existing in self._records
            ):
                return
            self._records.append(record)
            with self._header_guard():
                disk_records = _read_header_records(self.run_dir)
                merged: list[HostRecord] = []
                seen: set[str] = set()
                for candidate in (*disk_records, *self._records):
                    if candidate.recorder_id in seen:
                        continue
                    seen.add(candidate.recorder_id)
                    merged.append(candidate)
                self._records = merged
                host_id = self.header.host_id or record.host_id
                self.header = RunHeader(
                    **{
                        **self.header.__dict__,
                        "host_id": host_id,
                        "reference_host": self.header.reference_host or host_id,
                        "hosts": tuple(merged),
                    }
                )
                self._persist_header(self.header)

    # -- writers --------------------------------------------------------- #

    def request_path(self, request_id: str) -> Path:
        return self.requests_dir / f"{_safe_name(request_id)}.jsonl"

    def command_path(self, command_id: str) -> Path:
        return self.commands_dir / f"{_safe_name(command_id)}.jsonl"

    def _writer(self, path: Path) -> TraceFileWriter:
        writer = self._writers.get(path)
        if writer is None:
            writer = TraceFileWriter(path, fsync=self.fsync)
            self._writers[path] = writer
        return writer

    def record(self, event: TraceEvent) -> TraceEvent:
        """Append one event to its request file and/or its command file."""

        line = event.to_json()
        with self._lock:
            if event.request_id:
                self._writer(self.request_path(event.request_id)).append_line(line)
            if event.command_id:
                self._writer(self.command_path(event.command_id)).append_line(line)
        return event

    def record_exchange(self, sample: "ExchangeSample") -> "ExchangeSample":
        document = sample.to_document()
        with self._lock:
            if self._exchange_writer is None:
                self._exchange_writer = TraceFileWriter(
                    self.exchange_path, fsync=self.fsync
                )
            self._exchange_writer.append_line(
                json.dumps(document, sort_keys=True, separators=(",", ":"))
            )
        return sample

    def sync(self) -> None:
        with self._lock:
            for writer in self._writers.values():
                writer.sync()
            if self._exchange_writer is not None:
                self._exchange_writer.sync()

    def close(self) -> None:
        with self._lock:
            for writer in self._writers.values():
                writer.close()
            self._writers.clear()
            if self._exchange_writer is not None:
                self._exchange_writer.close()
                self._exchange_writer = None

    def __enter__(self) -> "TraceRunDirectory":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _read_header_records(run_dir: Path) -> tuple[HostRecord, ...]:
    """Host records already persisted in a run directory (merged under a lock)."""

    header_path = run_dir / RUN_HEADER_FILENAME
    if not header_path.is_file():
        return ()
    try:
        document = json.loads(header_path.read_text(encoding="utf-8"))
        header = RunHeader.from_document(document)
    except (OSError, ValueError, TraceError):
        return ()
    return header.hosts


def _safe_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_.:" else "_"
        for character in value
    )
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:96]}-{digest}"


# --------------------------------------------------------------------------- #
# recorder
# --------------------------------------------------------------------------- #


class TraceRecorder:
    """Per-host event source: stamps one clock pair per transition, one sequence."""

    def __init__(
        self,
        host_id: str,
        *,
        run: TraceRunDirectory,
        clock: HostClock | None = None,
        pid: int | None = None,
        boot_id: str | None = None,
        hostname: str | None = None,
        role: str | None = None,
        recorder_id: str | None = None,
    ) -> None:
        if not host_id:
            raise TraceError("trace_recorder_requires_host_id")
        self.host_id = host_id
        self.run = run
        self.clock = clock or HostClock()
        self.pid = int(pid if pid is not None else os.getpid())
        self.boot_id = boot_id or read_boot_id()
        self.hostname = hostname if hostname is not None else read_hostname()
        self.role = role
        self.recorder_id = recorder_id or f"{self.host_id}-{self.pid}-{os.urandom(6).hex()}"
        self._lock = threading.Lock()
        self._sequence = 0
        started = self.clock.stamp()
        self.host_record = HostRecord(
            host_id=self.host_id,
            boot_id=self.boot_id,
            pid=self.pid,
            recorder_id=self.recorder_id,
            started_wall_ns=started.wall_ns,
            hostname=self.hostname,
            role=self.role,
        )
        self.run.register_recorder(self.host_record)

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def event(
        self,
        kind: EventKind | str,
        *,
        request_id: str | None = None,
        command_id: str | None = None,
        placement_id: str | None = None,
        status: str = "ok",
        detail: Mapping[str, Any] | None = None,
        stamp: ClockStamp | None = None,
    ) -> TraceEvent:
        parsed = EventKind.parse(kind)
        detail_document = _coerce_detail(dict(detail or {}))
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        moment = stamp if stamp is not None else self.clock.stamp()
        event = TraceEvent(
            kind=parsed,
            host_id=self.host_id,
            pid=self.pid,
            recorder_id=self.recorder_id,
            monotonic_ns=int(moment.monotonic_ns),
            wall_ns=int(moment.wall_ns),
            sequence=sequence,
            request_id=request_id,
            command_id=command_id,
            placement_id=placement_id,
            status=status,
            detail=detail_document,
        )
        return self.run.record(event)

    def failure(
        self,
        kind: EventKind | str,
        *,
        reason: str,
        request_id: str | None = None,
        command_id: str | None = None,
        placement_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> TraceEvent:
        document = dict(detail or {})
        document.setdefault("reason", reason)
        return self.event(
            kind,
            request_id=request_id,
            command_id=command_id,
            placement_id=placement_id,
            status=reason,
            detail=document,
        )


# --------------------------------------------------------------------------- #
# clock offset estimation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExchangeSample:
    """Paired send/receive stamps between two hosts.

    ``a_*`` is the initiating host.  A four-timestamp sample carries ``b_send``
    and ``a_recv`` (the reply leg); a one-way sample must supply
    ``one_way_bound_ns``.
    """

    a_host: str
    b_host: str
    a_send_monotonic_ns: int
    a_send_wall_ns: int
    b_recv_monotonic_ns: int
    b_recv_wall_ns: int
    b_send_monotonic_ns: int | None = None
    b_send_wall_ns: int | None = None
    a_recv_monotonic_ns: int | None = None
    a_recv_wall_ns: int | None = None
    one_way_bound_ns: int | None = None
    source: str = "recorded"
    correlation: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.a_host or not self.b_host:
            raise TraceError("exchange_sample_requires_both_hosts")
        if self.a_host == self.b_host:
            raise TraceError("exchange_sample_hosts_must_differ")
        if self.round_trip_fields_present and not self.is_round_trip:
            raise TraceError("exchange_sample_partial_round_trip_stamps")
        if not self.is_round_trip and not self.one_way_bound_ns:
            raise ClockOffsetUnavailable(
                "one_way_exchange_sample_requires_one_way_bound_ns"
            )
        if self.one_way_bound_ns is not None and self.one_way_bound_ns <= 0:
            raise TraceError("one_way_bound_ns_must_be_positive")

    @property
    def round_trip_fields_present(self) -> bool:
        return self.b_send_monotonic_ns is not None or self.a_recv_monotonic_ns is not None

    @property
    def is_round_trip(self) -> bool:
        return (
            self.b_send_monotonic_ns is not None
            and self.b_send_wall_ns is not None
            and self.a_recv_monotonic_ns is not None
            and self.a_recv_wall_ns is not None
        )

    @property
    def method(self) -> str:
        return "four_timestamp_ntp" if self.is_round_trip else "one_way_bounded"

    def monotonic_offset_ns(self) -> int:
        """Offset to add to host ``b`` stamps to express them in host ``a`` time."""

        b_recv = self.b_recv_monotonic_ns
        a_send = self.a_send_monotonic_ns
        if not self.is_round_trip:
            return b_recv - a_send
        b_send_mono, _b_send_wall, a_recv_mono, _a_recv_wall = self._round_trip_stamps()
        return ((b_recv - a_send) + (b_send_mono - a_recv_mono)) // 2

    def wall_offset_ns(self) -> int:
        b_recv = self.b_recv_wall_ns
        a_send = self.a_send_wall_ns
        if not self.is_round_trip:
            return b_recv - a_send
        _b_send_mono, b_send_wall, _a_recv_mono, a_recv_wall = self._round_trip_stamps()
        return ((b_recv - a_send) + (b_send_wall - a_recv_wall)) // 2

    def _round_trip_stamps(self) -> tuple[int, int, int, int]:
        """The four reply-leg stamps, or a loud error when they are incomplete."""

        b_send_monotonic = self.b_send_monotonic_ns
        b_send_wall = self.b_send_wall_ns
        a_recv_monotonic = self.a_recv_monotonic_ns
        a_recv_wall = self.a_recv_wall_ns
        if (
            b_send_monotonic is None
            or b_send_wall is None
            or a_recv_monotonic is None
            or a_recv_wall is None
        ):
            raise TraceError("exchange_sample_missing_round_trip_stamps")
        return (b_send_monotonic, b_send_wall, a_recv_monotonic, a_recv_wall)

    def round_trip_ns(self) -> int | None:
        if not self.is_round_trip:
            return None
        b_send_mono, _b_send_wall, a_recv_mono, _a_recv_wall = self._round_trip_stamps()
        return (a_recv_mono - self.a_send_monotonic_ns) - (
            b_send_mono - self.b_recv_monotonic_ns
        )

    def uncertainty_ns(self) -> int:
        round_trip = self.round_trip_ns()
        if round_trip is None:
            return int(self.one_way_bound_ns or 0)
        return max(1, round_trip // 2)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": EXCHANGE_SCHEMA_VERSION,
            "a_host": self.a_host,
            "b_host": self.b_host,
            "a_send_monotonic_ns": self.a_send_monotonic_ns,
            "a_send_wall_ns": self.a_send_wall_ns,
            "b_recv_monotonic_ns": self.b_recv_monotonic_ns,
            "b_recv_wall_ns": self.b_recv_wall_ns,
            "b_send_monotonic_ns": self.b_send_monotonic_ns,
            "b_send_wall_ns": self.b_send_wall_ns,
            "a_recv_monotonic_ns": self.a_recv_monotonic_ns,
            "a_recv_wall_ns": self.a_recv_wall_ns,
            "one_way_bound_ns": self.one_way_bound_ns,
            "source": self.source,
            "correlation": dict(self.correlation),
            "method": self.method,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "ExchangeSample":
        version = document.get("schema_version")
        if version != EXCHANGE_SCHEMA_VERSION:
            raise TraceSchemaVersionError(
                f"exchange_schema_version_mismatch:expected={EXCHANGE_SCHEMA_VERSION}:found={version!r}"
            )
        return cls(
            a_host=str(document.get("a_host")),
            b_host=str(document.get("b_host")),
            a_send_monotonic_ns=_required_int(document, "a_send_monotonic_ns"),
            a_send_wall_ns=_required_int(document, "a_send_wall_ns"),
            b_recv_monotonic_ns=_required_int(document, "b_recv_monotonic_ns"),
            b_recv_wall_ns=_required_int(document, "b_recv_wall_ns"),
            b_send_monotonic_ns=_optional_int(document.get("b_send_monotonic_ns")),
            b_send_wall_ns=_optional_int(document.get("b_send_wall_ns")),
            a_recv_monotonic_ns=_optional_int(document.get("a_recv_monotonic_ns")),
            a_recv_wall_ns=_optional_int(document.get("a_recv_wall_ns")),
            one_way_bound_ns=_optional_int(document.get("one_way_bound_ns")),
            source=str(document.get("source", "recorded")),
            correlation=dict(document.get("correlation") or {}),
        )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


@dataclass(frozen=True)
class ClockOffset:
    """An estimated offset with the uncertainty that comes with it."""

    reference_host: str
    host: str
    monotonic_offset_ns: int
    wall_offset_ns: int
    uncertainty_ns: int
    sample_count: int
    method: str
    round_trip_ns: int | None = None
    spread_ns: int | None = None

    def to_monotonic(self, stamp_ns: int) -> int:
        return int(stamp_ns) - self.monotonic_offset_ns

    def to_wall(self, stamp_ns: int) -> int:
        return int(stamp_ns) - self.wall_offset_ns

    def describe(self) -> str:
        round_trip = (
            f" rtt={self.round_trip_ns / 1e6:.3f}ms" if self.round_trip_ns is not None else ""
        )
        spread = f" spread={self.spread_ns / 1e6:.3f}ms" if self.spread_ns is not None else ""
        return (
            f"{self.host} vs {self.reference_host}: mono {_signed_seconds(self.monotonic_offset_ns)} "
            f"wall {_signed_seconds(self.wall_offset_ns)} "
            f"+/-{self.uncertainty_ns / 1e6:.3f}ms "
            f"(n={self.sample_count}, {self.method}{round_trip}{spread})"
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "reference_host": self.reference_host,
            "host": self.host,
            "monotonic_offset_ns": self.monotonic_offset_ns,
            "wall_offset_ns": self.wall_offset_ns,
            "uncertainty_ns": self.uncertainty_ns,
            "sample_count": self.sample_count,
            "method": self.method,
            "round_trip_ns": self.round_trip_ns,
            "spread_ns": self.spread_ns,
        }


def _signed_seconds(nanoseconds: int) -> str:
    sign = "-" if nanoseconds < 0 else "+"
    return f"{sign}{abs(nanoseconds) / 1e9:.9f}s"


def estimate_clock_offset(
    samples: Sequence[ExchangeSample],
    *,
    reference_host: str | None = None,
) -> ClockOffset:
    """Estimate one host pair's offset, or refuse when that cannot be done honestly."""

    if not samples:
        raise ClockOffsetUnavailable("no_exchange_samples")
    reference = reference_host or samples[0].a_host
    host_pairs = {frozenset((sample.a_host, sample.b_host)) for sample in samples}
    if len(host_pairs) != 1:
        raise TraceError(f"mixed_host_pairs:{sorted(tuple(sorted(pair)) for pair in host_pairs)}")
    pair = next(iter(host_pairs))
    if len(pair) != 2 or reference not in pair:
        raise TraceError(f"reference_host_not_in_pair:{reference}:{sorted(pair)}")
    subject = next(host for host in sorted(pair) if host != reference)
    oriented = [
        sample if sample.a_host == reference else _reversed_sample(sample)
        for sample in samples
    ]
    methods = {sample.method for sample in oriented}
    if len(methods) != 1:
        raise TraceError(f"mixed_estimation_methods:{sorted(methods)}")
    method = next(iter(methods))
    if method == "one_way_bounded" and any(
        not sample.one_way_bound_ns for sample in oriented
    ):
        raise ClockOffsetUnavailable("one_way_sample_without_bound")
    monotonic_offsets = [sample.monotonic_offset_ns() for sample in oriented]
    wall_offsets = [sample.wall_offset_ns() for sample in oriented]
    median_monotonic = int(statistics.median(monotonic_offsets))
    median_wall = int(statistics.median(wall_offsets))
    spread = max(abs(offset - median_monotonic) for offset in monotonic_offsets)
    per_sample_uncertainty = max(sample.uncertainty_ns() for sample in oriented)
    round_trips = [
        measured
        for measured in (sample.round_trip_ns() for sample in oriented)
        if measured is not None
    ]
    return ClockOffset(
        reference_host=reference,
        host=subject,
        monotonic_offset_ns=median_monotonic,
        wall_offset_ns=median_wall,
        uncertainty_ns=max(1, per_sample_uncertainty + spread),
        sample_count=len(oriented),
        method=method,
        round_trip_ns=(int(statistics.median(round_trips)) if round_trips else None),
        spread_ns=spread,
    )


def _reversed_sample(sample: ExchangeSample) -> ExchangeSample:
    """Express a sample in the opposite orientation by swapping the two hosts."""

    return ExchangeSample(
        a_host=sample.b_host,
        b_host=sample.a_host,
        a_send_monotonic_ns=(
            sample.b_send_monotonic_ns
            if sample.b_send_monotonic_ns is not None
            else sample.b_recv_monotonic_ns
        ),
        a_send_wall_ns=(
            sample.b_send_wall_ns
            if sample.b_send_wall_ns is not None
            else sample.b_recv_wall_ns
        ),
        b_recv_monotonic_ns=(
            sample.a_recv_monotonic_ns
            if sample.a_recv_monotonic_ns is not None
            else sample.a_send_monotonic_ns
        ),
        b_recv_wall_ns=(
            sample.a_recv_wall_ns
            if sample.a_recv_wall_ns is not None
            else sample.a_send_wall_ns
        ),
        b_send_monotonic_ns=(
            sample.a_send_monotonic_ns if sample.is_round_trip else None
        ),
        b_send_wall_ns=(sample.a_send_wall_ns if sample.is_round_trip else None),
        a_recv_monotonic_ns=(
            sample.b_recv_monotonic_ns if sample.is_round_trip else None
        ),
        a_recv_wall_ns=(sample.b_recv_wall_ns if sample.is_round_trip else None),
        one_way_bound_ns=sample.one_way_bound_ns,
        source=f"reversed:{sample.source}",
        correlation=dict(sample.correlation),
    )


def estimate_offsets_by_host(
    samples: Sequence[ExchangeSample],
    *,
    reference_host: str | None = None,
) -> dict[str, ClockOffset]:
    """Estimate one offset per non-reference host.

    Samples of either orientation belong to the same physical host pair, so they
    are grouped by host *set* and then oriented towards the reference.
    """

    if not samples:
        return {}
    reference = reference_host or samples[0].a_host
    grouped: dict[frozenset[str], list[ExchangeSample]] = {}
    for sample in samples:
        grouped.setdefault(frozenset((sample.a_host, sample.b_host)), []).append(sample)
    offsets: dict[str, ClockOffset] = {}
    for hosts, group in grouped.items():
        if reference not in hosts or len(hosts) != 2:
            continue
        subject = next(host for host in sorted(hosts) if host != reference)
        if subject in offsets:
            continue
        offsets[subject] = estimate_clock_offset(
            sorted(group, key=lambda item: item.a_send_monotonic_ns),
            reference_host=reference,
        )
    return offsets


def pair_round_trip_samples(
    events: Iterable[TraceEvent],
    *,
    a_host: str,
    b_host: str,
    request_id: str | None = None,
    max_samples: int | None = None,
) -> tuple[ExchangeSample, ...]:
    """Derive NTP-style samples from ``enqueue``/``delivered`` event pairs.

    A frame is identified by the caller-supplied ``frame_digest`` detail.  Every
    ordering decision compares stamps *within* one host, so none of them depends
    on the clock skew we are trying to measure.
    """

    ordered = sorted(events, key=event_sort_key)
    forward = _crossings(ordered, sender=a_host, receiver=b_host, request_id=request_id)
    backward = _crossings(ordered, sender=b_host, receiver=a_host, request_id=request_id)
    samples: list[ExchangeSample] = []
    for reply_enqueue, re_delivered in backward:
        candidates = [
            crossing
            for crossing in forward
            if crossing[1].monotonic_ns <= reply_enqueue.monotonic_ns
            and crossing[1].wall_ns <= reply_enqueue.wall_ns
        ]
        if not candidates:
            continue
        send_event, delivery_event = candidates[-1]
        round_trip = (re_delivered.monotonic_ns - send_event.monotonic_ns) - (
            reply_enqueue.monotonic_ns - delivery_event.monotonic_ns
        )
        if round_trip < 0:
            continue
        samples.append(
            ExchangeSample(
                a_host=a_host,
                b_host=b_host,
                a_send_monotonic_ns=send_event.monotonic_ns,
                a_send_wall_ns=send_event.wall_ns,
                b_recv_monotonic_ns=delivery_event.monotonic_ns,
                b_recv_wall_ns=delivery_event.wall_ns,
                b_send_monotonic_ns=reply_enqueue.monotonic_ns,
                b_send_wall_ns=reply_enqueue.wall_ns,
                a_recv_monotonic_ns=re_delivered.monotonic_ns,
                a_recv_wall_ns=re_delivered.wall_ns,
                one_way_bound_ns=None,
                source="event_pairing",
                correlation=dict(send_event.detail.get("correlation") or {}),
            )
        )
        if max_samples is not None and len(samples) >= max_samples:
            break
    return tuple(samples)


def _crossings(
    events: Sequence[TraceEvent],
    *,
    sender: str,
    receiver: str,
    request_id: str | None,
) -> list[tuple[TraceEvent, TraceEvent]]:
    """Match ``sender`` enqueues to ``receiver`` deliveries of the same frame."""

    sent: dict[str, list[TraceEvent]] = {}
    for event in events:
        if event.host_id != sender or event.kind is not EventKind.ENQUEUE:
            continue
        if request_id is not None and event.request_id != request_id:
            continue
        digest = event.detail.get("frame_digest")
        if isinstance(digest, str):
            sent.setdefault(digest, []).append(event)
    crossings: list[tuple[TraceEvent, TraceEvent]] = []
    for event in events:
        if event.host_id != receiver or event.kind is not EventKind.DELIVERED:
            continue
        if request_id is not None and event.request_id != request_id:
            continue
        digest = event.detail.get("frame_digest")
        if not isinstance(digest, str):
            continue
        candidates = sent.get(digest)
        if not candidates:
            continue
        crossings.append((candidates.pop(0), event))
    return crossings


def derive_exchange_samples(
    events: Iterable[TraceEvent],
    *,
    request_id: str | None = None,
    max_samples_per_pair: int | None = None,
) -> tuple[ExchangeSample, ...]:
    """Pair every ordered host pair present in a trace, in first-seen order."""

    ordered = sorted(events, key=event_sort_key)
    hosts: list[str] = []
    for event in ordered:
        if event.host_id not in hosts:
            hosts.append(event.host_id)
    samples: list[ExchangeSample] = []
    for index, a_host in enumerate(hosts):
        for b_host in hosts[index + 1 :]:
            samples.extend(
                pair_round_trip_samples(
                    ordered,
                    a_host=a_host,
                    b_host=b_host,
                    request_id=request_id,
                    max_samples=max_samples_per_pair,
                )
            )
            samples.extend(
                pair_round_trip_samples(
                    ordered,
                    a_host=b_host,
                    b_host=a_host,
                    request_id=request_id,
                    max_samples=max_samples_per_pair,
                )
            )
    return tuple(samples)


def pair_one_way_samples(
    events: Iterable[TraceEvent],
    *,
    a_host: str,
    b_host: str,
    one_way_bound_ns: int,
    request_id: str | None = None,
) -> tuple[ExchangeSample, ...]:
    """One-way samples: honest only when the caller supplies a delay bound."""

    if one_way_bound_ns <= 0:
        raise TraceError("one_way_bound_ns_must_be_positive")
    ordered = sorted(events, key=event_sort_key)
    return tuple(
        ExchangeSample(
            a_host=a_host,
            b_host=b_host,
            a_send_monotonic_ns=send_event.monotonic_ns,
            a_send_wall_ns=send_event.wall_ns,
            b_recv_monotonic_ns=delivery_event.monotonic_ns,
            b_recv_wall_ns=delivery_event.wall_ns,
            one_way_bound_ns=one_way_bound_ns,
            source="event_pairing_one_way",
            correlation=dict(send_event.detail.get("correlation") or {}),
        )
        for send_event, delivery_event in _crossings(
            ordered, sender=a_host, receiver=b_host, request_id=request_id
        )
    )


# --------------------------------------------------------------------------- #
# corrected timeline
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TimelineEntry:
    """One event placed in the reference host's frame of reference."""

    event: TraceEvent
    monotonic_ns: int
    wall_ns: int
    delta_ns: int
    reference: bool
    uncertainty_ns: int
    offset: ClockOffset | None

    @property
    def host_id(self) -> str:
        return self.event.host_id

    @property
    def kind(self) -> EventKind:
        return self.event.kind

    def line(self, *, origin_ns: int, previous_ns: int) -> str:
        """One rendered row: offset from the request's first event, and the delta."""

        offset = self.monotonic_ns - origin_ns
        delta = self.monotonic_ns - previous_ns
        marker = "" if self.reference else "~"
        placement = f" placement={self.event.placement_id}" if self.event.placement_id else ""
        uncertainty = "" if self.reference else f" +/-{self.uncertainty_ns / 1e6:.3f}ms"
        detail = _compact_detail(self.event.detail)
        return (
            f"t+{offset / 1e6:9.3f}ms (delta+{delta / 1e6:8.3f}ms) "
            f"{marker}{self.host_id}[seq {self.event.sequence}] "
            f"{str(self.event.kind):<9} {self.event.status:<12}"
            f"{placement}{uncertainty}{detail}"
        )


def correct_events(
    events: Iterable[TraceEvent],
    offsets: Mapping[str, ClockOffset],
    *,
    reference_host: str,
) -> tuple[TimelineEntry, ...]:
    """Place every event in the reference host's frame, or refuse to guess."""

    ordered = sorted(events, key=event_sort_key)
    missing = {event.host_id for event in ordered} - set(offsets) - {reference_host}
    if missing:
        raise ClockOffsetUnavailable(
            "no_estimated_offset_for_hosts:" + ",".join(sorted(missing))
        )
    entries: list[TimelineEntry] = []
    for event in ordered:
        if event.host_id == reference_host:
            entry = TimelineEntry(
                event=event,
                monotonic_ns=event.monotonic_ns,
                wall_ns=event.wall_ns,
                delta_ns=0,
                reference=True,
                uncertainty_ns=0,
                offset=None,
            )
        else:
            offset = offsets[event.host_id]
            entry = TimelineEntry(
                event=event,
                monotonic_ns=offset.to_monotonic(event.monotonic_ns),
                wall_ns=offset.to_wall(event.wall_ns),
                delta_ns=0,
                reference=False,
                uncertainty_ns=offset.uncertainty_ns,
                offset=offset,
            )
        entries.append(entry)
    entries.sort(key=lambda entry: (entry.monotonic_ns, entry.host_id, entry.event.sequence))
    if not entries:
        return ()
    base = entries[0].monotonic_ns
    return tuple(
        TimelineEntry(
            event=entry.event,
            monotonic_ns=entry.monotonic_ns,
            wall_ns=entry.wall_ns,
            delta_ns=entry.monotonic_ns - base,
            reference=entry.reference,
            uncertainty_ns=entry.uncertainty_ns,
            offset=entry.offset,
        )
        for entry in entries
    )


def merge_timeline(
    sampled: Sequence[TraceEvent] | "TraceRun",
    offsets: Mapping[str, ClockOffset] | None = None,
    *,
    reference_host: str | None = None,
) -> tuple[TimelineEntry, ...]:
    """Merge hosts into one ordered timeline, estimating offsets when not given."""

    run = sampled if isinstance(sampled, TraceRun) else None
    events = tuple(sampled.events) if run is not None else tuple(sampled)  # type: ignore[arg-type]
    samples = run.samples if run is not None else ()
    reference = reference_host or (
        run.header.reference_host if run is not None else None
    ) or _first_host(events)
    resolved = dict(offsets) if offsets is not None else estimate_offsets_by_host(
        samples, reference_host=reference
    )
    return correct_events(events, resolved, reference_host=reference)


def _first_host(events: Sequence[TraceEvent]) -> str:
    for event in events:
        return event.host_id
    return DEFAULT_REFERENCE_HOST


def first_failure(
    entries: Sequence[TimelineEntry],
) -> tuple[TimelineEntry, TimelineEntry | None] | None:
    """The earliest non-ok transition and the last ok transition before it."""

    previous_ok: TimelineEntry | None = None
    for entry in entries:
        if entry.event.status not in OK_STATUSES:
            return entry, previous_ok
        previous_ok = entry
    return None


# --------------------------------------------------------------------------- #
# reading a run back
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TraceFile:
    path: Path
    scope: str
    subject: str
    events: tuple[TraceEvent, ...]


@dataclass(frozen=True)
class TraceRun:
    run_dir: Path
    header: RunHeader
    requests: tuple[TraceFile, ...]
    commands: tuple[TraceFile, ...]
    samples: tuple[ExchangeSample, ...]
    events: tuple[TraceEvent, ...]
    samples_source: str = "recorded"

    def events_for_request(self, request_id: str) -> tuple[TraceEvent, ...]:
        for trace_file in self.requests:
            if trace_file.subject == request_id:
                return trace_file.events
        return ()

    def offsets(self, *, reference_host: str | None = None) -> dict[str, ClockOffset]:
        reference = reference_host or self.header.reference_host or _first_host(self.events)
        return estimate_offsets_by_host(self.samples, reference_host=reference)


def load_run(run_dir: str | os.PathLike[str]) -> TraceRun:
    """Read a run directory back, validating schema version and event kinds."""

    base = Path(run_dir)
    header_path = base / RUN_HEADER_FILENAME
    if not header_path.is_file():
        raise TraceError(f"run_header_missing:{header_path}")
    document = json.loads(header_path.read_text(encoding="utf-8"))
    header = RunHeader.from_document(document)
    requests = _load_scope(base / REQUESTS_DIRNAME, "request")
    commands = _load_scope(base / COMMANDS_DIRNAME, "command")
    recorded_samples = _load_samples(base / EXCHANGES_FILENAME)
    seen: set[tuple[str, str, int]] = set()
    events: list[TraceEvent] = []
    for trace_file in (*requests, *commands):
        for event in trace_file.events:
            key = (event.host_id, event.recorder_id, event.sequence)
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
    events.sort(key=event_sort_key)
    if recorded_samples:
        samples: tuple[ExchangeSample, ...] = recorded_samples
        samples_source = "recorded"
    else:
        samples = derive_exchange_samples(events)
        samples_source = "derived_from_events"
    return TraceRun(
        run_dir=base,
        header=header,
        requests=requests,
        commands=commands,
        samples=samples,
        events=tuple(events),
        samples_source=samples_source,
    )


def _load_scope(directory: Path, scope: str) -> tuple[TraceFile, ...]:
    if not directory.is_dir():
        return ()
    files: list[TraceFile] = []
    for path in sorted(directory.glob("*.jsonl")):
        events = tuple(_read_events(path))
        subject = _subject_of(events, path)
        files.append(TraceFile(path=path, scope=scope, subject=subject, events=events))
    return tuple(files)


def _read_events(path: Path) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                events.append(TraceEvent.from_json_line(line))
            except TraceError as error:
                raise type(error)(f"{error}:file={path.name}:line={number}") from error
    return events


def _subject_of(events: Sequence[TraceEvent], path: Path) -> str:
    for event in events:
        if event.request_id and path.parent.name == REQUESTS_DIRNAME:
            return event.request_id
        if event.command_id and path.parent.name == COMMANDS_DIRNAME:
            return event.command_id
    return path.stem


def _load_samples(path: Path) -> tuple[ExchangeSample, ...]:
    if not path.is_file():
        return ()
    samples: list[ExchangeSample] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            samples.append(ExchangeSample.from_document(json.loads(line)))
    return tuple(samples)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render_timeline(run: TraceRun, *, reference_host: str | None = None) -> str:
    """Print each request's events in order with deltas to the previous event."""

    reference = reference_host or run.header.reference_host or _first_host(run.events)
    lines: list[str] = []
    try:
        entries = merge_timeline(run, reference_host=reference)
    except ClockOffsetUnavailable as error:
        return (
            f"timeline unavailable: {error}\n"
            "no clock offset was estimated, so hosts are not interleaved\n"
            + render_per_host(run)
        )
    for trace_file in run.requests:
        subject_events = {id(event) for event in trace_file.events}
        subset = [entry for entry in entries if id(entry.event) in subject_events]
        if not subset:
            continue
        lines.append(_request_header(subset, trace_file))
        origin = subset[0].monotonic_ns
        previous = origin
        for entry in subset:
            lines.append("  " + entry.line(origin_ns=origin, previous_ns=previous))
            previous = entry.monotonic_ns
        lines.append("")
    if not run.requests:
        lines.append("(no request files)")
    return "\n".join(lines).rstrip("\n")


def render_per_host(run: TraceRun) -> str:
    lines: list[str] = []
    for host_id in sorted({event.host_id for event in run.events}):
        host_events = [event for event in run.events if event.host_id == host_id]
        lines.append(f"host {host_id}: {len(host_events)} events (uncorrected, host-local order)")
        base = host_events[0].monotonic_ns
        for event in host_events:
            lines.append(
                f"  t+{(event.monotonic_ns - base) / 1e6:9.3f}ms [seq {event.sequence}] "
                f"{str(event.kind):<9} {event.status:<12}{_compact_detail(event.detail)}"
            )
    return "\n".join(lines)


def _request_header(entries: Sequence[TimelineEntry], trace_file: TraceFile) -> str:
    terminal = [entry for entry in entries if entry.kind is EventKind.TERMINAL]
    outcome = terminal[-1].event.status if terminal else "no-terminal"
    span = entries[-1].monotonic_ns - entries[0].monotonic_ns
    hosts = sorted({entry.host_id for entry in entries})
    return (
        f"request {trace_file.subject}: terminal={outcome} events={len(entries)} "
        f"span={span / 1e6:.3f}ms hosts={','.join(hosts)}"
    )


def summarise(
    run_dir: str | os.PathLike[str],
    *,
    reference_host: str | None = None,
    include_timeline: bool = True,
) -> str:
    """Compact human-readable report: the first failure plus the ordered timeline."""

    run = load_run(run_dir)
    header = run.header
    reference = reference_host or header.reference_host or _first_host(run.events)
    sections: list[str] = []
    sections.append(
        f"run {header.run_id} ({header.schema_version})\n"
        f"  started {header.start_wall_iso}  hosts={len({e.host_id for e in run.events})}  "
        f"git={header.git_head or 'unknown'}\n"
        f"  host_id={header.host_id or 'unknown'}  boot_id={header.boot_id}  pid={os.getpid()}\n"
        f"  package_digest={header.package_digest}"
        + (
            f"\n  product_package_digest={header.product_package_digest}"
            if header.product_package_digest
            else ""
        )
        + f"\n  events={len(run.events)} requests={len(run.requests)} "
        f"commands={len(run.commands)} exchanges={len(run.samples)}"
    )
    sections.append(_clock_section(run, reference))
    failure = None
    try:
        entries = merge_timeline(run, reference_host=reference)
    except ClockOffsetUnavailable:
        entries = ()
    if entries:
        failure = first_failure(entries)
    sections.append(_failure_section(entries, failure))
    if include_timeline:
        sections.append(render_timeline(run, reference_host=reference))
    return "\n".join(section for section in sections if section) + "\n"


def _clock_section(run: TraceRun, reference: str) -> str:
    try:
        offsets = run.offsets(reference_host=reference)
    except (ClockOffsetUnavailable, TraceError) as error:
        return f"clock correction: not applied ({error}) - hosts are NOT interleaved"
    if not offsets:
        return (
            f"clock correction: no paired exchange found for reference {reference}; "
            "hosts are NOT interleaved"
        )
    lines = [f"clock correction: reference {reference} (monotonic and wall offsets, +/- uncertainty)"]
    lines.append(f"  samples: {run.samples_source} (n={len(run.samples)})")
    for host in sorted(offsets):
        lines.append("  " + offsets[host].describe())
    return "\n".join(lines)


def _failure_section(
    entries: Sequence[TimelineEntry],
    failure: tuple[TimelineEntry, TimelineEntry | None] | None,
) -> str:
    if failure is None:
        if not entries:
            return "first failure: undetermined (no aligned timeline)"
        lines = ["first failure: none (every transition is ok)"]
        request_ids = {
            entry.event.request_id for entry in entries
            if entry.event.request_id is not None
        }
        for request_id in sorted(request_ids):
            lines.append(_outcome_line(entries, request_id=request_id))
        return "\n".join(lines)
    failed, previous_ok = failure
    event = failed.event
    lines = [
        "FIRST FAILURE",
        f"  request={event.request_id or '-'} command={event.command_id or '-'} "
        f"placement={event.placement_id or '-'}",
        f"  host={event.host_id} pid={event.pid} kind={str(event.kind)} status={event.status} "
        f"at t+{failed.delta_ns / 1e6:.3f}ms (corrected, +/-{failed.uncertainty_ns / 1e6:.3f}ms)",
        f"  detail={json.dumps(dict(event.detail), sort_keys=True)}",
    ]
    if previous_ok is not None:
        lines.append(
            f"  last ok transition: {str(previous_ok.kind)} on {previous_ok.host_id} "
            f"at t+{previous_ok.delta_ns / 1e6:.3f}ms "
            f"({(failed.monotonic_ns - previous_ok.monotonic_ns) / 1e6:.3f}ms earlier)"
        )
    else:
        lines.append("  last ok transition: none (failure is the first transition)")
    lines.append(_outcome_line(entries, failed))
    return "\n".join(lines)


def _outcome_line(
    entries: Sequence[TimelineEntry],
    failed: TimelineEntry | None = None,
    *,
    request_id: str | None = None,
) -> str:
    """How the request itself ended, so a teardown artifact is not mistaken for it."""

    if failed is not None:
        request_id = failed.event.request_id
    terminals = [
        entry
        for entry in entries
        if entry.kind is EventKind.TERMINAL
        and (request_id is None or entry.event.request_id == request_id)
    ]
    if not terminals:
        return "  request outcome: no terminal transition recorded" + (
            f" (request={request_id})" if failed is None else ""
        )
    outcome = terminals[-1]
    if failed is None:
        return (
            f"  request outcome: {outcome.event.status} (request={request_id}, terminal at "
            f"t+{outcome.delta_ns / 1e6:.3f}ms)"
        )
    difference = outcome.monotonic_ns - failed.monotonic_ns
    relation = (
        f"{difference / 1e6:.3f}ms after this failure"
        if difference >= 0
        else f"{abs(difference) / 1e6:.3f}ms before this failure"
    )
    if outcome.event.status in OK_STATUSES:
        return (
            f"  request outcome: {outcome.event.status} (terminal at "
            f"t+{outcome.delta_ns / 1e6:.3f}ms, {relation}) - this failure did not "
            "stop the request; read it as a post-completion or teardown observation"
        )
    return (
        f"  request outcome: {outcome.event.status} (terminal at "
        f"t+{outcome.delta_ns / 1e6:.3f}ms, {relation})"
    )


def _compact_detail(detail: Mapping[str, Any]) -> str:
    if not detail:
        return ""
    parts = [f"{key}={_short(value)}" for key, value in sorted(detail.items())]
    return "  " + " ".join(parts)


def _short(value: Any, *, limit: int = 48) -> str:
    rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    if len(rendered) > limit:
        return rendered[: limit - 3] + "..."
    return rendered


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_testbed",
        description="Summarise a testbed lifecycle trace run directory.",
    )
    parser.add_argument("run_dir", help="run directory written by the testbed tracer")
    parser.add_argument("--reference-host", default=None)
    parser.add_argument("--no-timeline", action="store_true")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    print(
        summarise(
            arguments.run_dir,
            reference_host=arguments.reference_host,
            include_timeline=not arguments.no_timeline,
        ),
        end="",
    )
    return 0


__all__ = [
    "COMMANDS_DIRNAME",
    "ClockOffset",
    "ClockOffsetUnavailable",
    "ClockStamp",
    "DEFAULT_REFERENCE_HOST",
    "EXCHANGE_SCHEMA_VERSION",
    "EventKind",
    "ExchangeSample",
    "HostClock",
    "HostRecord",
    "JSONL",
    "LIFECYCLE_ORDER",
    "OK_STATUSES",
    "REQUESTS_DIRNAME",
    "RUN_HEADER_FILENAME",
    "RunHeader",
    "TRACE_SCHEMA_VERSION",
    "TimelineEntry",
    "TraceDetailNotSerialisable",
    "TraceError",
    "TraceEvent",
    "TraceFile",
    "TraceFileWriter",
    "TraceRecorder",
    "TraceRun",
    "TraceRunDirectory",
    "TraceSchemaVersionError",
    "UnknownTraceEventKind",
    "canonical_event_kinds",
    "compute_package_digest",
    "correct_events",
    "derive_exchange_samples",
    "estimate_clock_offset",
    "estimate_offsets_by_host",
    "event_sort_key",
    "first_failure",
    "iso_utc",
    "load_run",
    "main",
    "merge_timeline",
    "pair_one_way_samples",
    "pair_round_trip_samples",
    "read_boot_id",
    "read_git_head",
    "render_per_host",
    "render_timeline",
    "summarise",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
