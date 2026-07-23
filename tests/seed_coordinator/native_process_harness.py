from __future__ import annotations

from builtins import BaseExceptionGroup
from contextlib import contextmanager
from functools import partial
import os
from pathlib import Path
import queue
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator, NamedTuple


WORKTREE_ROOT = Path(__file__).resolve().parents[2]
TEMP_ROOT_PREFIX = "myc-seed-e2e-"
STOP_TIMEOUT, STOP_REJOIN_TIMEOUT = 2.0, 0.25
PROCESS_WAIT_TIMEOUT = POLL_TIMEOUT = 2.0


class ProcessRecord(NamedTuple):
    pid: int
    ppid: int
    pgid: int
    started_at: str
    comm: str


class OwnedGroup(NamedTuple):
    node_id: str
    process: subprocess.Popen[str]
    pid: int
    pgid: int
    sid: int
    executable: Path
    worktree: Path
    leader: ProcessRecord | None


TempRootIdentity = tuple[Path, Path, os.stat_result]


def same_canonical_file(actual: str | Path, expected: Path) -> bool:
    try:
        return Path(actual).resolve(strict=True).samefile(expected.resolve(strict=True))
    except (FileNotFoundError, OSError):
        return False


def process_inventory() -> dict[int, ProcessRecord]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,lstart=,comm="],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
    )
    processes: dict[int, ProcessRecord] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=8)
        assert len(fields) == 9, f"unparseable process inventory row: {line!r}"
        pid, ppid, pgid = map(int, fields[:3])
        assert pid not in processes, f"duplicate process inventory PID: {pid}"
        processes[pid] = ProcessRecord(
            pid, ppid, pgid, " ".join(fields[3:8]), fields[8]
        )
    return processes


def register_owned_group(
    owners: dict[str, OwnedGroup],
    node_id: str,
    process: subprocess.Popen[str],
    expected_executable: Path,
    *,
    worktree: Path = WORKTREE_ROOT,
) -> OwnedGroup:
    pid = process.pid
    owner = OwnedGroup(
        node_id, process, pid, pid, pid, expected_executable, worktree, None
    )
    owners[node_id] = owner
    _validate_owner(owner, None)
    assert process.poll() is None, f"registration requires live process {pid}"
    assert os.getpgid(pid) == pid and os.getsid(pid) == pid
    inventory = process_inventory()
    leader = inventory.get(pid)
    assert leader is not None, f"registration missing process {pid}"
    assert leader.ppid == os.getpid() and leader.pgid == pid
    assert same_canonical_file(leader.comm, expected_executable), (
        "unexpected executable"
    )
    assert process.poll() is None, f"registration lost live process {pid}"
    owners[node_id] = owner._replace(leader=leader)
    return owners[node_id]


def _validate_owner(
    owner: OwnedGroup, inventory: dict[int, ProcessRecord] | None
) -> None:
    identifiers = (owner.pid, owner.pgid, owner.sid, owner.process.pid)
    assert all(type(value) is int and value > 1 for value in identifiers)
    assert owner.pid == owner.pgid == owner.sid == owner.process.pid
    assert same_canonical_file(owner.worktree, WORKTREE_ROOT)
    protected_pids = {os.getpid(), os.getppid()}
    protected_groups = {os.getpgrp(), os.getpgid(os.getppid())}
    if inventory is not None:
        cursor = os.getpid()
        visited: set[int] = set()
        while cursor in inventory and cursor not in visited:
            visited.add(cursor)
            record = inventory[cursor]
            protected_pids.add(record.pid)
            protected_groups.add(record.pgid)
            cursor = record.ppid if record.ppid > 1 else 0
    assert owner.pid not in protected_pids
    assert owner.pgid not in protected_groups, f"cleanup refused group {owner.pgid}"


def _kernel_identity(pid: int, owner: OwnedGroup) -> bool:
    try:
        pgid, sid = os.getpgid(pid), os.getsid(pid)
    except ProcessLookupError:
        return False
    assert pgid == owner.pgid and sid == owner.sid
    return True


def _revalidate_owned_group(
    owner: OwnedGroup, inventory: dict[int, ProcessRecord]
) -> bool:
    _validate_owner(owner, inventory)
    leader = inventory.get(owner.pid)
    if leader is not None:
        assert owner.process.poll() is None, f"refused exited leader {owner.pid}"
        assert owner.leader is not None and owner.leader == leader
        assert same_canonical_file(leader.comm, owner.executable)
        return _kernel_identity(owner.pid, owner)

    members = [item for item in inventory.values() if item.pgid == owner.pgid]
    if not members:
        return False
    raise AssertionError(
        f"cleanup refused uncertain leader-absent group {owner.pgid}: "
        f"{sorted(item.pid for item in members)}"
    )


