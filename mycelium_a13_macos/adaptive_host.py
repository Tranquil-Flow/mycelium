# SPDX-License-Identifier: AGPL-3.0-only
"""A16 optional adaptive host-resource mode (macOS first, deterministic core).

This module decides, from coarse host signals and explicit owner controls,
whether this Mac may admit *new* Mycelium work (modes ``normal`` -> ``reduced``
-> ``drain`` -> ``withdraw``).  It is off by default, reads nothing while off,
never inspects applications (no process names, window titles or bundle IDs),
never softens the A13 power/thermal gates, and never claims A6 recovery,
A9 qualification or a physical result.  Timings are owner-configurable; the
defaults are the draft's *unapproved targets* (``DEFAULT_TIMINGS``), not
measurements.  See ``status/DRAFT-A16-ADAPTIVE-HOST-RESOURCES-20261018.md``.
"""
from __future__ import annotations

import copy
import inspect
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping

from mycelium_router import adaptive_overlay as _overlay

POLICY_FILENAME: Final = "adaptive-host-policy.json"
MAX_POLICY_BYTES: Final = 4096
MODES: Final = ("normal", "reduced", "drain", "withdraw")
_RANK: Final = {mode: index for index, mode in enumerate(MODES)}

REASON_CATEGORIES: Final = frozenset({
    "none", "owner_game_mode", "owner_stop", "host_memory_pressure",
    "host_cpu_saturation",
})
PUBLICATION_KEYS: Final = frozenset({
    "adaptive_enabled", "mode", "reason", "generation", "issued_at",
    "expires_at", "new_admissions_allowed", "capacity_scale",
})
_SCALE_RANK: Final = {"zero": 0, "committed_only": 1, "reduced": 2, "full": 3}

# Draft option A values; option B (owner answer 3) makes every one of them
# owner-configurable within the validated bounds below.
DEFAULT_TIMINGS: Final[dict[str, float]] = {
    "sample_interval_s": 2,
    "stale_after_s": 6,          # derived: 3 x sample interval
    "game_drain_s": 10,
    "pressure_drain_s": 15,
    "zero_capacity_s": 30,
    "withdraw_s": 120,
    "reduced_dwell_s": 30,
    "drain_dwell_s": 60,
    "recovery_s": 120,
    "cpu_sustain_s": 15,         # provisional: equals pressure_drain_s
    "authority_observations": 2,
}
_TIMING_MIN: Final = 1
_TIMING_MAX: Final = 3600

DEFAULT_POLICY: Final[dict[str, Any]] = {
    "schema": 1,
    "enabled": False,
    "game_mode": False,
    "reserve_bytes": 0,              # owner-set headroom reserve; 0 = none
    "cpu_saturation_fraction": None,  # None = CPU signal is never acted on
    "timings": {},
}
_POLICY_KEYS: Final = frozenset(DEFAULT_POLICY)
# Optional (R26 'Adaptive sharing'): whether the *automatic* controller runs.  Absent = the
# value of ``enabled`` (every file written before this key existed), so DEFAULT_POLICY and the
# file schema are unchanged.  ``enabled`` stays "the controller exists" (automatic or a manual
# Pause sharing is on).
_OPTIONAL_POLICY_KEYS: Final = frozenset({"automatic"})


def policy_automatic(policy: Mapping[str, Any]) -> bool:
    """Whether the automatic (memory/CPU) controller is on for ``policy``."""
    return bool(policy.get("automatic", policy["enabled"]))
_SAMPLE_KEYS: Final = frozenset({
    "observed_at", "pressure", "pressure_crosscheck", "available_bytes",
    "cpu_utilization",
})
_PRESSURE: Final = {"normal": 0, "warn": 1, "critical": 2}
_MAX_BYTES: Final = 1 << 50


class AdaptiveHostError(ValueError):
    """Raised with one bounded, content-free ``code``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


# ------------------------------------------------------------------ policy
def validate_policy(policy: object) -> dict[str, Any]:
    """Return a normalized copy of ``policy`` or raise ``adaptive_policy_invalid``."""
    bad = AdaptiveHostError("adaptive_policy_invalid")
    if not isinstance(policy, Mapping) or set(policy) - _OPTIONAL_POLICY_KEYS != _POLICY_KEYS:
        raise bad
    if "automatic" in policy and (
        type(policy["automatic"]) is not bool or (policy["automatic"] and policy["enabled"] is not True)
    ):
        raise bad
    if policy["schema"] != 1 or type(policy["schema"]) is not int:
        raise bad
    if type(policy["enabled"]) is not bool or type(policy["game_mode"]) is not bool:
        raise bad
    reserve = policy["reserve_bytes"]
    if type(reserve) is not int or not 0 <= reserve <= _MAX_BYTES:
        raise bad
    fraction = policy["cpu_saturation_fraction"]
    if fraction is not None and not (_is_number(fraction) and 0 < fraction <= 1):
        raise bad
    timings = policy["timings"]
    if not isinstance(timings, Mapping) or not set(timings) <= set(DEFAULT_TIMINGS):
        raise bad
    for key, value in timings.items():
        if not _is_number(value) or not _TIMING_MIN <= value <= _TIMING_MAX:
            raise bad
    if "authority_observations" in timings and (
        type(timings["authority_observations"]) is not int
        or not 2 <= timings["authority_observations"] <= 10
    ):
        raise bad
    merged = {**DEFAULT_TIMINGS, **timings}
    if merged["stale_after_s"] < merged["sample_interval_s"]:
        raise bad
    chain = ("game_drain_s", "pressure_drain_s", "zero_capacity_s", "withdraw_s")
    if any(merged[a] > merged[b] for a, b in zip(chain, chain[1:])):
        raise bad
    return copy.deepcopy(dict(policy)) | {"timings": dict(timings)}


def resolve_timings(policy: Mapping[str, Any]) -> dict[str, float]:
    """Full timing table: draft defaults overlaid by the owner's values."""
    return {**DEFAULT_TIMINGS, **dict(policy.get("timings") or {})}


