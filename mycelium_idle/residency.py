# SPDX-License-Identifier: AGPL-3.0-or-later
"""Weight residency of one stage worker: loaded -> idle (unloaded) -> warming -> loaded.

The worker process, its identity, its membership and its route registration are never touched; only
the weights are dropped (``release``) and brought back (``restore``).  A member that is reloading is
*warming*: callers get a bounded wait and ``member_warming`` if it takes too long; the member is not
failed or quarantined.  A reload that errors, or never finishes within ``reload_deadline_s``, fails
closed through ``on_reload_failed`` (the node then exits, so the route's existing loss path applies).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Final

LOADED: Final = "loaded"
IDLE: Final = "idle"
WARMING: Final = "warming"
FAILED: Final = "failed"
STATES: Final = frozenset({LOADED, IDLE, WARMING, FAILED})
STATUS_LINES: Final = {
    LOADED: "loaded",
    IDLE: "idle (weights unloaded)",
    WARMING: "warming (reloading weights)",
    FAILED: "reload failed",
}
HISTORY: Final = 16


class ResidencyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MemberWarming(RuntimeError):
    """The member is reloading and did not become ready inside the bounded wait (retriable)."""

    code = "member_warming"

    def __init__(self, node_ids: tuple[str, ...] = (), retry_after_s: float = 5.0) -> None:
        super().__init__(self.code)
        self.node_ids = tuple(node_ids)
        self.retry_after_s = retry_after_s


def _default_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="idle-weights-reload", daemon=True).start()


class WeightResidency:
    def __init__(
        self, *, release: Callable[[], Any], restore: Callable[[], Any],
        unload_after_s: Callable[[], float | None],
        clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
        inflight: Callable[[], int] = lambda: 0,
        rss_bytes: Callable[[], int | None] = lambda: None,
        reload_deadline_s: float = 120.0,
        on_reload_failed: Callable[[str], Any] | None = None,
        spawn: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self._release, self._restore = release, restore
        self._unload_after = unload_after_s
        self._clock, self._wall = clock, wall
        self._inflight, self._rss = inflight, rss_bytes
        self._reload_deadline_s = reload_deadline_s
        self._on_failed = on_reload_failed
        self._spawn = spawn or _default_spawn
        self._cond = threading.Condition(threading.RLock())
        self._state = LOADED
        self._last_activity = clock()
        self._unloaded_at: float | None = None
        self._warming_since: float | None = None
        self._error: str | None = None
        self._reload_ms: list[int] = []
        self._reload_count = 0
        self._rss_before: int | None = None
        self._rss_after: int | None = None
        self._rss_after_reload: int | None = None
        self._generation = 0

    # ------------------------------------------------------------ activity
    def note_activity(self) -> None:
        with self._cond:
            self._last_activity = self._clock()

    # --------------------------------------------------------------- tick
    def tick(self) -> str:
        with self._cond:
            now = self._clock()
            if self._state == WARMING:
                if self._warming_since is not None and now - self._warming_since > self._reload_deadline_s:
                    self._fail_locked("member_reload_timeout")
            elif self._state == LOADED:
                after = self._unload_after()
                if after is not None and now - self._last_activity >= after and int(self._inflight()) == 0:
                    self._unload_locked()
            return self._state

    # ------------------------------------------------------------- unload
    def unload(self) -> bool:
        with self._cond:
            if self._state != LOADED or int(self._inflight()) != 0:
                return False
            return self._unload_locked()

    def _unload_locked(self) -> bool:
        self._rss_before = self._rss()
        try:
            self._release()
        except Exception:
            self._fail_locked("member_unload_failed")
            return False
        self._rss_after = self._rss()
        self._state = IDLE
        self._unloaded_at = self._clock()
        self._cond.notify_all()
        return True

    # ------------------------------------------------------------- reload
    def begin_reload(self) -> str:
        with self._cond:
            if self._state != IDLE:
                return self._state
            self._state = WARMING
            self._warming_since = self._clock()
            self._generation += 1
            generation = self._generation
        self._spawn(lambda: self._do_reload(generation))
        with self._cond:
            return self._state

    def _do_reload(self, generation: int) -> None:
        started = self._clock()
        try:
            self._restore()
        except BaseException:
            with self._cond:
                if self._state == WARMING and generation == self._generation:
                    self._fail_locked("member_reload_failed")
            return
        with self._cond:
            if self._state != WARMING or generation != self._generation:
                return  # the hard deadline already failed this reload
            elapsed_ms = int(round((self._clock() - started) * 1000))
            self._reload_ms.append(elapsed_ms)
            self._reload_ms = self._reload_ms[-HISTORY:]
            self._reload_count += 1
            self._rss_after_reload = self._rss()
            self._state = LOADED
            self._warming_since = None
            self._unloaded_at = None
            self._last_activity = self._clock()
            self._cond.notify_all()

    def _fail_locked(self, code: str) -> None:
        self._state = FAILED
        self._error = code
        self._warming_since = None
        self._cond.notify_all()
        if self._on_failed is not None:
            try:
                self._on_failed(code)
            except Exception:
                pass

    # ------------------------------------------------------------ waiting
    def ensure_resident(self, timeout_s: float) -> str:
        with self._cond:
            if self._state == LOADED:
                self._last_activity = self._clock()
                return LOADED
            if self._state == FAILED:
                raise ResidencyError("member_reload_failed")
            self.begin_reload()
            end = time.monotonic() + timeout_s
            while self._state in (WARMING, IDLE):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise MemberWarming()
                self._cond.wait(remaining)
            if self._state == FAILED:
                raise ResidencyError("member_reload_failed")
            self._last_activity = self._clock()
            return self._state

    def wait_loaded(self, timeout_s: float) -> str:
        with self._cond:
            end = time.monotonic() + timeout_s
            while self._state == WARMING:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._state

    # ------------------------------------------------------------ outputs
    @property
    def state(self) -> str:
        with self._cond:
            return self._state

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            after = self._unload_after()
            now = self._clock()
            return {
                "state": self._state,
                "line": STATUS_LINES[self._state],
                "unload_after_minutes": "never" if after is None else int(round(after / 60.0)),
                "idle_for_s": round(max(0.0, now - self._last_activity), 1) if self._state == LOADED else None,
                "unloaded_for_s": (round(max(0.0, now - self._unloaded_at), 1)
                                   if self._unloaded_at is not None and self._state in (IDLE, WARMING) else None),
                "reload_count": self._reload_count,
                "last_reload_ms": self._reload_ms[-1] if self._reload_ms else None,
                "reload_ms_history": list(self._reload_ms),
                "rss_before_unload_bytes": self._rss_before,
                "rss_after_unload_bytes": self._rss_after,
                "rss_after_reload_bytes": self._rss_after_reload,
                "error_code": self._error,
            }
