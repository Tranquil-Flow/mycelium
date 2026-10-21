# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native-node side of the idle policy: policy file in, status file out, commands, health detail.

The node owns one ``NodeIdleLink`` after ``configure``.  It reads ``idle-unload-policy.json`` from
its private capacity root on every tick (a broken file means *never unload*), drops/restores the
runtime port's weights through ``release_weights`` / ``restore_weights``, and publishes
``idle-status.json`` beside it for the app.  The process, sidecar, signer and router stay up.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Final

from . import policy as _policy
from .residency import WeightResidency

STATUS_FILENAME: Final = "idle-status.json"
COMMANDS: Final = frozenset({"idle_status", "idle_reload", "idle_unload"})
_HEALTH_KEYS: Final = ("state", "line", "unload_after_minutes", "reload_count", "last_reload_ms",
                       "unloaded_for_s", "error_code")


class IdleCommandError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def process_rss_bytes() -> int | None:
    """Resident set size of this process (``ps``, read-only); ``None`` when unobservable."""
    import subprocess
    try:
        out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())], stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=2.0, check=False)
        return int(out.stdout.decode().strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class RuntimePortAdapter:
    """Presents a runtime port plus a loader closure as the no-argument release/restore interface."""

    def __init__(self, runtime: Any, load: Callable[[], Any]) -> None:
        self._runtime, self._load = runtime, load

    def release_weights(self) -> None:
        self._runtime.release_weights()

    def restore_weights(self) -> None:
        self._runtime.restore_weights(lambda _placement_id: self._load())

    def active_request_count(self) -> int:
        return int(self._runtime.active_request_count())

    def activity_counter(self) -> int:
        return int(self._runtime.operation_counter_snapshot()["applied_operation_count"])


def terminate_self() -> None:
    """Ask this process to stop through its normal SIGTERM path (route sees a lost member)."""
    import signal
    import sys
    if sys.platform == "win32":
        import _thread
        _thread.interrupt_main(signal.SIGTERM)
    else:
        os.kill(os.getpid(), signal.SIGTERM)


class NodeIdleLink:
    def __init__(self, *, capacity_root: str | os.PathLike[str], port: Any,
                 clock: Callable[[], float] | None = None,
                 on_reload_failed: Callable[[str], Any] | None = None,
                 spawn: Callable[[Callable[[], None]], Any] | None = None,
                 rss_bytes: Callable[[], int | None] = process_rss_bytes,
                 reload_deadline_s: float = 180.0) -> None:
        self._root = Path(capacity_root)
        self._port = port
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        self.residency = WeightResidency(
            release=port.release_weights, restore=port.restore_weights,
            unload_after_s=lambda: _policy.unload_after_s_or_never(self._root, missing_means_never=True),
            inflight=port.active_request_count, rss_bytes=rss_bytes,
            reload_deadline_s=reload_deadline_s, on_reload_failed=on_reload_failed, spawn=spawn, **kwargs)
        self._written: str | None = None
        self._write_lock = threading.Lock()
        self._counter: int | None = None

    # ------------------------------------------------------------- driving
    def tick(self) -> str:
        counter = getattr(self._port, "activity_counter", None)
        if callable(counter):  # hops served by a downstream stage never pass through a command
            try:
                value = int(counter())
            except Exception:
                value = None
            if value is not None and value != self._counter:
                if self._counter is not None:
                    self.residency.note_activity()
                self._counter = value
        state = self.residency.tick()
        self._publish()
        return state

    def note_activity(self) -> None:
        self.residency.note_activity()

    def work_gate(self, timeout_s: float) -> str:
        """Called before model work: reloads an unloaded node or raises ``MemberWarming``."""
        try:
            return self.residency.ensure_resident(timeout_s)
        finally:
            self._publish()

    # ------------------------------------------------------------ commands
    def command(self, name: str, payload: Any) -> dict[str, Any]:
        if name not in COMMANDS:
            raise IdleCommandError("unsupported_command")
        if payload != {}:
            raise IdleCommandError("invalid_idle_fields")
        if name == "idle_reload":
            self.residency.begin_reload()
        elif name == "idle_unload":
            self.residency.unload()
        self._publish()
        return self.residency.snapshot()

    def health_detail(self) -> dict[str, Any]:
        snap = self.residency.snapshot()
        return {key: snap[key] for key in _HEALTH_KEYS if snap.get(key) is not None or key == "state"}

    # ---------------------------------------------------------------- file
    def _publish(self) -> None:
        snap = self.residency.snapshot()
        stable = json.dumps({k: v for k, v in snap.items() if k not in ("idle_for_s", "unloaded_for_s")},
                            sort_keys=True)
        with self._write_lock:
            if stable == self._written:
                return
            path = self._root / STATUS_FILENAME
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(json.dumps(snap, sort_keys=True).encode("utf-8"))
                os.replace(temporary, path)
                self._written = stable
            except OSError:
                temporary.unlink(missing_ok=True)  # a status-file problem never touches serving
