# SPDX-License-Identifier: AGPL-3.0-only
"""A stop request that a Python signal handler may set at any call boundary.

CPython runs a Python-level signal handler on the main thread, between
bytecodes or around a call.  ``threading.Event.set()`` takes the event's
condition lock, a plain non-reentrant ``Lock``, and the main thread holds that
same lock inside ``Event.wait()`` (and ``set``/``clear``).  A SIGTERM or
SIGINT that lands there makes the handler wait forever on a lock its own
thread holds: the process never stops.

:class:`StopRequest` keeps the ``is_set``/``set``/``wait`` contract of an
event but takes no lock from the setter: ``set()`` writes a plain flag and
puts a token on a :class:`queue.SimpleQueue`, whose ``put()`` is documented
as reentrant (safe from signal handlers and ``__del__``).  ``wait()`` blocks
on ``SimpleQueue.get(timeout)``, which a signal handler's ``put`` wakes.

Pure standard library and platform neutral: safe to import on every host.
"""

from __future__ import annotations

from queue import Empty, SimpleQueue


class StopRequest:
    """A one-way stop flag that is safe to set from a signal handler."""

    __slots__ = ("_requested", "_wakeups")

    def __init__(self) -> None:
        self._requested = False
        self._wakeups: SimpleQueue[None] = SimpleQueue()

    def set(self) -> None:
        """Request the stop.  Takes no lock; callable from a signal handler."""

        self._requested = True
        self._wakeups.put(None)

    def is_set(self) -> bool:
        return self._requested

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the stop is requested or ``timeout`` elapses.

        Returns whether the stop has been requested, like ``Event.wait``.
        """

        if self._requested:
            return True
        if timeout is not None and timeout <= 0:
            return False
        try:
            self._wakeups.get(timeout=timeout)
        except Empty:
            return self._requested
        # Hand the token on so every other waiter wakes as well.
        self._wakeups.put(None)
        return self._requested

    def handle_signal(self, _signum: int, _frame: object) -> None:
        """A ``signal.signal`` handler that only requests the stop."""

        self.set()


__all__ = ["StopRequest"]
