# SPDX-License-Identifier: AGPL-3.0-only
"""Scale test deadlines by how slow the host measures right now.

A deadline in a test is a hang guard: it exists so a stuck process fails the test instead of the run.  On a host
under Spotlight indexing or other suites, a fixed 1-5 s guard turns into a failure although nothing hangs.  The
factor below is measured (a fixed CPU spin and a fixed process start, each against a quiet-host baseline), never
below 1, capped, and cached for a few seconds.  Use it for guards and waits; never to loosen what an assertion
compares (a bound that must stay below a poll interval takes a ``ceiling``).

``MYCELIUM_TEST_HOST_FACTOR`` overrides the measurement (clamped to ``[1, MAX_FACTOR]``).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Callable

ENV_OVERRIDE = "MYCELIUM_TEST_HOST_FACTOR"
BASELINE_SPIN_SECONDS = 0.013    # 60 000 chained SHA-256 on a quiet Apple-silicon host (best of 8, 12.7-13.6 ms)
BASELINE_SPAWN_SECONDS = 0.016   # ``python -I -S -c pass`` on the same host (best of 8, 15.9-18.9 ms)
MAX_FACTOR = 16.0
CACHE_SECONDS = 15.0

_cache: tuple[float, float] | None = None  # (measured_at, factor)


def reset() -> None:
    global _cache
    _cache = None


def _spin() -> float:
    started = time.perf_counter()
    digest = b"x"
    for _ in range(60_000):
        digest = hashlib.sha256(digest).digest()
    return time.perf_counter() - started


def _spawn() -> float:
    started = time.perf_counter()
    subprocess.run([sys.executable, "-I", "-S", "-c", "pass"], check=True)
    return time.perf_counter() - started


def measure() -> tuple[float, float]:
    """Best of three for each probe, so a single scheduling blip does not count but sustained load does."""
    return min(_spin() for _ in range(3)), min(_spawn() for _ in range(3))


def _clamp(value: float) -> float:
    return max(1.0, min(MAX_FACTOR, value))


def host_speed_factor(
    *,
    measure: Callable[[], tuple[float, float]] = measure,
    clock: Callable[[], float] = time.monotonic,
) -> float:
    global _cache
    override = os.environ.get(ENV_OVERRIDE)
    if override is not None:
        try:
            return _clamp(float(override))
        except ValueError:
            pass
    now = clock()
    if _cache is not None and now - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    spin, spawn = measure()
    factor = _clamp(max(spin / BASELINE_SPIN_SECONDS, spawn / BASELINE_SPAWN_SECONDS))
    _cache = (now, factor)
    return factor


def scaled(seconds: float, *, ceiling: float | None = None) -> float:
    """``seconds`` times the host factor; never below ``seconds``; at most ``ceiling`` (when above ``seconds``)."""
    value = seconds * host_speed_factor()
    if ceiling is not None:
        value = min(value, max(ceiling, seconds))
    return value


def wait_until(
    condition: Callable[[], object],
    timeout: float,
    *,
    what: str,
    interval: float = 0.02,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll ``condition`` until it is truthy; the deadline is ``scaled(timeout)``.  Raises ``TimeoutError``."""
    deadline = clock() + scaled(timeout)
    while True:
        if condition():
            return True
        if clock() >= deadline:
            raise TimeoutError(f"never became true: {what}")
        sleep(interval)
