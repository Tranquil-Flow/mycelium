# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded node cleanup must not spend its budget forking ``/bin/ps``.

Under process churn on macOS, ``PhysicalNodeProcess.close()`` used to fail
with ``node_process_cleanup_failed`` (1 s budget) for two reasons, both seen
in an in-pytest probe of
``test_process_cleanup_retry_and_handshake_fd_thread_inventory_stay_stable``:

* the ancestor walk (``_protected_process_groups``) forked
  ``/bin/ps -o pid=,ppid=,pgid= -p`` for every ancestor that
  ``PROC_PIDTBSDINFO`` refuses (a root-owned ``login`` above a Terminal shell),
  once per 20 ms poll, so the walk could use the whole budget before SIGKILL
  and leave a SIGTERM-ignoring child alive;
* once the child had exited and been reaped, confirming that its process
  group was empty forked ``/bin/ps -axo``, which timed out, so a cleanup that
  had already succeeded reported failure;
* while the leader was still alive (grace and TERM-wait polls, or the poll
  right after SIGKILL) each poll also forked ``/bin/ps -axo``; one such fork
  overran its sub-deadline by ~0.8 s and used up the budget.

These tests make the relevant ``/bin/ps`` query fail exactly as observed
(``subprocess.TimeoutExpired``) instead of relying on load.  Nothing in them
is released by elapsed time: the budget is generous, and the base tree fails
because the fork never completes, not because a clock runs out.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from typing import Any, Callable
from unittest.mock import patch

import pytest

import mycelium_node.process as process_module


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="libproc/ps process discovery is the macOS cleanup path",
)

_ANCESTOR_QUERY = ("/bin/ps", "-o", "pid=,ppid=,pgid=", "-p")
_GROUP_QUERY = ("/bin/ps", "-axo", "pid=,pgid=,state=")


def _ps_that_never_finishes(
    monkeypatch: pytest.MonkeyPatch,
    query: tuple[str, ...],
) -> list[tuple[str, ...]]:
    """Make one ``/bin/ps`` query time out, as it did under process churn."""

    real_check_output = process_module.subprocess.check_output
    refused: list[tuple[str, ...]] = []

    def check_output(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if tuple(argv[: len(query)]) == query:
            refused.append(tuple(argv))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0.0))
        return real_check_output(argv, *args, **kwargs)

    monkeypatch.setattr(process_module.subprocess, "check_output", check_output)
    return refused


def _wait_for(predicate: Callable[[], bool], what: str) -> None:
    # Event-driven: returns as soon as the predicate holds. The bound is only
    # a failure guard for a broken fixture, never a release condition.
    for _ in range(3_000):
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"fixture never reached: {what}")


