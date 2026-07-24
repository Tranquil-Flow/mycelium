from __future__ import annotations

from builtins import BaseExceptionGroup
from contextlib import contextmanager
import ctypes
import errno
from functools import partial
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, NamedTuple


WORKTREE_ROOT = Path(__file__).resolve().parents[2]
TEMP_ROOT_PREFIX = "myc-seed-e2e-"
OWNED_ROOT_SENTINEL = ".mycelium-owned-root-v1"
PROCESS_WAIT_TIMEOUT = POLL_TIMEOUT = 2.0
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_OWNED_ROOT_SENTINEL_KEY = secrets.token_bytes(32)
_OWNED_ROOT_SENTINEL_VERSION = b"mycelium.native-root.v1"


def _rename_noreplace_at(
    source_fd: int,
    source: str,
    destination_fd: int,
    destination: str,
) -> None:
    """Atomically rename one directory entry without replacing another."""
    libc = ctypes.CDLL(None, use_errno=True)
    arguments = (
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
    )
    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as error:
            raise OSError(
                errno.ENOTSUP,
                "renameatx_np(RENAME_EXCL) is unavailable",
            ) from error
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        call_arguments = (*arguments, _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as error:
            raise OSError(
                errno.ENOTSUP,
                "renameat2(RENAME_NOREPLACE) is unavailable",
            ) from error
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        call_arguments = (*arguments, _RENAME_NOREPLACE)
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory rename is unsupported",
        )
    ctypes.set_errno(0)
    if rename(*call_arguments) != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination,
        )


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


class TempRootIdentity(NamedTuple):
    root: Path
    parent: Path
    root_metadata: os.stat_result
    sentinel_metadata: os.stat_result
    sentinel_content: bytes


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


def _sentinel_content(root: Path, nonce: bytes) -> bytes:
    binding = (
        _OWNED_ROOT_SENTINEL_VERSION
        + b"\0"
        + os.fsencode(root.name)
        + b"\0"
        + nonce
    )
    signature = hmac.digest(
        _OWNED_ROOT_SENTINEL_KEY,
        binding,
        hashlib.sha256,
    )
    return b"v1:" + nonce.hex().encode() + b":" + signature.hex().encode() + b"\n"