def read_adaptive_policy(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Load the adaptive policy; a missing file is the migrated *off* default."""
    if sys.platform == "win32":
        return copy.deepcopy(DEFAULT_POLICY)
    path = Path(root) / POLICY_FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_POLICY)
    except OSError as error:
        raise AdaptiveHostError("adaptive_policy_invalid") from error
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_POLICY_BYTES
        ):
            raise AdaptiveHostError("adaptive_policy_invalid")
        raw = os.read(fd, MAX_POLICY_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise AdaptiveHostError("adaptive_policy_invalid") from error
    return validate_policy(value)


def write_adaptive_policy(
    root: str | os.PathLike[str], policy: Mapping[str, Any], *, owner_authorized: object
) -> None:
    """Persist ``policy``; refuses unless the caller proves owner authority (``True``)."""
    if owner_authorized is not True:
        raise AdaptiveHostError("adaptive_toggle_unauthorized")
    normalized = validate_policy(policy)
    raw = json.dumps(normalized, sort_keys=True).encode("utf-8")
    path = Path(root) / POLICY_FILENAME
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# ----------------------------------------------------------------- signals
def parse_sample(raw: object) -> dict[str, Any]:
    """Strict sample schema; any extra (e.g. app-identity) key is refused."""
    bad = AdaptiveHostError("adaptive_sample_invalid")
    if not isinstance(raw, Mapping) or not set(raw) <= _SAMPLE_KEYS:
        raise bad
    if not {"observed_at", "pressure"} <= set(raw) or not _is_number(raw["observed_at"]):
        raise bad
    if raw["pressure"] not in _PRESSURE and raw["pressure"] != "unknown":
        raise bad
    cross = raw.get("pressure_crosscheck")
    if cross is not None and cross not in _PRESSURE and cross != "unknown":
        raise bad
    available = raw.get("available_bytes")
    if available is not None and (type(available) is not int or not 0 <= available <= _MAX_BYTES):
        raise bad
    cpu = raw.get("cpu_utilization")
    if cpu is not None and not (_is_number(cpu) and 0 <= cpu <= 1):
        raise bad
    return dict(raw)


def classify_sample(
    raw: object,
    *,
    now: float,
    timings: Mapping[str, float],
    footprint_bytes: int,
    reserve_bytes: int,
) -> str:
    """``healthy`` | ``moderate`` | ``critical`` | ``unknown`` (never healthy by default)."""
    try:
        sample = parse_sample(raw)
    except AdaptiveHostError:
        return "unknown"
    age = now - sample["observed_at"]
    if age < 0 or age > timings["stale_after_s"]:
        return "unknown"
    level = sample["pressure"]
    if level == "unknown":
        return "unknown"
    cross = sample.get("pressure_crosscheck")
    if cross == "unknown":
        return "unknown"
    if cross is not None and abs(_PRESSURE[level] - _PRESSURE[cross]) >= 2:
        return "unknown"  # contradictory independent probes
    if level == "critical":
        return "critical"
    available = sample.get("available_bytes")
    if footprint_bytes > 0 and available is None and level == "normal":
        return "unknown"  # headroom relative to the admitted footprint is unmeasured
    if level == "warn":
        return "moderate"
    if footprint_bytes > 0 and available < footprint_bytes + reserve_bytes:
        return "moderate"
    if available is not None and available < reserve_bytes:
        return "moderate"
    return "healthy"


def _default_runner(argv: list[str]) -> str:
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=2.5, check=True, text=True,
    ).stdout


def sample_host_memory(
    runner: Callable[[list[str]], str] = _default_runner,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Read-only coarse memory signal; failure yields ``pressure: unknown``.

    Pressure comes from ``kern.memorystatus_vm_pressure_level`` (1/2/4); headroom
    is free+inactive+speculative pages from ``vm_stat``.  Free pages alone are
    never used.  No process, application or window information is read.
    """
    result: dict[str, Any] = {"observed_at": clock(), "pressure": "unknown"}
    try:
        level = runner(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    except Exception:
        return result
    result["pressure"] = {"1": "normal", "2": "warn", "4": "critical"}.get(level, "unknown")
    if result["pressure"] == "unknown":
        return result
    try:
        text = runner(["vm_stat"])
        page = int(text.split("page size of", 1)[1].split("bytes", 1)[0])
        pages = 0
        for label in ("Pages free", "Pages inactive", "Pages speculative"):
            line = next(row for row in text.splitlines() if row.startswith(label + ":"))
            pages += int(line.split(":", 1)[1].strip().rstrip("."))
        if page > 0 and pages >= 0:
            result["available_bytes"] = pages * page
    except Exception:
        pass
    try:  # secondary, independent view: only a parseable value is used, prose drift is "absent"
        cross = parse_memory_pressure(runner(["memory_pressure"]))
    except Exception:
        cross = None
    if cross is not None:
        result["pressure_crosscheck"] = cross
    return result


# Provisional, unmeasured cross-check bands (flag only extreme contradictions with the kernel
# level; the middle band can never contradict because ``classify_sample`` needs a 2-step gap).
CROSSCHECK_NORMAL_FREE_PERCENT: Final = 50
CROSSCHECK_CRITICAL_FREE_PERCENT: Final = 5
_FREE_PERCENT = re.compile(r"System-wide memory free percentage:\s*(\d{1,3})%")


def parse_memory_pressure(text: object) -> str | None:
    """Coarse level from ``memory_pressure`` output, or ``None`` if the line is not found."""
    match = _FREE_PERCENT.search(text) if isinstance(text, str) else None
    if match is None or int(match.group(1)) > 100:
        return None
    free = int(match.group(1))
    if free >= CROSSCHECK_NORMAL_FREE_PERCENT:
        return "normal"
    if free <= CROSSCHECK_CRITICAL_FREE_PERCENT:
        return "critical"
    return "warn"


# Provisional, unmeasured Linux PSI bands over the ``avg10`` window (percent of the last 10 s that
# tasks stalled on memory).  Like the macOS cross-check bands they are not a measurement; an
# unreadable or malformed file is ``unknown``, never ``normal``.
PSI_WARN_SOME_AVG10: Final = 10.0
PSI_WARN_FULL_AVG10: Final = 1.0
PSI_CRITICAL_SOME_AVG10: Final = 40.0
PSI_CRITICAL_FULL_AVG10: Final = 5.0
LINUX_MEMINFO_PATH: Final = "/proc/meminfo"
LINUX_PSI_MEMORY_PATH: Final = "/proc/pressure/memory"
_MAX_PROC_BYTES: Final = 16384
_PSI_ROW = re.compile(r"^(some|full) avg10=(\d{1,3}(?:\.\d{1,2})?) avg60=\S+ avg300=\S+ total=\d+$")
_MEMINFO_KB = re.compile(r"^(MemTotal|MemAvailable):\s+(\d+) kB$", re.MULTILINE)


def _read_proc_text(path: str) -> str:
    with open(path, "r", encoding="ascii", errors="strict") as handle:
        return handle.read(_MAX_PROC_BYTES)


def parse_linux_psi(text: object) -> str | None:
    """``normal`` | ``warn`` | ``critical`` from ``/proc/pressure/memory``, or ``None`` if malformed."""
    if not isinstance(text, str):
        return None
    rows: dict[str, float] = {}
    for line in text.splitlines():
        match = _PSI_ROW.match(line.strip())
        if match is not None:
            rows[match.group(1)] = float(match.group(2))
    if set(rows) != {"some", "full"} or any(not 0 <= value <= 100 for value in rows.values()):
        return None
    if rows["some"] >= PSI_CRITICAL_SOME_AVG10 or rows["full"] >= PSI_CRITICAL_FULL_AVG10:
        return "critical"
    if rows["some"] >= PSI_WARN_SOME_AVG10 or rows["full"] >= PSI_WARN_FULL_AVG10:
        return "warn"
    return "normal"


def sample_linux_memory(
    read: Callable[[str], str] = _read_proc_text,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Linux counterpart of :func:`sample_host_memory`, same sample shape and levels.

    Pressure is PSI ``some``/``full`` ``avg10`` from ``/proc/pressure/memory`` (an unreadable or
    malformed file, or a kernel without PSI, yields ``unknown``).  Headroom is ``MemAvailable``
    from ``/proc/meminfo`` (never ``MemFree`` alone); its share of ``MemTotal`` is the independent
    cross-check using the macOS bands.  No process or application information is read.
    """
    result: dict[str, Any] = {"observed_at": clock(), "pressure": "unknown"}
    try:
        level = parse_linux_psi(read(LINUX_PSI_MEMORY_PATH))
    except Exception:
        return result
    if level is None:
        return result
    result["pressure"] = level
    try:
        fields = {name: int(kb) for name, kb in _MEMINFO_KB.findall(read(LINUX_MEMINFO_PATH))}
        available, total = fields["MemAvailable"], fields["MemTotal"]
        if 0 <= available <= total and total > 0:
            result["available_bytes"] = available * 1024
            cross_percent = available * 100 // total
            if cross_percent >= CROSSCHECK_NORMAL_FREE_PERCENT:
                result["pressure_crosscheck"] = "normal"
            elif cross_percent <= CROSSCHECK_CRITICAL_FREE_PERCENT:
                result["pressure_crosscheck"] = "critical"
            else:
                result["pressure_crosscheck"] = "warn"
    except Exception:
        pass
    return result


def read_cpu_ticks() -> tuple[int, int, int, int]:
    """Cumulative (user, system, idle, nice) ticks via ``host_statistics`` (macOS, unprivileged)."""
    import ctypes
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    libc.mach_host_self.restype = ctypes.c_uint
    ticks = (ctypes.c_uint * 4)()
    count = ctypes.c_uint(4)
    status = libc.host_statistics(libc.mach_host_self(), 3, ticks, ctypes.byref(count))  # HOST_CPU_LOAD_INFO
    if status != 0 or count.value != 4:
        raise OSError("host_statistics")
    return int(ticks[0]), int(ticks[1]), int(ticks[2]), int(ticks[3])


class CpuMeter:
    """Aggregate host CPU utilization from tick deltas; ``None`` means unknown, never zero."""

    def __init__(self, read: Callable[[], tuple[int, ...]] = read_cpu_ticks) -> None:
        self._read = read
        self._previous: tuple[int, ...] | None = None

    def utilization(self) -> float | None:
        try:
            current = tuple(int(value) for value in self._read())
        except Exception:
            self._previous = None
            return None
        previous, self._previous = self._previous, current
        if previous is None or len(current) != 4 or len(previous) != 4:
            return None
        deltas = [now - before for now, before in zip(current, previous)]
        total = sum(deltas)
        if any(delta < 0 for delta in deltas) or total <= 0:
            return None
        return max(0.0, min(1.0, 1.0 - deltas[2] / total))


class HostSampler:
    """The default live sampler: memory signals plus aggregate CPU.  GPU is not collected (unknown).

    Memory comes from ``sample_linux_memory`` on Linux and ``sample_host_memory`` on macOS;
    Windows has no sampler and reports ``pressure: unknown``.  CPU ticks are macOS-only for now.
    """

    def __init__(
        self,
        *,
        runner: Callable[[list[str]], str] = _default_runner,
        clock: Callable[[], float] = time.monotonic,
        meter: CpuMeter | None = None,
        platform: str | None = None,
        file_reader: Callable[[str], str] = _read_proc_text,
    ) -> None:
        self._platform = sys.platform if platform is None else platform
        self._file_reader = file_reader
        self._runner = runner
        self._clock = clock
        self._meter = meter if meter is not None else CpuMeter()

    def __call__(self) -> dict[str, Any]:
        if self._platform.startswith("linux"):
            sample = sample_linux_memory(self._file_reader, self._clock)
        else:  # macOS tools; elsewhere (Windows) the runner fails and the sample stays ``unknown``
            sample = sample_host_memory(self._runner, self._clock)
        cpu = self._meter.utilization()
        if cpu is not None:
            sample["cpu_utilization"] = cpu
        return sample


def sampler_for(app: Any) -> Callable[[], Mapping[str, Any]]:
    """``app._adaptive_sampler`` when set (tests, overrides), else one ``HostSampler`` per app."""
    override = getattr(app, "_adaptive_sampler", None)
    if override is not None:
        return override
    sampler = getattr(app, "_adaptive_host_sampler", None)
    if sampler is None:
        sampler = HostSampler()
        try:
            app._adaptive_host_sampler = sampler
        except AttributeError:
            pass
    return sampler


# --------------------------------------------------------------- capacity
def effective_capacity(
    mode: str,
    *,
    qualified: int,
    loaded: int,
    consent: int,
    available_bytes: int | None,
    reserve_bytes: int,
    per_member_kv_bytes: int,
    committed_members: int,
) -> dict[str, int]:
    """Runtime overlay: min(qualified profile, loaded stage, consent, headroom)."""
    if mode not in _RANK or per_member_kv_bytes <= 0:
        raise AdaptiveHostError("adaptive_capacity_invalid")
    committed = max(0, committed_members)
    if mode == "withdraw":
        concurrency = 0
    elif mode == "drain":
        concurrency = committed
    else:
        base = max(0, min(qualified, loaded, consent))
        if available_bytes is None:
            fit = 0 if mode == "reduced" else base
        else:
            fit = max(0, available_bytes - reserve_bytes) // per_member_kv_bytes
        concurrency = max(committed, min(base, committed + fit))
    new = 0 if mode in ("drain", "withdraw") else max(0, concurrency - committed)
    return {
        "concurrency": concurrency,
        "new_admissions": new,
        "kv_budget_bytes": new * per_member_kv_bytes,
    }


def plan_new_admissions(
    capacity: Mapping[str, int],
    requests: list[Mapping[str, Any]],
    *,
    committed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Admit whole requests within the overlay; never touch committed state."""
    floor = sum(int(entry["kv_reserved_bytes"]) for entry in committed.values())
    slots, remaining, total = capacity["new_admissions"], capacity["kv_budget_bytes"], capacity["kv_budget_bytes"]
    accepted: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    for request in requests:
        need = int(request["kv_bytes"])
        if need > total:
            refused.append({"id": request["id"], "reason": "adaptive_context_exceeds_budget"})
        elif slots <= 0 or need > remaining:
            refused.append({"id": request["id"], "reason": "adaptive_capacity_reduced"})
        else:
            accepted.append(dict(request))
            slots -= 1
            remaining -= need
    return {"accepted": accepted, "refused": refused, "committed_floor_bytes": floor}


def allocate_shared_headroom(available_bytes: int, wants: Mapping[str, int]) -> dict[str, int]:
    """Split one host's free RAM across co-located nodes without double counting."""
    remaining = max(0, available_bytes)
    shares: dict[str, int] = {}
    for node in sorted(wants):
        shares[node] = min(max(0, wants[node]), remaining)
        remaining -= shares[node]
    return shares


def inflight_disposition(
    *,
    mode: str,
    deadline_remaining_s: float,
    cancelled: bool,
    successor: Mapping[str, bool] | None,
    can_finish: bool,
    critical: bool = False,
) -> str:
    """``finish`` | ``cancel`` | ``a6_handoff`` | ``abort_terminal``; never a fake success."""
    if cancelled:
        return "cancel"  # A4 cancellation keeps precedence
    if mode not in ("drain", "withdraw"):
        return "finish"
    if deadline_remaining_s <= 0:
        return "abort_terminal"  # never extend A4/A6 deadlines
    if can_finish and not critical:
        return "finish"
    required = ("qualified", "compatible", "fence_ready", "checkpoint_committed")
    if successor is not None and all(successor.get(name) is True for name in required):
        return "a6_handoff"
    return "abort_terminal"


def publication_admissible(
    current: Mapping[str, Any], candidate: Mapping[str, Any], *, now: float
) -> str | None:
    """Reject stale, future, expired or late-positive publications."""
    stale = "adaptive_publication_stale"
    try:
        if candidate["generation"] != current["generation"] or now > candidate["expires_at"]:
            return stale
        if _SCALE_RANK[candidate["capacity_scale"]] > _SCALE_RANK[current["capacity_scale"]]:
            return stale
        if candidate["new_admissions_allowed"] and not current["new_admissions_allowed"]:
            return stale
    except (KeyError, TypeError):
        return stale
    return None


# -------------------------------------------------------------- controller
_MARKER_REASONS = frozenset(
    {"none", "owner_game_mode", "owner_stop", "host_memory_pressure", "host_cpu_saturation"})


class AdaptiveHostController:
    """Clock-injected state machine; all inputs are coarse and local."""

    def __init__(
        self,
        policy: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        footprint_bytes: Callable[[], int] = lambda: 0,
        inflight: Callable[[], int] = lambda: 0,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._footprint = footprint_bytes
        self._inflight = inflight
        self.stopped = False
        self.mode = "normal"
        self.reason = "none"
        self.generation = 0
        self._entered_at = clock()
        self._last_tick: float | None = None
        self._classification = "unknown"
        self._available: int | None = None
        self._healthy_since: float | None = None
        self._moderate_since: float | None = None
        self._cpu_since: float | None = None
        self._authority: list[tuple[int, float]] = []
        self._cleanup_complete = False
        self._requalified = False
        # Set when this Mac pauses (drain/withdraw); cleared once the route is seen ready again, so
        # the page can say "Rejoining" between a released fence and a ready route (derived, no wire).
        self.rejoin_pending = False
        self._policy = validate_policy(policy)
        self._timings = resolve_timings(self._policy)

    # -- policy / owner controls
    @property
    def enabled(self) -> bool:
        return self._policy["enabled"]

    @property
    def automatic(self) -> bool:
        """Adaptive sharing: automatic reduce/drain from memory and CPU signals."""
        return policy_automatic(self._policy)

    def apply_policy(self, policy: Mapping[str, Any]) -> None:
        with self._lock:
            self._policy = validate_policy(policy)
            self._timings = resolve_timings(self._policy)
            if not self.enabled:
                self._reset()
            elif self._policy["game_mode"]:
                self._escalate("drain", "owner_game_mode")

    @property
    def critical(self) -> bool:
        """Whether the latest sample classified as critical memory pressure."""
        return self._classification == "critical"

    @property
    def sample_interval_s(self) -> float:
        return self._timings["sample_interval_s"]

    def recovery_ready(self) -> bool:
        """True once only authority evidence is missing for leaving a non-normal mode."""
        with self._lock:
            if not self.enabled or self.mode == "normal" or self.stopped or self._policy["game_mode"]:
                return False
            now = self._clock()
            return (
                self._classification == "healthy" and self._healthy_since is not None
                and now - self._healthy_since >= self._timings["recovery_s"]
                and now - self._entered_at >= self._dwell()
            )

    def needs_requalification(self) -> bool:
        with self._lock:
            return self.mode == "withdraw" and not self._requalified

    def stop_serving(self) -> None:
        with self._lock:
            self.stopped = True
            if self.enabled:
                self._escalate("drain", "owner_stop")

    def resume_serving(self) -> None:
        with self._lock:
            self.stopped = False

    def restore_withdrawn(self, reason: object) -> None:
        """Start fenced after a restart that follows a withdraw (persisted marker).

        The fresh process holds no in-flight work and no model it must still drain, so cleanup
        counts as complete; everything else is the ordinary withdraw exit: a full healthy
        ``recovery_s`` window measured from *this* process's samples, the dwell, fresh
        authority observations and requalification.  An unknown reason is recorded as ``none``.
        """
        with self._lock:
            if self.mode == "withdraw":
                return
            self._enter("withdraw", reason if reason in _MARKER_REASONS else "none")
            self._cleanup_complete = True
            self._healthy_since = None

    def mark_cleanup_complete(self) -> None:
        with self._lock:
            self._cleanup_complete = True

    def mark_requalified(self, generation: int) -> None:
        with self._lock:
            if generation == self.generation:
                self._requalified = True

    def record_authority_observation(self, generation: int, *, observed_at: float) -> None:
        with self._lock:
            now = self._clock()
            if generation != self.generation or not _is_number(observed_at):
                return
            if observed_at > now or now - observed_at > self._timings["stale_after_s"]:
                return
            if any(seen == observed_at for _g, seen in self._authority):
                return
            self._authority.append((generation, observed_at))

    # -- transitions
    def _reset(self) -> None:
        if self.mode != "normal" or self.reason != "none":
            self.generation += 1
        self.mode, self.reason = "normal", "none"
        self._entered_at = self._clock()
        self._healthy_since = self._moderate_since = self._cpu_since = None
        self._authority.clear()
        self._cleanup_complete = self._requalified = False
        self.rejoin_pending = False

    def clear_rejoin(self) -> None:
        with self._lock:
            self.rejoin_pending = False

    def _enter(self, mode: str, reason: str) -> None:
        self.generation += 1
        self.mode, self.reason = mode, reason
        if mode in ("drain", "withdraw"):
            self.rejoin_pending = True
        self._entered_at = self._clock()
        self._authority.clear()
        self._requalified = False
        if mode == "drain":
            self._cleanup_complete = False

    def _escalate(self, mode: str, reason: str) -> None:
        if _RANK[mode] > _RANK[self.mode] or (mode == self.mode and reason != self.reason and mode != "normal"):
            self._enter(mode, reason)

    def _dwell(self) -> float:
        return self._timings["reduced_dwell_s" if self.mode == "reduced" else "drain_dwell_s"]

    def _fresh_authority(self, now: float) -> int:
        stale = self._timings["stale_after_s"]
        return sum(1 for gen, seen in self._authority if gen == self.generation and 0 <= now - seen <= stale)

    # -- main tick
    def observe(self, raw: object) -> dict[str, Any]:
        with self._lock:
            if not self.enabled:
                return self.status()  # no monitoring or action when A16 is off
            now = self._clock()
            self._last_tick = now
            timings = self._timings
            if self.automatic:
                cls = classify_sample(
                    raw, now=now, timings=timings, footprint_bytes=int(self._footprint()),
                    reserve_bytes=self._policy["reserve_bytes"],
                )
            else:  # Adaptive sharing off: no signal is read; only Pause sharing / Stop act
                raw, cls = None, "healthy"
            self._classification = cls
            available = raw.get("available_bytes") if isinstance(raw, Mapping) else None
            self._available = available if cls != "unknown" and type(available) is int else None
            cpu_sustained = self._track_cpu(raw, cls, now)
            self._healthy_since = (self._healthy_since or now) if cls == "healthy" and not cpu_sustained else None
            memory_moderate = cls == "moderate"
            self._moderate_since = (self._moderate_since or now) if memory_moderate else None

            if self.stopped:
                self._escalate("drain", "owner_stop")
            if self._policy["game_mode"]:
                self._escalate("drain", "owner_game_mode")
            if cls == "critical":
                self._escalate("drain", "host_memory_pressure")
            elif memory_moderate:
                if now - self._moderate_since >= timings["pressure_drain_s"]:
                    self._escalate("drain", "host_memory_pressure")
                else:
                    self._escalate("reduced", "host_memory_pressure")
            elif cpu_sustained:
                self._escalate("reduced", "host_cpu_saturation")

            self._progress(now)
            return self.status()

    def _track_cpu(self, raw: object, cls: str, now: float) -> bool:
        fraction = self._policy["cpu_saturation_fraction"]
        cpu = raw.get("cpu_utilization") if isinstance(raw, Mapping) else None
        if fraction is None or cls == "unknown" or not _is_number(cpu) or cpu < fraction:
            self._cpu_since = None
            return False
        self._cpu_since = self._cpu_since if self._cpu_since is not None else now
        return now - self._cpu_since >= self._timings["cpu_sustain_s"]

    def _pressure_persists(self) -> bool:
        return self.stopped or self._policy["game_mode"] or self._classification in ("critical", "moderate")

    def _progress(self, now: float) -> None:
        if self.mode == "drain" and self._pressure_persists():
            if int(self._inflight()) == 0 and self._cleanup_complete:
                self._enter("withdraw", self.reason)
            return
        if self.mode == "normal" or self._pressure_persists() or self._classification != "healthy":
            return
        if self._healthy_since is None or self.stopped or self._policy["game_mode"]:
            return
        if now - self._healthy_since < self._timings["recovery_s"]:
            return
        if now - self._entered_at < self._dwell():
            return
        if self._fresh_authority(now) < self._timings["authority_observations"]:
            return
        if self.mode == "withdraw" and not self._requalified:
            return
        self._enter("normal", "none")

    # -- outputs
    def _stale(self, now: float) -> bool:
        return self._last_tick is None or now - self._last_tick > self._timings["stale_after_s"]

    def admission(self, hard_gate_reason: str | None = None) -> str | None:
        """Why new work must be refused now, else ``None`` (A13 hard gates first)."""
        if hard_gate_reason:
            return hard_gate_reason
        with self._lock:
            if not self.enabled:
                return None
            if self.mode == "withdraw":
                return "adaptive_host_withdrawn"
            if self.mode == "drain":
                return "adaptive_host_draining"
            if not self.automatic:
                return None
            if self._stale(self._clock()):
                return "adaptive_controller_stale"
            if self._classification == "unknown":
                return "adaptive_signal_unknown"
            return None

    def _scale(self, now: float) -> str:
        if not self.enabled:
            return "full"
        if self.automatic and (self._stale(now) or self._classification == "unknown"):
            return "zero"
        if self.mode == "normal":
            return "full"
        if self.mode == "reduced":
            return "reduced"
        if self.mode == "drain" and int(self._inflight()) > 0 and (
            now - self._entered_at < self._timings["zero_capacity_s"]
        ):
            return "committed_only"
        return "zero"

    def publication(self) -> dict[str, Any]:
        """Coarse, expiring, allow-listed projection for A1/status (no app identity)."""
        with self._lock:
            now = self._clock()
            scale = self._scale(now)
            issued = self._last_tick if self._last_tick is not None else now
            return {
                "adaptive_enabled": self.enabled,
                "mode": self.mode,
                "reason": self.reason,
                "generation": self.generation,
                "issued_at": issued,
                "expires_at": issued + self._timings["stale_after_s"],
                "new_admissions_allowed": self.admission() is None and scale in ("full", "reduced"),
                "capacity_scale": scale,
            }

    def overlay(self, *, wall_now: float) -> dict[str, Any] | None:
        """The node-facing overlay document, or ``None`` while A16 is off.

        Its lease is anchored to the last *sample*, so a stalled controller cannot keep a
        fresh-looking permission alive.  The byte budget is the measured headroom minus
        the owner's reserve; it only ever bounds *new* reservations.
        """
        with self._lock:
            if not self.enabled:
                return None
            now = self._clock()
            refusal = self.admission()
            stale = self._timings["stale_after_s"]
            age = stale if self._last_tick is None else max(0.0, now - self._last_tick)
            if refusal is not None:
                budget: int | None = 0
            elif self._available is None:
                budget = 0 if self.mode == "reduced" else None
            else:
                budget = max(0, self._available - self._policy["reserve_bytes"])
            return {
                "schema": 1, "mode": self.mode, "reason": self.reason,
                "generation": self.generation, "admit": refusal is None, "refusal": refusal,
                "capacity_scale": self._scale(now), "new_kv_budget_bytes": budget,
                "epoch": float(self._last_tick) if self._last_tick is not None else 0.0,
                "issued_at": wall_now, "expires_at": wall_now + max(0.0, stale - age),
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            overdue = (
                self.mode == "drain"
                and now - self._entered_at >= self._timings["withdraw_s"]
                and int(self._inflight()) > 0
            )
            return {
                "adaptive_enabled": self.enabled,
                "mode": self.mode,
                "reason": self.reason,
                "generation": self.generation,
                "withdraw_overdue": overdue,
                "stopped": self.stopped,
            }


# ----------------------------------------------------------------- runtime
class AdaptiveHostRuntime:
    """Samples lazily at the admission boundary; does nothing while A16 is off."""

    def __init__(
        self,
        *,
        policy_loader: Callable[[], Mapping[str, Any]],
        sampler: Callable[[], Mapping[str, Any]],
        clock: Callable[[], float] = time.monotonic,
        footprint_bytes: Callable[[], int] = lambda: 0,
        inflight: Callable[[], int] = lambda: 0,
    ) -> None:
        self._load = policy_loader
        self._sampler = sampler
        self._clock = clock
        self._controller = AdaptiveHostController(
            DEFAULT_POLICY, clock=clock, footprint_bytes=footprint_bytes, inflight=inflight)
        self._last_policy: dict[str, Any] | None = None
        self._last_sample_at: float | None = None
        self._lock = threading.RLock()
        self._synced_roots: set[str] = set()

    @property
    def controller(self) -> AdaptiveHostController:
        return self._controller

    def check(self, hard_gate_reason: str | None = None) -> str | None:
        if hard_gate_reason:
            return hard_gate_reason
        with self._lock:
            return self._check()

    def sync_capacity_root(self, root: str | os.PathLike[str], *, wall_clock: Callable[[], float] = time.time) -> None:
        """Once per node ``capacity_root``: a withdraw marker left there means start fenced.

        Idempotent and read-only.  Any marker (valid, expired, malformed, unsafe) restores the
        withdraw state; it never grants anything.
        """
        key = os.fspath(root)
        with self._lock:
            if key in self._synced_roots:
                return
            policy = self._apply_policy()
            if policy is None or not policy["enabled"]:
                return  # nothing to restore while A16 is off or its policy is unreadable; retry later
            self._synced_roots.add(key)
            marker = _overlay.read_marker(root, wall_clock=wall_clock)
            if marker is not None:
                self._controller.restore_withdrawn(marker["reason"])

    def refresh_markers(
        self, roots: Iterable[str | os.PathLike[str]], *, wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Keep the withdraw markers of ``roots`` current when no node publisher is attached.

        Restores the fence from each root first, then rewrites a marker that is present while
        withdrawn and removes it once back to ``normal`` or while A16 is off.  A root without a
        marker is never written, and an unreadable policy leaves every marker untouched.
        """
        with self._lock:
            if self._apply_policy() is None:
                return
            wall = wall_clock()
            for root in roots:
                try:
                    self.sync_capacity_root(root, wall_clock=wall_clock)
                    if _overlay.marker_present(root):
                        self._persist_marker(root, wall)
                except (OSError, _overlay.AdaptiveOverlayError):
                    continue  # the in-memory fence stands; the next tick retries

    def overlay(
        self, *, wall_clock: Callable[[], float] = time.time,
        capacity_root: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any] | None:
        """Sample if due and return the node overlay; ``None`` while A16 is off.

        With ``capacity_root`` the withdraw marker there is restored before the first sample and
        kept current: written (refreshed) while withdrawn, removed once back to ``normal`` or
        when A16 is off.  A marker write failure raises so the publisher fences.
        """
        with self._lock:
            if capacity_root is not None:
                self.sync_capacity_root(capacity_root, wall_clock=wall_clock)
            reason = self._check()
            wall = wall_clock()
            if reason == "adaptive_policy_invalid":
                return _overlay.fence_document(reason, wall_now=wall)
            if capacity_root is not None:
                self._persist_marker(capacity_root, wall)
            return self._controller.overlay(wall_now=wall)

    def _persist_marker(self, root: str | os.PathLike[str], wall: float) -> None:
        controller = self._controller
        if not controller.enabled or controller.mode == "normal":
            _overlay.clear_marker(root)
        elif controller.mode == "withdraw":
            _overlay.write_marker(root, reason=controller.reason, wall_now=wall)

    def _apply_policy(self) -> Mapping[str, Any] | None:
        try:
            policy = self._load()
        except AdaptiveHostError:
            return None
        if policy != self._last_policy:
            self._controller.apply_policy(policy)
            self._last_policy = copy.deepcopy(dict(policy))
        return policy

    def _check(self) -> str | None:
        policy = self._apply_policy()
        if policy is None:
            return "adaptive_policy_invalid"
        if not policy["enabled"]:
            return None
        now = self._clock()
        interval = resolve_timings(policy)["sample_interval_s"]
        if self._last_sample_at is None or now - self._last_sample_at >= interval:
            self._last_sample_at = now
            raw: object = None
            if policy_automatic(policy):  # Adaptive sharing off: the sampler is never invoked
                try:
                    raw = self._sampler()
                except Exception:
                    raw = None
            self._controller.observe(raw)
        return self._controller.admission()


def _drain() -> Any:
    from . import adaptive_drain
    return adaptive_drain


def adaptive_resource_check(app: Any) -> str | None:
    """A16 admission boundary for ``DemoApp._resource_check`` (after A13 passes)."""
    root = getattr(app, "root", None)
    if root is None:
        return None
    runtime = getattr(app, "_adaptive_runtime", None)
    if runtime is None:
        runtime = AdaptiveHostRuntime(
            policy_loader=lambda: read_adaptive_policy(root),
            sampler=lambda: sampler_for(app)(),
            clock=lambda: getattr(app, "_adaptive_clock", time.monotonic)(),
            footprint_bytes=lambda: _drain().footprint_bytes(_drain().target_runtime(app)),
            inflight=lambda: _drain().inflight_count(_drain().target_runtime(app)),
        )
        try:
            app._adaptive_runtime = runtime
        except AttributeError:
            pass
    for capacity_root in capacity_roots(app):  # a restart after a withdraw must already be fenced here
        runtime.sync_capacity_root(capacity_root)
    return runtime.check()


def capacity_roots(app: Any) -> list[Path]:
    """The attached runtime's node root plus every managed run directory of the target.

    A restarted target has no runtime attached yet, but the withdraw marker its previous
    process left lives in ``<target state root>/managed/<run id>`` (the runtime's own root).
    """
    roots: dict[str, Path] = {}
    attached = getattr(_drain().target_runtime(app), "root", None)
    if attached is not None:
        roots[os.fspath(attached)] = Path(attached)
    state_root = getattr(getattr(getattr(app, "authority", None), "peer", None), "state_root", None)
    if state_root is not None:
        try:
            for entry in sorted(Path(state_root, "managed").iterdir()):
                if entry.is_dir() and not entry.is_symlink():
                    roots.setdefault(os.fspath(entry), entry)
        except OSError:
            pass
    return list(roots.values())


def adaptive_detached_tick(app: Any, *, wall_clock: Callable[[], float] = time.time) -> None:
    """Policy-loop tick for a target with no managed runtime attached (no-op otherwise).

    The managed runtime's control loop normally drives sampling, marker upkeep and re-entry;
    after a restart nothing does until a runtime attaches.  This keeps the restored withdraw
    fence sampled and persisted and lets ``ReentryFeed`` run, so the authority observations and
    requalification that end a withdraw are reachable.  Ordinary route qualification still has
    to run before any request is placed.
    """
    if getattr(app, "installation_role", None) != "target" or _drain().target_runtime(app) is not None:
        return
    adaptive_resource_check(app)  # creates the shared runtime, restores the fence, samples if due
    runtime = getattr(app, "_adaptive_runtime", None)
    if runtime is None:
        return
    runtime.refresh_markers(capacity_roots(app), wall_clock=wall_clock)
    if runtime.controller.enabled:
        from .adaptive_reentry import feed_for
        feed_for(app).tick(runtime.controller, app.authority)


def adaptive_overlay_provider(app: Any) -> Callable[..., dict[str, Any] | None]:
    """``overlay(wall_clock=...)`` for ``app``: the same lazily-created runtime A13 admission uses."""
    adaptive_resource_check(app)  # ensure the shared runtime exists (no sampling while off)
    runtime = getattr(app, "_adaptive_runtime", None)
    if runtime is None:
        return lambda *, wall_clock=time.time: None
    return runtime.overlay


class OverlayPublisher:
    """Throttled writer of the node overlay into a node's private ``capacity_root``."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        provider: Callable[..., dict[str, Any] | None],
        *,
        wall_clock: Callable[[], float] = time.time,
        interval_s: float = 1.0,
    ) -> None:
        self._directory = directory
        self._provider = provider
        self._wall = wall_clock
        self._interval = interval_s
        self._last: float | None = None
        try:
            self._wants_root = "capacity_root" in inspect.signature(provider).parameters
        except (TypeError, ValueError):
            self._wants_root = False

    def tick(self) -> None:
        now = self._wall()
        if self._last is not None and 0 <= now - self._last < self._interval:
            return
        self._last = now
        try:
            if self._wants_root:
                doc = self._provider(wall_clock=self._wall, capacity_root=self._directory)
            else:
                doc = self._provider(wall_clock=self._wall)
            if doc is None:
                _overlay.clear_overlay(self._directory)
            else:
                _overlay.write_overlay(self._directory, doc)
        except Exception:
            try:  # a broken controller must fence, never leave a stale permission
                _overlay.write_overlay(
                    self._directory, _overlay.fence_document("adaptive_controller_stale", wall_now=now))
            except Exception:
                pass
