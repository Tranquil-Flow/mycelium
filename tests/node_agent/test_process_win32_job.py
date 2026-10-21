# SPDX-License-Identifier: AGPL-3.0-only
"""The physical node service is launched and terminated inside a Windows job.

A ``FakeWin32Api`` models the job object and process API over a real child
process (``sys.platform`` reports ``win32``).  The contract under test:

* order: suspended spawn -> job limits -> assign -> identity -> resume;
* ``KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION`` set, breakaway refused;
* assign failure kills the suspended child -> ``node_process_start_failed``;
* image mismatch -> ``node_process_identity_invalid``, child never resumed;
* graceful stop is the control ``stop`` frame; hard stop is
  ``TerminateJobObject`` and completes only at ``ActiveProcesses == 0``;
* a job that contains the current process is never terminated;
* a control-frame write that misses its deadline fails closed as partial.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import textwrap
import threading
import time

import pytest

import mycelium_host_platform
from mycelium_host_platform import _win32_api as w
from mycelium_host_platform._win32_job import CREATION_FLAGS, REQUIRED_LIMITS
from mycelium_node import process

from tests.host_platform.fake_win32 import simulate_win32


REAL_INTERPRETER = sys.executable

_CHILD = textwrap.dedent(
    """
    import json, sys, time

    behavior = sys.argv[1]

    def reply(document, result):
        response = {
            "protocol": document["protocol"],
            "command_id": document["command_id"],
            "node_id": "node-win",
            "ok": True,
            "route_ready": False,
            "result": result,
        }
        sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        document = json.loads(line)
        if document["command"] == "hello":
            reply(document, {"route_ready": False})
        elif document["command"] == "stop":
            reply(document, {"stopped": True})
            if behavior == "ignore_stop":
                time.sleep(120)
            sys.exit(0)
    if behavior == "ignore_stop":
        time.sleep(120)
    """
)


@pytest.fixture
def launch(tmp_path, monkeypatch):
    fake = simulate_win32(monkeypatch)
    fake.interpreter = REAL_INTERPRETER
    interpreter = tmp_path / "bin" / "python.exe"
    interpreter.parent.mkdir()
    interpreter.write_bytes(b"MZ" + b"\0" * 126)
    monkeypatch.setattr(process.sys, "executable", str(interpreter))
    script = tmp_path / "service_child.py"
    script.write_text(_CHILD, encoding="utf-8")
    started: list[process.PhysicalNodeProcess] = []

    def start(behavior: str = "normal", **overrides) -> process.PhysicalNodeProcess:
        node = process.PhysicalNodeProcess(
            command=(str(interpreter), str(script), behavior),
            node_id="node-win",
            run_id="run-win",
            deployment_id="deployment-win",
            response_timeout_seconds=overrides.get("response_timeout_seconds", 10.0),
            shutdown_timeout_seconds=overrides.get("shutdown_timeout_seconds", 5.0),
        )
        started.append(node)
        return node

    start.fake = fake
    start.interpreter = interpreter
    yield start
    fake.current_process_in_job.clear()
    for node in started:
        try:
            node.close()
        except process.NodeProcessError:
            pass
    for popen in fake.popen_by_pid.values():
        if popen.poll() is None:
            popen.kill()
            popen.wait(timeout=5)
        for stream in (popen.stdin, popen.stdout, popen.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def _code(call) -> str:
    with pytest.raises(process.NodeProcessError) as caught:
        call()
    return caught.value.code


def _only_child(fake):
    assert len(fake.popen_by_pid) == 1
    return next(iter(fake.popen_by_pid.values()))


def test_the_child_is_contained_before_it_is_resumed(launch) -> None:
    node = launch()
    fake = launch.fake
    names = [call[0] for call in fake.calls]
    order = [
        "spawn_suspended",
        "create_job",
        "set_job_limits",
        "job_limit_flags",
        "open_process",
        "assign_process",
        "is_process_in_job",
        "process_image_name",
        "process_creation_time",
        "resume_process",
    ]
    positions = [names.index(name) for name in order]
    assert positions == sorted(positions)
    spawn = next(call for call in fake.calls if call[0] == "spawn_suspended")
    assert spawn[1][0] == str(launch.interpreter)
    assert spawn[2] == CREATION_FLAGS
    assert CREATION_FLAGS & w.CREATE_SUSPENDED
    assert CREATION_FLAGS & w.CREATE_NEW_PROCESS_GROUP
    limits = next(call for call in fake.calls if call[0] == "set_job_limits")
    assert limits[2] == REQUIRED_LIMITS
    assert limits[2] & w.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert limits[2] & w.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    assert not limits[2] & (
        w.JOB_OBJECT_LIMIT_BREAKAWAY_OK | w.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    )
    assert node.command("hello") == {"route_ready": False}


def test_graceful_stop_uses_the_control_frame_and_empties_the_job(launch) -> None:
    node = launch()
    child = _only_child(launch.fake)
    # close() gives the stop frame at most 0.5 s; send it with a load-tolerant
    # budget first so the graceful path is what is observed.
    node._command_stop(timeout=30.0)
    child.wait(timeout=30.0)
    node.close()
    assert child.returncode == 0
    assert "terminate_job" not in launch.fake.call_names()
    assert len(launch.fake.closed_jobs) == 1
    assert node.running is False


def test_hard_stop_is_terminate_job_object_until_no_process_is_active(launch) -> None:
    node = launch("ignore_stop")
    child = _only_child(launch.fake)
    started = time.monotonic()
    node.close()
    assert time.monotonic() - started < 5.0
    assert child.poll() is not None and child.returncode != 0
    names = launch.fake.call_names()
    terminate = names.index("terminate_job")
    assert "job_active_processes" in names[terminate:]
    assert len(launch.fake.closed_jobs) == 1


def test_assign_failure_kills_the_suspended_child(launch) -> None:
    launch.fake.fail_next("assign_process", w.ERROR_ACCESS_DENIED)
    assert _code(launch) == "node_process_start_failed"
    child = _only_child(launch.fake)
    assert child.poll() is not None
    assert "resume_process" not in launch.fake.call_names()
    assert launch.fake.jobs == {}


@pytest.mark.parametrize(
    "flags",
    [
        w.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION,
        REQUIRED_LIMITS | w.JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        REQUIRED_LIMITS | w.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
    ],
)
def test_a_job_without_the_required_limits_is_refused(launch, flags) -> None:
    launch.fake.limit_flags_override = flags
    assert _code(launch) == "node_process_start_failed"
    assert _only_child(launch.fake).poll() is not None
    assert "resume_process" not in launch.fake.call_names()


def test_an_image_mismatch_is_refused_before_resume(launch, tmp_path) -> None:
    other = tmp_path / "bin" / "other.exe"
    other.write_bytes(b"MZ" + b"\1" * 126)
    launch.fake.image_override = str(other)
    assert _code(launch) == "node_process_identity_invalid"
    names = launch.fake.call_names()
    assert "resume_process" not in names
    assert "terminate_job" in names
    assert _only_child(launch.fake).poll() is not None


def test_a_changed_interpreter_image_is_refused(launch) -> None:
    original = launch.fake.process_image_name

    def swapped(handle: int) -> str:
        image = original(handle)
        launch.interpreter.write_bytes(b"MZ" + b"\2" * 200)
        return image

    launch.fake.process_image_name = swapped
    assert _code(launch) == "node_process_identity_invalid"
    assert _only_child(launch.fake).poll() is not None


def test_a_resume_that_resumes_nothing_fails_closed(launch) -> None:
    launch.fake.resume_count = 0
    assert _code(launch) == "node_process_start_failed"
    assert _only_child(launch.fake).poll() is not None


def test_a_job_containing_this_process_is_never_terminated(launch) -> None:
    node = launch("ignore_stop", shutdown_timeout_seconds=1.0)
    job = next(iter(launch.fake.jobs))
    launch.fake.current_process_in_job.add(job)
    assert _code(node.close) == "node_process_cleanup_failed"
    assert "terminate_job" not in launch.fake.call_names()
    assert _only_child(launch.fake).poll() is None
    launch.fake.current_process_in_job.clear()
    node.close()
    assert _only_child(launch.fake).poll() is not None


def test_a_spawn_failure_starts_nothing(launch) -> None:
    launch.fake.fail_next("spawn_suspended", w.ERROR_ACCESS_DENIED)
    assert _code(launch) == "node_process_start_failed"
    assert launch.fake.popen_by_pid == {}
    assert "create_job" not in launch.fake.call_names()


def test_a_control_frame_write_past_its_deadline_is_partial(monkeypatch) -> None:
    read_end, write_end = os.pipe()
    stream = os.fdopen(write_end, "wb", buffering=0)
    try:
        started = time.monotonic()
        with pytest.raises(process._ControlFrameWriteTimeout) as caught:
            process._win32_write_control_frame_before(
                stream, b"x" * (4 * 1024 * 1024), time.monotonic() + 0.2
            )
        assert caught.value.partial is True
        assert time.monotonic() - started < 2.0
    finally:
        os.close(read_end)
        stream.close() if not stream.closed else None
    # An expired deadline never starts a write.
    with pytest.raises(process._ControlFrameWriteTimeout) as caught:
        process._win32_write_control_frame_before(stream, b"x", time.monotonic() - 1)
    assert caught.value.partial is False


def test_a_control_frame_write_inside_its_deadline_is_complete(monkeypatch) -> None:
    read_end, write_end = os.pipe()
    received: list[bytes] = []
    reader = threading.Thread(target=lambda: received.append(os.read(read_end, 1 << 20)))
    reader.start()
    with os.fdopen(write_end, "wb", buffering=0) as stream:
        process._win32_write_control_frame_before(stream, b"frame\n", time.monotonic() + 5)
    reader.join(5)
    os.close(read_end)
    assert received == [b"frame\n"]


def test_the_write_path_dispatches_to_the_threaded_writer_on_win32(monkeypatch) -> None:
    simulate_win32(monkeypatch)
    seen: list[bytes] = []
    monkeypatch.setattr(
        process,
        "_win32_write_control_frame_before",
        lambda stream, payload, deadline: seen.append(payload),
    )
    process._write_control_frame_before(object(), b"p", time.monotonic() + 1)
    assert seen == [b"p"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launch path: this host is win32")
def test_posix_launch_never_touches_the_job_seam(tmp_path) -> None:
    assert mycelium_host_platform.is_win32() is False
    assert Path(process.__file__).exists()
    # The POSIX launch path is covered by test_process.py unchanged; this
    # guards that the win32 attribute stays unset there.
    script = tmp_path / "noop.py"
    script.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    node = process.PhysicalNodeProcess(
        command=(REAL_INTERPRETER, str(script)),
        node_id="node-posix",
        run_id="run-posix",
        deployment_id="deployment-posix",
        shutdown_timeout_seconds=5.0,
    )
    try:
        assert node._win32_job is None
    finally:
        node.close()


def test_job_memory_is_the_sum_of_every_process_working_set(launch) -> None:
    node = launch()
    child = _only_child(launch.fake)
    launch.fake.working_set_by_pid = {child.pid: 5_000_000}
    assert node.win32_job_resident_bytes() == 5_000_000
    assert node.win32_job_owned() is True
    launch.fake.fail_next("process_working_set_bytes_of", 5)
    with pytest.raises(Exception):
        node.win32_job_resident_bytes()
    node._command_stop(timeout=30.0)
    child.wait(timeout=30.0)
    node.close()
    assert node.win32_job_owned() is False
    assert _code(node.win32_job_resident_bytes) == "node_process_job_unavailable"
