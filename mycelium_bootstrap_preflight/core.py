"""Deterministic, read-only bootstrap prerequisites for Mycelium."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

PROTOCOL = "mycelium.bootstrap_preflight.v1"

DEFAULT_LOCK_SPECS: tuple[dict[str, str], ...] = (
    {
        "path": "ui/web/package-lock.json",
        "digest": "sha256",
        "type": "npm-package-lock-v3",
    },
    {
        "path": "native/iroh_transport/Cargo.lock",
        "digest": "sha256",
        "type": "cargo-lock-v4",
    },
)
DEFAULT_LIMITS: dict[str, int] = {
    "max_lockfiles": 8,
    "max_path_bytes": 1024,
    "max_component_bytes": 255,
    "max_component_depth": 32,
    "max_lockfile_bytes": 4 * 1024 * 1024,
    "max_bytes_hashed": 8 * 1024 * 1024,
    "max_command_output_bytes": 4096,
    "max_git_output_bytes": 64 * 1024,
    "max_dependency_entries": 4096,
}

_EXPECTED_TYPES = {spec["path"]: spec["type"] for spec in DEFAULT_LOCK_SPECS}
_TOOL_PROBES: tuple[tuple[str, tuple[str, ...], re.Pattern[str]], ...] = (
    ("python3.14", ("python3.14", "--version"), re.compile(r"Python (\d+\.\d+\.\d+)(?:\s.*)?")),
    ("cargo", ("cargo", "--version"), re.compile(r"cargo (\d+\.\d+\.\d+)(?:\s.*)?")),
    ("rustc", ("rustc", "--version"), re.compile(r"rustc (\d+\.\d+\.\d+)(?:\s.*)?")),
    (
        "rustfmt",
        ("rustfmt", "--version"),
        re.compile(r"rustfmt (\d+\.\d+\.\d+)(?:-[A-Za-z0-9.]+)?(?:\s.*)?"),
    ),
    (
        "cargo-clippy",
        ("cargo-clippy", "--version"),
        re.compile(r"(?:clippy|cargo-clippy) (\d+\.\d+\.\d+)(?:\s.*)?"),
    ),
    ("node", ("node", "--version"), re.compile(r"v(\d+\.\d+\.\d+)(?:\s.*)?")),
    ("npm", ("npm", "--version"), re.compile(r"(\d+\.\d+\.\d+)(?:\s.*)?")),
)
_TOOL_COMMANDS = frozenset(probe[1] for probe in _TOOL_PROBES)
_GIT_STATIC_COMMANDS = frozenset(
    {
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "rev-parse", "--verify", "HEAD"),
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
    }
)
_GATES: tuple[dict[str, object], ...] = (
    {
        "code": "bootstrap_preflight_tests",
        "command": "python3.14 -m pytest -q tests/bootstrap_preflight",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "full_python_tests",
        "command": "python3.14 -m pytest -q",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "contract_audit",
        "command": "python3.14 scripts/contract_audit.py",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "python_compileall",
        "command": "python3.14 -m compileall -q .",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "git_diff_check",
        "command": "git diff --check",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "ruff",
        "command": "ruff check mycelium_bootstrap_preflight tests/bootstrap_preflight",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "release_security_audit",
        "command": "python3.14 scripts/release_security_audit.py",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "claim_boundary_audit",
        "command": "python3.14 scripts/claim_boundary_audit.py",
        "cwd": ".",
        "executed": False,
    },
    {
        "code": "rust_fmt",
        "command": "cargo fmt --check",
        "cwd": "native/iroh_transport",
        "executed": False,
    },
    {
        "code": "rust_clippy",
        "command": "cargo clippy --all-targets --all-features -- -D warnings",
        "cwd": "native/iroh_transport",
        "executed": False,
    },
    {
        "code": "rust_tests",
        "command": "cargo test",
        "cwd": "native/iroh_transport",
        "executed": False,
    },
    {
        "code": "ui_check",
        "command": "npm run check",
        "cwd": "ui/web",
        "executed": False,
    },
)


class CommandResult(NamedTuple):
    """Bounded command result returned by an injected runner."""

    returncode: int
    stdout: bytes
    stderr: bytes


class DependencyRequirements(NamedTuple):
    """Validated dependency names derived from the already-read lockfiles."""

    node_paths: tuple[tuple[str, ...], ...]
    cargo_archives: tuple[str, ...]


Runner = Callable[[Sequence[str], Path, float, int], CommandResult]


def canonical_json(value: Any) -> str:
    """Serialize canonical report JSON with exactly one trailing newline."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _allowed_git_inventory(command: tuple[str, ...]) -> bool:
    prefix = ("git", "ls-files", "-z", "--stage", "--")
    return (
        command[: len(prefix)] == prefix
        and command[len(prefix) :] == tuple(sorted(_EXPECTED_TYPES))
    )


