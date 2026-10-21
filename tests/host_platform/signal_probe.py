# SPDX-License-Identifier: AGPL-3.0-only
"""Test probe: a Python signal handler must not take a thread lock.

CPython runs a Python-level signal handler on the main thread between
bytecodes.  If the handler acquires a ``threading`` lock (``Event.set()``
takes its condition lock, ``Condition``/``RLock``/``Lock`` directly) and the
signal lands while the main thread already holds that lock, the handler waits
on its own thread forever.  Which lock the main thread holds at the moment of
delivery is a matter of timing, so the deterministic property to test is the
handler itself: it must not acquire any lock at all.

:func:`call_without_locks` runs a handler under a profile hook that refuses
the first call into a lock primitive *before* the acquire happens, so a
violating handler fails the test with :class:`SignalHandlerTookLock` instead
of wedging it.  :func:`guard_signal_handlers` wraps every handler a product
installs for the given signals in that probe.
"""

from __future__ import annotations

import _thread
import signal
import sys
from typing import Any, Callable

import pytest


LOCK_TYPES: tuple[type, ...] = (type(_thread.allocate_lock()), _thread.RLock)


class SignalHandlerTookLock(AssertionError):
    """A signal handler called into a thread lock primitive."""


def _lock_call(arg: Any) -> str | None:
    owner = getattr(arg, "__self__", None)
    declared = getattr(arg, "__objclass__", None)
    if isinstance(owner, LOCK_TYPES) or (
        isinstance(declared, type) and issubclass(declared, LOCK_TYPES)
    ):
        return getattr(arg, "__qualname__", repr(arg))
    return None


def call_without_locks(handler: Callable[..., Any], *args: Any) -> Any:
    """Call ``handler(*args)``; raise before it would take any thread lock."""

    def profile(_frame: Any, event: str, arg: Any) -> None:
        if event == "c_call":
            name = _lock_call(arg)
            if name is not None:
                sys.setprofile(previous)
                raise SignalHandlerTookLock(f"signal handler took a lock: {name}")

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        return handler(*args)
    finally:
        sys.setprofile(previous)


def guard_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
    signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
) -> dict[int, Callable[[int, Any], Any]]:
    """Wrap each new handler installed for ``signals`` in the lock probe.

    Returns ``{signum: product_handler}`` for the handlers installed so far;
    restoring the handler that was current when the guard started passes
    through unwrapped.
    """

    real_signal = signal.signal
    baseline = {signum: signal.getsignal(signum) for signum in signals}
    installed: dict[int, Callable[[int, Any], Any]] = {}

    def patched(signum: int, handler: Any) -> Any:
        if signum in baseline and callable(handler) and handler is not baseline[signum]:
            installed[signum] = handler

            def guarded(number: int, frame: Any, _product: Any = handler) -> Any:
                return call_without_locks(_product, number, frame)

            return real_signal(signum, guarded)
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", patched)
    return installed
