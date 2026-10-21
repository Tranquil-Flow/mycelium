# SPDX-License-Identifier: AGPL-3.0-only
"""``StopRequest``: the stop flag every process sets from its signal handler."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from mycelium_host_platform import StopRequest
from tests.host_platform.signal_probe import SignalHandlerTookLock, call_without_locks


ROOT = Path(__file__).resolve().parents[2]


def test_threading_event_set_is_refused_by_the_lock_probe() -> None:
    # Teeth for the probe the per-process tests rely on.
    with pytest.raises(SignalHandlerTookLock, match="lock"):
        call_without_locks(threading.Event().set)


def test_set_takes_no_lock_and_is_observed() -> None:
    stop = StopRequest()
    assert stop.is_set() is False
    call_without_locks(stop.handle_signal, signal.SIGTERM, None)
    assert stop.is_set() is True
    assert stop.wait(0) is True
    assert stop.wait() is True


def test_wait_times_out_without_a_request() -> None:
    stop = StopRequest()
    started = time.monotonic()
    assert stop.wait(0.05) is False
    assert time.monotonic() - started >= 0.04
    assert stop.wait(0) is False
    assert stop.wait(-1) is False


def test_one_set_wakes_every_waiting_thread() -> None:
    stop = StopRequest()
    woke: list[bool] = []
    waiters = [
        threading.Thread(target=lambda: woke.append(stop.wait(30)), daemon=True)
        for _ in range(4)
    ]
    for waiter in waiters:
        waiter.start()
    time.sleep(0.05)
    stop.set()
    for waiter in waiters:
        waiter.join(5)
    assert woke == [True, True, True, True]


# The main thread waits on the request while SIGTERM is raised at every
# Python/C call boundary (where CPython runs pending handlers), so the handler
# lands inside every step of ``wait`` itself.
_SIGTERM_AT_EVERY_CALL_BOUNDARY = """
import signal
import sys

from mycelium_host_platform import StopRequest

stop = StopRequest()
signal.signal(signal.SIGTERM, stop.handle_signal)
remaining = [5_000]


def deliver(_frame, _event, _arg):
    if remaining[0] > 0:
        remaining[0] -= 1
        signal.raise_signal(signal.SIGTERM)


sys.setprofile(deliver)
stopped = stop.wait(30)
sys.setprofile(None)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
raise SystemExit(0 if stopped else 3)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_wait_returns_when_the_handler_lands_on_any_call_boundary() -> None:
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _SIGTERM_AT_EVERY_CALL_BOUNDARY],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        raise AssertionError("stop wait did not return after SIGTERM") from None
    assert process.returncode == 0, stderr
    assert stdout == ""
    assert stderr == ""