def _fallback_live_leader(owner: OwnedGroup) -> bool:
    _validate_owner(owner, None)
    assert owner.leader is not None, f"cleanup cannot prove group {owner.pgid}"
    assert owner.process.poll() is None, f"cleanup cannot prove leader {owner.pgid}"
    return _kernel_identity(owner.pid, owner)


def signal_owned_groups(
    owners: list[OwnedGroup], process_signal: signal.Signals
) -> tuple[list[BaseException], bool]:
    errors, inventory_uncertain = [], False
    for owner in owners:
        try:
            owner.process.poll()
            inventory = process_inventory()
        except BaseException as error:
            inventory_uncertain = True
            note = f"native-Iroh cleanup context: ps-before-signal[{owner.pgid}]"
            error.add_note(note)
            errors.append(error)
            try:
                should_signal = _fallback_live_leader(owner)
            except BaseException as fallback_error:
                errors.append(fallback_error)
                continue
        else:
            try:
                should_signal = _revalidate_owned_group(owner, inventory)
            except BaseException as error:
                inventory_uncertain = True
                errors.append(error)
                continue
        if not should_signal:
            continue
        try:
            os.killpg(owner.pgid, process_signal)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(error)
    return errors, inventory_uncertain


def owned_inventory(
    owners: list[OwnedGroup], *, known_pids: set[int]
) -> tuple[dict[int, list[int]], list[int]]:
    inventory = process_inventory()
    groups: dict[int, list[int]] = {}
    for owner in owners:
        if _revalidate_owned_group(owner, inventory):
            groups[owner.pgid] = sorted(
                item.pid for item in inventory.values() if item.pgid == owner.pgid
            )
    return groups, sorted(pid for pid in known_pids if pid in inventory)


def groups_still_present(
    owners: list[OwnedGroup], *, timeout: float
) -> list[OwnedGroup]:
    deadline = time.monotonic() + timeout
    while True:
        inventory = process_inventory()
        remaining = [
            owner for owner in owners if _revalidate_owned_group(owner, inventory)
        ]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.02)


def capture_temp_root(socket_root: Path) -> TempRootIdentity:
    parent = socket_root.parent.resolve(strict=True)
    root = parent / socket_root.name
    return root, parent, os.lstat(root)


def remove_temp_root(identity: TempRootIdentity) -> None:
    root, parent, original = identity
    expected_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    assert parent == expected_parent, f"cleanup refused temporary parent {parent}"
    assert root.parent == parent and root.name.startswith(TEMP_ROOT_PREFIX)
    assert stat.S_ISDIR(original.st_mode)
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        lstat_at = partial(os.stat, dir_fd=parent_fd, follow_symlinks=False)
        try:
            current = lstat_at(root.name)
        except FileNotFoundError:
            return
        assert (current.st_dev, current.st_ino, current.st_mode) == (
            original.st_dev,
            original.st_ino,
            original.st_mode,
        ), f"cleanup refused replaced temporary root {root}"
        assert stat.S_ISDIR(current.st_mode)
        shutil.rmtree(root.name, dir_fd=parent_fd)
        try:
            lstat_at(root.name)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError(f"cleanup failed to remove {root}")
    finally:
        os.close(parent_fd)


