from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from mycelium_bootstrap_preflight import (
    DEFAULT_LIMITS,
    DEFAULT_LOCK_SPECS,
    CommandResult,
    canonical_json,
    default_runner,
    run_preflight,
)

LOCK_PATHS = tuple(spec["path"] for spec in DEFAULT_LOCK_SPECS)
TRACKED_PATHS = tuple(sorted(LOCK_PATHS))
GOOD_VERSIONS = {
    ("python3.14", "--version"): b"Python 3.14.1\n",
    ("cargo", "--version"): b"cargo 1.91.0 (ea2d97820 2025-10-10)\n",
    ("rustc", "--version"): b"rustc 1.91.0 (f8297e351 2025-10-28)\n",
    ("rustfmt", "--version"): b"rustfmt 1.8.0-stable (f8297e351 2025-10-28)\n",
    ("cargo-clippy", "--version"): b"clippy 0.1.91 (f8297e351 2025-10-28)\n",
    ("node", "--version"): b"v24.1.0\n",
    ("npm", "--version"): b"11.3.0\n",
}

Runner = Callable[[Sequence[str], Path, float, int], CommandResult]


def _write_locks(root: Path) -> None:
    npm = root / LOCK_PATHS[0]
    npm.parent.mkdir(parents=True)
    npm.write_text(
        '{"lockfileVersion":3,"name":"fixture","packages":{},"version":"0.0.0"}\n',
        encoding="utf-8",
    )
    cargo = root / LOCK_PATHS[1]
    cargo.parent.mkdir(parents=True)
    cargo.write_text(
        'version = 4\n\n[[package]]\nname = "fixture"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )


def _materialize_dependencies(root: Path, cargo_home: Path) -> None:
    node_modules = root / "ui/web/node_modules"
    (node_modules / "typescript").mkdir(parents=True)
    (node_modules / "typescript/package.json").write_text("{}\n", encoding="utf-8")
    source = cargo_home / "registry/src/index/fixture-1.0.0"
    source.mkdir(parents=True)
    (source / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    archive = cargo_home / "registry/cache/index/fixture-1.0.0.crate"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"crate")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _write_locks(root)
    cargo_home = tmp_path / "cargo-home"
    _materialize_dependencies(root, cargo_home)
    return root, cargo_home


def _git_entries(
    *,
    root: Path | None = None,
    mode_by_path: dict[str, str] | None = None,
    stage_by_path: dict[str, str] | None = None,
    object_by_path: dict[str, str] | None = None,
) -> bytes:
    mode_by_path = mode_by_path or {}
    stage_by_path = stage_by_path or {}
    object_by_path = object_by_path or {}
    records = []
    for index, path in enumerate(LOCK_PATHS, start=1):
        mode = mode_by_path.get(path, "100644")
        stage = stage_by_path.get(path, "0")
        if path in object_by_path:
            object_id = object_by_path[path]
        elif root is not None:
            try:
                content = (root / path).read_bytes()
            except OSError:
                object_id = f"{index:040x}"
            else:
                blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
                object_id = hashlib.sha1(blob).hexdigest()
        else:
            object_id = f"{index:040x}"
        records.append(f"{mode} {object_id} {stage}\t{path}".encode("utf-8"))
    return b"\0".join(records) + b"\0"


def _runner(
    root: Path,
    *,
    tool_overrides: dict[tuple[str, ...], CommandResult | BaseException] | None = None,
    git_overrides: dict[tuple[str, ...], CommandResult | BaseException] | None = None,
) -> Runner:
    tool_overrides = tool_overrides or {}
    git_overrides = git_overrides or {}

    def run(argv: Sequence[str], cwd: Path, timeout: float, max_output: int) -> CommandResult:
        command = tuple(argv)
        assert cwd == root
        assert timeout > 0
        assert max_output > 0
        override = tool_overrides.get(command, git_overrides.get(command))
        if isinstance(override, BaseException):
            raise override
        if override is not None:
            return override
        if command in GOOD_VERSIONS:
            return CommandResult(0, GOOD_VERSIONS[command], b"")
        if command == ("git", "rev-parse", "--show-toplevel"):
            return CommandResult(0, os.fsencode(root) + b"\n", b"")
        if command == ("git", "rev-parse", "--verify", "HEAD"):
            return CommandResult(0, b"a" * 40 + b"\n", b"")
        if command == (
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ):
            return CommandResult(0, b"", b"")
        if command == ("git", "ls-files", "-z", "--stage", "--", *TRACKED_PATHS):
            return CommandResult(0, _git_entries(root=root), b"")
        raise AssertionError(f"unexpected command: {command!r}")

    return run


def _run(root: Path, cargo_home: Path, **kwargs: Any) -> dict[str, Any]:
    return run_preflight(
        root,
        runner=kwargs.pop("runner", _runner(root)),
        cargo_home=cargo_home,
        **kwargs,
    )


def _assert_claim_boundary(report: dict[str, Any]) -> None:
    assert report["route_ready"] is False
    assert report["release_ready"] is False
    assert report["fresh_checkout_proven"] is False
    assert report["physical_qualification_evaluated"] is False
    assert report["verification_bundle_executed"] is False


def _snapshot(root: Path) -> list[tuple[str, int, bytes | None]]:
    result = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        payload = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        result.append((path.relative_to(root).as_posix(), info.st_mode, payload))
    return result


def test_honest_preflight_separates_prerequisites_from_unexecuted_gates(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)

    report = _run(root, cargo_home)

    assert report["protocol"] == "mycelium.bootstrap_preflight.v1"
    assert report["preflight_ready"] is True
    assert report["blockers"] == []
    assert report["source_lockfile_prerequisites"]["ok"] is True
    assert report["source_lockfile_prerequisites"]["revision"] == "a" * 40
    assert [item["path"] for item in report["source_lockfile_prerequisites"]["lockfiles"]] == sorted(LOCK_PATHS)
    assert all(len(item["sha256"]) == 64 for item in report["source_lockfile_prerequisites"]["lockfiles"])
    assert report["toolchain_availability"]["ok"] is True
    assert report["dependency_materialization"]["ok"] is True
    assert report["gates_requiring_execution"]
    assert all(gate["executed"] is False for gate in report["gates_requiring_execution"])
    _assert_claim_boundary(report)


def test_preflight_is_read_only_and_report_omits_private_paths(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    before_root = _snapshot(root)
    before_cache = _snapshot(cargo_home)

    report = _run(root, cargo_home)
    rendered = canonical_json(report)

    assert _snapshot(root) == before_root
    assert _snapshot(cargo_home) == before_cache
    assert str(root) not in rendered
    assert str(cargo_home) not in rendered
    assert rendered.endswith("\n")
    assert rendered == canonical_json(json.loads(rendered))


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "symlink-parent"])
def test_malicious_or_unavailable_roots_fail_closed(tmp_path: Path, kind: str) -> None:
    real_root, cargo_home = _fixture(tmp_path)
    if kind == "missing":
        root = tmp_path / "missing"
    elif kind == "file":
        root = tmp_path / "file"
        root.write_text("not a directory\n", encoding="utf-8")
    elif kind == "symlink":
        root = tmp_path / "alias"
        root.symlink_to(real_root, target_is_directory=True)
    else:
        parent = tmp_path / "real-parent"
        parent.mkdir()
        nested = parent / "repo"
        real_root.rename(nested)
        alias = tmp_path / "alias-parent"
        alias.symlink_to(parent, target_is_directory=True)
        root = alias / "repo"

    report = run_preflight(root, runner=_runner(real_root), cargo_home=cargo_home)

    assert report["preflight_ready"] is False
    assert "repository_root_unsafe" in report["blockers"]
    assert report["source_lockfile_prerequisites"]["ok"] is False
    _assert_claim_boundary(report)


