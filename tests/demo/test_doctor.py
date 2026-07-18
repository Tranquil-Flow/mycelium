from __future__ import annotations

import importlib
import json
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest


PROTOCOL = "mycelium.release_doctor_preflight.v1"
REQUIRED_FILES = (
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


def _doctor():
    return importlib.import_module("mycelium_demo.doctor")


def _fixture_repo(root: Path, *, name: str = "repo") -> Path:
    repo = root / name
    for relative in REQUIRED_FILES:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    return repo


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in report["checks"] if item["name"] == name)


def _assert_unqualified(report: dict[str, Any]) -> None:
    assert report["route_ready"] is False
    assert report["release_ready"] is False
    assert report["qualification_evaluated"] is False
    assert report["claim_boundary"] == CLAIM_BOUNDARY
    assert report["release_blockers"] == RELEASE_BLOCKERS
    assert all(type(item["ok"]) is bool for item in report["checks"])
    names = [item["name"] for item in report["checks"]]
    assert len(names) == len(set(names))


def _run(
    repo: Path | str,
    state: Path | str,
    *,
    commands: Iterable[Any] = (),
    required_files: Iterable[Any] = (),
    ports: Iterable[Any] = (),
    which=lambda _command: "/tools/executable",
    port_available=lambda _port: True,
) -> dict[str, Any]:
    return _doctor().run_preflight(
        repo_root=repo,
        state_dir=state,
        commands=commands,
        required_files=required_files,
        ports=ports,
        which=which,
        port_available=port_available,
    )


def test_preflight_reports_honest_non_release_boundary(tmp_path: Path) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    report = _run(
        repo,
        tmp_path / "state",
        commands=("python3.14", "cargo"),
        required_files=REQUIRED_FILES,
        ports=(41001, 41002),
    )

    assert report["protocol"] == PROTOCOL
    assert report["local_preflight_ok"] is True
    _assert_unqualified(report)
    assert all(check["ok"] for check in report["checks"])
    rendered = doctor.canonical_json(report)
    assert json.loads(rendered)["protocol"] == PROTOCOL
    assert rendered == json.dumps(report, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    "commands",
    [
        pytest.param(("",), id="empty"),
        pytest.param(("git", "git"), id="duplicate"),
        pytest.param((17,), id="non-string"),
        pytest.param(("../git",), id="traversal-shaped"),
        pytest.param(("/usr/bin/git",), id="absolute-shaped"),
        pytest.param(("bin/git",), id="path-shaped"),
        pytest.param(("git\\helper",), id="windows-path-shaped"),
        pytest.param(("git\nhelper",), id="control-character"),
        pytest.param(("g\u0456t",), id="unicode-confusable"),
    ],
)
def test_invalid_command_inputs_fail_closed_without_probe(
    tmp_path: Path, commands: tuple[Any, ...]
) -> None:
    repo = _fixture_repo(tmp_path)
    calls: list[Any] = []
    report = _run(
        repo,
        tmp_path / "state",
        commands=commands,
        which=lambda command: calls.append(command) or "/tools/executable",
    )

    assert report["local_preflight_ok"] is False
    assert _check(report, "commands_input")["ok"] is False
    assert calls == []
    _assert_unqualified(report)


@pytest.mark.parametrize(
    ("returned", "case"),
    [
        pytest.param("", "empty-string", id="empty-string"),
        pytest.param("\0private-probe-value", "nul-string", id="nul-string"),
        pytest.param(False, "bool", id="bool"),
        pytest.param(7, "int", id="int"),
        pytest.param(object(), "object", id="object"),
    ],
)
def test_malformed_which_results_fail_closed(
    tmp_path: Path, returned: Any, case: str
) -> None:
    repo = _fixture_repo(tmp_path)
    report = _run(
        repo,
        tmp_path / "state",
        commands=("git",),
        which=lambda _command: returned,
    )

    assert case
    assert report["local_preflight_ok"] is False
    assert _check(report, "command:0001") == {
        "name": "command:0001",
        "ok": False,
        "detail": "required command probe returned an invalid result",
    }
    _assert_unqualified(report)


