from __future__ import annotations

from builtins import BaseExceptionGroup
from contextlib import contextmanager
from functools import partial
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, NamedTuple


WORKTREE_ROOT = Path(__file__).resolve().parents[2]
TEMP_ROOT_PREFIX = "myc-seed-e2e-"
PROCESS_WAIT_TIMEOUT = POLL_TIMEOUT = 2.0


class ProcessRecord(NamedTuple):
    pid: int
    ppid: int
    pgid: int
    started_at: str
    comm: str


PathIdentity = tuple[int, int, int, int, int, int, int, int, int | None]


class OwnedMember(NamedTuple):
    record: ProcessRecord
    executable: Path
    executable_identity: PathIdentity
    worktree: Path
    worktree_identity: PathIdentity


class OwnedGroup(NamedTuple):
    node_id: str
    process: subprocess.Popen[str]
    pid: int
    pgid: int
    sid: int
    executable: Path
    worktree: Path
    executable_identity: PathIdentity
    worktree_identity: PathIdentity
    leader: ProcessRecord | None
    members: tuple[OwnedMember, ...] = ()


TempRootIdentity = tuple[Path, Path, os.stat_result]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def same_canonical_file(actual: str | Path, expected: Path) -> bool:
    try:
        return Path(actual).resolve(strict=True).samefile(expected.resolve(strict=True))
    except (FileNotFoundError, OSError):
        return False


def path_identity(path: str | Path) -> PathIdentity:
    metadata = os.stat(Path(path).resolve(strict=True), follow_symlinks=False)
    birthtime = getattr(metadata, "st_birthtime", None)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        None if birthtime is None else int(birthtime * 1_000_000_000),
    )


def process_cwd(pid: int) -> Path:
    if sys.platform != "darwin":
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    result = subprocess.run(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
    )
    lines = result.stdout.splitlines()
    _require(
        lines[:2] == [f"p{pid}", "fcwd"] and len(lines) == 3,
        f"unexpected cwd inventory for process {pid}",
    )
    _require(
        lines[2].startswith("n/") and "\x00" not in lines[2],
        f"invalid cwd inventory for process {pid}",
    )
    return Path(lines[2][1:])


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
        _require(len(fields) == 9, f"unparseable process inventory row: {line!r}")
        pid, ppid, pgid = map(int, fields[:3])
        _require(pid not in processes, f"duplicate process inventory PID: {pid}")
        processes[pid] = ProcessRecord(
            pid, ppid, pgid, " ".join(fields[3:8]), fields[8]
        )
    return processes


def _capture_member(
    owner: OwnedGroup,
    record: ProcessRecord,
    expected_executable: Path,
) -> OwnedMember:
    executable = expected_executable.resolve(strict=True)
    _require(
        type(record.pid) is int and record.pid > 1,
        "member PID must be an exact positive integer",
    )
    _require(record.pgid == owner.pgid, "member process group changed")
    _require(
        record.ppid == os.getpid()
        if record.pid == owner.pid
        else record.ppid in {member.record.pid for member in owner.members},
        "member has unexpected ancestry",
    )
    _require(
        same_canonical_file(record.comm, executable),
        "member has unexpected executable",
    )
    executable_identity = path_identity(executable)
    _require(
        path_identity(record.comm) == executable_identity,
        "member executable identity changed",
    )
    _require(_kernel_identity(record.pid, owner), "member kernel identity changed")
    cwd = process_cwd(record.pid).resolve(strict=True)
    _require(
        same_canonical_file(cwd, owner.worktree),
        "member has unexpected working directory",
    )
    cwd_identity = path_identity(cwd)
    _require(
        cwd_identity == owner.worktree_identity,
        "member working directory identity changed",
    )
    return OwnedMember(
        record,
        executable,
        executable_identity,
        cwd,
        cwd_identity,
    )


def register_owned_group(
    owners: dict[str, OwnedGroup],
    node_id: str,
    process: subprocess.Popen[str],
    expected_executable: Path,
    *,
    worktree: Path = WORKTREE_ROOT,
) -> OwnedGroup:
    pid = process.pid
    executable = expected_executable.resolve(strict=True)
    worktree = worktree.resolve(strict=True)
    owner = OwnedGroup(
        node_id,
        process,
        pid,
        pid,
        pid,
        executable,
        worktree,
        path_identity(executable),
        path_identity(worktree),
        None,
    )
    owners[node_id] = owner
    _validate_owner(owner, None)
    _require(process.poll() is None, f"registration requires live process {pid}")
    _require(
        os.getpgid(pid) == pid and os.getsid(pid) == pid,
        f"registration requires isolated session {pid}",
    )
    inventory = process_inventory()
    leader = inventory.get(pid)
    _require(leader is not None, f"registration missing process {pid}")
    _require(
        leader.ppid == os.getpid() and leader.pgid == pid,
        "registration found unexpected leader ancestry",
    )
    member = _capture_member(owner, leader, executable)
    _require(process.poll() is None, f"registration lost live process {pid}")
    owners[node_id] = owner._replace(leader=leader, members=(member,))
    return owners[node_id]