def test_root_descriptor_closes_when_initial_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, cargo_home = _fixture(tmp_path)
    real_open = os.open
    real_close = os.close
    opened: set[int] = set()

    def tracking_open(*args: Any, **kwargs: Any) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        opened.discard(descriptor)
        real_close(descriptor)

    def failing_fstat(_descriptor: int) -> os.stat_result:
        raise OSError("PRIVATE_FSTAT_FAILURE")

    monkeypatch.setattr("mycelium_bootstrap_preflight.core.os.open", tracking_open)
    monkeypatch.setattr("mycelium_bootstrap_preflight.core.os.close", tracking_close)
    monkeypatch.setattr("mycelium_bootstrap_preflight.core.os.fstat", failing_fstat)

    report = run_preflight(root, runner=_runner(root), cargo_home=cargo_home)

    assert report["blockers"] == ["repository_root_unsafe"]
    assert opened == set()


def test_git_must_confirm_exact_repository_root(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    command = ("git", "rev-parse", "--show-toplevel")
    runner = _runner(
        root,
        git_overrides={command: CommandResult(0, b"/private/other-root\n", b"")},
    )

    report = _run(root, cargo_home, runner=runner)

    assert "repository_root_mismatch" in report["blockers"]
    assert "/private/other-root" not in canonical_json(report)


def test_dirty_source_checkout_is_explicit_blocker(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    command = ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all")
    runner = _runner(
        root,
        git_overrides={command: CommandResult(0, b"?? private-name\0", b"")},
    )

    report = _run(root, cargo_home, runner=runner)

    assert "source_checkout_not_clean" in report["blockers"]
    assert "private-name" not in canonical_json(report)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "lockfile_missing"),
        ("symlink", "lockfile_symlink"),
        ("directory", "lockfile_not_regular"),
    ],
)
def test_missing_symlinked_and_nonregular_lockfiles_are_rejected(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[0]
    lock.unlink()
    if mutation == "symlink":
        outside = tmp_path / "outside-lock"
        outside.write_text("{}\n", encoding="utf-8")
        lock.symlink_to(outside)
    elif mutation == "directory":
        lock.mkdir()

    report = _run(root, cargo_home)

    assert reason in report["blockers"]
    assert report["source_lockfile_prerequisites"]["ok"] is False


def test_hardlinked_lockfiles_are_rejected_as_identity_aliases(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    first = root / LOCK_PATHS[0]
    second = root / LOCK_PATHS[1]
    second.unlink()
    os.link(first, second)

    report = _run(root, cargo_home)

    assert "lockfile_identity_alias" in report["blockers"]


def test_oversized_lockfile_and_total_hash_budget_fail_before_unbounded_read(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[0]
    lock.write_bytes(b"x" * 65)
    limits = {**DEFAULT_LIMITS, "max_lockfile_bytes": 64, "max_bytes_hashed": 80}

    report = _run(root, cargo_home, limits=limits)

    assert "lockfile_too_large" in report["blockers"]
    assert report["source_lockfile_prerequisites"]["bytes_hashed"] <= 80


@pytest.mark.parametrize(
    ("specs", "limits", "reason"),
    [
        (
            (*DEFAULT_LOCK_SPECS, DEFAULT_LOCK_SPECS[0]),
            None,
            "duplicate_normalized_lock_path",
        ),
        (
            ({**DEFAULT_LOCK_SPECS[0], "path": "ui/web/./package-lock.json"}, DEFAULT_LOCK_SPECS[1]),
            None,
            "lockfile_path_not_canonical",
        ),
        (
            ({**DEFAULT_LOCK_SPECS[0], "digest": "sha-256"}, DEFAULT_LOCK_SPECS[1]),
            None,
            "lockfile_digest_alias",
        ),
        (
            ({**DEFAULT_LOCK_SPECS[0], "type": "npm"}, DEFAULT_LOCK_SPECS[1]),
            None,
            "lockfile_type_alias",
        ),
        (
            DEFAULT_LOCK_SPECS,
            {**DEFAULT_LIMITS, "max_lockfiles": 1},
            "lockfile_count_exceeded",
        ),
        (
            ({**DEFAULT_LOCK_SPECS[0], "path": "x/" + "a" * 40}, DEFAULT_LOCK_SPECS[1]),
            {**DEFAULT_LIMITS, "max_path_bytes": 16},
            "lockfile_path_too_long",
        ),
        (
            ({**DEFAULT_LOCK_SPECS[0], "path": "x/" + "a" * 40}, DEFAULT_LOCK_SPECS[1]),
            {**DEFAULT_LIMITS, "max_component_bytes": 16},
            "lockfile_component_too_long",
        ),
        (
            ({**DEFAULT_LOCK_SPECS[0], "path": "/".join(["x"] * 10)}, DEFAULT_LOCK_SPECS[1]),
            {**DEFAULT_LIMITS, "max_component_depth": 4},
            "lockfile_component_depth_exceeded",
        ),
    ],
)
def test_lock_spec_aliases_and_all_inventory_bounds_fail_closed(
    tmp_path: Path,
    specs: Sequence[dict[str, str]],
    limits: dict[str, int] | None,
    reason: str,
) -> None:
    root, cargo_home = _fixture(tmp_path)

    report = _run(root, cargo_home, lock_specs=specs, limits=limits or DEFAULT_LIMITS)

    assert reason in report["blockers"]
    assert report["source_lockfile_prerequisites"]["ok"] is False


def test_lone_surrogate_lock_path_fails_closed_without_crashing(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    specs = (
        {**DEFAULT_LOCK_SPECS[0], "path": "ui/web/\ud800/package-lock.json"},
        DEFAULT_LOCK_SPECS[1],
    )

    report = _run(root, cargo_home, lock_specs=specs)

    assert "lockfile_spec_invalid" in report["blockers"]
    assert report["preflight_ready"] is False
    _assert_claim_boundary(report)


def test_partially_raising_lock_spec_iterable_is_discarded(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)

    def broken_specs() -> Any:
        yield DEFAULT_LOCK_SPECS[0]
        raise Exception("PRIVATE_ITERATOR_FAILURE")

    report = _run(root, cargo_home, lock_specs=broken_specs())

    assert "lockfile_specs_invalid" in report["blockers"]
    assert "PRIVATE_ITERATOR_FAILURE" not in canonical_json(report)
    assert report["source_lockfile_prerequisites"]["lockfiles"] == []


@pytest.mark.parametrize(
    ("mode", "stage", "reason"),
    [
        ("120000", "0", "tracked_lockfile_type_mismatch"),
        ("100644", "1", "tracked_lockfile_unmerged"),
        ("100755", "0", "tracked_lockfile_mode_mismatch"),
    ],
)
def test_git_index_type_stage_and_mode_are_exact(
    tmp_path: Path, mode: str, stage: str, reason: str
) -> None:
    root, cargo_home = _fixture(tmp_path)
    path = LOCK_PATHS[0]
    command = ("git", "ls-files", "-z", "--stage", "--", *TRACKED_PATHS)
    runner = _runner(
        root,
        git_overrides={
            command: CommandResult(
                0,
                _git_entries(
                    root=root,
                    mode_by_path={path: mode},
                    stage_by_path={path: stage},
                ),
                b"",
            )
        },
    )

    report = _run(root, cargo_home, runner=runner)

    assert reason in report["blockers"]


def test_lockfile_bytes_must_match_the_tracked_index_object(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    path = LOCK_PATHS[0]
    command = ("git", "ls-files", "-z", "--stage", "--", *TRACKED_PATHS)
    runner = _runner(
        root,
        git_overrides={
            command: CommandResult(
                0,
                _git_entries(root=root, object_by_path={path: "0" * 40}),
                b"",
            )
        },
    )

    report = _run(root, cargo_home, runner=runner)

    assert "lockfile_index_content_mismatch" in report["blockers"]


def test_final_git_status_detects_checkout_change_during_preflight(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    base_runner = _runner(root)
    status_calls = 0

    def changing_runner(
        argv: Sequence[str], cwd: Path, timeout: float, max_output: int
    ) -> CommandResult:
        nonlocal status_calls
        command = tuple(argv)
        if command == (
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ):
            status_calls += 1
            if status_calls == 2:
                return CommandResult(0, b"?? private-late-change\0", b"")
        return base_runner(argv, cwd, timeout, max_output)

    report = _run(root, cargo_home, runner=changing_runner)

    assert status_calls == 2
    assert "source_checkout_changed_during_preflight" in report["blockers"]
    assert "private-late-change" not in canonical_json(report)


def test_lockfile_formats_are_exact_not_version_aliases(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    (root / LOCK_PATHS[0]).write_text('{"lockfileVersion":"3","packages":{}}\n', encoding="utf-8")
    (root / LOCK_PATHS[1]).write_text('version = "4"\n', encoding="utf-8")

    report = _run(root, cargo_home)

    assert "npm_lockfile_format_mismatch" in report["blockers"]
    assert "cargo_lockfile_format_mismatch" in report["blockers"]


def test_absent_tool_and_malformed_version_are_distinct_blockers(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    runner = _runner(
        root,
        tool_overrides={
            ("node", "--version"): CommandResult(127, b"", b"not found"),
            ("npm", "--version"): CommandResult(0, b"npm version from private wrapper\n", b""),
        },
    )

    report = _run(root, cargo_home, runner=runner)

    assert "tool_missing:node" in report["blockers"]
    assert "tool_version_malformed:npm" in report["blockers"]
    rendered = canonical_json(report)
    assert "private wrapper" not in rendered


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (subprocess.TimeoutExpired(["cargo", "--version"], 2), "tool_timeout:cargo"),
        (OSError("PRIVATE_EXECUTION_FAILURE"), "tool_execution_error:cargo"),
        (Exception("PRIVATE_CUSTOM_FAILURE"), "tool_execution_error:cargo"),
    ],
)
def test_command_timeouts_and_errors_fail_closed_without_internal_text(
    tmp_path: Path, failure: BaseException, reason: str
) -> None:
    root, cargo_home = _fixture(tmp_path)
    runner = _runner(root, tool_overrides={("cargo", "--version"): failure})

    report = _run(root, cargo_home, runner=runner)
    rendered = canonical_json(report)

    assert reason in report["blockers"]
    assert "PRIVATE_EXECUTION_FAILURE" not in rendered
    assert "Traceback" not in rendered


def test_tool_version_output_is_bounded(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    runner = _runner(
        root,
        tool_overrides={("cargo", "--version"): CommandResult(0, b"x" * 5000, b"")},
    )

    report = _run(root, cargo_home, runner=runner)

    assert "tool_output_too_large:cargo" in report["blockers"]


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("node_modules", "node_dependencies_not_materialized"),
        ("rust_cache", "rust_cache_not_materialized"),
    ],
)
def test_missing_dependencies_are_blockers_never_repaired(
    tmp_path: Path, missing: str, reason: str
) -> None:
    root, cargo_home = _fixture(tmp_path)
    if missing == "node_modules":
        for path in sorted((root / "ui/web/node_modules").rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        (root / "ui/web/node_modules").rmdir()
    else:
        for path in sorted(cargo_home.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        cargo_home.rmdir()
    before = _snapshot(root)

    report = _run(root, cargo_home)

    assert reason in report["blockers"]
    assert report["dependency_materialization"]["ok"] is False
    assert _snapshot(root) == before


def test_nonempty_node_modules_missing_a_locked_package_is_blocked(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[0]
    lock.write_text(
        '{"lockfileVersion":3,"packages":{"":{},"node_modules/typescript":{}}}\n',
        encoding="utf-8",
    )
    package = root / "ui/web/node_modules/typescript"
    for path in package.iterdir():
        path.unlink()
    package.rmdir()
    (root / "ui/web/node_modules/unrelated").mkdir()

    report = _run(root, cargo_home)

    assert "node_dependencies_not_materialized" in report["blockers"]
    node = report["dependency_materialization"]["node_modules"]
    assert node == {"materialized": False, "required": 1, "present": 0}


def test_nonempty_rust_cache_missing_a_locked_archive_is_blocked(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[1]
    lock.write_text(
        """version = 4

[[package]]
name = "required"
version = "2.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""",
        encoding="utf-8",
    )

    report = _run(root, cargo_home)

    assert "rust_cache_not_materialized" in report["blockers"]
    rust = report["dependency_materialization"]["rust_cache"]
    assert rust == {"materialized": False, "required": 1, "present": 0}


@pytest.mark.parametrize(
    ("lock_index", "payload", "reason"),
    [
        (
            0,
            b'{"lockfileVersion":3,"packages":{"":{},"../outside":{}}}\n',
            "node_dependency_inventory_invalid",
        ),
        (
            1,
            b'version = 4\n\n[[package]]\nname = "../outside"\nversion = "1.0.0"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n',
            "rust_dependency_inventory_invalid",
        ),
    ],
)
def test_malicious_dependency_inventory_paths_fail_closed(
    tmp_path: Path, lock_index: int, payload: bytes, reason: str
) -> None:
    root, cargo_home = _fixture(tmp_path)
    (root / LOCK_PATHS[lock_index]).write_bytes(payload)

    report = _run(root, cargo_home)

    assert reason in report["blockers"]
    assert report["dependency_materialization"]["ok"] is False


def test_symlinked_dependency_materialization_is_not_followed(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    node_modules = root / "ui/web/node_modules"
    outside = tmp_path / "outside-node-modules"
    node_modules.rename(outside)
    node_modules.symlink_to(outside, target_is_directory=True)

    report = _run(root, cargo_home)

    assert "node_dependencies_unsafe" in report["blockers"]


def test_repository_identity_replacement_during_probes_fails_closed(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    base_runner = _runner(root)
    replaced = False

    def replacing_runner(
        argv: Sequence[str], cwd: Path, timeout: float, max_output: int
    ) -> CommandResult:
        nonlocal replaced
        result = base_runner(argv, cwd, timeout, max_output)
        if tuple(argv) == ("npm", "--version") and not replaced:
            displaced = root.with_name("displaced-repo")
            root.rename(displaced)
            root.mkdir()
            replaced = True
        return result

    report = _run(root, cargo_home, runner=replacing_runner)

    assert "repository_root_changed" in report["blockers"]
    assert report["source_lockfile_prerequisites"]["ok"] is False


def test_output_is_deterministic_even_when_runner_returns_private_stderr(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    runner = _runner(
        root,
        tool_overrides={
            ("rustfmt", "--version"): CommandResult(1, b"", b"PRIVATE HOST DETAIL")
        },
    )

    first = canonical_json(_run(root, cargo_home, runner=runner))
    second = canonical_json(_run(root, cargo_home, runner=runner))

    assert first == second
    assert "PRIVATE HOST DETAIL" not in first
    assert "timestamp" not in first


def test_default_runner_allows_only_exact_read_only_probe_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed.append(tuple(argv))
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, b"Python 3.14.1\n", b"")

    monkeypatch.setattr("mycelium_bootstrap_preflight.core.subprocess.run", fake_run)

    result = default_runner(("python3.14", "--version"), tmp_path, 2.0, 4096)
    assert result.returncode == 0
    assert observed == [("python3.14", "--version")]
    with pytest.raises(ValueError, match="command is not an allowed read-only probe"):
        default_runner(("npm", "install"), tmp_path, 2.0, 4096)
    with pytest.raises(ValueError, match="command is not an allowed read-only probe"):
        default_runner(("git", "fetch"), tmp_path, 2.0, 4096)


def test_default_runner_rejects_dot_dot_in_command_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"Python 3.14.1\n", b"")

    monkeypatch.setattr("mycelium_bootstrap_preflight.core.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="command is not an allowed read-only probe"):
        default_runner(("python3.14", "--root", "../escape"), tmp_path, 2.0, 4096)


def test_tool_version_emitted_via_stderr_only_is_accepted(
    tmp_path: Path,
) -> None:
    root, cargo_home = _fixture(tmp_path)
    runner = _runner(
        root,
        tool_overrides={
            ("cargo", "--version"): CommandResult(0, b"", b"cargo 1.91.0 (private)\n")
        },
    )

    report = _run(root, cargo_home, runner=runner)
    rendered = canonical_json(report)

    assert report["toolchain_availability"]["ok"] is True
    assert "private" not in rendered


def test_cargo_lockfile_with_missing_required_field_fails_closed(
    tmp_path: Path,
) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[1]
    lock.write_text(
        'version = 4\n\n[[package]]\nname = "fixture"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n',
        encoding="utf-8",
    )

    report = _run(root, cargo_home)

    assert "rust_dependency_inventory_invalid" in report["blockers"]


def test_npm_lockfile_with_non_string_packages_key_fails_closed(
    tmp_path: Path,
) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[0]
    # Python json happily uses last-wins for duplicate string keys but our
    # object_pairs_hook rejects them. Two real-looking paths duplicate each
    # other; the duplicate must be flagged instead of silently coerced.
    lock.write_text(
        '{"lockfileVersion":3,"packages":{"":"","":"a","node_modules/foo":""}}\n',
        encoding="utf-8",
    )

    report = _run(root, cargo_home)

    assert "npm_lockfile_format_mismatch" in report["blockers"]


def test_npm_lockfile_with_string_but_invalid_workspace_path_fails_closed(
    tmp_path: Path,
) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[0]
    lock.write_text(
        '{"lockfileVersion":3,"packages":{"":{},"@scope/pkg":{}}}\n',
        encoding="utf-8",
    )

    report = _run(root, cargo_home)

    assert "node_dependency_inventory_invalid" in report["blockers"]


def test_cargo_lockfile_with_non_string_source_fails_closed(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[1]
    lock.write_text(
        'version = 4\n\n[[package]]\nname = "fixture"\nversion = "1.0.0"\nsource = 7\nchecksum = "'
        + "a" * 64
        + '"\n',
        encoding="utf-8",
    )

    report = _run(root, cargo_home)

    assert "rust_dependency_inventory_invalid" in report["blockers"]


def test_npm_lockfile_with_trailing_garbage_is_accepted(tmp_path: Path) -> None:
    root, cargo_home = _fixture(tmp_path)
    lock = root / LOCK_PATHS[0]
    lock.write_text(
        '{"lockfileVersion":3,"packages":{"":{}},',  # truncated JSON
        encoding="utf-8",
    )

    report = _run(root, cargo_home, runner=_runner(root))

    assert "npm_lockfile_format_mismatch" in report["blockers"]


def test_default_runner_passes_subprocess_devnull_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"Python 3.14.1\n", b"")

    monkeypatch.setattr("mycelium_bootstrap_preflight.core.subprocess.run", fake_run)
    default_runner(("python3.14", "--version"), tmp_path, 2.0, 4096)
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["shell"] is False
    assert captured["check"] is False


def test_gate_inventory_names_every_requested_verification_without_executing_it(
    tmp_path: Path,
) -> None:
    root, cargo_home = _fixture(tmp_path)
    report = _run(root, cargo_home)
    rendered = canonical_json(report)

    for fragment in (
        "tests/bootstrap_preflight",
        "scripts/contract_audit.py",
        "compileall",
        "git diff --check",
        "ruff check",
        "release_security_audit.py",
        "claim_boundary_audit.py",
        "cargo fmt --check",
        "cargo clippy",
        "cargo test",
        "npm run check",
    ):
        assert fragment in rendered
    assert all(gate["executed"] is False for gate in report["gates_requiring_execution"])