def cleanup_node_processes(
    node_processes: dict[str, Any],
    *,
    owned_groups: dict[str, OwnedGroup],
    known_pids: set[int],
    root_identity: TempRootIdentity,
) -> None:
    cleanup_errors: list[BaseException] = []
    inventory_failures: list[bool] = []
    owners = list(owned_groups.values())

    def record(error: BaseException, phase: str) -> None:
        error.add_note(f"native-Iroh cleanup phase: {phase}")
        cleanup_errors.append(error)

    def attempt(
        phase: str, action: Any, fallback: Any, *, inventory: bool = False
    ) -> Any:
        try:
            return action()
        except BaseException as error:
            if inventory:
                inventory_failures.append(True)
            record(error, phase)
            return fallback

    failed_inventory = ({owner.pgid: [] for owner in owners}, sorted(known_pids))
    inventory_action = partial(owned_inventory, owners, known_pids=known_pids)
    attempt("initial-inventory", inventory_action, failed_inventory, inventory=True)
    stop_results: queue.SimpleQueue[tuple[str, BaseException]] = queue.SimpleQueue()

    def stop_client(node_id: str, client: Any) -> None:
        try:
            client.stop()
        except BaseException as error:
            stop_results.put((node_id, error))

    stop_threads: dict[str, threading.Thread] = {}
    for node_id, client in node_processes.items():
        thread = attempt(
            f"stop-thread-create[{node_id}]",
            partial(
                threading.Thread,
                target=stop_client,
                args=(node_id, client),
                daemon=True,
                name=f"native-iroh-cleanup-stop-{node_id}",
            ),
            None,
        )
        if thread is not None:
            stop_threads[node_id] = thread
    started_threads: set[str] = set()
    for node_id, thread in stop_threads.items():
        if attempt(f"stop-thread-start[{node_id}]", thread.start, False) is None:
            started_threads.add(node_id)

    def drain_stop_errors() -> None:
        while True:
            try:
                node_id, error = stop_results.get_nowait()
            except queue.Empty:
                return
            record(error, f"stop[{node_id}]")

    def thread_alive(node_id: str) -> bool:
        return attempt(
            f"stop-thread-state[{node_id}]", stop_threads[node_id].is_alive, True
        )

    def join_stops(timeout: float, *, report_timeout: bool = False) -> None:
        deadline = time.monotonic() + timeout
        for node_id in started_threads:
            attempt(
                f"stop-thread-join[{node_id}]",
                lambda: stop_threads[node_id].join(
                    max(0.0, deadline - time.monotonic())
                ),
                None,
            )
            if report_timeout and thread_alive(node_id):
                record(TimeoutError(f"stop timeout: {node_id}"), f"stop[{node_id}]")
        drain_stop_errors()

    join_stops(STOP_TIMEOUT, report_timeout=True)
    remaining = owners
    for requested, label in ((signal.SIGTERM, "TERM"), (signal.SIGKILL, "KILL")):
        phase = f"signal[{label}]"
        errors, uncertain = attempt(
            phase,
            lambda: signal_owned_groups(remaining, requested),
            ([], True),
            inventory=True,
        )
        inventory_failures.extend([True] * uncertain)
        for error in errors:
            record(error, phase)
        join_stops(STOP_REJOIN_TIMEOUT)
        for owner in owners:
            attempt(
                f"wait[{label}][{owner.node_id}]",
                lambda: owner.process.wait(timeout=PROCESS_WAIT_TIMEOUT),
                None,
            )
        poll = partial(groups_still_present, remaining, timeout=POLL_TIMEOUT)
        remaining = attempt(f"poll[{label}]", poll, remaining, inventory=True)

    for owner in owners:
        process_exited = (
            attempt(f"close-skip[{owner.node_id}]", owner.process.poll, None)
            is not None
        )
        stop_alive = owner.node_id in started_threads and thread_alive(owner.node_id)
        if not process_exited or stop_alive or owner in remaining:
            record(
                AssertionError("unsafe stream close"), f"close-skip[{owner.node_id}]"
            )
            continue
        for stream_name in ("stdin", "stdout", "stderr"):
            phase = f"close[{owner.node_id}.{stream_name}]"
            stream = attempt(phase, lambda: getattr(owner.process, stream_name), None)
            if stream is not None and not attempt(phase, lambda: stream.closed, True):
                attempt(phase, stream.close, None)

    attempt("root-removal", partial(remove_temp_root, root_identity), None)
    final_inventory = attempt(
        "final-inventory", inventory_action, failed_inventory, inventory=True
    )
    if any(final_inventory):
        record(AssertionError(f"native-Iroh leak: {final_inventory}"), "leak-check")
    join_stops(STOP_REJOIN_TIMEOUT)
    for node_id in started_threads:
        if thread_alive(node_id):
            record(AssertionError("cleanup thread survived"), f"stop-thread[{node_id}]")
    drain_stop_errors()
    if inventory_failures:
        record(AssertionError("unproven empty process inventory"), "inventory-proof")
    if cleanup_errors:
        raise BaseExceptionGroup("native-Iroh process cleanup failed", cleanup_errors)


@contextmanager
def node_process_cleanup(
    node_processes: dict[str, Any],
    *,
    owned_groups: dict[str, OwnedGroup],
    known_pids: set[int],
    socket_root: Path,
) -> Iterator[None]:
    root_identity = capture_temp_root(socket_root)
    cleanup = partial(
        cleanup_node_processes,
        node_processes,
        owned_groups=owned_groups,
        known_pids=known_pids,
        root_identity=root_identity,
    )
    try:
        yield
    except BaseException as body_error:
        try:
            cleanup()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "test body and native-Iroh cleanup both failed",
                [body_error, cleanup_error],
            ) from cleanup_error
        raise
    else:
        cleanup()
