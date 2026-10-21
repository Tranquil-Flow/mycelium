# SPDX-License-Identifier: AGPL-3.0-or-later
"""App-process side of PV25: the owner/member setting, the node's idle status and the energy meter.

The app keeps the setting in ``<app root>/idle-unload-policy.json`` (default 10 minutes, 'never'
allowed) and publishes it into each managed run's private root, where the native node reads it.  It
reads back the node's ``idle-status.json`` and meters worker CPU/GPU time into an energy *estimate*.
Everything it exposes is allow-listed: no paths, no pids.
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Callable, Final, Iterable

from . import energy as _energy
from . import observers as _observers
from . import policy as _policy
from .node_link import STATUS_FILENAME

_MEMBER_KEYS: Final = (
    "state", "line", "reload_count", "last_reload_ms", "reload_ms_history", "rss_before_unload_bytes",
    "rss_after_unload_bytes", "rss_after_reload_bytes", "unload_after_minutes", "unloaded_for_s", "error_code",
)
_MAX_STATUS_BYTES: Final = 8192
_AUTO: Final = object()


def read_member_status(root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The node's published idle status, reduced to the allow-list; ``None`` when absent or doubtful."""
    try:
        fd = os.open(Path(root) / STATUS_FILENAME, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_STATUS_BYTES):
            return None
        raw = os.read(fd, _MAX_STATUS_BYTES + 1)
    finally:
        os.close(fd)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("state") not in ("loaded", "idle", "warming", "failed"):
        return None
    return {key: document[key] for key in _MEMBER_KEYS if key in document}


class AppIdleLink:
    def __init__(self, *, app_root: str | os.PathLike[str], source: Any = None, chip: Any = _AUTO,
                 cpu_cores: int | None = None, clock: Callable[[], float] = time.monotonic,
                 energy_interval_s: float = 5.0,
                 expand: Callable[[int], Iterable[int]] = _observers.process_tree) -> None:
        self._root = Path(app_root)
        self._clock = clock
        self._interval = energy_interval_s
        self._expand = expand
        self._source = source if source is not None else _observers.process_time_observer()
        self._meter = _energy.EnergyMeter(
            source=self._source, chip=_energy.detect_chip() if chip is _AUTO else chip,
            cpu_cores=cpu_cores, clock=clock)
        self._member: dict[str, Any] | None = None
        self._next_energy: float | None = None

    # ---------------------------------------------------------------- setting
    def set_policy(self, setting: Any) -> dict[str, Any]:
        _policy.write_policy(self._root, setting)
        return self.status()

    def _policy_status(self) -> dict[str, Any]:
        try:
            return {"unload_after_minutes": _policy.read_policy(self._root).setting}
        except _policy.IdlePolicyError as error:
            return {"unload_after_minutes": _policy.NEVER, "error_code": error.code}

    # ------------------------------------------------------------------- tick
    def tick(self, runtime_root: str | os.PathLike[str], root_pids: Iterable[int]) -> None:
        self._publish(Path(runtime_root))
        self._member = read_member_status(runtime_root)
        now = self._clock()
        if self._next_energy is not None and now < self._next_energy:
            return
        self._next_energy = now + self._interval
        pids: list[int] = []
        for root_pid in root_pids:
            pids.extend(p for p in self._expand(root_pid) if p not in pids)
        if pids:
            self._meter.update(pids)

    def _publish(self, runtime_root: Path) -> None:
        try:
            wanted = _policy.read_policy(self._root).setting
        except _policy.IdlePolicyError:
            wanted = _policy.NEVER  # fail safe: an unreadable setting never unloads
        try:
            current = _policy.read_policy(runtime_root).setting
        except _policy.IdlePolicyError:
            current = None
        if not (runtime_root / _policy.POLICY_FILENAME).exists() or current != wanted:
            if (runtime_root / _policy.POLICY_FILENAME).is_symlink():
                return
            try:
                _policy.write_policy(runtime_root, wanted)
            except OSError:
                pass

    def new_session(self) -> None:
        self._meter.reset()
        self._next_energy = None
        self._member = None

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        snapshot = self._meter.snapshot()
        return {
            "policy": self._policy_status(),
            "member": None if self._member is None else dict(self._member),
            "energy": {**snapshot, "text": _energy.display_text(snapshot)},
        }
