# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV9 service modes: ``interactive`` (lowest time-to-first-token) and ``batch`` (most total throughput).

The owner picks one in the app.  A mode is a small, closed set of Router batching knobs plus two
behaviours of the app (whether queued batch jobs are accepted, and that they run only while sharing
is allowed).  This module holds the policy objects and the one tiny file that carries the owner's
choice to a node; it never touches a route.

Bounds.  A knob is raised above today's value only to a value a device round has proven, and the
proof source is recorded in :data:`PROOF_SOURCES`.  Every other knob keeps today's value in Batch
mode and is listed in :data:`KEPT_AT_TODAY`, so the status can say so.  ``interactive`` is exactly
today's :class:`~mycelium_router.contracts.RouterConfig` defaults.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from mycelium_router.contracts import RouterConfig

INTERACTIVE: Final = "interactive"
BATCH: Final = "batch"
MODES: Final = (INTERACTIVE, BATCH)
DEFAULT_MODE: Final = INTERACTIVE

SERVICE_MODE_FILENAME: Final = "service-mode.json"
MAX_MODE_FILE_BYTES: Final = 256

_TODAY: Final = RouterConfig()

#: Largest value of each raised knob that a device round has shown to complete.
PROVEN_LIMITS: Final[dict[str, int]] = {
    # 7B two-Mac A10 calibration 2026-10-18: windows 0/2/23/46 ms all completed 24/24 at the
    # frozen 0.125/s rung (46 ms batched 48 members, TTFT -5.6%); 74 ms starved under backlog.
    "live_hop_collection_window_ms": 46,
    # Shipped RouterConfig caps, exercised by the A10 acceptance/physical runs; nothing larger
    # has run on a device.
    "maximum_runtime_batch_size": _TODAY.maximum_runtime_batch_size,
    "decode_runtime_batch_size": _TODAY.decode_runtime_batch_size,
    "prefill_runtime_batch_size": _TODAY.prefill_runtime_batch_size,
    "maximum_runtime_batch_bytes": _TODAY.maximum_runtime_batch_bytes,
}

PROOF_SOURCES: Final[dict[str, str]] = {
    "live_hop_collection_window_ms": (
        "status/decisions.log 2026-10-18 7B t6 CALIBRATE-3: w46 24/24 completed, batched 48"),
    "maximum_runtime_batch_size": "RouterConfig default; no larger cap has a device round",
    "decode_runtime_batch_size": "RouterConfig default; no larger cap has a device round",
    "prefill_runtime_batch_size": "RouterConfig default; no larger cap has a device round",
    "maximum_runtime_batch_bytes": "RouterConfig default; no larger payload has a device round",
}

#: Knobs Batch mode deliberately leaves at today's value because no larger value is proven.
KEPT_AT_TODAY: Final[tuple[str, ...]] = (
    "maximum_runtime_batch_size",
    "decode_runtime_batch_size",
    "prefill_runtime_batch_size",
    "maximum_runtime_batch_bytes",
    "prefill_collection_window_ms",
    "prefill_chunk_size_tokens",
    "batch_job_concurrency",
)


@dataclass(frozen=True)
class ModeKnobs:
    mode: str
    label: str
    sentence: str
    live_hop_collection_window_ms: int
    maximum_runtime_batch_size: int
    decode_runtime_batch_size: int
    prefill_runtime_batch_size: int
    maximum_runtime_batch_bytes: int
    prefill_collection_window_ms: int
    #: ``None``: the node picks its own prefill chunk size from its decode mode; a mode never overrides it.
    prefill_chunk_size_tokens: int | None
    #: A request, never a grant: the entry node still refuses it when no draft is staged.
    speculation_preference: str
    accepts_queued_batch_jobs: bool
    batch_jobs_need_sharing_allowed: bool
    batch_job_concurrency: int

    def document(self) -> dict[str, Any]:
        return {
            "live_hop_collection_window_ms": self.live_hop_collection_window_ms,
            "maximum_runtime_batch_size": self.maximum_runtime_batch_size,
            "decode_runtime_batch_size": self.decode_runtime_batch_size,
            "prefill_runtime_batch_size": self.prefill_runtime_batch_size,
            "maximum_runtime_batch_bytes": self.maximum_runtime_batch_bytes,
            "prefill_collection_window_ms": self.prefill_collection_window_ms,
            "prefill_chunk_size_tokens": self.prefill_chunk_size_tokens,
            "speculation_preference": self.speculation_preference,
            "accepts_queued_batch_jobs": self.accepts_queued_batch_jobs,
            "batch_jobs_need_sharing_allowed": self.batch_jobs_need_sharing_allowed,
            "batch_job_concurrency": self.batch_job_concurrency,
        }


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


