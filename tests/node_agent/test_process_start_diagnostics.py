# SPDX-License-Identifier: AGPL-3.0-or-later
"""``node_process_start_failed`` must carry a bounded, closed cause.

Two-Mac run #10: the joined 8 GB Air's accepted work failed with
``node_process_start_failed`` and nothing on either Mac said WHICH launch
step failed.  ``PhysicalNodeProcess`` raises that one code from the
handshake pipes, the ``Popen``, the launcher ready byte, the pre-exec
ownership capture, the release, the target exec, the post-exec inventory and
the identity check -- and an inner ``node_process_identity_invalid`` was
collapsed into it as well.  These tests pin a closed cause onto the error:
phase, outcome kind, elapsed time against the launch budget, exception type
and errno -- never a path, a message or an identifier.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
import sys
from typing import Any

import pytest

import mycelium_node.process as process_module


_RECORD_KEYS = {
    "phase", "kind", "elapsed_ms", "budget_ms", "popen_ms", "exception_type",
    "errno", "inner_code", "launcher_exited", "launcher_returncode", "phase_ms",
}


def _launch(tmp_path: Path, **overrides: Any) -> process_module.PhysicalNodeProcess:
    target = tmp_path / "target.py"
    if not target.exists():
        target.write_text("import sys, time\nsys.stdin.read()\n")
    options: dict[str, Any] = dict(
        command=(sys.executable, str(target)),
        node_id="node-start-diagnostics",
        run_id="run-start-diagnostics",
        deployment_id="deployment-start-diagnostics",
        response_timeout_seconds=0.05,
        shutdown_timeout_seconds=1.0,
    )
    options.update(overrides)
    return process_module.PhysicalNodeProcess(**options)


def _failure(tmp_path: Path, **overrides: Any) -> process_module.NodeProcessError:
    with pytest.raises(process_module.NodeProcessError) as caught:
        _launch(tmp_path, **overrides)
    return caught.value


def _assert_bounded(record: dict[str, Any], *forbidden: str) -> None:
    assert set(record) == _RECORD_KEYS
    assert record["phase"] in process_module.NODE_START_FAILURE_PHASES
    assert record["kind"] in process_module.NODE_START_FAILURE_KINDS
    assert record["budget_ms"] == int(process_module._LAUNCH_HANDSHAKE_SECONDS * 1000)
    raw = json.dumps(record, sort_keys=True)
    assert len(raw) < 1024
    for text in forbidden:
        assert text not in raw


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher handshake")
def test_launcher_that_never_signals_ready_is_named_launcher_ready_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED before the fix: the error said only ``node_process_start_failed``."""

    monkeypatch.setattr(process_module, "_LAUNCH_HANDSHAKE_SECONDS", 0.3)
    monkeypatch.setattr(
        process_module, "_LAUNCHER_SOURCE",
        "import time\ntime.sleep(30)\n" + process_module._LAUNCHER_SOURCE,
    )
    error = _failure(tmp_path)
    assert error.code == "node_process_start_failed"
    assert str(error) == "node_process_start_failed"  # existing string contract kept
    failure = error.start_failure
    assert failure is not None
    record = failure.as_record()
    _assert_bounded(record, str(tmp_path), "start-diagnostics")
    assert record["phase"] == "launcher_ready"
    assert record["kind"] == "timeout"
    assert record["budget_ms"] == 300
    assert 300 <= record["elapsed_ms"] < 5_000
    assert record["launcher_exited"] is False  # alive and silent, not killed
    assert record["exception_type"] == "TimeoutError"
    assert record["phase_ms"] == {}  # no step completed
    assert error.public_code == "node_process_start_failed_launcher_ready_timeout"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher handshake")
def test_launcher_that_dies_before_ready_records_exit_and_returncode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_module, "_LAUNCHER_SOURCE", "import os\nos._exit(3)\n")
    error = _failure(tmp_path)
    # Unchanged fail-closed verdict: with no pre-exec ownership captured the
    # owner cannot prove the group is gone, so this is a CLEANUP failure.  A
    # bare ``node_process_start_failed`` therefore never means "the launcher
    # died before ready".
    assert error.code == "node_process_cleanup_failed"
    record = error.start_failure.as_record()
    _assert_bounded(record, str(tmp_path))
    assert (record["phase"], record["kind"]) == ("launcher_ready", "exited")
    assert record["launcher_exited"] is True
    assert record["launcher_returncode"] == 3
    assert error.public_code == "node_process_cleanup_failed_launcher_ready_exited"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher handshake")