def default_runner(
    argv: Sequence[str], cwd: Path, timeout: float, max_output: int
) -> CommandResult:
    """Run one exact read-only Git or version probe without a shell."""
    command = tuple(argv)
    if command not in _TOOL_COMMANDS | _GIT_STATIC_COMMANDS and not _allowed_git_inventory(command):
        raise ValueError("command is not an allowed read-only probe")
    environment = os.environ.copy()
    if command[0] == "git":
        environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        shell=False,
        env=environment,
    )
    stdout = bytes(completed.stdout)
    stderr = bytes(completed.stderr)
    if len(stdout) + len(stderr) > max_output:
        return CommandResult(completed.returncode, stdout[: max_output + 1], b"")
    return CommandResult(completed.returncode, stdout, stderr)


def _empty_source(reason_codes: Iterable[str]) -> dict[str, object]:
    return {
        "ok": False,
        "reason_codes": sorted(set(reason_codes)),
        "revision": None,
        "clean": False,
        "lockfiles": [],
        "bytes_hashed": 0,
        "tracked_lockfiles": 0,
        "limits": {
            key: DEFAULT_LIMITS[key]
            for key in (
                "max_lockfiles",
                "max_path_bytes",
                "max_component_bytes",
                "max_component_depth",
                "max_lockfile_bytes",
                "max_bytes_hashed",
            )
        },
    }


def _base_report(reason: str) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "preflight_ready": False,
        "blockers": [reason],
        "source_lockfile_prerequisites": _empty_source([reason]),
        "toolchain_availability": {
            "ok": False,
            "reason_codes": ["toolchain_not_evaluated"],
            "tools": [],
        },
        "dependency_materialization": {
            "ok": False,
            "reason_codes": ["dependency_materialization_not_evaluated"],
            "node_modules": {"materialized": False},
            "rust_cache": {"materialized": False},
        },
        "gates_requiring_execution": [dict(gate) for gate in _GATES],
        "verification_bundle_executed": False,
        "fresh_checkout_proven": False,
        "physical_qualification_evaluated": False,
        "route_ready": False,
        "release_ready": False,
    }


def _validate_limits(limits: Mapping[str, int]) -> dict[str, int] | None:
    if set(limits) != set(DEFAULT_LIMITS):
        return None
    normalized: dict[str, int] = {}
    for key in DEFAULT_LIMITS:
        value = limits.get(key)
        if type(value) is not int or value <= 0:
            return None
        normalized[key] = value
    return normalized


