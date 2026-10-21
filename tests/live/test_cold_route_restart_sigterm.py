# SPDX-License-Identifier: AGPL-3.0-only
"""SIGTERM during a cold route restart: the serve-shaped process exits in bounds.

A6 browser matrix on hardware (1bc4d342): SIGTERM reached the serve while the
route owner was cold-restarting. The serve did not exit within the receiver
factory's 120 s and was SIGKILLed (``serve_receiver_teardown_invalid``).

This drives the same shutdown shape with real local node processes: the restart
runs in a request-worker thread pool (the request gateway's executor, joined at
interpreter exit), the main thread waits on ``StopRequest`` set from the
SIGTERM handler, then closes the owner and returns, as ``serve`` does. The
replacement's open is held inside its first ``configure`` (a slow model load)
until that node is stopped, so SIGTERM lands while the replacement is opening
and shutdown must not wait the open out.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tests.physical_qualification.test_node_service import ROOT, SIDECAR_BINARY

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)

_EXIT_BOUND_S = 30.0

_SERVE_SHAPE = r"""
import faulthandler, json, os, signal, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mycelium_host_platform import StopRequest
from mycelium_live.route import _ConcurrentNodeSession
from mycelium_live.supervisor import ColdRouteRestartError, ColdRouteRestartOwner
from tests.live._a6_local_physical import build_local_physical, synthetic_open_and_qualify

faulthandler.register(signal.SIGUSR1, all_threads=True)


def say(**document):
    print(json.dumps(document, sort_keys=True), flush=True)

local = build_local_physical(Path(sys.argv[2]), shape="single")
routes = []
def factory():
    route = local.new_route()
    routes.append(route)
    return route

owner = ColdRouteRestartOwner(
    route_factory=factory, open_and_qualify=synthetic_open_and_qualify, restart_budget=1)
owner.open()
first_sessions = dict(local.session_factory.sessions)
held = []
original_send = _ConcurrentNodeSession.send
def held_send(self, *, command_id, command, payload):
    replacement = len(routes) == 2 and self not in first_sessions.values()
    if replacement and command == "configure" and not held:
        held.append(command_id)
        say(event="replacement_opening")
        # A slow configure (a real model load): it ends only when the node is
        # stopped, or after 60 s, far past the exit bound.
        deadline = time.monotonic() + 60.0
        while self.returncode is None and time.monotonic() < deadline:
            time.sleep(0.05)
    return original_send(self, command_id=command_id, command=command, payload=payload)
_ConcurrentNodeSession.send = held_send

stop = StopRequest()
signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mycelium-request-worker")
dead = first_sessions["node-a"]
os.killpg(os.getpgid(dead._process.pid), signal.SIGKILL)
deadline = time.monotonic() + 10
while dead.returncode is None and time.monotonic() < deadline:
    time.sleep(0.02)

def release():
    try:
        owner.release_request("request-after-source-loss")
        return "returned"
    except ColdRouteRestartError as exc:
        return exc.code
restart = workers.submit(release)
while not stop.wait(0.2):
    pass
stopped_at = time.monotonic()
owner.close()
try:
    outcome = restart.result(timeout=60)
except Exception as exc:
    outcome = type(exc).__name__
replacement = [] if len(routes) < 2 else list(routes[1]._sessions.values())
say(event="closed", outcome=outcome, state=owner.public_status()["cold_route_restart"]["state"],
    replacement_sessions=len(replacement),
    replacement_running=sum(1 for item in replacement if item.returncode is None),
    close_s=round(time.monotonic() - stopped_at, 3))
workers.shutdown(wait=False)
"""


def _reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def test_sigterm_during_cold_restart_exits_within_bound(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _SERVE_SHAPE, str(ROOT), str(tmp_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        if not line:
            process.wait(timeout=30)
            pytest.fail("serve-shaped setup failed:\n" + process.stderr.read()[-4000:])
        assert json.loads(line) == {"event": "replacement_opening"}, line
        process.send_signal(signal.SIGTERM)
        signalled = time.monotonic()
        try:
            stdout, stderr = process.communicate(timeout=_EXIT_BOUND_S)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGUSR1)  # every thread's stack to stderr
            time.sleep(1.0)
            _reap(process)
            stdout, stderr = process.communicate()
            pytest.fail(
                f"serve-shaped process still running {_EXIT_BOUND_S} s after SIGTERM "
                f"during a cold restart\nstdout:\n{stdout}\nstderr:\n{stderr[-6000:]}")
        elapsed = time.monotonic() - signalled
    finally:
        _reap(process)

    assert process.returncode == 0, stderr[-4000:]
    closed = json.loads(stdout.strip().splitlines()[-1])
    assert closed["event"] == "closed"
    # The idle restart swallows its own restart error; the released request
    # already has its terminal.
    assert closed["outcome"] == "returned"
    assert closed["state"] == "closed"
    assert closed["replacement_sessions"] >= 1
    assert closed["replacement_running"] == 0, closed
    assert elapsed < _EXIT_BOUND_S