def test_popen_failure_keeps_errno_but_not_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.EAGAIN, "secret-fork-message /private/secret/path")

    monkeypatch.setattr(process_module.subprocess, "Popen", refuse)
    error = _failure(tmp_path)
    assert error.code == "node_process_start_failed"
    record = error.start_failure.as_record()
    _assert_bounded(record, "secret", "/private")
    assert (record["phase"], record["kind"]) == ("popen", "error")
    assert record["errno"] == errno.EAGAIN
    assert record["exception_type"] == "BlockingIOError"
    assert record["launcher_exited"] is None
    assert error.public_code == "node_process_start_failed_popen_error"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher handshake")
def test_handshake_pipe_exhaustion_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def exhausted() -> Any:
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(process_module, "_handshake_pipes", exhausted)
    record = _failure(tmp_path).start_failure.as_record()
    _assert_bounded(record)
    assert (record["phase"], record["kind"], record["errno"]) == (
        "handshake_pipes", "error", errno.EMFILE,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher handshake")
def test_release_failure_names_the_release_phase_and_keeps_cleanup_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_descriptor: int) -> None:
        raise OSError("secret-release-failure")

    monkeypatch.setattr(process_module, "_release_pre_exec_launcher", refuse)
    error = _failure(tmp_path)
    assert error.code == "node_process_start_failed"
    assert error.cleanup_owner is None
    record = error.start_failure.as_record()
    _assert_bounded(record, "secret")
    assert (record["phase"], record["kind"]) == ("launcher_release", "error")
    assert list(record["phase_ms"]) == ["launcher_ready", "pre_exec_ownership"]
    assert record["phase_ms"]["launcher_ready"] <= record["elapsed_ms"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher handshake")
def test_inner_identity_refusal_is_no_longer_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ownership check itself is unchanged: it still refuses, and says so."""

    monkeypatch.setattr(
        process_module, "_identity_matches_pre_exec_ownership", lambda *_args: False,
    )
    error = _failure(tmp_path)
    assert error.code == "node_process_start_failed"
    record = error.start_failure.as_record()
    _assert_bounded(record)
    assert record["kind"] == "identity_invalid"
    assert record["inner_code"] == "node_process_identity_invalid"
    assert record["phase"] in {"inventory", "identity"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher handshake")
def test_successful_launch_exposes_bounded_phase_timing(tmp_path: Path) -> None:
    process = _launch(tmp_path)
    try:
        timing = process.launch_timing
        assert set(timing) == {"budget_ms", "popen_ms", "phase_ms"}
        assert timing["budget_ms"] == int(process_module._LAUNCH_HANDSHAKE_SECONDS * 1000)
        marks = timing["phase_ms"]
        assert list(marks) == [
            "launcher_ready", "pre_exec_ownership", "launcher_release",
            "target_exec", "inventory", "identity",
        ]
        values = list(marks.values())
        assert values == sorted(values) and all(isinstance(v, int) for v in values)
        assert values[-1] < timing["budget_ms"]
    finally:
        process.close()


def test_start_failure_record_rejects_unbounded_values() -> None:
    failure = process_module.NodeStartFailure.from_exception(
        phase="not-a-phase", exception=ValueError("x" * 500), started=None, budget_seconds=2.0,
        popen_ms=None, phase_ms={"identity": 9, "launcher_ready": 1, "bogus": 2}, launcher_returncode=None,
        launcher_exited=None,
    )
    record = failure.as_record()
    assert record["phase"] == "unknown"
    assert record["phase_ms"] == {"launcher_ready": 1, "identity": 9}  # closed, in launch order
    assert record["elapsed_ms"] is None
    assert "x" * 10 not in json.dumps(record)


def test_public_code_falls_back_to_the_bare_code_without_a_cause() -> None:
    assert process_module.NodeProcessError("node_process_start_failed").public_code == (
        "node_process_start_failed"
    )