def test_which_exception_fails_closed_without_internal_text(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    marker = "INTERNAL_SECRET_EXCEPTION_TEXT"

    def raising_which(_command: str) -> str:
        raise RuntimeError(marker)

    report = _run(
        repo,
        tmp_path / "state",
        commands=("git",),
        which=raising_which,
    )
    rendered = _doctor().canonical_json(report)

    assert report["local_preflight_ok"] is False
    assert _check(report, "command:0001")["detail"] == "required command probe failed"
    assert marker not in rendered
    assert "Traceback" not in rendered
    _assert_unqualified(report)


@pytest.mark.parametrize(
    "ports",
    [
        pytest.param((True,), id="bool"),
        pytest.param(("41001",), id="string"),
        pytest.param((41001.0,), id="float"),
        pytest.param((0,), id="zero"),
        pytest.param((65536,), id="too-large"),
        pytest.param((41001, 41001), id="duplicate"),
    ],
)
def test_invalid_port_inputs_fail_closed_without_probe(
    tmp_path: Path, ports: tuple[Any, ...]
) -> None:
    repo = _fixture_repo(tmp_path)
    calls: list[Any] = []
    report = _run(
        repo,
        tmp_path / "state",
        ports=ports,
        port_available=lambda port: calls.append(port) or True,
    )

    assert report["local_preflight_ok"] is False
    assert _check(report, "ports_input")["ok"] is False
    assert calls == []
    _assert_unqualified(report)


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param(None, id="none"),
        pytest.param(1, id="int"),
        pytest.param("available", id="string"),
        pytest.param(object(), id="object"),
    ],
)
def test_malformed_port_probe_results_fail_closed(tmp_path: Path, returned: Any) -> None:
    repo = _fixture_repo(tmp_path)
    report = _run(
        repo,
        tmp_path / "state",
        ports=(41001,),
        port_available=lambda _port: returned,
    )

    assert report["local_preflight_ok"] is False
    assert _check(report, "port:0001") == {
        "name": "port:0001",
        "ok": False,
        "detail": "local TCP port probe returned an invalid result",
    }
    _assert_unqualified(report)