_INTERACTIVE = ModeKnobs(
    mode=INTERACTIVE,
    label="Interactive",
    sentence=("Interactive: answers start as fast as possible - requests are grouped for only 2 ms, "
              "speculation is used where a draft is set up, and nothing is queued."),
    live_hop_collection_window_ms=_ms(_TODAY.live_hop_collection_window_seconds),
    maximum_runtime_batch_size=_TODAY.maximum_runtime_batch_size,
    decode_runtime_batch_size=_TODAY.decode_runtime_batch_size,
    prefill_runtime_batch_size=_TODAY.prefill_runtime_batch_size,
    maximum_runtime_batch_bytes=_TODAY.maximum_runtime_batch_bytes,
    prefill_collection_window_ms=_ms(_TODAY.prefill_collection_window_seconds),
    prefill_chunk_size_tokens=None,
    speculation_preference="speculative_requested",
    accepts_queued_batch_jobs=False,
    batch_jobs_need_sharing_allowed=False,
    batch_job_concurrency=1,
)

_BATCH = replace(
    _INTERACTIVE,
    mode=BATCH,
    label="Batch / overnight",
    sentence=("Batch / overnight: favors total throughput - requests are grouped for up to 46 ms, "
              "queued jobs are accepted and run only while Adaptive sharing allows, and chats still go first."),
    live_hop_collection_window_ms=PROVEN_LIMITS["live_hop_collection_window_ms"],
    speculation_preference="target_only",
    accepts_queued_batch_jobs=True,
    batch_jobs_need_sharing_allowed=True,
)

_BY_MODE: Final = {INTERACTIVE: _INTERACTIVE, BATCH: _BATCH}


def knobs_for(mode: object) -> ModeKnobs:
    if type(mode) is not str or mode not in _BY_MODE:
        raise ValueError("service_mode_invalid")
    return _BY_MODE[mode]


def router_config_for_mode(config: RouterConfig, mode: object) -> RouterConfig:
    """``config`` with the mode's batching knobs.  Caps only ever go down to the proven limit.

    The prefill chunk size is the node's own (it follows the runtime's decode mode) and is never
    touched here.
    """
    knobs = knobs_for(mode)
    return replace(
        config,
        live_hop_collection_window_seconds=knobs.live_hop_collection_window_ms / 1000,
        prefill_collection_window_seconds=knobs.prefill_collection_window_ms / 1000,
        maximum_runtime_batch_size=min(config.maximum_runtime_batch_size, knobs.maximum_runtime_batch_size),
        decode_runtime_batch_size=min(config.decode_runtime_batch_size, knobs.decode_runtime_batch_size),
        prefill_runtime_batch_size=min(config.prefill_runtime_batch_size, knobs.prefill_runtime_batch_size),
        maximum_runtime_batch_bytes=min(config.maximum_runtime_batch_bytes, knobs.maximum_runtime_batch_bytes),
    )


def wire_policy_for_mode(mode: object) -> str:
    """The existing node batch-policy string (``candidate@<ms>``) that carries this mode's window."""
    return f"candidate@{knobs_for(mode).live_hop_collection_window_ms}"


def describe(mode: object) -> dict[str, Any]:
    knobs = knobs_for(mode)
    return {
        "mode": knobs.mode,
        "label": knobs.label,
        "sentence": knobs.sentence,
        "knobs": knobs.document(),
        "kept_at_today": list(KEPT_AT_TODAY),
        "proven_limits": dict(PROVEN_LIMITS),
    }


# ----------------------------------------------------------------------------- the mode file
def write_service_mode(root: str | os.PathLike[str], mode: object) -> None:
    """Atomically publish the owner's mode into ``root`` (owner-only file)."""
    knobs = knobs_for(mode)
    raw = json.dumps({"schema": 1, "mode": knobs.mode}, sort_keys=True).encode("utf-8")
    path = Path(root) / SERVICE_MODE_FILENAME
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


def read_service_mode(root: str | os.PathLike[str]) -> str:
    """The published mode, or ``interactive`` (today's behaviour) for anything absent or untrusted."""
    path = Path(root) / SERVICE_MODE_FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return DEFAULT_MODE
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > MAX_MODE_FILE_BYTES
        ):
            return DEFAULT_MODE
        raw = os.read(fd, MAX_MODE_FILE_BYTES + 1)
    except OSError:
        return DEFAULT_MODE
    finally:
        os.close(fd)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return DEFAULT_MODE
    if (
        not isinstance(document, dict) or set(document) != {"schema", "mode"}
        or document["schema"] != 1 or type(document["mode"]) is not str
        or document["mode"] not in _BY_MODE
    ):
        return DEFAULT_MODE
    return document["mode"]


__all__ = [
    "BATCH", "DEFAULT_MODE", "INTERACTIVE", "KEPT_AT_TODAY", "MODES", "ModeKnobs", "PROOF_SOURCES",
    "PROVEN_LIMITS", "SERVICE_MODE_FILENAME", "describe", "knobs_for", "read_service_mode",
    "router_config_for_mode", "wire_policy_for_mode", "write_service_mode",
]
