# SPDX-License-Identifier: AGPL-3.0-only
"""Remote (ssh) peers are faulted on their verified remote worker, never the local ssh client."""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_a6_fault_control import FaultRejected
from mycelium_a6_product_fault_seams import (
    REMOTE_SIGNAL_SCRIPT,
    LiveProductFaultTarget,
    remote_signal_command,
)
from physical_inference_qualification import PeerIdentity

RUN_ID = "run-a6-remote"
REMOTE_PID = 424242
SHELLS = tuple(shell for shell in ("/bin/sh", shutil.which("dash")) if shell)


class _LocalSshClient:
    """The local ssh client process group that carries a remote node's session."""

    def __init__(self) -> None:
        self.signals: list[int] = []

    def _signal_process_group(self, signum: int) -> bool:
        self.signals.append(signum)
        return True


def _ssh_peer(tmp_path: Path) -> PeerIdentity:
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    key = key_dir / "id_ed25519"
    key.write_text("not-a-real-key\n", encoding="ascii")
    key.chmod(0o600)
    return PeerIdentity(
        node_id="node-2", ssh_target="operator@node-2.invalid", host_id="host-2",
        boot_id="boot-2", staging_root="/opt/mycelium/run/stage",
        process_transport="ssh", ssh_identity_file=str(key))


def _local_peer() -> PeerIdentity:
    return PeerIdentity(
        node_id="node-2", ssh_target="operator@localhost", host_id="host-2", boot_id="boot-2",
        staging_root="/opt/mycelium/run/stage", process_transport="local")


def _route(peer, client, process_id=lambda _node: REMOTE_PID):
    return SimpleNamespace(
        _sessions={"node-2": SimpleNamespace(_process=client)},
        _peers={"node-2": peer},
        _plan={"run_id": RUN_ID},
        process_id=process_id,
    )


@pytest.fixture
def no_local_kill(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a remote fault must never signal a local process")
    monkeypatch.setattr(os, "kill", refuse)
    monkeypatch.setattr(os, "killpg", refuse)


def test_ssh_peer_signals_the_verified_remote_worker_not_the_local_ssh_client(
        tmp_path, no_local_kill):
    client = _LocalSshClient()
    ran: list[tuple[str, ...]] = []
    target = LiveProductFaultTarget(
        router=None, route=_route(_ssh_peer(tmp_path), client), service=None,
        remote_runner=lambda argv: (ran.append(argv), 0)[1])

    target.signal_node("node-2", signal.SIGSTOP)
    target.resume_stopped_nodes()
    target.signal_node("node-2", signal.SIGKILL)

    assert client.signals == []
    assert len(ran) == 3
    for argv, name in zip(ran, ("STOP", "CONT", "KILL")):
        assert argv[0] == "ssh" and "operator@node-2.invalid" in argv
        remote = shlex.split(argv[-1])
        assert remote == ["/bin/sh", "-c", REMOTE_SIGNAL_SCRIPT, "a6-fault-signal",
                          name, str(REMOTE_PID), RUN_ID]
        assert remote == list(remote_signal_command(
            getattr(signal, f"SIG{name}"), REMOTE_PID, RUN_ID))


def test_resume_targets_the_route_that_was_stalled(tmp_path, no_local_kill):
    (tmp_path / "other").mkdir()
    stalled_route = _route(_ssh_peer(tmp_path), _LocalSshClient(),
                           process_id=lambda _node: 1111)
    current_route = _route(_ssh_peer(tmp_path / "other"), _LocalSshClient(),
                           process_id=lambda _node: 2222)
    ran: list[tuple[str, ...]] = []
    target = LiveProductFaultTarget(
        router=None, route=current_route, service=None,
        remote_runner=lambda argv: (ran.append(argv), 0)[1])
    target.signal_node("node-2", signal.SIGSTOP, stalled_route)
    target.resume_stopped_nodes()
    assert [shlex.split(argv[-1])[4:6] for argv in ran] == [["STOP", "1111"], ["CONT", "1111"]]


def test_unverified_remote_pid_fails_closed_without_signalling(tmp_path, no_local_kill):
    def unavailable(_node):
        raise RuntimeError("peer_process_identity_unavailable")
    client = _LocalSshClient()
    ran: list[tuple[str, ...]] = []
    target = LiveProductFaultTarget(
        router=None, route=_route(_ssh_peer(tmp_path), client, unavailable), service=None,
        remote_runner=lambda argv: (ran.append(argv), 0)[1])
    with pytest.raises(FaultRejected, match="a6_fault_remote_process_identity_unavailable"):
        target.signal_node("node-2", signal.SIGKILL)
    assert ran == [] and client.signals == []


def test_remote_identity_refusal_and_transport_failure_are_distinct(tmp_path, no_local_kill):
    route = _route(_ssh_peer(tmp_path), _LocalSshClient())
    refused = LiveProductFaultTarget(router=None, route=route, service=None,
                                     remote_runner=lambda _argv: 65)
    with pytest.raises(FaultRejected, match="a6_fault_remote_process_identity_mismatch"):
        refused.signal_node("node-2", signal.SIGSTOP)
    assert refused._stopped_nodes == {}
    unreachable = LiveProductFaultTarget(router=None, route=route, service=None,
                                         remote_runner=lambda _argv: 255)
    with pytest.raises(FaultRejected, match="a6_fault_peer_signal_refused"):
        unreachable.signal_node("node-2", signal.SIGKILL)


def test_local_peer_keeps_signalling_its_owned_process_group(tmp_path):
    client = _LocalSshClient()
    ran: list[tuple[str, ...]] = []
    target = LiveProductFaultTarget(
        router=None, route=_route(_local_peer(), client), service=None,
        remote_runner=lambda argv: (ran.append(argv), 0)[1])
    target.signal_node("node-2", signal.SIGSTOP)
    target.resume_stopped_nodes()
    assert client.signals == [signal.SIGSTOP, signal.SIGCONT]
    assert ran == []


@pytest.mark.parametrize("bad", [
    (signal.SIGKILL, 0, RUN_ID), (signal.SIGKILL, 1, RUN_ID), (signal.SIGKILL, True, RUN_ID),
    (signal.SIGKILL, 77, "run id"), (signal.SIGKILL, 77, "run*"), (signal.SIGUSR1, 77, RUN_ID),
])
def test_remote_signal_command_rejects_unsafe_inputs(bad):
    with pytest.raises(FaultRejected):
        remote_signal_command(*bad)


# -- the remote script itself, exercised on harmless local dummy processes -----

def _dummy(*marker_args: str, new_session: bool = True) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", *marker_args],
        start_new_session=new_session)


