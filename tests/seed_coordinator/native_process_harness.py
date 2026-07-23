from __future__ import annotations

from builtins import BaseExceptionGroup
from contextlib import contextmanager
from functools import partial
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
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


def same_canonical_file(actual: str | Path, expected: Path) -> bool:
    try:
        actual_path = Path(actual).resolve(strict=True)
        expected_path = expected.resolve(strict=True)
        return actual_path == expected_path and actual_path.samefile(expected_path)
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
        try:
            pid, ppid, pgid = map(int, fields[:3])
        except ValueError as error:
            raise AssertionError(f"invalid process inventory row: {line!r}") from error
        assert pid not in processes, f"duplicate process inventory PID: {pid}"
        started_at = " ".join(fields[3:8])
        processes[pid] = ProcessRecord(pid, ppid, pgid, started_at, fields[8])
    return processes


def _validate_owner(
    owner: OwnedGroup, inventory: dict[int, ProcessRecord] | None
) -> None:
    identifiers = (owner.pid, owner.pgid, owner.sid, owner.process.pid)
    assert all(type(value) is int and value > 1 for value in identifiers)
    assert owner.pid == owner.pgid == owner.sid == owner.process.pid
    assert same_canonical_file(owner.worktree, WORKTREE_ROOT)
    assert same_canonical_file(owner.executable, Path(sys.executable))
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
            if record.ppid <= 1:
                break
            cursor = record.ppid
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
        unchanged = owner.leader == leader
        assert unchanged or (owner.leader is None and owner.process.poll() is None), (
            f"cleanup refused changed/reused group leader {owner.pid}"
        )
        assert leader.pgid == owner.pgid
        return _kernel_identity(owner.pid, owner)

    members = [item for item in inventory.values() if item.pgid == owner.pgid]
    if not members:
        return False
    assert owner.leader is not None, (
        f"cleanup cannot prove leader-exited group {owner.pgid} without fingerprint"
    )
    assert owner.process.poll() is not None, (
        f"cleanup inventory lost live leader for group {owner.pgid}"
    )
    identities = [_kernel_identity(member.pid, owner) for member in members]
    return any(identities)


def _fallback_live_leader(owner: OwnedGroup) -> bool:
    _validate_owner(owner, None)
    assert owner.process.poll() is None, (
        f"cleanup cannot prove exited leader {owner.pgid}"
    )
    return _kernel_identity(owner.pid, owner)


def signal_owned_groups(
    owners: list[OwnedGroup], process_signal: signal.Signals
) -> tuple[list[BaseException], bool]:
    errors: list[BaseException] = []
    inventory_uncertain = False
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


def cleanup_node_processes(
    node_processes: dict[str, Any],
    *,
    owned_groups: dict[str, OwnedGroup],
    known_pids: set[int],
    socket_root: Path,
) -> None:
    cleanup_errors: list[BaseException] = []
    inventory_failures: list[bool] = []
    owners = list(owned_groups.values())

    def record(error: BaseException, phase: str) -> None:
        error.add_note(f"native-Iroh cleanup phase: {phase}")
        cleanup_errors.append(error)

    def attempt(phase: str, action: Any, fallback: Any) -> Any:
        try:
            return action()
        except BaseException as error:
            inventory_failures.append(True)
            record(error, phase)
            return fallback

    def signal_groups(
        groups: list[OwnedGroup], requested: signal.Signals, phase: str
    ) -> None:
        try:
            errors, uncertain = signal_owned_groups(groups, requested)
        except BaseException as error:
            inventory_failures.append(True)
            record(error, phase)
            return
        if uncertain:
            inventory_failures.append(True)
        for error in errors:
            record(error, phase)

    def wait_for_processes(label: str) -> None:
        for owner in owners:
            try:
                owner.process.wait(timeout=PROCESS_WAIT_TIMEOUT)
            except BaseException as error:
                record(error, f"wait[{label}][{owner.node_id}]")

    failed_inventory = ({owner.pgid: [] for owner in owners}, sorted(known_pids))
    inventory_action = partial(owned_inventory, owners, known_pids=known_pids)
    attempt("initial-inventory", inventory_action, failed_inventory)
    stop_results = {node_id: [] for node_id in node_processes}

    def stop_client(node_id: str, client: Any) -> None:
        try:
            client.stop()
        except BaseException as error:
            stop_results[node_id].append(error)

    stop_threads = {
        node_id: threading.Thread(
            target=stop_client, args=(node_id, client), daemon=True
        )
        for node_id, client in node_processes.items()
    }
    for thread in stop_threads.values():
        thread.start()
    timed_out: set[str] = set()

    def join_stops(timeout: float, *, report_timeout: bool = False) -> None:
        deadline = time.monotonic() + timeout
        for node_id, thread in stop_threads.items():
            thread.join(max(0.0, deadline - time.monotonic()))
            if report_timeout and thread.is_alive():
                timed_out.add(node_id)
                record(
                    TimeoutError(f"stop timed out for {node_id}"), f"stop[{node_id}]"
                )
        for node_id, errors in stop_results.items():
            while errors:
                record(errors.pop(0), f"stop[{node_id}]")

    join_stops(STOP_TIMEOUT, report_timeout=True)
    remaining = owners
    for requested, label in ((signal.SIGTERM, "TERM"), (signal.SIGKILL, "KILL")):
        signal_groups(remaining, requested, f"signal[{label}]")
        join_stops(STOP_REJOIN_TIMEOUT)
        wait_for_processes(label)
        poll = partial(groups_still_present, remaining, timeout=POLL_TIMEOUT)
        remaining = attempt(f"poll[{label}]", poll, remaining)

    for owner in owners:
        for stream_name in ("stdin", "stdout", "stderr"):
            try:
                stream = getattr(owner.process, stream_name, None)
                if stream is not None and not stream.closed:
                    stream.close()
            except BaseException as error:
                record(error, f"close[{owner.node_id}.{stream_name}]")

    try:
        assert socket_root.name.startswith(TEMP_ROOT_PREFIX)
        shutil.rmtree(socket_root)
    except FileNotFoundError:
        pass
    except BaseException as error:
        record(error, "root-removal")

    final_inventory = attempt("final-inventory", inventory_action, failed_inventory)
    if any(final_inventory):
        error = AssertionError(f"native-Iroh process leak: {final_inventory}")
        record(error, "leak-check")
    for node_id in timed_out:
        if stop_threads[node_id].is_alive():
            error = AssertionError(f"cleanup thread survived for {node_id}")
            record(error, f"stop-thread[{node_id}]")
    if inventory_failures:
        error = AssertionError("cleanup could not prove an empty process inventory")
        record(error, "inventory-proof")
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
    cleanup = partial(
        cleanup_node_processes,
        node_processes,
        owned_groups=owned_groups,
        known_pids=known_pids,
        socket_root=socket_root,
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
