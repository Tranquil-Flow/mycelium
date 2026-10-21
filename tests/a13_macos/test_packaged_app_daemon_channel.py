# SPDX-License-Identifier: AGPL-3.0-only
"""The operator's packaged-app daemon stops its app when its controlling channel is gone.

Two-Mac run 21 counted 2 leftover "Mycelium A13 Demo" processes on the laptop:
run 20's daemon and app.  Run 20 lost its ssh link (laptop on a phone
hotspot), the daemon behind ssh never noticed, and kept the app up for its
whole --max-seconds (1800 s) -- into the next run (also round15_strays.py:3,
pids 12931/12932 on 2026-09-16).  The daemon now writes a keepalive line on
its channel and, once a write fails, quits its app (then terminates it).
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DAEMON = Path(__file__).resolve().parents[2] / 'scripts' / 'a13_operator' / 'packaged_app_daemon.py'


def _fake_app(tmp_path):
    exe = tmp_path / 'fake-app'
    pidfile = tmp_path / 'app.pid'
    exe.write_text('#!/bin/sh\necho $$ > %s\nexec sleep 600\n' % pidfile)
    exe.chmod(0o700)
    return exe, pidfile


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _start(tmp_path, *, stdout):
    exe, pidfile = _fake_app(tmp_path)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'A13_DAEMON_KEEPALIVE_SECONDS': '0.2'}
    daemon = subprocess.Popen([sys.executable, '-B', str(DAEMON), '--root', str(tmp_path / 'root'),
                               '--executable', str(exe), '--max-seconds', '60'],
                              cwd=str(DAEMON.parent), env=env, stdout=stdout, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    app = int(pidfile.read_text())
    return daemon, app


def _gone(pid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_a_lost_channel_stops_the_app(tmp_path):
    read, write = os.pipe()
    daemon, app = _start(tmp_path, stdout=write)
    os.close(write)
    try:
        assert _alive(app)
        os.close(read)  # the ssh channel is gone: the next keepalive write fails
        assert _gone(app, 15), 'app still running after its channel closed'
        assert daemon.wait(15) is not None
    finally:
        for pid in (app, daemon.pid):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_terminating_the_daemon_stops_the_app(tmp_path):
    daemon, app = _start(tmp_path, stdout=subprocess.DEVNULL)
    try:
        daemon.send_signal(signal.SIGTERM)
        assert _gone(app, 15), 'app outlived its terminated daemon'
        daemon.wait(15)
    finally:
        for pid in (app, daemon.pid):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
