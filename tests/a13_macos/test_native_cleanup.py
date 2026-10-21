# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import mycelium_node.process as process_module

# Bounded re-observation window for one owned-child observation.  It only ever
# covers a kernel observation that could not be taken consistently during the
# child's own exec transition, never the test's outer wait (see _wait_until).
_OWNED_OBSERVATION_SECONDS = 0.5
_READY_WAIT_SECONDS = 2.0
# macOS /bin/sh is a re-exec shim: it execs the shell selected by
# /private/var/select/sh.  This is the image that the shim ends up running.
_SELECTED_SHELL = (
    Path("/private/var/select/sh").resolve()
    if Path("/private/var/select/sh").exists()
    else Path("/bin/bash")
)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def _observe_owned(
    supervised: process_module.PhysicalNodeProcess,
    ownership: process_module._PreExecOwnership,
) -> process_module._ProcessIdentity:
    """Observe the supervised child without racing its own exec transition.

    ``observe_owned_process_identity`` re-observes only while this owner still
    holds the child unreaped and only inside this bounded window, so a
    transient mid-exec observation (the two Darwin snapshots disagreeing because
    the owned launcher is replacing its image) is retried instead of being read
    as a vanished pid.  A reaped child still raises immediately, and the
    returned identity is a complete observation that every assertion below
    still checks.
    """

    return process_module.observe_owned_process_identity(
        supervised._process,
        ownership,
        time.monotonic() + _OWNED_OBSERVATION_SECONDS,
    )


def _owned_exec_transition_observed(
    supervised: process_module.PhysicalNodeProcess,
    ownership: process_module._PreExecOwnership,
) -> bool:
    return _observe_owned(supervised, ownership).executable == ownership.launcher_executable


def _write_exec_transition_target(tmp_path: Path, launcher_path: str) -> tuple[Path, Path, Path]:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    target = tmp_path / "exec-transition.sh"
    target.write_text(
        "#!/bin/sh\n"
        f"printf ready > {str(ready)!r}\n"
        f"while test ! -e {str(release)!r}; do /bin/sleep 0.01; done\n"
        f"exec {launcher_path!r} -I -S -c "
        "'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()'\n",
        encoding="utf-8",
    )
    return ready, release, target