def test_port_probe_exception_fails_closed_without_internal_text(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    marker = "PRIVATE_PORT_PROBE_FAILURE"

    def raising_probe(_port: int) -> bool:
        raise RuntimeError(marker)

    report = _run(
        repo,
        tmp_path / "state",
        ports=(41001,),
        port_available=raising_probe,
    )
    rendered = _doctor().canonical_json(report)

    assert report["local_preflight_ok"] is False
    assert _check(report, "port:0001")["detail"] == "local TCP port probe failed"
    assert marker not in rendered
    assert "Traceback" not in rendered
    _assert_unqualified(report)


@pytest.mark.parametrize("value", [True, False, "41001", 41001.0, None])
def test_local_port_probe_rejects_bool_and_non_int_values(value: Any) -> None:
    assert _doctor().local_tcp_port_available(value) is False


@pytest.mark.parametrize(
    "required_files",
    [
        pytest.param(("",), id="empty"),
        pytest.param((REQUIRED_FILES[0], REQUIRED_FILES[0]), id="duplicate"),
        pytest.param((17,), id="non-string"),
        pytest.param(("/private/credential-token.json",), id="absolute"),
        pytest.param(("contracts/../credential-token.json",), id="traversal"),
        pytest.param(("contracts//manifest.json",), id="repeated-separator"),
        pytest.param(("contracts/./manifest.json",), id="dot-segment"),
        pytest.param(("contracts/manifest.json/",), id="trailing-separator"),
        pytest.param(("contracts\\manifest.json",), id="backslash"),
        pytest.param(("contracts/manifest\n.json",), id="control-character"),
        pytest.param(("contracts/\uff4danifest.json",), id="unicode-confusable-letter"),
        pytest.param(("contracts\uff0fmanifest.json",), id="unicode-confusable-separator"),
    ],
)
def test_invalid_required_file_inputs_fail_closed_without_leakage(
    tmp_path: Path, required_files: tuple[Any, ...]
) -> None:
    repo = _fixture_repo(tmp_path)
    report = _run(
        repo,
        tmp_path / "state",
        required_files=required_files,
    )
    rendered = _doctor().canonical_json(report)

    assert report["local_preflight_ok"] is False
    assert _check(report, "required_files_input")["ok"] is False
    assert "/private/credential-token.json" not in rendered
    assert "credential-token" not in rendered
    assert "manifest\\n.json" not in rendered
    _assert_unqualified(report)


def test_required_file_symlink_escape_fails_closed(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    escaped = repo / REQUIRED_FILES[0]
    escaped.unlink()
    escaped.symlink_to(outside)

    report = _run(
        repo,
        tmp_path / "state",
        required_files=(REQUIRED_FILES[0],),
    )

    assert report["local_preflight_ok"] is False
    assert _check(report, "file:0001") == {
        "name": "file:0001",
        "ok": False,
        "detail": "required file is unavailable or unsafe",
    }
    _assert_unqualified(report)


def test_required_file_symlink_swap_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    relative = REQUIRED_FILES[0]
    required = repo / relative
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    real_open = doctor.os.open
    swapped = False

    def swapping_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if dir_fd is not None and path == required.name and not swapped:
            required.unlink()
            required.symlink_to(outside)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(doctor.os, "open", swapping_open)
    report = _run(
        repo,
        tmp_path / "state",
        required_files=(relative,),
    )

    assert swapped is True
    assert report["local_preflight_ok"] is False
    assert _check(report, "file:0001")["ok"] is False
    _assert_unqualified(report)


def test_required_file_descriptor_failure_fails_closed_without_internal_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    marker = "PRIVATE_DESCRIPTOR_FAILURE"

    def raising_dup(_descriptor: int) -> int:
        raise OSError(marker)

    monkeypatch.setattr(doctor.os, "dup", raising_dup)
    report = _run(
        repo,
        tmp_path / "state",
        required_files=(REQUIRED_FILES[0],),
    )
    rendered = doctor.canonical_json(report)

    assert report["local_preflight_ok"] is False
    assert _check(report, "file:0001")["ok"] is False
    assert marker not in rendered
    assert "Traceback" not in rendered
    _assert_unqualified(report)


def test_required_file_identity_mismatch_closes_open_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    real_open = doctor.os.open
    real_close = doctor.os.close
    real_stat = doctor.os.stat
    opened_contract_fds: list[int] = []
    closed_fds: list[int] = []

    def recording_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and path == "contracts":
            opened_contract_fds.append(descriptor)
        return descriptor

    def recording_close(descriptor: int) -> None:
        closed_fds.append(descriptor)
        real_close(descriptor)

    def mismatching_stat(path: Any, *args: Any, **kwargs: Any):
        result = real_stat(path, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and path == "contracts":
            return type(
                "MismatchedIdentity",
                (),
                {
                    "st_dev": result.st_dev,
                    "st_ino": result.st_ino + 1,
                    "st_mode": result.st_mode,
                },
            )()
        return result

    monkeypatch.setattr(doctor.os, "open", recording_open)
    monkeypatch.setattr(doctor.os, "close", recording_close)
    monkeypatch.setattr(doctor.os, "stat", mismatching_stat)
    report = _run(
        repo,
        tmp_path / "state",
        required_files=(REQUIRED_FILES[0],),
    )

    assert report["local_preflight_ok"] is False
    assert opened_contract_fds
    assert set(opened_contract_fds) <= set(closed_fds)
    _assert_unqualified(report)


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "symlink-parent"])
def test_unsafe_repository_roots_fail_closed(tmp_path: Path, kind: str) -> None:
    real_repo = _fixture_repo(tmp_path, name="real-repo")
    if kind == "missing":
        candidate = tmp_path / "missing-repo"
    elif kind == "file":
        candidate = tmp_path / "repo-file"
        candidate.write_text("not a directory\n", encoding="utf-8")
    elif kind == "symlink":
        candidate = tmp_path / "repo-alias"
        candidate.symlink_to(real_repo, target_is_directory=True)
    else:
        real_parent = tmp_path / "real-parent"
        nested_repo = _fixture_repo(real_parent, name="nested-repo")
        alias_parent = tmp_path / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        candidate = alias_parent / nested_repo.name

    report = _run(candidate, tmp_path / "state")

    assert report["local_preflight_ok"] is False
    assert _check(report, "repository_root")["ok"] is False
    _assert_unqualified(report)


def test_repository_root_identity_change_fails_closed(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, name="repo")
    replacement = _fixture_repo(tmp_path, name="replacement")
    parked = tmp_path / "parked"
    changed = False

    def swapping_which(_command: str) -> str:
        nonlocal changed
        repo.rename(parked)
        replacement.rename(repo)
        changed = True
        return "/tools/executable"

    report = _run(
        repo,
        tmp_path / "state",
        commands=("git",),
        required_files=REQUIRED_FILES,
        which=swapping_which,
    )

    assert changed is True
    assert report["local_preflight_ok"] is False
    assert _check(report, "repository_root_stable") == {
        "name": "repository_root_stable",
        "ok": False,
        "detail": "repository root identity changed during preflight",
    }
    _assert_unqualified(report)


@pytest.mark.parametrize("kind", ["inside", "equal", "symlink-into-source"])
def test_state_directory_inside_or_equal_to_source_fails_closed(
    tmp_path: Path, kind: str
) -> None:
    repo = _fixture_repo(tmp_path)
    if kind == "inside":
        state = repo / ".state"
    elif kind == "equal":
        state = repo
    else:
        target = repo / ".state-target"
        target.mkdir()
        state = tmp_path / "state-alias"
        state.symlink_to(target, target_is_directory=True)

    report = _run(repo, state)

    assert report["local_preflight_ok"] is False
    assert _check(report, "state_directory_outside_source") == {
        "name": "state_directory_outside_source",
        "ok": False,
        "detail": "state directory must resolve outside repository root",
    }
    _assert_unqualified(report)


def test_state_directory_symlink_swap_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    parked = tmp_path / "parked-state"
    replacement = tmp_path / "replacement-state"
    replacement.mkdir()
    real_is_symlink = doctor.Path.is_symlink
    swapped = False

    def swapping_is_symlink(path: Path) -> bool:
        nonlocal swapped
        result = real_is_symlink(path)
        if path == state and not swapped:
            state.rename(parked)
            state.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(doctor.Path, "is_symlink", swapping_is_symlink)
    report = _run(repo, state)

    assert swapped is True
    assert state.is_symlink()
    assert report["local_preflight_ok"] is False
    assert _check(report, "state_directory_target_safe")["ok"] is False
    _assert_unqualified(report)


def test_state_directory_missing_immediate_parent_fails_closed(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    state = tmp_path / "missing-parent" / "state"

    report = _run(repo, state)

    assert report["local_preflight_ok"] is False
    assert _check(report, "state_directory_parent_writable") == {
        "name": "state_directory_parent_writable",
        "ok": False,
        "detail": "state-directory immediate parent is missing or not writable",
    }
    assert not state.exists()
    assert not state.parent.exists()
    _assert_unqualified(report)


def test_state_directory_unwritable_existing_parent_fails_closed(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    parent = tmp_path / "state-parent"
    parent.mkdir()
    state = parent / "state"
    parent.chmod(0o500)
    try:
        if os.access(parent, os.W_OK | os.X_OK):
            pytest.skip("host access checks do not expose an unwritable owner directory")
        report = _run(repo, state)
    finally:
        parent.chmod(0o700)

    assert report["local_preflight_ok"] is False
    assert _check(report, "state_directory_parent_writable")["ok"] is False
    assert not state.exists()
    _assert_unqualified(report)


@pytest.mark.parametrize("target", ["commands", "required_files", "ports"])
def test_iterable_exception_midway_fails_closed_without_partial_probes(
    tmp_path: Path, target: str
) -> None:
    repo = _fixture_repo(tmp_path)
    marker = "MIDWAY_PRIVATE_ITERATOR_FAILURE"
    command_calls: list[str] = []
    port_calls: list[int] = []

    def raising(values: tuple[Any, ...]):
        yield values[0]
        raise RuntimeError(marker)

    kwargs: dict[str, Any] = {
        "commands": (),
        "required_files": (),
        "ports": (),
    }
    values_by_target = {
        "commands": ("git",),
        "required_files": (REQUIRED_FILES[0],),
        "ports": (41001,),
    }
    kwargs[target] = raising(values_by_target[target])
    report = _run(
        repo,
        tmp_path / "state",
        **kwargs,
        which=lambda command: command_calls.append(command) or "/tools/executable",
        port_available=lambda port: port_calls.append(port) or True,
    )
    rendered = _doctor().canonical_json(report)

    assert report["local_preflight_ok"] is False
    assert _check(report, f"{target}_input")["ok"] is False
    assert command_calls == []
    assert port_calls == []
    assert marker not in rendered
    assert "Traceback" not in rendered
    _assert_unqualified(report)


class _OneShot:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self._values = values
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations != 1:
            raise RuntimeError("iterable consumed more than once")
        yield from self._values


def test_one_shot_iterables_are_consumed_once(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    commands = _OneShot(("python3.14", "cargo"))
    files = _OneShot(REQUIRED_FILES)
    ports = _OneShot((41001, 41002))

    report = _run(
        repo,
        tmp_path / "state",
        commands=commands,
        required_files=files,
        ports=ports,
    )

    assert report["local_preflight_ok"] is True
    assert commands.iterations == files.iterations == ports.iterations == 1
    _assert_unqualified(report)


def test_report_is_deterministic_for_order_insensitive_iterables(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)

    def make_report(reverse: bool) -> dict[str, Any]:
        commands = ("zeta", "alpha")
        files = (REQUIRED_FILES[1], REQUIRED_FILES[0])
        ports = (41002, 41001)
        if reverse:
            commands = tuple(reversed(commands))
            files = tuple(reversed(files))
            ports = tuple(reversed(ports))
        return _run(
            repo,
            tmp_path / "state",
            commands=commands,
            required_files=files,
            ports=ports,
            which=lambda command: None if command == "zeta" else "/tools/executable",
            port_available=lambda port: port == 41001,
        )

    first = make_report(False)
    second = make_report(True)

    assert _doctor().canonical_json(first) == _doctor().canonical_json(second)
    _assert_unqualified(first)
    _assert_unqualified(second)


def test_duplicate_inputs_cannot_create_duplicate_check_names(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    report = _run(
        repo,
        tmp_path / "state",
        commands=("git", "git"),
        required_files=(REQUIRED_FILES[0], REQUIRED_FILES[0]),
        ports=(41001, 41001),
    )

    assert report["local_preflight_ok"] is False
    assert _check(report, "commands_input")["ok"] is False
    assert _check(report, "required_files_input")["ok"] is False
    assert _check(report, "ports_input")["ok"] is False
    _assert_unqualified(report)


def test_report_omits_inputs_paths_environment_and_probe_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fixture_repo(tmp_path)
    state = tmp_path / "state"
    command_marker = "credential-token-value"
    absolute_marker = "/private/credentials/api-token.json"
    environment_marker = "ENVIRONMENT_PRIVATE_VALUE_6d7d"
    probe_marker = "/private/environment/private-executable"
    monkeypatch.setenv("MYCELIUM_DOCTOR_PRIVATE_VALUE", environment_marker)

    report = _run(
        repo,
        state,
        commands=(command_marker,),
        required_files=(absolute_marker,),
        which=lambda _command: probe_marker,
    )
    rendered = _doctor().canonical_json(report)

    for forbidden in (
        command_marker,
        absolute_marker,
        environment_marker,
        probe_marker,
        str(repo),
        str(state),
        "Traceback",
    ):
        assert forbidden not in rendered
    _assert_unqualified(report)


def _snapshot(root: Path) -> list[tuple[str, int, int, bytes | None]]:
    snapshot: list[tuple[str, int, int, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        payload = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        snapshot.append((relative, info.st_mode, info.st_size, payload))
    return snapshot


def test_preflight_is_read_only_and_uses_only_read_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctor = _doctor()
    repo = _fixture_repo(tmp_path)
    state = tmp_path / "state"
    before = _snapshot(repo)
    real_open = doctor.os.open
    observed_flags: list[int] = []

    def recording_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        observed_flags.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(doctor.os, "open", recording_open)
    report = _run(
        repo,
        state,
        commands=("git",),
        required_files=REQUIRED_FILES,
        ports=(41001,),
    )

    mutating = os.O_CREAT | os.O_TRUNC | os.O_APPEND
    if hasattr(os, "O_EXCL"):
        mutating |= os.O_EXCL
    assert report["local_preflight_ok"] is True
    assert observed_flags
    assert all(flags & os.O_ACCMODE == os.O_RDONLY for flags in observed_flags)
    assert all(flags & mutating == 0 for flags in observed_flags)
    assert not state.exists()
    assert _snapshot(repo) == before
    _assert_unqualified(report)


def test_local_port_probe_closes_its_point_in_time_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doctor = _doctor()

    class FakeSocket:
        entered = False
        exited = False
        bound: tuple[str, int] | None = None

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, *_args: Any) -> None:
            self.exited = True

        def bind(self, address: tuple[str, int]) -> None:
            self.bound = address

    fake = FakeSocket()
    monkeypatch.setattr(doctor.socket, "socket", lambda *_args: fake)

    assert doctor.local_tcp_port_available(41001) is True
    assert fake.entered is True
    assert fake.exited is True
    assert fake.bound == ("127.0.0.1", 41001)


def test_doctor_cli_emits_canonical_json_and_uses_only_local_preflight_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("mycelium_demo.cli")
    repo = _fixture_repo(tmp_path)

    exit_code = cli.main(
        [
            "doctor",
            "--repo-root",
            str(repo),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        which=lambda _command: "/tools/executable",
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    assert report["local_preflight_ok"] is True
    _assert_unqualified(report)


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("credential-token-private-port", id="non-int"),
        pytest.param("9" * 5000, id="oversized-numeric"),
    ],
)
def test_doctor_cli_invalid_port_is_private_json_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], marker: str
) -> None:
    cli = importlib.import_module("mycelium_demo.cli")
    repo = _fixture_repo(tmp_path)

    exit_code = cli.main(
        [
            "doctor",
            "--repo-root",
            str(repo),
            "--state-dir",
            str(tmp_path / "state"),
            "--port",
            marker,
        ],
        which=lambda _command: "/tools/executable",
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 1
    assert captured.err == ""
    assert report["local_preflight_ok"] is False
    assert marker not in captured.out
    _assert_unqualified(report)


def test_doctor_cli_probe_failure_is_json_exit_one_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("mycelium_demo.cli")
    repo = _fixture_repo(tmp_path)
    marker = "CLI_INTERNAL_PRIVATE_FAILURE"

    def raising_which(_command: str) -> str:
        raise RuntimeError(marker)

    exit_code = cli.main(
        [
            "doctor",
            "--repo-root",
            str(repo),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        which=raising_which,
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert exit_code == 1
    assert captured.err == ""
    assert report["local_preflight_ok"] is False
    assert marker not in captured.out
    assert "Traceback" not in captured.out
    _assert_unqualified(report)


def test_doctor_runbook_preserves_exact_claim_boundary() -> None:
    runbook = Path("docs/demo/release-doctor-preflight.md").read_text(encoding="utf-8")

    for required in (
        "python3.14 -m mycelium_demo doctor",
        "Local environment preflight only.",
        "No physical-host evidence.",
        "No qualification consumption.",
        "No release-readiness claim.",
        "route_ready=false",
        "release_ready=false",
        "qualification_evaluated=false",
        "does not start processes",
        "does not perform physical qualification",
        "No package installation",
        "point-in-time",
        "does not reserve the port",
        "Observatory request and qualification event adapter",
    ):
        assert required in runbook