def register_owned_member(
    owners: dict[str, OwnedGroup],
    node_id: str,
    record: ProcessRecord,
    expected_executable: Path,
) -> OwnedGroup:
    owner = owners.get(node_id)
    _require(owner is not None, f"member registration missing owner {node_id}")
    _validate_owner(owner, None)
    registered_pids = {member.record.pid for member in owner.members}
    _require(record.pid not in registered_pids, "duplicate member registration")
    member = _capture_member(owner, record, expected_executable)
    owners[node_id] = owner._replace(members=(*owner.members, member))
    return owners[node_id]


def _validate_owner(
    owner: OwnedGroup, inventory: dict[int, ProcessRecord] | None
) -> None:
    identifiers = (owner.pid, owner.pgid, owner.sid, owner.process.pid)
    _require(
        all(type(value) is int and value > 1 for value in identifiers),
        "owner identifiers must be exact positive integers",
    )
    _require(
        owner.pid == owner.pgid == owner.sid == owner.process.pid,
        "owner process/session identifiers changed",
    )
    _require(
        same_canonical_file(owner.worktree, WORKTREE_ROOT),
        "owner worktree is outside the registered repository",
    )
    _require(
        path_identity(owner.executable) == owner.executable_identity,
        "owner executable path identity changed",
    )
    _require(
        path_identity(owner.worktree) == owner.worktree_identity,
        "owner worktree path identity changed",
    )
    member_pids = [member.record.pid for member in owner.members]
    _require(
        len(member_pids) == len(set(member_pids)),
        "duplicate registered group member",
    )
    if owner.leader is not None:
        _require(bool(owner.members), "registered leader is missing member fingerprint")
        _require(
            owner.members[0].record == owner.leader
            and owner.members[0].record.pid == owner.pid,
            "registered leader fingerprint changed",
        )
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
    _require(owner.pid not in protected_pids, f"cleanup refused PID {owner.pid}")
    _require(
        owner.pgid not in protected_groups,
        f"cleanup refused group {owner.pgid}",
    )


def _kernel_identity(pid: int, owner: OwnedGroup) -> bool:
    try:
        pgid, sid = os.getpgid(pid), os.getsid(pid)
    except ProcessLookupError:
        return False
    _require(
        pgid == owner.pgid and sid == owner.sid,
        f"process {pid} kernel group/session identity changed",
    )
    return True


def _revalidate_owned_group(
    owner: OwnedGroup, inventory: dict[int, ProcessRecord]
) -> bool:
    _validate_owner(owner, inventory)
    live_members = {
        item.pid: item for item in inventory.values() if item.pgid == owner.pgid
    }
    if not live_members:
        return False
    registered_members = {member.record.pid: member for member in owner.members}
    _require(
        set(live_members) == set(registered_members),
        f"cleanup refused unregistered, missing, or reused group member "
        f"{owner.pgid}: live={sorted(live_members)} "
        f"registered={sorted(registered_members)}",
    )
    _require(
        owner.process.poll() is None,
        f"refused exited leader {owner.pid}",
    )
    _require(owner.leader is not None, "group leader was never registered")
    for pid, member in registered_members.items():
        current = live_members[pid]
        _require(current == member.record, f"process {pid} fingerprint changed")
        if pid == owner.pid:
            _require(
                current.ppid == os.getpid(),
                f"leader {pid} ancestry changed",
            )
        else:
            _require(
                current.ppid in registered_members,
                f"member {pid} ancestry changed",
            )
        _require(
            same_canonical_file(current.comm, member.executable),
            f"process {pid} executable changed",
        )
        _require(
            path_identity(current.comm) == member.executable_identity,
            f"process {pid} executable identity changed",
        )
        cwd = process_cwd(pid)
        _require(
            same_canonical_file(cwd, member.worktree),
            f"process {pid} working directory changed",
        )
        _require(
            path_identity(cwd) == member.worktree_identity,
            f"process {pid} working directory identity changed",
        )
        _require(
            _kernel_identity(pid, owner),
            f"process {pid} disappeared during identity check",
        )
    return True


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
    expected_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    parent = socket_root.parent.resolve(strict=True)
    root = parent / socket_root.name
    _require(
        parent == expected_parent,
        f"capture refused temporary parent {parent}",
    )
    _require(
        root.parent == parent and root.name.startswith(TEMP_ROOT_PREFIX),
        f"capture refused temporary root {root}",
    )
    original = os.lstat(root)
    parent_metadata = os.stat(parent, follow_symlinks=False)
    _validate_temp_root_metadata(
        original,
        parent_metadata,
        phase="captured",
    )
    _require(not os.path.ismount(root), "captured root must not be a mount")
    return root, parent, original