def _safe_root(root_value: str | os.PathLike[str]) -> tuple[Path | None, int | None]:
    descriptor: int | None = None
    try:
        raw = os.fspath(root_value)
        if not isinstance(raw, str) or not raw or "\0" in raw:
            return None, None
        root = Path(os.path.abspath(raw))
        if root.resolve(strict=True) != root:
            return None, None
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return None, None
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(root, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            os.close(descriptor)
            descriptor = None
            return None, None
        return root, descriptor
    except (OSError, RuntimeError, ValueError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None, None


def _root_identity_matches(root: Path, descriptor: int) -> bool:
    try:
        metadata = root.lstat()
        opened = os.fstat(descriptor)
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and root.resolve(strict=True) == root
            and (metadata.st_dev, metadata.st_ino) == (opened.st_dev, opened.st_ino)
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _call(
    runner: Runner,
    argv: Sequence[str],
    root: Path,
    *,
    max_output: int,
) -> tuple[CommandResult | None, str | None]:
    try:
        result = runner(tuple(argv), root, 2.0, max_output)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception:
        return None, "execution_error"
    if (
        not isinstance(result, tuple)
        or len(result) != 3
        or type(result[0]) is not int
        or not isinstance(result[1], bytes)
        or not isinstance(result[2], bytes)
    ):
        return None, "invalid_result"
    normalized = CommandResult(result[0], bytes(result[1]), bytes(result[2]))
    if len(normalized.stdout) + len(normalized.stderr) > max_output:
        return normalized, "output_too_large"
    return normalized, None


def _validate_specs(
    values: Iterable[Mapping[str, str]], limits: Mapping[str, int]
) -> tuple[list[dict[str, str]], list[str]]:
    reasons: list[str] = []
    try:
        specs = list(values)
    except Exception:
        return [], ["lockfile_specs_invalid"]
    if len(specs) > limits["max_lockfiles"]:
        reasons.append("lockfile_count_exceeded")
    normalized_seen: set[str] = set()
    accepted: list[dict[str, str]] = []
    for spec in specs[: limits["max_lockfiles"]]:
        if not isinstance(spec, Mapping) or set(spec) != {"path", "digest", "type"}:
            reasons.append("lockfile_spec_invalid")
            continue
        path = spec.get("path")
        digest = spec.get("digest")
        kind = spec.get("type")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(kind, str):
            reasons.append("lockfile_spec_invalid")
            continue
        normalized = PurePosixPath(path).as_posix()
        if normalized in normalized_seen:
            reasons.append("duplicate_normalized_lock_path")
        normalized_seen.add(normalized)
        parts = PurePosixPath(path).parts
        try:
            encoded = path.encode("utf-8", errors="strict")
            encoded_parts = tuple(part.encode("utf-8", errors="strict") for part in parts)
        except UnicodeEncodeError:
            reasons.append("lockfile_spec_invalid")
            continue
        canonical = (
            bool(path)
            and not PurePosixPath(path).is_absolute()
            and normalized == path
            and "\\" not in path
            and all(part not in {"", ".", ".."} for part in parts)
            and not any(ord(character) < 32 or ord(character) == 127 for character in path)
        )
        if not canonical:
            reasons.append("lockfile_path_not_canonical")
        if len(encoded) > limits["max_path_bytes"]:
            reasons.append("lockfile_path_too_long")
        if len(parts) > limits["max_component_depth"]:
            reasons.append("lockfile_component_depth_exceeded")
        if any(len(part) > limits["max_component_bytes"] for part in encoded_parts):
            reasons.append("lockfile_component_too_long")
        if digest != "sha256":
            reasons.append("lockfile_digest_alias")
        if _EXPECTED_TYPES.get(normalized) != kind:
            reasons.append("lockfile_type_alias")
        if canonical and len(encoded) <= limits["max_path_bytes"] and digest == "sha256":
            accepted.append({"path": path, "digest": digest, "type": kind})
    if set(normalized_seen) != set(_EXPECTED_TYPES) or len(specs) != len(DEFAULT_LOCK_SPECS):
        reasons.append("required_lockfile_set_mismatch")
    return sorted(accepted, key=lambda item: item["path"]), sorted(set(reasons))


def _git_prerequisites(
    root: Path,
    runner: Runner,
    paths: tuple[str, ...],
    max_output: int,
) -> tuple[str | None, bool, dict[str, tuple[str, str, str]] | None, list[str]]:
    reasons: list[str] = []
    result, error = _call(
        runner, ("git", "rev-parse", "--show-toplevel"), root, max_output=max_output
    )
    if error is not None or result is None or result.returncode != 0:
        return None, False, None, ["git_repository_probe_failed"]
    try:
        raw_reported = os.fsdecode(result.stdout.strip())
        if not raw_reported or "\0" in raw_reported:
            raise ValueError
        reported = Path(os.path.abspath(raw_reported))
    except (OSError, UnicodeDecodeError, ValueError):
        return None, False, None, ["git_repository_probe_failed"]
    if reported != root:
        return None, False, None, ["repository_root_mismatch"]

    result, error = _call(
        runner, ("git", "rev-parse", "--verify", "HEAD"), root, max_output=max_output
    )
    revision: str | None = None
    if error is not None or result is None or result.returncode != 0:
        reasons.append("source_revision_unavailable")
    else:
        try:
            candidate = result.stdout.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError:
            candidate = ""
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", candidate):
            revision = candidate
        else:
            reasons.append("source_revision_malformed")

    result, error = _call(
        runner,
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        root,
        max_output=max_output,
    )
    clean = False
    if error is not None or result is None or result.returncode != 0:
        reasons.append("source_status_probe_failed")
    elif result.stdout:
        reasons.append("source_checkout_not_clean")
    else:
        clean = True

    result, error = _call(
        runner,
        ("git", "ls-files", "-z", "--stage", "--", *paths),
        root,
        max_output=max_output,
    )
    if error is not None or result is None or result.returncode != 0:
        reasons.append("tracked_lockfile_inventory_failed")
        return revision, clean, None, sorted(set(reasons))
    entries: dict[str, tuple[str, str, str]] = {}
    try:
        records = [record for record in result.stdout.split(b"\0") if record]
        for record in records:
            metadata, separator, raw_path = record.partition(b"\t")
            if not separator:
                raise ValueError
            fields = metadata.decode("ascii", errors="strict").split()
            if len(fields) != 3:
                raise ValueError
            mode, object_id, stage = fields
            path = raw_path.decode("utf-8", errors="strict")
            if path in entries or path not in paths:
                raise ValueError
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None:
                raise ValueError
            entries[path] = (mode, object_id, stage)
    except (UnicodeDecodeError, ValueError):
        reasons.append("tracked_lockfile_inventory_malformed")
        return revision, clean, None, sorted(set(reasons))
    for path in paths:
        entry = entries.get(path)
        if entry is None:
            reasons.append("tracked_lockfile_missing")
            continue
        mode, _object_id, stage = entry
        if stage != "0":
            reasons.append("tracked_lockfile_unmerged")
        if mode == "120000":
            reasons.append("tracked_lockfile_type_mismatch")
        elif mode != "100644":
            reasons.append("tracked_lockfile_mode_mismatch")
    return revision, clean, entries, sorted(set(reasons))


def _final_checkout_reason(
    root: Path, runner: Runner, max_output: int
) -> str | None:
    result, error = _call(
        runner,
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        root,
        max_output=max_output,
    )
    if error is not None or result is None or result.returncode != 0:
        return "source_checkout_recheck_failed"
    if result.stdout:
        return "source_checkout_changed_during_preflight"
    return None


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_parent(root_fd: int, parts: Sequence[str]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_lock(
    root_fd: int,
    path: str,
    *,
    max_file_bytes: int,
    remaining_bytes: int,
) -> tuple[bytes | None, os.stat_result | None, str | None]:
    parts = PurePosixPath(path).parts
    try:
        parent_fd = _open_parent(root_fd, parts[:-1])
    except FileNotFoundError:
        return None, None, "lockfile_missing"
    except (NotADirectoryError, OSError):
        return None, None, "lockfile_path_unsafe"
    try:
        try:
            metadata = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None, None, "lockfile_missing"
        except OSError:
            return None, None, "lockfile_read_error"
        if stat.S_ISLNK(metadata.st_mode):
            return None, metadata, "lockfile_symlink"
        if not stat.S_ISREG(metadata.st_mode):
            return None, metadata, "lockfile_not_regular"
        if metadata.st_size > max_file_bytes:
            return None, metadata, "lockfile_too_large"
        if metadata.st_size > remaining_bytes:
            return None, metadata, "lockfile_hash_budget_exceeded"
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(parts[-1], flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                or opened.st_size != metadata.st_size
            ):
                return None, opened, "lockfile_changed_during_read"
            chunks: list[bytes] = []
            consumed = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, max_file_bytes + 1 - consumed))
                if not chunk:
                    break
                chunks.append(chunk)
                consumed += len(chunk)
                if consumed > max_file_bytes or consumed > remaining_bytes:
                    return None, opened, "lockfile_hash_budget_exceeded"
            final = os.fstat(descriptor)
            if (
                (final.st_dev, final.st_ino) != (metadata.st_dev, metadata.st_ino)
                or final.st_size != metadata.st_size
                or final.st_mtime_ns != metadata.st_mtime_ns
            ):
                return None, final, "lockfile_changed_during_read"
            return b"".join(chunks), final, None
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return None, None, "lockfile_missing"
    except OSError:
        return None, None, "lockfile_read_error"
    finally:
        os.close(parent_fd)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _format_reason(kind: str, content: bytes) -> str | None:
    if kind == "npm-package-lock-v3":
        try:
            document = json.loads(content, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return "npm_lockfile_format_mismatch"
        if (
            not isinstance(document, dict)
            or type(document.get("lockfileVersion")) is not int
            or document.get("lockfileVersion") != 3
            or not isinstance(document.get("packages"), dict)
        ):
            return "npm_lockfile_format_mismatch"
        return None
    if kind == "cargo-lock-v4":
        try:
            document = tomllib.loads(content.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            return "cargo_lockfile_format_mismatch"
        if (
            type(document.get("version")) is not int
            or document.get("version") != 4
            or not isinstance(document.get("package"), list)
        ):
            return "cargo_lockfile_format_mismatch"
        return None
    return "lockfile_type_alias"


def _dependency_inventory(
    kind: str,
    content: bytes,
    limits: Mapping[str, int],
) -> tuple[DependencyRequirements, str | None]:
    empty = DependencyRequirements((), ())
    maximum = limits["max_dependency_entries"]
    if kind == "npm-package-lock-v3":
        try:
            document = json.loads(content, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return empty, "node_dependency_inventory_invalid"
        packages = document.get("packages") if isinstance(document, dict) else None
        if not isinstance(packages, dict) or len(packages) > maximum:
            return empty, "node_dependency_inventory_invalid"
        paths: list[tuple[str, ...]] = []
        for raw_path in packages:
            if raw_path == "":
                continue
            if not isinstance(raw_path, str):
                return empty, "node_dependency_inventory_invalid"
            path = PurePosixPath(raw_path)
            parts = path.parts
            try:
                encoded = raw_path.encode("utf-8", errors="strict")
                encoded_parts = tuple(
                    part.encode("utf-8", errors="strict") for part in parts
                )
            except UnicodeEncodeError:
                return empty, "node_dependency_inventory_invalid"
            if (
                path.is_absolute()
                or path.as_posix() != raw_path
                or "\\" in raw_path
                or not parts
                or parts[0] != "node_modules"
                or any(part in {"", ".", ".."} for part in parts)
                or any(ord(character) < 32 or ord(character) == 127 for character in raw_path)
                or len(encoded) > limits["max_path_bytes"]
                or len(parts) > limits["max_component_depth"]
                or any(
                    len(component) > limits["max_component_bytes"]
                    for component in encoded_parts
                )
            ):
                return empty, "node_dependency_inventory_invalid"
            paths.append(parts)
        if len(set(paths)) != len(paths):
            return empty, "node_dependency_inventory_invalid"
        return DependencyRequirements(tuple(sorted(paths)), ()), None

    if kind == "cargo-lock-v4":
        try:
            document = tomllib.loads(content.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            return empty, "rust_dependency_inventory_invalid"
        packages = document.get("package") if isinstance(document, dict) else None
        if not isinstance(packages, list) or len(packages) > maximum:
            return empty, "rust_dependency_inventory_invalid"
        archives: list[str] = []
        for package in packages:
            if not isinstance(package, dict):
                return empty, "rust_dependency_inventory_invalid"
            source = package.get("source")
            if source is None:
                continue
            if not isinstance(source, str) or not source.startswith("registry+"):
                return empty, "rust_dependency_inventory_invalid"
            name = package.get("name")
            version = package.get("version")
            checksum = package.get("checksum")
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None
                or not isinstance(version, str)
                or re.fullmatch(r"[0-9A-Za-z.+-]+", version) is None
                or not isinstance(checksum, str)
                or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            ):
                return empty, "rust_dependency_inventory_invalid"
            archives.append(f"{name}-{version}.crate")
        if len(set(archives)) != len(archives):
            return empty, "rust_dependency_inventory_invalid"
        return DependencyRequirements((), tuple(sorted(archives))), None

    return empty, "dependency_inventory_type_invalid"


def _inspect_locks(
    root_fd: int,
    specs: Sequence[Mapping[str, str]],
    tracked: Mapping[str, tuple[str, str, str]],
    limits: Mapping[str, int],
) -> tuple[list[dict[str, object]], int, DependencyRequirements, list[str]]:
    reports: list[dict[str, object]] = []
    reasons: list[str] = []
    bytes_hashed = 0
    identities: set[tuple[int, int]] = set()
    digests: set[str] = set()
    node_paths: tuple[tuple[str, ...], ...] = ()
    cargo_archives: tuple[str, ...] = ()
    for spec in specs:
        path = spec["path"]
        content, metadata, error = _read_lock(
            root_fd,
            path,
            max_file_bytes=limits["max_lockfile_bytes"],
            remaining_bytes=max(0, limits["max_bytes_hashed"] - bytes_hashed),
        )
        if metadata is not None:
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in identities:
                reasons.append("lockfile_identity_alias")
            identities.add(identity)
        if error is not None:
            reasons.append(error)
            continue
        assert content is not None
        bytes_hashed += len(content)
        object_id = tracked[path][1]
        blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
        indexed_digest = (
            hashlib.sha1(blob).hexdigest()
            if len(object_id) == 40
            else hashlib.sha256(blob).hexdigest()
        )
        if indexed_digest != object_id:
            reasons.append("lockfile_index_content_mismatch")
        digest = hashlib.sha256(content).hexdigest()
        if digest in digests:
            reasons.append("lockfile_content_digest_alias")
        digests.add(digest)
        format_error = _format_reason(spec["type"], content)
        if format_error is not None:
            reasons.append(format_error)
        else:
            inventory, inventory_error = _dependency_inventory(spec["type"], content, limits)
            if inventory_error is not None:
                reasons.append(inventory_error)
            node_paths += inventory.node_paths
            cargo_archives += inventory.cargo_archives
        reports.append(
            {
                "path": path,
                "type": spec["type"],
                "digest": "sha256",
                "sha256": digest,
                "bytes": len(content),
            }
        )
    requirements = DependencyRequirements(
        tuple(sorted(node_paths)), tuple(sorted(cargo_archives))
    )
    return (
        sorted(reports, key=lambda item: str(item["path"])),
        bytes_hashed,
        requirements,
        sorted(set(reasons)),
    )


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _compatible_version(name: str, version: str) -> bool:
    value = _version_tuple(version)
    if name == "python3.14":
        return value[:2] == (3, 14)
    if name in {"cargo", "rustc"}:
        return value >= (1, 91, 0)
    if name == "node":
        major, minor, _patch = value
        return (
            (major == 20 and minor >= 19)
            or (major == 22 and minor >= 12)
            or major >= 24
        )
    return True


def _probe_tools(
    root: Path, runner: Runner, max_output: int
) -> tuple[list[dict[str, object]], list[str]]:
    tools: list[dict[str, object]] = []
    reasons: list[str] = []
    for name, argv, pattern in _TOOL_PROBES:
        result, error = _call(runner, argv, root, max_output=max_output)
        if error is not None:
            reason = f"tool_{error}:{name}"
            reasons.append(reason)
            tools.append({"name": name, "available": False, "reason_code": reason})
            continue
        assert result is not None
        if result.returncode != 0:
            reason = f"tool_{'missing' if result.returncode == 127 else 'command_failed'}:{name}"
            reasons.append(reason)
            tools.append({"name": name, "available": False, "reason_code": reason})
            continue
        payload = result.stdout.strip() or result.stderr.strip()
        try:
            output = payload.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            output = ""
        match = pattern.fullmatch(output)
        if match is None:
            reason = f"tool_version_malformed:{name}"
            reasons.append(reason)
            tools.append({"name": name, "available": False, "reason_code": reason})
            continue
        version = match.group(1)
        if not _compatible_version(name, version):
            reason = f"tool_version_incompatible:{name}"
            reasons.append(reason)
            tools.append(
                {"name": name, "available": True, "version": version, "reason_code": reason}
            )
            continue
        tools.append({"name": name, "available": True, "version": version})
    return tools, sorted(set(reasons))


def _check_directory_at(parent_fd: int, parts: Sequence[str]) -> tuple[bool, bool]:
    descriptor = os.dup(parent_fd)
    try:
        for part in parts:
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                return False, False
            except OSError:
                try:
                    metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                except OSError:
                    return False, False
                return False, stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                    metadata.st_mode
                )
            os.close(descriptor)
            descriptor = child
        return True, False
    finally:
        os.close(descriptor)


def _node_materialized(
    root_fd: int, required: Sequence[Sequence[str]]
) -> tuple[bool, bool, int]:
    base_ok, base_unsafe = _check_directory_at(
        root_fd, ("ui", "web", "node_modules")
    )
    if not base_ok:
        return False, base_unsafe, 0
    present = 0
    for parts in required:
        exists, unsafe = _check_directory_at(root_fd, ("ui", "web", *parts))
        if unsafe:
            return False, True, present
        if exists:
            present += 1
    return present == len(required), False, present


def _cargo_home_path(cargo_home: str | os.PathLike[str] | None) -> Path | None:
    try:
        selected = cargo_home
        if selected is None:
            selected = os.environ.get("CARGO_HOME") or Path.home() / ".cargo"
        raw = os.fspath(selected)
        if not isinstance(raw, str) or not raw or "\0" in raw:
            return None
        path = Path(os.path.abspath(raw))
        if path.resolve(strict=True) != path:
            return None
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return None
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def _bounded_entries(
    descriptor: int, maximum: int
) -> tuple[list[tuple[str, int]] | None, bool]:
    entries: list[tuple[str, int]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if len(entries) >= maximum:
                    return None, False
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    return None, True
                entries.append((entry.name, metadata.st_mode))
    except OSError:
        return None, True
    return entries, False


def _rust_cache_materialized(
    cargo_home: str | os.PathLike[str] | None,
    required: Sequence[str],
    maximum: int,
) -> tuple[bool, bool, int]:
    path = _cargo_home_path(cargo_home)
    if path is None:
        return False, False, 0
    try:
        root_fd = os.open(path, _directory_flags())
    except OSError:
        return False, True, 0
    try:
        try:
            cache_fd = _open_parent(root_fd, ("registry", "cache"))
        except (FileNotFoundError, NotADirectoryError):
            return False, False, 0
        except OSError:
            return False, True, 0
        try:
            indexes, unsafe = _bounded_entries(cache_fd, maximum)
            if indexes is None:
                return False, unsafe, 0
            available: set[str] = set()
            observed = len(indexes)
            for name, mode in indexes:
                if stat.S_ISLNK(mode):
                    return False, True, 0
                if not stat.S_ISDIR(mode):
                    continue
                try:
                    index_fd = _open_parent(cache_fd, (name,))
                except OSError:
                    return False, True, 0
                try:
                    entries, unsafe = _bounded_entries(
                        index_fd, max(1, maximum - observed)
                    )
                    if entries is None:
                        return False, unsafe, 0
                    observed += len(entries)
                    if observed > maximum:
                        return False, False, 0
                    for entry, entry_mode in entries:
                        if stat.S_ISLNK(entry_mode):
                            return False, True, 0
                        if entry.endswith(".crate") and stat.S_ISREG(entry_mode):
                            available.add(entry)
                finally:
                    os.close(index_fd)
            present = sum(archive in available for archive in required)
            return bool(available) and present == len(required), False, present
        finally:
            os.close(cache_fd)
    finally:
        os.close(root_fd)


def _probe_dependencies(
    root_fd: int,
    cargo_home: str | os.PathLike[str] | None,
    requirements: DependencyRequirements,
    maximum: int,
) -> tuple[dict[str, object], list[str]]:
    reasons: list[str] = []
    node_ok, node_unsafe, node_present = _node_materialized(
        root_fd, requirements.node_paths
    )
    if node_unsafe:
        reasons.append("node_dependencies_unsafe")
    elif not node_ok:
        reasons.append("node_dependencies_not_materialized")
    rust_ok, rust_unsafe, rust_present = _rust_cache_materialized(
        cargo_home, requirements.cargo_archives, maximum
    )
    if rust_unsafe:
        reasons.append("rust_cache_unsafe")
    elif not rust_ok:
        reasons.append("rust_cache_not_materialized")
    return {
        "node_modules": {
            "materialized": node_ok,
            "required": len(requirements.node_paths),
            "present": node_present,
        },
        "rust_cache": {
            "materialized": rust_ok,
            "required": len(requirements.cargo_archives),
            "present": rust_present,
        },
    }, sorted(set(reasons))


def run_preflight(
    root_value: str | os.PathLike[str],
    *,
    runner: Runner = default_runner,
    cargo_home: str | os.PathLike[str] | None = None,
    lock_specs: Iterable[Mapping[str, str]] = DEFAULT_LOCK_SPECS,
    limits: Mapping[str, int] = DEFAULT_LIMITS,
) -> dict[str, object]:
    """Inspect prerequisites without setup, gate execution, or repository mutation."""
    normalized_limits = _validate_limits(limits)
    if normalized_limits is None:
        return _base_report("limits_invalid")
    root, root_fd = _safe_root(root_value)
    if root is None or root_fd is None:
        return _base_report("repository_root_unsafe")
    try:
        specs, spec_reasons = _validate_specs(lock_specs, normalized_limits)
        paths = tuple(sorted(spec["path"] for spec in specs))
        revision: str | None = None
        clean = False
        tracked: dict[str, tuple[str, str, str]] | None = None
        git_reasons: list[str] = []
        if not spec_reasons:
            revision, clean, tracked, git_reasons = _git_prerequisites(
                root,
                runner,
                paths,
                normalized_limits["max_git_output_bytes"],
            )
        lock_reports: list[dict[str, object]] = []
        bytes_hashed = 0
        requirements = DependencyRequirements((), ())
        lock_reasons: list[str] = []
        if not spec_reasons and tracked is not None:
            lock_reports, bytes_hashed, requirements, lock_reasons = (
                _inspect_locks(root_fd, specs, tracked, normalized_limits)
            )
        source_reasons = sorted(set(spec_reasons + git_reasons + lock_reasons))
        source = {
            "ok": not source_reasons,
            "reason_codes": source_reasons,
            "revision": revision,
            "clean": clean,
            "lockfiles": lock_reports,
            "bytes_hashed": bytes_hashed,
            "tracked_lockfiles": len(tracked or {}),
            "limits": {
                key: normalized_limits[key]
                for key in (
                    "max_lockfiles",
                    "max_path_bytes",
                    "max_component_bytes",
                    "max_component_depth",
                    "max_lockfile_bytes",
                    "max_bytes_hashed",
                )
            },
        }

        tool_reports, tool_reasons = _probe_tools(
            root, runner, normalized_limits["max_command_output_bytes"]
        )
        toolchain = {
            "ok": not tool_reasons,
            "reason_codes": tool_reasons,
            "tools": tool_reports,
        }
        dependencies: dict[str, object]
        if not spec_reasons and tracked is not None and not lock_reasons:
            dependencies, dependency_reasons = _probe_dependencies(
                root_fd,
                cargo_home,
                requirements,
                normalized_limits["max_dependency_entries"],
            )
        else:
            dependencies = {
                "node_modules": {
                    "materialized": False,
                    "required": len(requirements.node_paths),
                    "present": 0,
                },
                "rust_cache": {
                    "materialized": False,
                    "required": len(requirements.cargo_archives),
                    "present": 0,
                },
            }
            dependency_reasons = ["dependency_materialization_not_evaluated"]
        dependencies.update(
            {"ok": not dependency_reasons, "reason_codes": dependency_reasons}
        )
        late_source_reasons: list[str] = []
        if tracked is not None:
            final_status_reason = _final_checkout_reason(
                root, runner, normalized_limits["max_git_output_bytes"]
            )
            if final_status_reason is not None:
                late_source_reasons.append(final_status_reason)
        if not _root_identity_matches(root, root_fd):
            late_source_reasons.append("repository_root_changed")
        if late_source_reasons:
            source_reasons = sorted(set(source_reasons + late_source_reasons))
            source["ok"] = False
            source["reason_codes"] = source_reasons
        blockers = sorted(set(source_reasons + tool_reasons + dependency_reasons))
        ready = not blockers
        return {
            "protocol": PROTOCOL,
            "preflight_ready": ready,
            "blockers": blockers,
            "source_lockfile_prerequisites": source,
            "toolchain_availability": toolchain,
            "dependency_materialization": dependencies,
            "gates_requiring_execution": [dict(gate) for gate in _GATES],
            "verification_bundle_executed": False,
            "fresh_checkout_proven": False,
            "physical_qualification_evaluated": False,
            "route_ready": False,
            "release_ready": False,
        }
    finally:
        os.close(root_fd)
