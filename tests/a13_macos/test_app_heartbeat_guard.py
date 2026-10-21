# SPDX-License-Identifier: AGPL-3.0-only
"""Seed heartbeats do not wait behind a slow standing tick (A13 joined Mac on a slow link).

The standing tick holds the app guard across managed-control round trips
(10 s socket timeout each, peer.py) and runs every 2 s; on a ~1 MB/s phone
hotspot those round trips are slow.  A heartbeat that waits for the guard is
late, and the owner then sees a silent Mac whose seed lease is ageing.
"""
import threading
import time
from types import SimpleNamespace

from mycelium_a13_macos.app import DemoApp


class Closed:
    """``closed`` whose waits are short ticks: renew every 0.05 s, standing every 0.05 s."""

    def __init__(self):
        self.event = threading.Event()

    def wait(self, timeout=None):
        return self.event.wait(0.05)

    def is_set(self):
        return self.event.is_set()

    def set(self):
        self.event.set()


def test_a_slow_standing_call_does_not_delay_seed_heartbeats():
    beats = []
    calls = []
    app = SimpleNamespace(guard=threading.RLock(), closed=Closed(), running=True, installation_role='target',
                          renew_error=None, standing_error=None, local_contributor=None,
                          authority=SimpleNamespace(renew=lambda: beats.append(time.monotonic())))

    def slow_standing_tick():
        calls.append(time.monotonic())
        time.sleep(0.8)  # managed-control round trips over a slow hotspot, under the guard

    app._standing_tick = slow_standing_tick
    threads = [threading.Thread(target=DemoApp._standing_loop, args=(app,), daemon=True),
               threading.Thread(target=DemoApp._renew_loop, args=(app,), daemon=True)]
    for thread in threads:
        thread.start()
    time.sleep(3.0)
    app.closed.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) >= 2, calls
    window = [b for b in beats if b >= calls[0]]
    gaps = [b - a for a, b in zip(window, window[1:])]
    assert len(window) >= 10 and max(gaps) < 0.4, (len(window), max(gaps or [0]))
    assert app.renew_error is None