def _run_script(shell: str, name: str, pid: int, run_id: str = RUN_ID) -> int:
    return subprocess.run(
        [shell, "-c", REMOTE_SIGNAL_SCRIPT, "a6-fault-signal", name, str(pid), run_id],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=20).returncode


def _stat(pid: int) -> str:
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def _wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return _stat(pid).startswith("Z")


@pytest.mark.parametrize("shell", SHELLS)
def test_script_stalls_resumes_and_kills_a_matching_group_leader(shell):
    worker = _dummy("physical_inference_node.py", "--run-id", RUN_ID, "--node-id", "node-2")
    try:
        assert _run_script(shell, "STOP", worker.pid) == 0
        assert _wait_for(lambda: _stat(worker.pid).startswith("T"))
        assert _run_script(shell, "CONT", worker.pid) == 0
        assert _wait_for(lambda: not _stat(worker.pid).startswith("T"))
        assert _run_script(shell, "KILL", worker.pid) == 0
        assert worker.wait(timeout=5) == -signal.SIGKILL
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("marker_args", [
    ("--run-id", RUN_ID),  # not the node worker
    ("physical_inference_node.py", "--run-id", RUN_ID + "-other"),  # another run
    ("physical_inference_node.py", "--run-id", "x" + RUN_ID),
    ("physical_inference_node.py",),  # no run binding at all
])
def test_script_refuses_a_pid_whose_command_line_does_not_match(shell, marker_args):
    worker = _dummy(*marker_args)
    try:
        assert _run_script(shell, "KILL", worker.pid) == 65
        assert _run_script(shell, "STOP", worker.pid) == 65
        time.sleep(0.1)
        assert worker.poll() is None and not _stat(worker.pid).startswith("T")
    finally:
        worker.kill()
        worker.wait(timeout=5)


@pytest.mark.parametrize("shell", SHELLS)
def test_script_refuses_a_missing_pid_and_invalid_arguments(shell):
    gone = _dummy("physical_inference_node.py", "--run-id", RUN_ID)
    gone.kill()
    gone.wait(timeout=5)
    assert _run_script(shell, "KILL", gone.pid) == 65
    assert _run_script(shell, "USR1", os.getpid()) == 64
    assert _run_script(shell, "KILL", 1) == 64
    assert _run_script(shell, "KILL", os.getpid(), "bad*run") == 64


@pytest.mark.parametrize("shell", SHELLS)
def test_script_signals_children_then_worker_when_the_worker_does_not_lead_its_group(shell):
    # A session leader shell owns the group; the worker is a non-leader member
    # with its own child (standing in for the sidecar). Only the worker and its
    # child may be signalled; the leader must survive.
    worker_code = ("import subprocess,sys,time;"
                   "c=subprocess.Popen(['sleep','60']);"
                   "print(c.pid,flush=True);time.sleep(60)")
    leader = subprocess.Popen(
        ["/bin/sh", "-c",
         f"{shlex.quote(sys.executable)} -c {shlex.quote(worker_code)} "
         f"physical_inference_node.py --run-id {RUN_ID} & echo $!; wait; sleep 60"],
        start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        worker_pid = int(leader.stdout.readline())
        child_pid = int(leader.stdout.readline())
        assert os.getpgid(worker_pid) == leader.pid != worker_pid
        assert _run_script(shell, "STOP", worker_pid) == 0
        assert _wait_for(lambda: _stat(worker_pid).startswith("T")
                         and _stat(child_pid).startswith("T"))
        assert not _stat(leader.pid).startswith("T")
        assert _run_script(shell, "CONT", worker_pid) == 0
        assert _wait_for(lambda: not _stat(worker_pid).startswith("T")
                         and not _stat(child_pid).startswith("T"))
        assert _run_script(shell, "KILL", worker_pid) == 0
        assert _wait_for(lambda: _gone(worker_pid) and _gone(child_pid))
        assert leader.poll() is None
    finally:
        os.killpg(leader.pid, signal.SIGKILL)
        leader.wait(timeout=5)


def test_the_ssh_remote_command_string_runs_the_script_verbatim(tmp_path):
    """sshd hands the remote command string to the login shell: replay that locally."""

    ran: list[tuple[str, ...]] = []
    target = LiveProductFaultTarget(
        router=None, route=_route(_ssh_peer(tmp_path), _LocalSshClient()), service=None,
        remote_runner=lambda argv: (ran.append(argv), 0)[1])
    worker = _dummy("physical_inference_node.py", "--run-id", RUN_ID)
    try:
        target.route.process_id = lambda _node: worker.pid
        target.signal_node("node-2", signal.SIGKILL)
        (argv,) = ran
        assert subprocess.run(["/bin/sh", "-c", argv[-1]], timeout=20).returncode == 0
        assert worker.wait(timeout=5) == -signal.SIGKILL
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)

