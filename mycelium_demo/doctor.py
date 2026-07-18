from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any

PROTOCOL = "mycelium.release_doctor_preflight.v1"
DEFAULT_COMMANDS = ("python3.14", "git", "cargo", "node", "npm")
DEFAULT_REQUIRED_FILES = (
    "contracts/contract-manifest.v1.json",
    "scripts/contract_audit.py",
    "native/iroh_transport/Cargo.lock",
    "ui/web/package-lock.json",
    "docs/automation/2026-07-18-manual-driver-handover.md",
)
CLAIM_BOUNDARY = (
    "read-only local environment preflight only; no physical-host evidence; "
    "no qualification consumption; no release-readiness claim"
)
RELEASE_BLOCKERS = [
    "request gateway waits for qualification authority commit and schema freeze",
    "recovery integration waits for stable KV and iroh path plus physical base-route proof",
    "Observatory request and qualification event adapter waits for both producer contracts",
    "physical two-Mac qualification requires explicit authorization plus a staging and cleanup plan",
]

Which = Callable[[str], str | None]
PortProbe = Callable[[int], bool]
_COMMAND_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9._-]+\Z")


def canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def local_tcp_port_available(port: int) -> bool:
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok is True, "detail": detail}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _absolute_path(value: Path | str) -> Path | None:
    if not isinstance(value, (str, Path)):
        return None
    try:
        raw = os.fspath(value)
        if type(raw) is not str or not raw or "\0" in raw:
            return None
        expanded = Path(raw).expanduser()
        return Path(os.path.abspath(os.fspath(expanded)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _path_snapshot(
    path: Path, *, follow_symlinks: bool
) -> tuple[os.stat_result | None, bool]:
    try:
        return path.stat(follow_symlinks=follow_symlinks), True
    except FileNotFoundError:
        return None, True
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, False


def _same_snapshot(
    first: os.stat_result | None, second: os.stat_result | None
) -> bool:
    if first is None or second is None:
        return first is None and second is None
    return _same_identity(first, second) and stat.S_IFMT(first.st_mode) == stat.S_IFMT(
        second.st_mode
    )


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("directory path must be absolute")
    descriptor = os.open(os.sep, _directory_flags())
    try:
        for part in path.parts[1:]:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(opened.st_mode) or not _same_identity(opened, named):
                    raise OSError("directory identity mismatch")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_repository_root(path: Path | None) -> tuple[int | None, os.stat_result | None]:
    if path is None:
        return None, None
    try:
        descriptor = _open_absolute_directory(path)
        identity = os.fstat(descriptor)
        if not stat.S_ISDIR(identity.st_mode):
            os.close(descriptor)
            return None, None
        return descriptor, identity
    except (OSError, TypeError, ValueError):
        return None, None


def _repository_root_stable(path: Path | None, identity: os.stat_result | None) -> bool:
    if path is None or identity is None:
        return False
    descriptor: int | None = None
    try:
        descriptor = _open_absolute_directory(path)
        return _same_identity(identity, os.fstat(descriptor))
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _state_checks(state_value: Path | str, root: Path | None) -> list[dict[str, Any]]:
    state = _absolute_path(state_value)
    resolved: Path | None = None
    state_initial: os.stat_result | None = None
    parent_initial: os.stat_result | None = None
    symlinked = False
    directory = False
    writable = False
    stable = False
    parent_writable = False

    if state is not None:
        try:
            symlinked = state.is_symlink()
            state_initial, state_known = _path_snapshot(state, follow_symlinks=False)
            resolved = state.resolve(strict=False)
            parent = resolved.parent
            parent_initial, parent_known = _path_snapshot(parent, follow_symlinks=True)
            exists = state_initial is not None
            directory = exists and stat.S_ISDIR(state_initial.st_mode)
            writable = (
                os.access(state, os.W_OK | os.X_OK)
                if exists and directory and not symlinked
                else not exists
            )
            parent_writable = (
                parent_initial is not None
                and stat.S_ISDIR(parent_initial.st_mode)
                and os.access(parent, os.W_OK | os.X_OK)
            )

            final_symlinked = state.is_symlink()
            state_final, state_final_known = _path_snapshot(
                state, follow_symlinks=False
            )
            final_resolved = state.resolve(strict=False)
            parent_final, parent_final_known = _path_snapshot(
                parent, follow_symlinks=True
            )
            initial_mode_symlink = state_initial is not None and stat.S_ISLNK(
                state_initial.st_mode
            )
            final_mode_symlink = state_final is not None and stat.S_ISLNK(
                state_final.st_mode
            )
            stable = (
                state_known
                and state_final_known
                and parent_known
                and parent_final_known
                and symlinked == initial_mode_symlink
                and final_symlinked == final_mode_symlink
                and symlinked == final_symlinked
                and _same_snapshot(state_initial, state_final)
                and _same_snapshot(parent_initial, parent_final)
                and resolved == final_resolved
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            resolved = None

    outside = root is not None and resolved is not None and not _inside(resolved, root)
    target_safe = (
        resolved is not None
        and not symlinked
        and writable
        and (state_initial is None or directory)
        and stable
    )
    parent_writable = parent_writable and stable

    return [
        _check(
            "state_directory_outside_source",
            outside,
            "state directory resolves outside repository root"
            if outside
            else "state directory must resolve outside repository root",
        ),
        _check(
            "state_directory_target_safe",
            target_safe,
            "state directory target is absent or a writable real directory"
            if target_safe
            else "state directory target is unsafe or unusable",
        ),
        _check(
            "state_directory_parent_writable",
            parent_writable,
            "state-directory immediate parent exists and is writable"
            if parent_writable
            else "state-directory immediate parent is missing or not writable",
        ),
    ]


def _collect(iterable: Iterable[Any]) -> tuple[tuple[Any, ...], bool]:
    if isinstance(iterable, (str, bytes, bytearray)):
        return (), False
    try:
        return tuple(iterable), True
    except Exception:
        return (), False


def _valid_command(command: Any) -> bool:
    return type(command) is str and _COMMAND_NAME.fullmatch(command) is not None


def _valid_required_file(relative: Any) -> bool:
    if type(relative) is not str or not relative or not relative.isascii():
        return False
    if "\\" in relative:
        return False
    pure = PurePosixPath(relative)
    if pure.is_absolute() or pure.as_posix() != relative or not pure.parts:
        return False
    return all(
        part not in {"", ".", ".."} and _PATH_PART.fullmatch(part) is not None
        for part in pure.parts
    )


def _valid_port(port: Any) -> bool:
    return type(port) is int and 1 <= port <= 65535


def _validated_inputs(
    name: str,
    iterable: Iterable[Any],
    validator: Callable[[Any], bool],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    values, collected = _collect(iterable)
    valid = (
        collected
        and all(validator(value) for value in values)
        and len(values) == len(set(values))
    )
    if not valid:
        return (), _check(name, False, "input collection is invalid")
    return tuple(sorted(values)), _check(name, True, "input collection is valid")


def _regular_file_under_root(root_fd: int, relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    try:
        descriptor = os.dup(root_fd)
    except (OSError, TypeError, ValueError):
        return False
    try:
        for part in parts[:-1]:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                matches = stat.S_ISDIR(opened.st_mode) and _same_identity(opened, named)
            except BaseException:
                os.close(child)
                raise
            if not matches:
                os.close(child)
                return False
            os.close(descriptor)
            descriptor = child

        file_descriptor = os.open(parts[-1], _file_flags(), dir_fd=descriptor)
        try:
            opened = os.fstat(file_descriptor)
            named = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            return stat.S_ISREG(opened.st_mode) and _same_identity(opened, named)
        finally:
            os.close(file_descriptor)
    except (OSError, TypeError, ValueError):
        return False
    finally:
        os.close(descriptor)


def _command_checks(commands: tuple[str, ...], which: Which) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for index, command in enumerate(commands, start=1):
        name = f"command:{index:04d}"
        try:
            found = which(command)
        except Exception:
            checks.append(_check(name, False, "required command probe failed"))
            continue
        if found is None:
            checks.append(_check(name, False, "required command was not found"))
        elif type(found) is not str or not found or "\0" in found:
            checks.append(
                _check(name, False, "required command probe returned an invalid result")
            )
        else:
            checks.append(_check(name, True, "required command was found"))
    return checks


def _file_checks(root_fd: int | None, required_files: tuple[str, ...]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for index, relative in enumerate(required_files, start=1):
        ok = root_fd is not None and _regular_file_under_root(root_fd, relative)
        checks.append(
            _check(
                f"file:{index:04d}",
                ok,
                "required file is present and safe"
                if ok
                else "required file is unavailable or unsafe",
            )
        )
    return checks


def _port_checks(ports: tuple[int, ...], port_available: PortProbe) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for index, port in enumerate(ports, start=1):
        name = f"port:{index:04d}"
        try:
            available = port_available(port)
        except Exception:
            checks.append(_check(name, False, "local TCP port probe failed"))
            continue
        if type(available) is not bool:
            checks.append(
                _check(name, False, "local TCP port probe returned an invalid result")
            )
        elif available:
            checks.append(_check(name, True, "local TCP port is available"))
        else:
            checks.append(_check(name, False, "local TCP port is unavailable"))
    return checks


def run_preflight(
    *,
    repo_root: Path | str,
    state_dir: Path | str,
    commands: Iterable[str] = DEFAULT_COMMANDS,
    required_files: Iterable[str] = DEFAULT_REQUIRED_FILES,
    ports: Iterable[int] = (),
    which: Which = shutil.which,
    port_available: PortProbe = local_tcp_port_available,
) -> dict[str, Any]:
    root = _absolute_path(repo_root)
    root_fd, root_identity = _open_repository_root(root)
    checks: list[dict[str, Any]] = [
        _check(
            "repository_root",
            root_fd is not None,
            "repository root is a real directory"
            if root_fd is not None
            else "repository root is unavailable or unsafe",
        )
    ]

    try:
        checks.extend(_state_checks(state_dir, root if root_fd is not None else None))
        valid_commands, command_input = _validated_inputs(
            "commands_input", commands, _valid_command
        )
        valid_files, file_input = _validated_inputs(
            "required_files_input", required_files, _valid_required_file
        )
        valid_ports, port_input = _validated_inputs("ports_input", ports, _valid_port)
        checks.extend((command_input, file_input, port_input))
        checks.extend(_command_checks(valid_commands, which))
        checks.extend(_file_checks(root_fd, valid_files))
        checks.extend(_port_checks(valid_ports, port_available))
        stable = _repository_root_stable(root, root_identity)
        checks.append(
            _check(
                "repository_root_stable",
                stable,
                "repository root identity remained stable during preflight"
                if stable
                else "repository root identity changed during preflight",
            )
        )
    finally:
        if root_fd is not None:
            os.close(root_fd)

    return {
        "protocol": PROTOCOL,
        "local_preflight_ok": all(check["ok"] is True for check in checks),
        "route_ready": False,
        "release_ready": False,
        "qualification_evaluated": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "release_blockers": list(RELEASE_BLOCKERS),
        "checks": checks,
    }