def _supervise(target: Path) -> process_module.PhysicalNodeProcess:
    # Launch the selected shell itself, not the /bin/sh shim: the shim's own
    # re-exec is not an owned transition and the launch identity check would
    # refuse it whenever it lands before the owner's first observation.
    return process_module.PhysicalNodeProcess(
        command=(str(_SELECTED_SHELL), str(target)),
        node_id="node-native-cleanup",
        run_id="run-native-cleanup",
        deployment_id="deployment-native-cleanup",
        response_timeout_seconds=0.05,
        shutdown_timeout_seconds=1.0,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc regression")
def test_cleanup_accepts_owned_macos_exec_transition(tmp_path: Path) -> None:
    """Cleanup retains the incarnation while an owned Mac executable changes."""

    launcher_path = process_module._inventory_process(os.getpid()).executable.path
    ready, release, target = _write_exec_transition_target(tmp_path, launcher_path)

    supervised = _supervise(target)
    ownership = supervised._pre_exec_ownership
    launch = supervised._launch_identity
    assert ownership is not None
    assert launch is not None
    try:
        _wait_until(ready.exists)
        release.touch()
        _wait_until(lambda: _owned_exec_transition_observed(supervised, ownership))
        current = _observe_owned(supervised, ownership)
        assert current.start_token == ownership.start_token
        assert current.process_group == ownership.process_group == supervised.pid
        assert current.session_id == ownership.session_id == supervised.pid
        assert current.executable == ownership.launcher_executable
        assert current.executable != launch.executable

        supervised.close()
        assert supervised.returncode == -signal.SIGKILL
    finally:
        if supervised._process.poll() is None:
            current = _observe_owned(supervised, ownership)
            assert current.pid == ownership.pid
            assert current.parent_pid == ownership.parent_pid == os.getpid()
            assert current.start_token == ownership.start_token
            assert current.process_group == ownership.process_group
            assert current.session_id == ownership.session_id
            assert process_module._identity_matches_pre_exec_ownership(
                current,
                ownership,
                supervised._target_executable_identity,
            )
            try:
                os.killpg(ownership.process_group, signal.SIGKILL)
            except ProcessLookupError:
                # Already gone: the owned group holds nothing left to kill.
                # The authoritative cleanup below still has to finish.
                pass
            supervised._process.wait(timeout=2.0)
        # Complete pipe/thread cleanup even when the assertion above is RED.
        supervised.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc regression")
def test_owned_observation_retries_the_exec_transition_and_still_rejects_unowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-exec observation of our child is retried, never read as death.

    Regression for the ``pytest -n8`` red at ``mycelium_node/process.py:870`` in
    the L2 tree (line 884 in the batch base): the two Darwin snapshots taken by
    ``_inventory_darwin_process`` disagree while the owned launcher replaces its
    own image, and that disagreement was reported as a vanished process.  The
    injected disagreement below reproduces exactly that observation on a child
    that is provably still alive and unreaped.

    The retry must not launder a foreign incarnation into acceptance: a stable
    but unowned executable is still rejected by the ownership predicate.
    """

    launcher_path = process_module._inventory_process(os.getpid()).executable.path
    ready, release, target = _write_exec_transition_target(tmp_path, launcher_path)

    supervised = _supervise(target)
    ownership = supervised._pre_exec_ownership
    assert ownership is not None
    real_executable_path = process_module._darwin_executable_path
    injected = {"remaining": 0, "count": 0}

    def straddling_executable_path(pid: int) -> Path:
        # One observation is split across the pre-exec image and the current
        # one, exactly as the kernel does when the exec lands between the two
        # snapshots.  The pre-exec image is a real, statable file so only the
        # disagreement itself is injected.
        if injected["remaining"] > 0:
            injected["remaining"] -= 1
            injected["count"] += 1
            return target
        return real_executable_path(pid)

    try:
        _wait_until(ready.exists, timeout=_READY_WAIT_SECONDS)
        monkeypatch.setattr(process_module, "_darwin_executable_path", straddling_executable_path)

        # 1. The injected disagreement is reported as a transient observation,
        #    and the child is provably alive and unreaped while it is raised.
        injected["remaining"] = 1
        with pytest.raises(process_module.TransientIdentityObservation):
            process_module._inventory_process(supervised.pid)
        assert injected["count"] == 1
        assert supervised._process.poll() is None

        # 2. The owned observation retries that same disagreement inside its
        #    bounded window and still returns this child's own incarnation.
        #    At this point the child's live image is the launched target's
        #    shell - neither the launcher nor the final executable - so what
        #    must hold after the retry is the unchanged incarnation, which the
        #    final transition check below pins to the owned executable.
        injected["remaining"] = 1
        current = _observe_owned(supervised, ownership)
        assert injected["count"] == 2
        assert current.pid == ownership.pid
        assert current.parent_pid == ownership.parent_pid == os.getpid()
        assert current.start_token == ownership.start_token
        assert current.process_group == ownership.process_group == supervised.pid
        assert current.session_id == ownership.session_id == supervised.pid

        # 3. A stable but unowned executable is still rejected: the retry above
        #    only tolerates an unstable observation, it never accepts a foreign
        #    incarnation.
        monkeypatch.setattr(
            process_module,
            "_darwin_executable_path",
            lambda pid: Path("/bin/cat"),
        )
        foreign = _observe_owned(supervised, ownership)
        assert foreign.executable.path == "/bin/cat"
        assert foreign.executable != ownership.launcher_executable
        assert not process_module._identity_matches_pre_exec_ownership(
            foreign,
            ownership,
            supervised._target_executable_identity,
        )

        monkeypatch.setattr(process_module, "_darwin_executable_path", real_executable_path)

        # 4. Cleanup of the owned child is still proven end to end.
        release.touch()
        _wait_until(lambda: _owned_exec_transition_observed(supervised, ownership))
        supervised.close()
        assert supervised.returncode == -signal.SIGKILL
    finally:
        monkeypatch.setattr(process_module, "_darwin_executable_path", real_executable_path)
        if supervised._process.poll() is None:
            try:
                os.killpg(ownership.process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            supervised._process.wait(timeout=2.0)
        supervised.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc regression")
def test_supervised_target_still_launches_when_it_runs_ahead_of_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch identity check must see the image the target keeps.

    Under -n8 with load, both tests above failed 3 times in 150 runs with
    ``node_process_identity_invalid`` from ``_supervise``.  The launch check
    compares the child's image with the command's first element right after
    the target exec.  A target that execs once more on its own - as the macOS
    ``/bin/sh`` shim does into the selected shell - is then refused whenever it
    runs ahead of that observation.  The owner is held here, after the target
    exec, until the child runs the selected shell image, which is the ordering
    a loaded host produces by chance.  The hold releases on that observed fact
    in both trees, never on elapsed time.
    """

    launcher_path = process_module._inventory_process(os.getpid()).executable.path
    _ready, _release, target = _write_exec_transition_target(tmp_path, launcher_path)
    real_await_target_exec = process_module._await_target_exec
    held: list[str] = []

    def target_runs_ahead(exec_read: int, process, deadline: float) -> None:
        real_await_target_exec(exec_read, process, deadline)
        while True:
            try:
                image = process_module._darwin_executable_path(process.pid)
            except ProcessLookupError:
                image = None
            held.append(str(image))
            if image == _SELECTED_SHELL:
                return
            assert time.monotonic() < deadline, f"target never ran {_SELECTED_SHELL}: {held[-3:]}"
            time.sleep(0.005)

    monkeypatch.setattr(process_module, "_await_target_exec", target_runs_ahead)
    supervised = _supervise(target)
    try:
        assert held and held[-1] == str(_SELECTED_SHELL)
        assert supervised._launch_identity is not None
        assert Path(supervised._launch_identity.executable.path) == _SELECTED_SHELL
    finally:
        if supervised._process.poll() is None:
            os.killpg(supervised.pid, signal.SIGKILL)
            supervised._process.wait(timeout=2.0)
        supervised.close()


def _group_members(process_group: int) -> list[str]:
    output = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,command="],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    return [line for line in output.splitlines() if line.split()[1:2] == [str(process_group)]]


def _hold_until_image(
    monkeypatch: pytest.MonkeyPatch, image: Path, spawned: list[subprocess.Popen[bytes]]
) -> list[str]:
    """Hold the owner after the target exec until the child runs ``image``.

    The child then changes its executable before the owner's first identity
    observation.  The hold releases on that observed image, never on time.
    """

    real_await_target_exec = process_module._await_target_exec
    held: list[str] = []

    def target_runs_ahead(exec_read: int, process, deadline: float) -> None:
        spawned.append(process)
        real_await_target_exec(exec_read, process, deadline)
        while True:
            try:
                observed = Path(process_module._inventory_process(process.pid).executable.path)
            except ProcessLookupError:
                observed = None
            held.append(str(observed))
            if observed is not None and observed.resolve() == image.resolve():
                return
            assert time.monotonic() < deadline, f"target never ran {image}: {held[-3:]}"
            time.sleep(0.005)

    monkeypatch.setattr(process_module, "_await_target_exec", target_runs_ahead)
    return held


def _assert_refused_and_nothing_left(
    error: process_module.NodeProcessError, child: subprocess.Popen[bytes]
) -> dict:
    record = error.start_failure.as_record()
    # The start is still refused for the changed executable ...
    assert (record["phase"], record["kind"]) == ("identity", "identity_invalid")
    assert record["inner_code"] == "node_process_identity_invalid"
    # Cleanup stopped and reaped our own child: nothing of it is left.
    assert error.code == "node_process_start_failed"
    assert error.cleanup_owner is None
    assert child.returncode is not None
    assert record["launcher_reaped"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
    assert _group_members(child.pid) == []
    # The bounded record says the owned child's identity had changed.
    assert record["identity_changed"] is True
    return record


def _kill_leftover(child: list[subprocess.Popen[bytes]]) -> None:
    # The base tree leaves the child running: never leak it from the test.
    for process in child:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.skipif(
    sys.platform != "darwin" or _SELECTED_SHELL == Path("/bin/sh").resolve(),
    reason="macOS /bin/sh re-exec shim",
)
def test_failed_start_stops_and_reaps_a_shim_child_that_changed_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused start must not orphan the child it spawned.

    The /bin/sh shim execs the selected shell before the owner's first
    observation, so the start is refused with node_process_identity_invalid.
    The child is still ours and unreaped (its pid cannot be reused), so
    cleanup must stop and reap its group rather than leave the busy-looping
    shell to be reparented to PID 1.
    """

    launcher_path = process_module._inventory_process(os.getpid()).executable.path
    _ready, _release, target = _write_exec_transition_target(tmp_path, launcher_path)
    spawned: list[subprocess.Popen[bytes]] = []
    held = _hold_until_image(monkeypatch, _SELECTED_SHELL, spawned)
    try:
        with pytest.raises(process_module.NodeProcessError) as caught:
            process_module.PhysicalNodeProcess(
                command=("/bin/sh", str(target)),
                node_id="node-native-cleanup",
                run_id="run-native-cleanup",
                deployment_id="deployment-native-cleanup",
                response_timeout_seconds=0.05,
                shutdown_timeout_seconds=1.0,
            )
        assert held and Path(held[-1]).resolve() == _SELECTED_SHELL
        assert len(spawned) == 1
        record = _assert_refused_and_nothing_left(caught.value, spawned[0])
        assert record["launcher_returncode"] in {-signal.SIGTERM, -signal.SIGKILL}
    finally:
        _kill_leftover(spawned)


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX process groups")
def test_failed_start_kills_a_term_ignoring_child_that_changed_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TERM then KILL inside the budget, even after the child exec'd away."""

    sleep_binary = Path("/bin/sleep")
    target = tmp_path / "exec-away.sh"
    target.write_text(f"trap '' TERM\nexec {str(sleep_binary)!r} 60\n", encoding="utf-8")
    spawned: list[subprocess.Popen[bytes]] = []
    held = _hold_until_image(monkeypatch, sleep_binary, spawned)
    try:
        with pytest.raises(process_module.NodeProcessError) as caught:
            process_module.PhysicalNodeProcess(
                command=(str(_SELECTED_SHELL), str(target)),
                node_id="node-native-cleanup",
                run_id="run-native-cleanup",
                deployment_id="deployment-native-cleanup",
                response_timeout_seconds=0.05,
                shutdown_timeout_seconds=1.0,
            )
        assert held and Path(held[-1]).resolve() == sleep_binary.resolve()
        assert len(spawned) == 1
        record = _assert_refused_and_nothing_left(caught.value, spawned[0])
        assert record["launcher_returncode"] == -signal.SIGKILL
    finally:
        _kill_leftover(spawned)