def _temp_root_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _stable_entry_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (*_temp_root_identity(metadata), metadata.st_nlink)


def _validate_temp_root_metadata(
    metadata: os.stat_result,
    parent_metadata: os.stat_result,
    *,
    phase: str,
) -> None:
    _require(stat.S_ISDIR(metadata.st_mode), f"{phase} root is not a directory")
    _require(
        metadata.st_uid == os.getuid(),
        f"{phase} root owner is not the current user",
    )
    _require(
        stat.S_IMODE(metadata.st_mode) == 0o700,
        f"{phase} root mode is not 0700",
    )
    _require(
        metadata.st_nlink >= 2,
        f"{phase} root has unsafe link semantics",
    )
    _require(
        metadata.st_dev == parent_metadata.st_dev,
        f"{phase} root has unsafe mount semantics",
    )


def remove_temp_root(identity: TempRootIdentity) -> None:
    root, parent, original = identity
    expected_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    _require(
        parent == expected_parent,
        f"cleanup refused temporary parent {parent}",
    )
    _require(
        root.parent == parent and root.name.startswith(TEMP_ROOT_PREFIX),
        f"cleanup refused temporary root {root}",
    )
    _require(stat.S_ISDIR(original.st_mode), "cleanup root is not a directory")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = os.open(parent, directory_flags)
    root_fd: int | None = None
    nonce = iter(range(1_000_000))

    def quarantine_name() -> str:
        return (
            f".myc-quarantine-{os.getpid()}-"
            f"{time.monotonic_ns()}-{next(nonce)}"
        )

    def lstat_at(directory_fd: int, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

    def entry_flags(metadata: os.stat_result) -> int:
        common = getattr(os, "O_CLOEXEC", 0)
        if stat.S_ISDIR(metadata.st_mode):
            return directory_flags
        if stat.S_ISLNK(metadata.st_mode):
            if hasattr(os, "O_SYMLINK"):
                return os.O_RDONLY | os.O_SYMLINK | common
            if hasattr(os, "O_PATH"):
                return os.O_PATH | getattr(os, "O_NOFOLLOW", 0) | common
            raise AssertionError("cleanup cannot open symlink identity")
        if stat.S_ISREG(metadata.st_mode):
            return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | common
        if hasattr(os, "O_PATH"):
            return os.O_PATH | getattr(os, "O_NOFOLLOW", 0) | common
        if hasattr(os, "O_EVTONLY"):
            return os.O_EVTONLY | getattr(os, "O_NOFOLLOW", 0) | common
        raise AssertionError("cleanup cannot open special entry identity")

    def restore_quarantine(
        directory_fd: int,
        quarantine: str,
        original_name: str,
        quarantined: os.stat_result,
    ) -> None:
        try:
            lstat_at(directory_fd, original_name)
        except FileNotFoundError:
            os.rename(
                quarantine,
                original_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            _require(
                _stable_entry_identity(
                    lstat_at(directory_fd, original_name)
                )
                == _stable_entry_identity(quarantined),
                "cleanup could not safely restore raced replacement",
            )
            return
        raise AssertionError(
            f"cleanup preserved raced replacement as {quarantine}; "
            f"{original_name} became occupied"
        )

    def remove_open_entry(
        directory_fd: int,
        name: str,
        opened_fd: int,
        opened: os.stat_result,
        *,
        quarantine: str | None = None,
    ) -> None:
        quarantine = quarantine or quarantine_name()
        os.rename(
            name,
            quarantine,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        quarantined = lstat_at(directory_fd, quarantine)
        if _stable_entry_identity(quarantined) != _stable_entry_identity(
            opened
        ):
            restore_quarantine(directory_fd, quarantine, name, quarantined)
            raise AssertionError(
                f"cleanup refused replaced or changed quarantined entry {name}; "
                "replacement restored"
            )
        if stat.S_ISDIR(opened.st_mode):
            empty_open_directory(opened_fd)
        final_quarantined = lstat_at(directory_fd, quarantine)
        final_opened = os.fstat(opened_fd)
        if stat.S_ISDIR(opened.st_mode):
            unchanged = (
                _temp_root_identity(final_quarantined)
                == _temp_root_identity(final_opened)
                == _temp_root_identity(opened)
                and final_quarantined.st_nlink == final_opened.st_nlink == 2
            )
        else:
            unchanged = (
                _stable_entry_identity(final_quarantined)
                == _stable_entry_identity(final_opened)
                == _stable_entry_identity(opened)
            )
        if not unchanged:
            restore_quarantine(
                directory_fd,
                quarantine,
                name,
                final_quarantined,
            )
            raise AssertionError(
                f"cleanup refused changed quarantined metadata for {name}; "
                "entry restored"
            )
        if stat.S_ISDIR(opened.st_mode):
            os.rmdir(quarantine, dir_fd=directory_fd)
        else:
            os.unlink(quarantine, dir_fd=directory_fd)

    def remove_socket_entry(
        directory_fd: int,
        name: str,
        inspected: os.stat_result,
    ) -> None:
        _require(
            stat.S_ISSOCK(inspected.st_mode),
            f"cleanup refused unopenable special entry {name}",
        )
        quarantine = quarantine_name()
        os.rename(
            name,
            quarantine,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        quarantined = lstat_at(directory_fd, quarantine)
        if _stable_entry_identity(quarantined) != _stable_entry_identity(
            inspected
        ):
            restore_quarantine(directory_fd, quarantine, name, quarantined)
            raise AssertionError(
                f"cleanup refused replaced socket {name}; replacement restored"
            )
        _require(
            _stable_entry_identity(lstat_at(directory_fd, quarantine))
            == _stable_entry_identity(inspected),
            f"cleanup refused changed quarantined socket {name}",
        )
        os.unlink(quarantine, dir_fd=directory_fd)

    def empty_open_directory(directory_fd: int) -> None:
        for name in os.listdir(directory_fd):
            inspected = lstat_at(directory_fd, name)
            try:
                child_fd = os.open(
                    name,
                    entry_flags(inspected),
                    dir_fd=directory_fd,
                )
            except OSError:
                if not stat.S_ISSOCK(inspected.st_mode):
                    raise
                remove_socket_entry(directory_fd, name, inspected)
                continue
            try:
                opened = os.fstat(child_fd)
                _require(
                    _stable_entry_identity(opened)
                    == _stable_entry_identity(inspected),
                    f"cleanup refused entry changed while opening {name}",
                )
                remove_open_entry(directory_fd, name, child_fd, opened)
            finally:
                os.close(child_fd)

    try:
        parent_metadata = os.fstat(parent_fd)
        _validate_temp_root_metadata(
            original,
            parent_metadata,
            phase="captured",
        )
        try:
            current = lstat_at(parent_fd, root.name)
        except FileNotFoundError:
            return
        _validate_temp_root_metadata(
            current,
            parent_metadata,
            phase="current",
        )
        _require(
            _temp_root_identity(current) == _temp_root_identity(original),
            "cleanup refused current root metadata mismatch",
        )
        _require(not os.path.ismount(root), "current root must not be a mount")
        root_fd = os.open(root.name, directory_flags, dir_fd=parent_fd)
        opened_root = os.fstat(root_fd)
        _validate_temp_root_metadata(
            opened_root,
            parent_metadata,
            phase="opened",
        )
        _require(
            _stable_entry_identity(opened_root)
            == _stable_entry_identity(current),
            "cleanup root metadata changed while opening",
        )
        remove_open_entry(
            parent_fd,
            root.name,
            root_fd,
            opened_root,
            quarantine=(
                f"{root.name}.quarantine-{os.getpid()}-{time.monotonic_ns()}"
            ),
        )
    finally:
        if root_fd is not None:
            os.close(root_fd)
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
    graceful_stop_requested: list[OwnedGroup] = []
    for node_id, client in node_processes.items():
        owner = owned_groups.get(node_id)
        if owner is None or getattr(client, "process", None) is not owner.process:
            record(
                AssertionError("cleanup client lacks exact registered process"),
                f"client-identity[{node_id}]",
            )
            continue
        request_stop = getattr(type(client), "request_stop", None)
        if request_stop is None:
            continue
        if attempt(
            f"stop-request[{node_id}]",
            lambda: request_stop(client),
            False,
        ) is not False:
            graceful_stop_requested.append(owner)
    for owner in graceful_stop_requested:
        try:
            owner.process.wait(timeout=PROCESS_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        except BaseException as error:
            record(error, f"stop-wait[{owner.node_id}]")
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
        if not process_exited or owner in remaining:
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
