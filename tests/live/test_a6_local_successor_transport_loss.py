# SPDX-License-Identifier: AGPL-3.0-only
"""A6 cases 8 and 15 when the successor's control link drops during its challenge.

On the 67bb87b7 a1 window both cases were refused before their fault:
``controlled_fault_injection_unproven`` with ``successor_attempt_started`` (case
8) / ``cleanup_held`` (case 15) false, the successor physical challenge on
placement-001 failed ``node_process_exited`` and, in case 15, the later cold
restart failed ``sidecar_socket_conflict``. The laptop that carries placement-001
lost its link at exactly those times (its membership heartbeats stalled and its
agent restarted, while SB2's stayed regular), so its ssh control session died
on ``ServerAliveCountMax`` while the remote worker stayed alive.

This reproduces that on real local node processes with three complete-model
replicas (node-a source, node-b successor placement-001, node-c spare), the
run-day placement. The successor node (node-b)
is launched behind a relay that plays the ssh client: it pipes the session to a
worker kept by a separate holder (the remote sshd). Dropping the link makes the
relay print ssh's timeout line and exit 255, as ssh does; the worker and its
sidecar keep running with their control stdin still open, as a half-open
connection leaves them. The link drops while the challenge's first command is
in flight on node-b.

The product must fail closed (the cases are refused, no successor publishes)
and the refusal must say why: the challenge outcome names the exited
participant with its transport exit status and last stderr line, and the seam's
measured state carries the challenge outcome.

Local multi-process session only; never physical or browser qualification.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from mycelium_a6_fault_control import FaultRejected, INJECTION_POINTS
from mycelium_a6_product_fault_seams import ProductCaseSeam
from mycelium_live.route import _ConcurrentNodeSession
from tests.live._a6_local_physical import LocalSessionFactory, simulated_host
from tests.live.test_a6_local_physical_recovery import (
    _TERMINALS,
    SIDECAR_BINARY,
    _compose,
    _drain,
    _submit,
    watchdog,  # noqa: F401 - fixture
)
from tests.physical_qualification.test_node_service import NODE_SCRIPT, ROOT

pytestmark = pytest.mark.skipif(
    not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built"
)

SUCCESSOR = "node-b"
_SSH_TIMEOUT_LINE = f"Timeout, server {simulated_host(SUCCESSOR)} not responding."

#: The ssh client of a remote node, as far as the control session can tell.
#: argv: pidfile, timeout line, worker stderr file, worker argv...
#: The holder (the remote sshd) owns the worker's stdin writer and stdout
#: reader. A clean stdin EOF closes the channel (SIGUSR1 to the holder); a
#: dropped link (SIGUSR2 to the relay) prints ssh's line and exits 255 without
#: closing the channel, so the worker keeps running.
_RELAY = r"""
import os, signal, subprocess, sys, threading
pidfile, timeout_line, worker_stderr = sys.argv[1], sys.argv[2], sys.argv[3]
command = sys.argv[4:]
to_worker_r, to_worker_w = os.pipe()
from_worker_r, from_worker_w = os.pipe()
holder = os.fork()
if holder == 0:
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    err = os.open(worker_stderr, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    worker = subprocess.Popen(command, stdin=to_worker_r, stdout=from_worker_w,
                              stderr=err, close_fds=True)
    keep = {0, 1, 2, to_worker_w, from_worker_r}
    for fd in range(3, 1024):
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass
    signal.signal(signal.SIGUSR1, lambda *_: os.close(to_worker_w))
    tmp = pidfile + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(f"{os.getpid()} {worker.pid}\n")
    os.replace(tmp, pidfile)
    while worker.poll() is None:
        try:
            worker.wait()
        except InterruptedError:
            pass
    os._exit(0)
os.close(to_worker_r)
os.close(from_worker_w)

def link_lost(*_):
    os.write(2, (timeout_line + "\n").encode())
    os._exit(255)

signal.signal(signal.SIGUSR2, link_lost)

def pump_in():
    while True:
        data = os.read(0, 65536)
        if not data:
            os.close(to_worker_w)
            os.kill(holder, signal.SIGUSR1)
            return
        os.write(to_worker_w, data)

threading.Thread(target=pump_in, daemon=True).start()
while True:
    data = os.read(from_worker_r, 65536)
    if not data:
        os._exit(0)
    os.write(1, data)
"""


class _RelayedSuccessorFactory(LocalSessionFactory):
    """Launch the successor's first process behind an ssh-like relay."""

    def __init__(self, base: LocalSessionFactory, work: Path) -> None:
        super().__init__(base._roots, base._socket_roots, base._launcher)
        self.step_delay_s = base.step_delay_s
        self._relay = work / "relay.py"
        self._relay.write_text(_RELAY, encoding="utf-8")
        self.pidfile = work / "holder.pid"
        self.worker_stderr = work / "worker.stderr"
        self.relayed: _ConcurrentNodeSession | None = None

    def __call__(self, *, argv, node_id, run_id, deployment_id, timeout_seconds):
        if node_id != SUCCESSOR or self.relayed is not None:
            return super().__call__(argv=argv, node_id=node_id, run_id=run_id,
                                    deployment_id=deployment_id,
                                    timeout_seconds=timeout_seconds)
        command = (
            sys.executable, "-B", str(self._relay), str(self.pidfile), _SSH_TIMEOUT_LINE,
            str(self.worker_stderr),
            sys.executable, "-B", str(self._launcher), str(ROOT), simulated_host(node_id),
            repr(float(self.step_delay_s)), str(NODE_SCRIPT),
            "--run-id", run_id,
            "--deployment-id", deployment_id,
            "--node-id", node_id,
            "--artifact-root", str(self._roots[node_id]),
            "--socket-root", str(self._socket_roots[node_id]),
            "--sidecar-binary", str(SIDECAR_BINARY),
            "--sidecar-local-only",
            "--command-timeout", "900",
            "--decode-mode", "stage_local_kv",
        )
        session = _ConcurrentNodeSession(
            argv=command, node_id=node_id, run_id=run_id,
            deployment_id=deployment_id, timeout_seconds=timeout_seconds,
        )
        self.relayed = session
        self.sessions[node_id] = session
        self.launches.append(node_id)
        return session

    def drop_link(self) -> None:
        assert self.relayed is not None
        os.kill(self.relayed._process._process.pid, signal.SIGUSR2)

    def kill_remote(self) -> None:
        try:
            holder = int(self.pidfile.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return
        try:
            os.killpg(holder, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _compose_relayed(tmp_path: Path, monkeypatch):
    work = tmp_path / "relay"
    work.mkdir(mode=0o700)
    import tests.live._a6_local_physical as fixture

    build = fixture.build_local_physical
    factories: list[_RelayedSuccessorFactory] = []

    def build_relayed(path, *, shape="single"):
        # Three complete replicas, as the run-day plan: the third keeps the
        # route alive after the source and the successor's link are lost.
        local = build(path, shape=shape, replica_count=3)
        local.session_factory = _RelayedSuccessorFactory(local.session_factory, work)
        factories.append(local.session_factory)
        return local

    monkeypatch.setattr("tests.live.test_a6_local_physical_recovery.build_local_physical",
                        build_relayed)
    composed = _compose(tmp_path, "full_replica")
    factory = factories[0]
    assert composed[0].session_factory is factory and factory.relayed is not None
    return composed


def _drop_link_during_challenge(owner, factory, stop: threading.Event) -> None:
    """Drop the successor's link while its challenge command is in flight."""

    while not stop.is_set():
        route = owner._current
        active = dict(getattr(route, "_active_route_requests", {}) or {})
        for request_id, state in active.items():
            if (request_id.startswith("a6-qual-")
                    and SUCCESSOR in (state.get("inflight_commands") or {})):
                factory.drop_link()
                return
        time.sleep(0.001)


def _run(service, owner, target, factory, case_id):
    request_id = _submit(service, owner)
    events: list = []
    first_token = threading.Event()
    reader = threading.Thread(
        target=_drain, args=(service, request_id, events, first_token), daemon=True)
    reader.start()
    assert first_token.wait(60), "request never produced a token"
    stop = threading.Event()
    dropper = threading.Thread(
        target=_drop_link_during_challenge, args=(owner, factory, stop), daemon=True)
    dropper.start()
    seam = ProductCaseSeam(case_id, target)
    refusal: FaultRejected | None = None
    post_state = None
    try:
        seam.arm(case_id=case_id)
        post_state = dict(seam.apply(case_id=case_id))
    except FaultRejected as error:
        refusal = error
    finally:
        stop.set()
    reader.join(timeout=180)
    return request_id, refusal, post_state, [e.kind for e in events if e.kind in _TERMINALS]


def _unmet_and_measured(case_id, post_state):
    """What the fault control would log for a refused seam state."""

    expected = INJECTION_POINTS[case_id].expected_pre_recovery_state
    unmet = {key: post_state.get(key) for key, value in expected.items()
             if post_state.get(key) != value}
    measured = {key: value for key, value in post_state.items()
                if key not in expected and isinstance(value, (bool, int, str, type(None)))}
    return unmet, measured


@pytest.mark.parametrize(
    ("case_id", "unmet_key"),
    [("successor_prefill_failure", "successor_attempt_started"),
     ("cleanup_authority_failure", "cleanup_authority_refused")],
)
def test_successor_link_loss_during_challenge_refuses_the_case_and_says_why(
        tmp_path, monkeypatch, watchdog, case_id, unmet_key) -> None:  # noqa: F811
    local, owner, router, service, target = _compose_relayed(tmp_path, monkeypatch)
    factory = local.session_factory
    # The seam's 30 s bound is the replay checkpoint's; nothing can start
    # after it. Locally the refusal is certain long before.
    target._wait_seconds = 8.0
    try:
        request_id, refusal, post_state, kinds = _run(
            service, owner, target, factory, case_id)

        # -- the window's signature (holds on 67bb87b7) ---------------------
        assert refusal is None, refusal
        assert post_state is not None
        unmet, measured = _unmet_and_measured(case_id, post_state)
        assert unmet.get(unmet_key) is False, (unmet, measured)
        challenge = target.qualifier.physical_challenge_outcome(request_id)
        assert challenge is not None
        assert challenge["outcome"] == "infer_error", challenge
        assert challenge["error"] == "node_process_exited", challenge
        assert challenge["placement_ids"] == ["placement-001"], challenge
        view = router.a6_request_view(request_id) or {}
        assert kinds == ["failed"], (kinds, view)
        # No cutover: the coordinator still commits the source attempt.
        assert (target.committed_route_identity(request_id) or {}).get(
            "request_attempt") == 1, view
        # The worker behind the dropped link is still alive and holds its
        # sidecar socket, so the cold restart that follows the released
        # request cannot reopen node-b (case 15's sidecar_socket_conflict).
        deadline = time.monotonic() + 120
        status = owner.public_status()["cold_route_restart"]
        while (status["state"] != "failed"
               and not (status["state"] == "ready" and status["generation"] >= 2)
               and time.monotonic() < deadline):
            time.sleep(0.2)
            status = owner.public_status()["cold_route_restart"]
        assert status["state"] == "failed", status
        assert status["last_restart_reason"] == "sidecar_socket_conflict", status

        # -- the refusal says why (the fix) ----------------------------------
        exits = challenge.get("participant_exits")
        assert exits == [{
            "node_id": SUCCESSOR,
            "returncode": 255,
            "stderr_last_line": _SSH_TIMEOUT_LINE,
        }], challenge
        assert measured.get("successor_challenge_outcome") == "infer_error", measured
        assert measured.get("successor_challenge_error") == "node_process_exited", measured
        assert measured.get("successor_transport_exit") == (
            f"{SUCCESSOR}:255:{_SSH_TIMEOUT_LINE}"), measured
    finally:
        service.close()
        owner.close()
        factory.kill_remote()
        local.close()