def _validate_sentinel_content(root: Path, content: bytes) -> None:
    parts = content.removesuffix(b"\n").split(b":")
    _require(
        len(parts) == 3 and parts[0] == b"v1",
        "owned-root sentinel has invalid content",
    )
    try:
        nonce = bytes.fromhex(parts[1].decode("ascii"))
        supplied_signature = bytes.fromhex(parts[2].decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AssertionError("owned-root sentinel has invalid encoding") from error
    _require(
        len(nonce) == 32 and len(supplied_signature) == hashlib.sha256().digest_size,
        "owned-root sentinel has invalid binding length",
    )
    expected = _sentinel_content(root, nonce)
    _require(
        hmac.compare_digest(content, expected),
        "owned-root sentinel authentication failed",
    )


def create_owned_temp_root() -> Path:
    """Create an owned temporary root and its birth-time run sentinel."""
    root = Path(tempfile.mkdtemp(prefix=TEMP_ROOT_PREFIX))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(root / OWNED_ROOT_SENTINEL, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        content = _sentinel_content(root, secrets.token_bytes(32))
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            _require(written > 0, "owned-root sentinel write made no progress")
            offset += written
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        try:
            os.unlink(root / OWNED_ROOT_SENTINEL)
        except FileNotFoundError:
            pass
        os.rmdir(root)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return root


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


def _bound_entry_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _tree_entry_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return _bound_entry_identity(metadata)[:-1]


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


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_REGULAR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _lstat_at(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _read_owned_sentinel(
    root_fd: int,
    root: Path,
) -> tuple[os.stat_result, bytes]:
    try:
        inspected = _lstat_at(root_fd, OWNED_ROOT_SENTINEL)
    except FileNotFoundError as error:
        raise AssertionError("owned-root sentinel is missing") from error
    _require(
        stat.S_ISREG(inspected.st_mode),
        "owned-root sentinel is not a regular file",
    )
    _require(
        inspected.st_uid == os.getuid(),
        "owned-root sentinel owner is not the current user",
    )
    _require(
        stat.S_IMODE(inspected.st_mode) == 0o600,
        "owned-root sentinel mode is not 0600",
    )
    _require(
        inspected.st_nlink == 1,
        "owned-root sentinel has an external hardlink",
    )
    descriptor = os.open(
        OWNED_ROOT_SENTINEL,
        _REGULAR_FLAGS,
        dir_fd=root_fd,
    )
    try:
        opened = os.fstat(descriptor)
        _require(
            _bound_entry_identity(opened) == _bound_entry_identity(inspected),
            "owned-root sentinel changed while opening",
        )
        content = os.read(descriptor, 1_025)
        _require(
            len(content) <= 1_024 and os.read(descriptor, 1) == b"",
            "owned-root sentinel is oversized",
        )
        final = os.fstat(descriptor)
        _require(
            _bound_entry_identity(final) == _bound_entry_identity(opened),
            "owned-root sentinel changed while reading",
        )
    finally:
        os.close(descriptor)
    _validate_sentinel_content(root, content)
    return final, content


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
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        opened = os.fstat(root_fd)
        _require(
            _stable_entry_identity(opened) == _stable_entry_identity(original),
            "captured root changed while opening",
        )
        sentinel_metadata, sentinel_content = _read_owned_sentinel(root_fd, root)
    finally:
        os.close(root_fd)
    return TempRootIdentity(
        root,
        parent,
        original,
        sentinel_metadata,
        sentinel_content,
    )


def remove_temp_root(
    identity: TempRootIdentity,
    *,
    owned_socket_paths: tuple[Path, ...] = (),
) -> None:
    root, parent, original, sentinel_metadata, sentinel_content = identity
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
    parent_fd = os.open(parent, _DIRECTORY_FLAGS)
    root_fd: int | None = None
    nonce = iter(range(1_000_000))
    mutation_occurred = False
    root_quarantine = (
        f"{root.name}.quarantine-{os.getpid()}-{time.monotonic_ns()}"
    )
    owned_socket_relatives: set[tuple[str, ...]] = set()
    for socket_path in owned_socket_paths:
        _require(
            isinstance(socket_path, Path) and socket_path.is_absolute(),
            "cleanup refused invalid owned socket path",
        )
        canonical_socket = socket_path.parent.resolve(strict=True) / socket_path.name
        try:
            relative_socket = canonical_socket.relative_to(root)
        except ValueError as error:
            raise AssertionError(
                f"cleanup refused socket outside owned root {canonical_socket}"
            ) from error
        relative_parts = relative_socket.parts
        _require(
            bool(relative_parts)
            and all(part not in {"", ".", ".."} for part in relative_parts),
            "cleanup refused unsafe owned socket path",
        )
        _require(
            relative_parts not in owned_socket_relatives,
            "cleanup refused duplicate owned socket path",
        )
        owned_socket_relatives.add(relative_parts)

    def quarantine_name() -> str:
        return (
            f".myc-quarantine-{os.getpid()}-"
            f"{time.monotonic_ns()}-{next(nonce)}"
        )

    def entry_flags(metadata: os.stat_result) -> int:
        if stat.S_ISDIR(metadata.st_mode):
            return _DIRECTORY_FLAGS
        _require(
            stat.S_ISREG(metadata.st_mode),
            "cleanup preflight refused symlink or nonregular special entry",
        )
        return _REGULAR_FLAGS

    def preflight_tree(
        directory_fd: int,
        relative: tuple[str, ...] = (),
    ) -> dict[tuple[str, ...], tuple[int, ...]]:
        before_directory = os.fstat(directory_fd)
        snapshot: dict[tuple[str, ...], tuple[int, ...]] = {}
        for name in sorted(os.listdir(directory_fd)):
            inspected = _lstat_at(directory_fd, name)
            _require(
                inspected.st_uid == os.getuid(),
                f"cleanup preflight refused foreign owner at {'/'.join((*relative, name))}",
            )
            _require(
                inspected.st_dev == original.st_dev,
                f"cleanup preflight refused mount boundary at {'/'.join((*relative, name))}",
            )
            if stat.S_ISREG(inspected.st_mode):
                _require(
                    inspected.st_nlink == 1,
                    f"cleanup preflight refused external hardlink at {'/'.join((*relative, name))}",
                )
            elif (
                stat.S_ISSOCK(inspected.st_mode)
                and (*relative, name) in owned_socket_relatives
            ):
                _require(
                    inspected.st_nlink == 1,
                    f"cleanup preflight refused unsafe owned socket links at {'/'.join((*relative, name))}",
                )
                path = (*relative, name)
                snapshot[path] = _tree_entry_identity(inspected)
                _require(
                    _tree_entry_identity(_lstat_at(directory_fd, name))
                    == snapshot[path],
                    f"cleanup preflight owned socket changed at {'/'.join(path)}",
                )
                continue
            else:
                _require(
                    stat.S_ISDIR(inspected.st_mode),
                    f"cleanup preflight refused symlink or nonregular special entry at {'/'.join((*relative, name))}",
                )
                _require(
                    inspected.st_nlink >= 2,
                    f"cleanup preflight refused unsafe directory links at {'/'.join((*relative, name))}",
                )
            child_fd = os.open(
                name,
                entry_flags(inspected),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                _require(
                    _bound_entry_identity(opened)
                    == _bound_entry_identity(inspected),
                    f"cleanup preflight entry changed while opening {'/'.join((*relative, name))}",
                )
                path = (*relative, name)
                snapshot[path] = _tree_entry_identity(opened)
                if stat.S_ISDIR(opened.st_mode):
                    snapshot.update(preflight_tree(child_fd, path))
                final = _lstat_at(directory_fd, name)
                _require(
                    _bound_entry_identity(final)
                    == _bound_entry_identity(opened),
                    f"cleanup preflight entry changed after opening {'/'.join(path)}",
                )
            finally:
                os.close(child_fd)
        after_directory = os.fstat(directory_fd)
        _require(
            _bound_entry_identity(after_directory)
            == _bound_entry_identity(before_directory),
            f"cleanup preflight directory changed at {'/'.join(relative) or '.'}",
        )
        return snapshot

    def verify_sentinel() -> None:
        current_metadata, current_content = _read_owned_sentinel(root_fd, root)
        _require(
            _bound_entry_identity(current_metadata)
            == _bound_entry_identity(sentinel_metadata),
            "cleanup refused replaced owned-root sentinel",
        )
        _require(
            hmac.compare_digest(current_content, sentinel_content),
            "cleanup refused tampered owned-root sentinel",
        )

    def restore_entry(
        directory_fd: int,
        quarantine: str,
        original_name: str,
        expected: tuple[int, ...],
    ) -> None:
        quarantined = _lstat_at(directory_fd, quarantine)
        _require(
            _tree_entry_identity(quarantined) == expected,
            f"cleanup refused changed quarantined entry {quarantine}",
        )
        try:
            _rename_noreplace_at(
                directory_fd,
                quarantine,
                directory_fd,
                original_name,
            )
        except FileExistsError as error:
            raise AssertionError(
                f"cleanup preserved quarantined entry as {quarantine}; "
                f"{original_name} became occupied"
            ) from error
        except OSError as error:
            raise AssertionError(
                f"cleanup could not atomically restore {quarantine} to "
                f"{original_name}; quarantine preserved"
            ) from error
        _require(
            _tree_entry_identity(_lstat_at(directory_fd, original_name))
            == expected,
            "cleanup could not safely restore quarantined entry",
        )

    def empty_directory(
        directory_fd: int,
        snapshot: dict[tuple[str, ...], tuple[int, ...]],
        relative: tuple[str, ...] = (),
    ) -> None:
        nonlocal mutation_occurred
        names = sorted(
            os.listdir(directory_fd),
            key=lambda name: (name == OWNED_ROOT_SENTINEL, name),
        )
        for name in names:
            path = (*relative, name)
            expected = snapshot[path]
            inspected = _lstat_at(directory_fd, name)
            _require(
                _tree_entry_identity(inspected) == expected,
                f"cleanup refused changed preflight entry {'/'.join(path)}",
            )
            if stat.S_ISSOCK(inspected.st_mode):
                _require(
                    path in owned_socket_relatives,
                    f"cleanup refused unregistered socket {'/'.join(path)}",
                )
                quarantine = quarantine_name()
                acquired = False
                try:
                    _rename_noreplace_at(
                        directory_fd,
                        name,
                        directory_fd,
                        quarantine,
                    )
                    acquired = True
                    _require(
                        _tree_entry_identity(
                            _lstat_at(directory_fd, quarantine)
                        )
                        == expected,
                        f"cleanup refused replaced owned socket {'/'.join(path)}",
                    )
                    os.unlink(quarantine, dir_fd=directory_fd)
                    mutation_occurred = True
                except BaseException as cleanup_error:
                    if acquired:
                        try:
                            restore_entry(
                                directory_fd,
                                quarantine,
                                name,
                                expected,
                            )
                        except BaseException as restoration_error:
                            raise BaseExceptionGroup(
                                "cleanup and owned-socket restoration both failed",
                                [cleanup_error, restoration_error],
                            ) from None
                    raise
                continue
            child_fd = os.open(
                name,
                entry_flags(inspected),
                dir_fd=directory_fd,
            )
            quarantine = quarantine_name()
            entry_mutation_before = mutation_occurred
            acquired = False
            try:
                opened = os.fstat(child_fd)
                _require(
                    _tree_entry_identity(opened) == expected,
                    f"cleanup refused entry changed while opening {'/'.join(path)}",
                )
                _rename_noreplace_at(
                    directory_fd,
                    name,
                    directory_fd,
                    quarantine,
                )
                acquired = True
                _require(
                    _tree_entry_identity(_lstat_at(directory_fd, quarantine))
                    == expected,
                    f"cleanup refused replaced quarantined entry {'/'.join(path)}",
                )
                if stat.S_ISDIR(opened.st_mode):
                    empty_directory(child_fd, snapshot, path)
                    final_opened = os.fstat(child_fd)
                    final_quarantined = _lstat_at(directory_fd, quarantine)
                    _require(
                        _stable_entry_identity(final_opened)
                        == _stable_entry_identity(final_quarantined)
                        and final_opened.st_nlink == 2,
                        f"cleanup refused changed emptied directory {'/'.join(path)}",
                    )
                    os.rmdir(quarantine, dir_fd=directory_fd)
                else:
                    _require(
                        _tree_entry_identity(os.fstat(child_fd)) == expected,
                        f"cleanup refused changed regular file {'/'.join(path)}",
                    )
                    os.unlink(quarantine, dir_fd=directory_fd)
                mutation_occurred = True
            except BaseException as cleanup_error:
                if acquired and mutation_occurred == entry_mutation_before:
                    try:
                        restore_entry(
                            directory_fd,
                            quarantine,
                            name,
                            expected,
                        )
                    except BaseException as restoration_error:
                        raise BaseExceptionGroup(
                            "cleanup and quarantine restoration both failed",
                            [cleanup_error, restoration_error],
                        ) from None
                raise
            finally:
                os.close(child_fd)

    def restore_root(
        preflight: dict[tuple[str, ...], tuple[int, ...]],
    ) -> None:
        try:
            _require(
                _temp_root_identity(
                    _lstat_at(parent_fd, root_quarantine)
                )
                == _temp_root_identity(original),
                "cleanup refused changed root quarantine restoration",
            )
            _require(
                preflight_tree(root_fd) == preflight,
                "cleanup could not restore changed quarantine tree",
            )
            verify_sentinel()
            _rename_noreplace_at(
                parent_fd,
                root_quarantine,
                parent_fd,
                root.name,
            )
        except FileExistsError as error:
            raise AssertionError(
                f"cleanup preserved quarantine {root_quarantine}; "
                f"{root.name} became occupied"
            ) from error
        except AssertionError:
            raise
        except OSError as error:
            raise AssertionError(
                f"cleanup could not atomically restore {root_quarantine} to "
                f"{root.name}; quarantine preserved"
            ) from error
        _require(
            _temp_root_identity(_lstat_at(parent_fd, root.name))
            == _temp_root_identity(original),
            "cleanup could not safely restore root quarantine",
        )

    try:
        parent_metadata = os.fstat(parent_fd)
        _validate_temp_root_metadata(
            original,
            parent_metadata,
            phase="captured",
        )
        try:
            current = _lstat_at(parent_fd, root.name)
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
        root_fd = os.open(root.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
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
        verify_sentinel()
        preflight = preflight_tree(root_fd)
        verify_sentinel()
        _rename_noreplace_at(
            parent_fd,
            root.name,
            parent_fd,
            root_quarantine,
        )
        try:
            _require(
                _stable_entry_identity(
                    _lstat_at(parent_fd, root_quarantine)
                )
                == _stable_entry_identity(opened_root),
                "cleanup refused replaced root quarantine",
            )
            _require(
                preflight_tree(root_fd) == preflight,
                "cleanup tree changed during root quarantine acquisition",
            )
            verify_sentinel()
            empty_directory(root_fd, preflight)
            final_root = os.fstat(root_fd)
            _require(
                _stable_entry_identity(
                    _lstat_at(parent_fd, root_quarantine)
                )
                == _stable_entry_identity(final_root)
                and final_root.st_nlink == 2,
                "cleanup refused changed empty root quarantine",
            )
            os.rmdir(root_quarantine, dir_fd=parent_fd)
            mutation_occurred = True
        except BaseException as cleanup_error:
            if mutation_occurred:
                partial_error = AssertionError(
                    f"partial cleanup mutation occurred; retained quarantine "
                    f"{root_quarantine} and refused root rollback"
                )
                raise BaseExceptionGroup(
                    "cleanup failed after partial mutation",
                    [cleanup_error, partial_error],
                ) from None
            try:
                restore_root(preflight)
            except BaseException as restoration_error:
                raise BaseExceptionGroup(
                    "cleanup and quarantine restoration both failed",
                    [cleanup_error, restoration_error],
                ) from None
            raise
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
    owned_socket_paths: list[Path] = []
    for node_id, client in node_processes.items():
        owner = owned_groups.get(node_id)
        if owner is None or getattr(client, "process", None) is not owner.process:
            record(
                AssertionError("cleanup client lacks exact registered process"),
                f"client-identity[{node_id}]",
            )
            continue
        owned_socket_path = vars(client).get("owned_socket_path")
        if owned_socket_path is not None:
            if not isinstance(owned_socket_path, Path):
                record(
                    AssertionError("cleanup client has invalid owned socket path"),
                    f"client-socket[{node_id}]",
                )
            else:
                owned_socket_paths.append(owned_socket_path)
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

    attempt(
        "root-removal",
        partial(
            remove_temp_root,
            root_identity,
            owned_socket_paths=tuple(owned_socket_paths),
        ),
        None,
    )
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