def _start_node(tmp_path: Path, script: str, name: str) -> Any:
    target = tmp_path / f"{name}.py"
    target.write_text(script)
    return process_module.PhysicalNodeProcess(
        command=(sys.executable, str(target)),
        node_id=f"node-{name}",
        run_id=f"run-{name}",
        deployment_id=f"deployment-{name}",
        response_timeout_seconds=0.01,
        shutdown_timeout_seconds=5.0,
    )


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _force_cleanup(supervised: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    # A Popen that has not been reaped still reserves its PID/PGID. Once
    # poll() reaps it, the numeric group can belong to someone else: never
    # send even a null signal to that stale group in test teardown.
    if supervised._process.poll() is None:
        ownership = supervised._pre_exec_ownership
        try:
            same_group = (
                ownership.pid == supervised.pid
                and ownership.parent_pid == os.getpid()
                and ownership.process_group == supervised.pid
                and ownership.session_id == supervised.pid
                and os.getpgid(supervised.pid) == ownership.process_group
                and os.getsid(supervised.pid) == ownership.session_id
            )
        except ProcessLookupError:
            same_group = False
        if same_group and supervised._process.poll() is None:
            try:
                os.killpg(ownership.process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        supervised._process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        pass
    try:
        supervised.close()
    except process_module.NodeProcessError:
        pass


def test_teardown_never_signals_the_reaped_leaders_numeric_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaped leader frees its PGID: a later killpg can reach a foreign group.

    Force the macOS EPERM at the precise unsafe teardown signal, rather than
    relying on PID reuse or scheduler load. The child is actually reaped first.
    """
    supervised = _start_node(
        tmp_path,
        "import signal\nwhile True:\n    signal.pause()\n",
        "reaped-teardown",
    )
    os.kill(supervised.pid, signal.SIGKILL)
    supervised._process.wait(timeout=10.0)
    assert not _group_exists(supervised.pid)
    signals: list[tuple[int, int]] = []

    def foreign_group(pgid: int, signum: int) -> None:
        signals.append((pgid, signum))
        raise PermissionError(1, "Operation not permitted")

    # _force_cleanup deliberately calls monkeypatch.undo(), so use an
    # independent patch to hold the exact OS seam through that call.
    with patch.object(os, "killpg", side_effect=foreign_group):
        _force_cleanup(supervised, monkeypatch)
    assert signals == []


def test_reaped_group_probe_eperm_requires_census_before_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EPERM from the null group probe is not itself evidence of emptiness."""
    supervised = _start_node(
        tmp_path,
        "import signal\nwhile True:\n    signal.pause()\n",
        "eperm-probe",
    )
    try:
        os.kill(supervised.pid, signal.SIGKILL)
        supervised._process.wait(timeout=10.0)
        assert not _group_exists(supervised.pid)
        real_kill = os.kill

        def deny_group_probe(pid: int, signum: int) -> None:
            if pid == -supervised.pid and signum == 0:
                raise PermissionError(1, "Operation not permitted")
            real_kill(pid, signum)

        monkeypatch.setattr(os, "kill", deny_group_probe)
        assert supervised._owned_group_state(process_module.time.monotonic() + 5.0) == "empty"
        supervised.close()
    finally:
        _force_cleanup(supervised, monkeypatch)


def test_unreadable_child_just_after_sigkill_is_reaped_before_group_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable just-killed leader is not proof that its group is foreign.

    Hold the Popen reap seam for one observation after SIGKILL, while libproc
    refuses that zombie's identity. The next observation is allowed to reap it.
    """
    marker = tmp_path / "term-ignored"
    supervised = _start_node(
        tmp_path,
        "import pathlib, signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text('ignored')\n"
        "while True:\n    signal.pause()\n",
        "kill-reap-gap",
    )
    try:
        _wait_for(marker.exists, "child ignores SIGTERM")
        real_signal = supervised._signal_process_group
        real_inventory = process_module._inventory_process
        real_poll = supervised._process.poll
        killed = False
        refused_identity = False
        held_polls = 0

        def signal_group(signum: int, deadline: float | None = None) -> bool:
            nonlocal killed
            sent = real_signal(signum, deadline)
            if signum == signal.SIGKILL and sent:
                killed = True
            return sent

        def inventory(pid: int) -> Any:
            nonlocal refused_identity
            if killed and pid == supervised.pid and not refused_identity:
                refused_identity = True
                raise ProcessLookupError(pid)
            return real_inventory(pid)

        def poll() -> int | None:
            nonlocal held_polls
            if killed and held_polls < 2:
                held_polls += 1
                return None  # not yet reaped, so this PID cannot be reused
            return real_poll()

        monkeypatch.setattr(supervised, "_signal_process_group", signal_group)
        monkeypatch.setattr(process_module, "_inventory_process", inventory)
        monkeypatch.setattr(supervised._process, "poll", poll)
        refused = _ps_that_never_finishes(monkeypatch, _GROUP_QUERY)
        supervised.close()
        assert killed and refused_identity and held_polls == 2
        assert supervised._process.returncode == -signal.SIGKILL
        assert refused == []
    finally:
        _force_cleanup(supervised, monkeypatch)


def test_reaped_child_group_is_confirmed_empty_without_forking_ps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervised = _start_node(
        tmp_path,
        "import signal\nwhile True:\n    signal.pause()\n",
        "reaped-group",
    )
    try:
        os.killpg(supervised.pid, signal.SIGKILL)
        supervised._process.wait(timeout=30.0)
        assert not _group_exists(supervised.pid)
        refused = _ps_that_never_finishes(monkeypatch, _GROUP_QUERY)

        supervised.close()

        assert refused == []
    finally:
        _force_cleanup(supervised, monkeypatch)


def test_sigterm_ignoring_child_is_killed_when_an_ancestor_needs_ps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "term-ignored"
    supervised = _start_node(
        tmp_path,
        "import pathlib, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text('ignored')\n"
        "while True:\n"
        "    signal.pause()\n",
        "term-ignored",
    )
    try:
        _wait_for(marker.exists, "child ignores SIGTERM")
        # An ancestor PROC_PIDTBSDINFO refuses (e.g. root-owned ``login``):
        # the walk has to find its parent and group some other way.
        refused_ancestor = os.getppid()
        real_inventory = process_module._inventory_process

        def inventory(pid: int) -> Any:
            if pid == refused_ancestor:
                raise PermissionError(1, "Operation not permitted")
            return real_inventory(pid)

        monkeypatch.setattr(process_module, "_inventory_process", inventory)
        refused = _ps_that_never_finishes(monkeypatch, _ANCESTOR_QUERY)

        supervised.close()

        assert supervised._process.returncode == -signal.SIGKILL
        assert not _group_exists(supervised.pid)
        assert refused == []
    finally:
        _force_cleanup(supervised, monkeypatch)


def test_sigterm_ignoring_child_is_killed_when_group_listing_never_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live owned leader's group is not empty; listing it cannot change that.

    Under churn one ``/bin/ps -axo`` fork overran its sub-deadline by ~0.8 s
    while the leader was alive (grace and TERM-wait polls, or the first poll
    right after SIGKILL), consuming the budget before SIGKILL or before the
    reap that would have shown the group gone.
    """

    marker = tmp_path / "term-ignored"
    supervised = _start_node(
        tmp_path,
        "import pathlib, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text('ignored')\n"
        "while True:\n"
        "    signal.pause()\n",
        "listing-never-finishes",
    )
    try:
        _wait_for(marker.exists, "child ignores SIGTERM")
        refused = _ps_that_never_finishes(monkeypatch, _GROUP_QUERY)

        supervised.close()

        assert supervised._process.returncode == -signal.SIGKILL
        assert not _group_exists(supervised.pid)
        assert refused == []
    finally:
        _force_cleanup(supervised, monkeypatch)


def test_reaped_leader_with_live_group_member_is_still_killed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: an empty-group shortcut must never hide a surviving member."""

    marker = tmp_path / "member-pid"
    supervised = _start_node(
        tmp_path,
        "import os, pathlib, signal\n"
        "member = os.fork()\n"
        "if member == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    pathlib.Path({str(marker)!r} + '.tmp').write_text(str(os.getpid()))\n"
        f"    os.replace({str(marker)!r} + '.tmp', {str(marker)!r})\n"
        "    while True:\n"
        "        signal.pause()\n"
        "while True:\n"
        "    signal.pause()\n",
        "live-member",
    )
    member = 0
    try:
        _wait_for(marker.exists, "group member started")
        member = int(marker.read_text())
        assert os.getpgid(member) == supervised.pid
        os.kill(supervised.pid, signal.SIGKILL)
        supervised._process.wait(timeout=30.0)
        assert _group_exists(supervised.pid)

        supervised.close()

        _wait_for(lambda: not _group_exists(supervised.pid), "member killed")
    finally:
        if member:
            # The leader was reaped above; its numeric PGID may now be reused.
            # A surviving member is signalled only while it still belongs to
            # the exact session created by this test.
            try:
                still_owned = (
                    os.getpgid(member) == supervised.pid
                    and os.getsid(member) == supervised.pid
                )
            except ProcessLookupError:
                still_owned = False
            if still_owned:
                try:
                    os.kill(member, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        _force_cleanup(supervised, monkeypatch)
