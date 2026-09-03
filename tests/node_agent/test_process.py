from __future__ import annotations

from collections import OrderedDict
import io
import json
import os
from pathlib import Path
from queue import Empty, Queue
import stat
import sys
import textwrap
import threading
import time

import pytest

import mycelium_node.process as process_module

from tests.e2e_request_iroh.conftest import (
    native_iroh_sidecar_binary as node_agent_sidecar_binary,  # noqa: F401
)

from mycelium_node.process import (
    MAX_CONTROL_FRAME_BYTES,
    NodeProcessError,
    PhysicalNodeProcess,
    build_physical_node_command,
)


PROTOCOL = "mycelium.physical_node_control.v1"
CLEANUP_PREFIX = process_module.CLEANUP_CONTROL_FRAME_PREFIX


def _fake_service(tmp_path: Path, behavior: str = "ok") -> Path:
    script = tmp_path / "fake_physical_node.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            import sys
            import time

            behavior = {behavior!r}
            node_id = "node-a"
            for raw in sys.stdin.buffer:
                command = json.loads(raw)
                if behavior == "timeout":
                    time.sleep(60)
                    continue
                command_id = command["command_id"]
                if behavior == "wrong_id":
                    command_id = "wrong-command"
                response = {{
                    "protocol": {PROTOCOL!r},
                    "command_id": command_id,
                    "node_id": node_id,
                    "ok": behavior != "remote_error",
                    "route_ready": behavior == "route_ready",
                }}
                if behavior == "remote_error":
                    response["error"] = {{"code": "assignment_rejected"}}
                else:
                    response["result"] = {{
                        "command": command["command"],
                        "value": command["payload"].get("value"),
                    }}
                encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
                if behavior == "cleanup_stderr":
                    sys.stderr.buffer.write({CLEANUP_PREFIX!r} + encoded + b"\\n")
                    sys.stderr.buffer.flush()
                else:
                    sys.stdout.buffer.write(encoded + b"\\n")
                    sys.stdout.buffer.flush()
                if command["command"] == "stop":
                    raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return script


def _process(
    tmp_path: Path, behavior: str = "ok", *, timeout: float = 1.0
) -> PhysicalNodeProcess:
    return PhysicalNodeProcess(
        command=(sys.executable, str(_fake_service(tmp_path, behavior))),
        node_id="node-a",
        run_id="run-1",
        deployment_id="deployment-1",
        response_timeout_seconds=timeout,
    )


def test_real_child_round_trip_and_graceful_stop(tmp_path: Path) -> None:
    process = _process(tmp_path)
    with process:
        assert process.running is True
        result = process.command("configure", {"value": 17})
        assert result == {"command": "configure", "value": 17}
        pid = process.pid
        assert isinstance(pid, int) and pid > 0
    assert process.running is False


def test_response_framing_drains_child_stdout_in_buffered_blocks(
    tmp_path: Path,
) -> None:
    process = _process(tmp_path)
    try:
        # Physical SSH responses can approach the control-frame bound.  The
        # framing loop calls readline(), so the underlying pipe must be a
        # BufferedReader; raw FileIO performs byte-at-a-time reads and can
        # strand a later cleanup receipt behind seconds of already-flushed
        # response bytes.
        assert isinstance(process._process.stdout, io.BufferedReader)
        assert process.command("configure", {"value": 17}) == {
            "command": "configure",
            "value": 17,
        }
    finally:
        process.close()


def test_cleanup_response_uses_separate_supervised_stderr_stream(
    tmp_path: Path,
) -> None:
    process = _process(tmp_path, "cleanup_stderr")
    try:
        assert process.command("infer_cancel_wait", {"value": 23}) == {
            "command": "infer_cancel_wait",
            "value": 23,
        }
        assert process.stderr_tail == ""
    finally:
        process.close()


def test_remote_error_preserves_stable_code_and_closes(tmp_path: Path) -> None:
    process = _process(tmp_path, "remote_error")
    try:
        with pytest.raises(NodeProcessError) as caught:
            process.command("configure")
        assert caught.value.code == "assignment_rejected"
    finally:
        process.close()
    assert process.running is False


@pytest.mark.parametrize(
    ("behavior", "expected_code"),
    (
        ("wrong_id", "response_command_mismatch"),
        ("route_ready", "invalid_node_response"),
    ),
)
def test_fail_closed_on_unbound_or_claim_inflating_response(
    tmp_path: Path, behavior: str, expected_code: str
) -> None:
    process = _process(tmp_path, behavior)
    try:
        with pytest.raises(NodeProcessError) as caught:
            process.command("configure")
        assert caught.value.code == expected_code
        assert process.running is False
    finally:
        process.close()


def test_timeout_terminates_child_and_rejects_future_commands(tmp_path: Path) -> None:
    process = _process(tmp_path, "timeout", timeout=0.05)
    started = time.monotonic()
    with pytest.raises(NodeProcessError) as caught:
        process.command("configure")
    assert caught.value.code == "node_response_timeout"
    assert time.monotonic() - started < 2.0
    assert process.running is False
    with pytest.raises(NodeProcessError) as second:
        process.command("status")
    assert second.value.code == "node_process_closed"


def test_correlated_waiters_allow_out_of_order_concurrent_responses(
    tmp_path: Path,
) -> None:
    script = tmp_path / "concurrent_physical_node.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            import sys
            import threading
            import time

            write_lock = threading.Lock()
            def respond(command):
                time.sleep(float(command["payload"].get("delay", 0)))
                response = {{
                    "protocol": {PROTOCOL!r},
                    "command_id": command["command_id"],
                    "node_id": "node-a",
                    "ok": True,
                    "route_ready": False,
                    "result": {{"value": command["payload"].get("value")}},
                }}
                with write_lock:
                    sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\\n")
                    sys.stdout.flush()

            workers = []
            for raw in sys.stdin.buffer:
                command = json.loads(raw)
                worker = threading.Thread(target=respond, args=(command,))
                worker.start()
                workers.append(worker)
                if command["command"] == "stop":
                    break
            for worker in workers:
                worker.join()
            """
        ),
        encoding="utf-8",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    process = PhysicalNodeProcess(
        command=(sys.executable, str(script)),
        node_id="node-a",
        run_id="run-1",
        deployment_id="deployment-1",
        response_timeout_seconds=1.0,
    )
    results: dict[str, object] = {}
    slow = threading.Thread(
        target=lambda: results.setdefault(
            "slow", process.command("configure", {"value": "slow", "delay": 0.2})
        )
    )
    fast = threading.Thread(
        target=lambda: results.setdefault(
            "fast", process.command("configure", {"value": "fast", "delay": 0.01})
        )
    )
    try:
        slow.start()
        time.sleep(0.02)
        fast.start()
        fast.join(timeout=0.1)
        assert not fast.is_alive()
        assert slow.is_alive()
        slow.join(timeout=1.0)
        assert results == {"fast": {"value": "fast"}, "slow": {"value": "slow"}}
    finally:
        process.close()


def test_request_scoped_timeout_does_not_terminate_shared_node(tmp_path: Path) -> None:
    process = _process(tmp_path, "timeout", timeout=0.05)
    try:
        with pytest.raises(NodeProcessError, match="node_response_timeout"):
            process.command("configure", terminate_on_timeout=False)
        assert process.running is True
    finally:
        process.close()


def test_late_scoped_result_is_discarded_and_shared_waiter_survives(
    tmp_path: Path,
) -> None:
    script = tmp_path / "late-response-node.py"
    script.write_text(
        """
import json
import sys
import threading
import time

write_lock = threading.Lock()

def respond(command):
    time.sleep(command["payload"]["delay"])
    response = {
        "protocol": command["protocol"],
        "command_id": command["command_id"],
        "node_id": "node-a",
        "ok": True,
        "route_ready": False,
        "result": {"value": command["payload"]["value"]},
    }
    with write_lock:
        print(json.dumps(response, sort_keys=True, separators=(",", ":")), flush=True)

for raw in sys.stdin:
    command = json.loads(raw)
    threading.Thread(target=respond, args=(command,), daemon=True).start()
""",
        encoding="utf-8",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    process = PhysicalNodeProcess(
        command=(sys.executable, str(script)),
        node_id="node-a",
        run_id="run-1",
        deployment_id="deployment-1",
        response_timeout_seconds=1.0,
    )
    healthy: list[object] = []

    def wait_for_late() -> None:
        with pytest.raises(NodeProcessError, match="node_response_timeout"):
            process.command(
                "configure",
                {"value": "late", "delay": 0.08},
                timeout_seconds=0.02,
                terminate_on_timeout=False,
            )

    late_thread = threading.Thread(target=wait_for_late)
    healthy_thread = threading.Thread(
        target=lambda: healthy.append(
            process.command(
                "configure",
                {"value": "healthy", "delay": 0.12},
                timeout_seconds=0.5,
                terminate_on_timeout=False,
            )
        )
    )
    try:
        late_thread.start()
        healthy_thread.start()
        late_thread.join(timeout=1.0)
        healthy_thread.join(timeout=1.0)
        assert not late_thread.is_alive()
        assert not healthy_thread.is_alive()
        assert healthy == [{"value": "healthy"}]
        assert process.running is True
    finally:
        process.close()


def test_request_scoped_interrupt_retires_only_target_waiter_and_discards_late_result(
    tmp_path: Path,
) -> None:
    script = tmp_path / "interruptible-response-node.py"
    script.write_text(
        """
import json
import sys
import threading
import time

write_lock = threading.Lock()

def respond(command):
    time.sleep(command["payload"].get("delay", 0))
    response = {
        "protocol": command["protocol"],
        "command_id": command["command_id"],
        "node_id": "node-a",
        "ok": True,
        "route_ready": False,
        "result": {"value": command["payload"].get("value")},
    }
    with write_lock:
        print(json.dumps(response, sort_keys=True, separators=(",", ":")), flush=True)

for raw in sys.stdin:
    command = json.loads(raw)
    threading.Thread(target=respond, args=(command,), daemon=True).start()
""",
        encoding="utf-8",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    process = PhysicalNodeProcess(
        command=(sys.executable, str(script)),
        node_id="node-a",
        run_id="run-1",
        deployment_id="deployment-1",
        response_timeout_seconds=1.0,
    )
    interrupted: list[str] = []
    healthy: list[object] = []

    def wait_for_interrupted() -> None:
        try:
            process.command(
                "configure",
                {"value": "late", "delay": 0.15},
                command_id="request-a-infer-start",
                terminate_on_timeout=False,
            )
        except NodeProcessError as exc:
            interrupted.append(exc.code)

    interrupted_thread = threading.Thread(target=wait_for_interrupted)
    try:
        interrupted_thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with process._waiters_lock:
                if "request-a-infer-start" in process._waiters:
                    break
            time.sleep(0.005)
        assert (
            process.interrupt_command(
                "request-a-infer-start",
                code="active_transport_failure",
            )
            is True
        )
        interrupted_thread.join(timeout=1.0)
        assert not interrupted_thread.is_alive()
        assert interrupted == ["active_transport_failure"]
        healthy.append(
            process.command(
                "status",
                {"value": "healthy", "delay": 0.2},
                command_id="unrelated-status",
                terminate_on_timeout=False,
            )
        )
        assert healthy == [{"value": "healthy"}]
        assert process.running is True
    finally:
        process.close()


def test_successful_responses_cannot_evict_delayed_interrupted_command(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release-late-response"
    script = tmp_path / "delayed-response-node.py"
    script.write_text(
        """
import json
from pathlib import Path
import sys
import threading
import time

write_lock = threading.Lock()

def respond(command):
    release = command["payload"].get("release")
    if release is not None:
        marker = Path(release)
        while not marker.exists():
            time.sleep(0.001)
    response = {
        "protocol": command["protocol"],
        "command_id": command["command_id"],
        "node_id": "node-a",
        "ok": True,
        "route_ready": False,
        "result": {"value": command["payload"].get("value")},
    }
    with write_lock:
        print(json.dumps(response, sort_keys=True, separators=(",", ":")), flush=True)

for raw in sys.stdin:
    command = json.loads(raw)
    threading.Thread(target=respond, args=(command,), daemon=True).start()
""",
        encoding="utf-8",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    process = PhysicalNodeProcess(
        command=(sys.executable, str(script)),
        node_id="node-a",
        run_id="run-1",
        deployment_id="deployment-1",
        response_timeout_seconds=1.0,
    )
    interrupted: list[str] = []

    def wait_for_delayed_response() -> None:
        try:
            process.command(
                "configure",
                {"release": str(release)},
                command_id="delayed-command",
                terminate_on_timeout=False,
            )
        except NodeProcessError as exc:
            interrupted.append(exc.code)

    waiter = threading.Thread(target=wait_for_delayed_response)
    try:
        waiter.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with process._waiters_lock:
                if "delayed-command" in process._waiters:
                    break
            time.sleep(0.001)
        assert (
            process.interrupt_command("delayed-command", code="request_cancelled")
            is True
        )
        waiter.join(timeout=1.0)
        assert interrupted == ["request_cancelled"]

        for index in range(1_100):
            assert process.command(
                "status",
                {"value": index},
                command_id=f"healthy-{index}",
                terminate_on_timeout=False,
            ) == {"value": index}

        release.touch()
        time.sleep(0.1)
        assert process.command(
            "status",
            {"value": "still-healthy"},
            command_id="after-late-response",
            terminate_on_timeout=False,
        ) == {"value": "still-healthy"}
        assert process.running is True
    finally:
        process.close()


def test_interrupted_waiter_is_retired_before_its_late_response() -> None:
    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._retired_command_ids = OrderedDict()
    waiter: Queue[object] = Queue(maxsize=1)
    process._waiters["interrupted-command"] = waiter

    assert (
        process.interrupt_command(
            "interrupted-command",
            code="request_cancelled",
        )
        is True
    )
    with process._waiters_lock:
        assert "interrupted-command" not in process._waiters
        assert "interrupted-command" in process._retired_command_ids

    late_response = json.dumps(
        {
            "protocol": PROTOCOL,
            "command_id": "interrupted-command",
            "node_id": "node-a",
            "ok": True,
            "route_ready": False,
            "result": {"value": "late"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert process._deliver_response(late_response) is True
    with process._waiters_lock:
        assert "interrupted-command" not in process._retired_command_ids


def test_acknowledged_interruptions_cannot_evict_outstanding_tombstone() -> None:
    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._retired_command_ids = OrderedDict()

    def response(command_id: str) -> bytes:
        return json.dumps(
            {
                "protocol": PROTOCOL,
                "command_id": command_id,
                "node_id": "node-a",
                "ok": True,
                "route_ready": False,
                "result": {"value": "late"},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    process._waiters["oldest-outstanding"] = Queue(maxsize=1)
    assert process.interrupt_command("oldest-outstanding", code="request_cancelled")

    # Exercise more than the defensive ledger capacity. Each exact late
    # response acknowledges its own retirement and therefore cannot displace
    # the one command whose response is still genuinely outstanding.
    for index in range(1_100):
        command_id = f"acknowledged-{index}"
        process._waiters[command_id] = Queue(maxsize=1)
        assert process.interrupt_command(command_id, code="request_cancelled")
        assert process._deliver_response(response(command_id)) is True

    with process._waiters_lock:
        assert tuple(process._retired_command_ids) == ("oldest-outstanding",)
    assert process._deliver_response(response("oldest-outstanding")) is True
    with process._waiters_lock:
        assert process._retired_command_ids == OrderedDict()


def test_reader_protocol_failure_is_persisted_for_future_commands() -> None:
    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._pending_interruptions = OrderedDict()
    process._retired_command_ids = OrderedDict()
    process._reader_failure_code = None
    process._write_condition = threading.Condition()
    process._write_active = False
    process._priority_writers_waiting = 0
    process._state_lock = threading.RLock()
    process._closed = False

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            raise AssertionError("fatal reader must reject before another write")

        def flush(self) -> None:
            raise AssertionError("fatal reader must reject before another flush")

    class FakeProcess:
        stdin = FakeStdin()

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()
    process._fail_waiters(process_module._ReaderError("response_command_mismatch"))

    assert process.reader_failure_code == "response_command_mismatch"
    with pytest.raises(NodeProcessError) as caught:
        process._exchange_frame(
            command_id="after-reader-failure",
            frame=b"{}",
            timeout=0.01,
            terminate_on_timeout=False,
        )
    assert caught.value.code == "response_command_mismatch"


def test_write_ownership_wait_consumes_command_timeout_budget() -> None:
    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._pending_interruptions = OrderedDict()
    process._retired_command_ids = OrderedDict()
    process._reader_failure_code = None
    process._write_condition = threading.Condition()
    process._write_active = True
    process._priority_writers_waiting = 0
    process._state_lock = threading.RLock()
    process._closed = False

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            raise AssertionError("expired writer must not reach stdin")

        def flush(self) -> None:
            raise AssertionError("expired writer must not flush stdin")

    class FakeProcess:
        stdin = FakeStdin()

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()
    errors: list[str] = []

    def exchange() -> None:
        try:
            process._exchange_frame(
                command_id="write-owner-starved",
                frame=b"{}",
                timeout=0.02,
                terminate_on_timeout=False,
                priority_write=True,
            )
        except NodeProcessError as error:
            errors.append(error.code)

    caller = threading.Thread(target=exchange, daemon=True)
    started = time.monotonic()
    caller.start()
    caller.join(timeout=0.25)

    assert not caller.is_alive()
    assert time.monotonic() - started < 0.25
    assert errors == ["node_response_timeout"]
    with process._waiters_lock:
        assert "write-owner-starved" not in process._waiters


def test_full_stdin_pipe_write_consumes_command_timeout_budget() -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    fill = b"x" * 4096
    while True:
        try:
            os.write(write_fd, fill)
        except BlockingIOError:
            break
    os.set_blocking(write_fd, True)

    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._pending_interruptions = OrderedDict()
    process._retired_command_ids = OrderedDict()
    process._reader_failure_code = None
    process._write_condition = threading.Condition()
    process._write_active = False
    process._priority_writers_waiting = 0
    process._state_lock = threading.RLock()
    process._closed = False
    process._abort = lambda: None

    class FakeProcess:
        stdin = os.fdopen(write_fd, "wb", buffering=0)

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()
    errors: list[str] = []

    def exchange() -> None:
        try:
            process._exchange_frame(
                command_id="full-stdin-pipe",
                frame=b"{}",
                timeout=0.02,
                terminate_on_timeout=False,
                priority_write=True,
            )
        except NodeProcessError as error:
            errors.append(error.code)

    caller = threading.Thread(target=exchange, daemon=True)
    started = time.monotonic()
    caller.start()
    try:
        caller.join(timeout=0.25)
        assert not caller.is_alive()
        assert time.monotonic() - started < 0.25
        assert errors == ["node_response_timeout"]
        with process._waiters_lock:
            assert "full-stdin-pipe" not in process._waiters
    finally:
        os.close(read_fd)
        caller.join(timeout=1.0)
        process._process.stdin.close()


def test_real_process_survives_1100_interrupt_late_response_lifecycles(
    tmp_path: Path,
) -> None:
    script = tmp_path / "sustained-late-response-node.py"
    script.write_text(
        """
import json
import sys
import threading
import time

write_lock = threading.Lock()

def respond(command):
    time.sleep(0.003)
    response = {
        "protocol": command["protocol"],
        "command_id": command["command_id"],
        "node_id": "node-a",
        "ok": True,
        "route_ready": False,
        "result": {"value": command["payload"].get("value")},
    }
    with write_lock:
        print(json.dumps(response, sort_keys=True, separators=(",", ":")), flush=True)

for raw in sys.stdin:
    command = json.loads(raw)
    threading.Thread(target=respond, args=(command,), daemon=True).start()
""",
        encoding="utf-8",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    process = PhysicalNodeProcess(
        command=(sys.executable, str(script)),
        node_id="node-a",
        run_id="run-1",
        deployment_id="deployment-1",
        response_timeout_seconds=1.0,
    )
    try:
        for index in range(1_100):
            command_id = f"interrupted-{index}"
            result: list[str] = []

            def exchange() -> None:
                try:
                    process.command(
                        "configure",
                        {"value": index},
                        command_id=command_id,
                        terminate_on_timeout=False,
                    )
                except NodeProcessError as error:
                    result.append(error.code)

            waiter = threading.Thread(target=exchange)
            waiter.start()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with process._waiters_lock:
                    if command_id in process._waiters:
                        break
                time.sleep(0.0001)
            assert process.interrupt_command(
                command_id,
                code="request_cancelled",
            )
            waiter.join(timeout=1.0)
            assert not waiter.is_alive()
            assert result == ["request_cancelled"]
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with process._waiters_lock:
                    if command_id not in process._retired_command_ids:
                        break
                time.sleep(0.0001)
            with process._waiters_lock:
                assert command_id not in process._retired_command_ids

        assert process.command(
            "status",
            {"value": "still-healthy"},
            command_id="after-1100-late-responses",
            terminate_on_timeout=False,
        ) == {"value": "still-healthy"}
        assert process.reader_failure_code is None
        assert process.running is True
    finally:
        process.close()


def test_response_published_at_timeout_does_not_leave_unanswerable_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._retired_command_ids = OrderedDict()
    process._write_condition = threading.Condition()
    process._write_active = False
    process._priority_writers_waiting = 0
    process._state_lock = threading.Lock()
    process._closed = False
    command_id = "response-at-timeout"
    response = json.dumps(
        {"command_id": command_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            return None

        def flush(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()
    process._control_frame_writer = lambda stream, payload, _deadline: (
        stream.write(payload),
        stream.flush(),
    )

    class ResponseAtTimeoutQueue(Queue[object]):
        def get(self, block: bool = True, timeout: float | None = None) -> object:
            if not block:
                return super().get(block=False)
            # Reproduce Queue.get crossing its deadline immediately before the
            # response reader publishes the exact bytes into the handoff.
            assert process._deliver_response(response) is True
            raise Empty

    monkeypatch.setattr(process_module, "Queue", ResponseAtTimeoutQueue)
    with pytest.raises(NodeProcessError) as caught:
        process._exchange_frame(
            command_id=command_id,
            frame=b"{}",
            timeout=0.01,
            terminate_on_timeout=False,
        )

    assert caught.value.code == "node_response_timeout"
    assert process._retired_command_ids == OrderedDict()


def test_late_response_acknowledged_before_interrupt_consumer_stays_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._retired_command_ids = OrderedDict()
    process._write_condition = threading.Condition()
    process._write_active = False
    process._priority_writers_waiting = 0
    process._state_lock = threading.Lock()
    process._closed = False
    command_id = "interrupt-acknowledged-before-consumer"
    response = json.dumps(
        {"command_id": command_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    class FakeStdin:
        def write(self, _payload: bytes) -> None:
            return None

        def flush(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()
    process._control_frame_writer = lambda stream, payload, _deadline: (
        stream.write(payload),
        stream.flush(),
    )

    class ResponseBeforeInterruptConsumerQueue(Queue[object]):
        def get(self, block: bool = True, timeout: float | None = None) -> object:
            if block:
                assert process.interrupt_command(command_id, code="request_cancelled")
                assert command_id in process._retired_command_ids
                assert process._deliver_response(response) is True
                assert command_id not in process._retired_command_ids
            return super().get(block=block, timeout=timeout)

    monkeypatch.setattr(process_module, "Queue", ResponseBeforeInterruptConsumerQueue)
    with pytest.raises(NodeProcessError) as caught:
        process._exchange_frame(
            command_id=command_id,
            frame=b"{}",
            timeout=0.01,
            terminate_on_timeout=False,
        )

    assert caught.value.code == "request_cancelled"
    assert process._retired_command_ids == OrderedDict()


def test_cleanup_control_frame_precedes_queued_normal_frame() -> None:
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    write_order: list[str] = []

    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._retired_command_ids = OrderedDict()
    process._write_condition = threading.Condition()
    process._write_active = False
    process._priority_writers_waiting = 0
    process._state_lock = threading.Lock()
    process._closed = False
    process._validate_response = lambda _raw, command_id: command_id

    class FakeStdin:
        def write(self, payload: bytes) -> None:
            command_id = json.loads(payload)["command_id"]
            write_order.append(command_id)
            if command_id == "active-normal":
                first_write_entered.set()
                assert release_first_write.wait(timeout=1.0)
            response = json.dumps(
                {"command_id": command_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            assert process._deliver_response(response) is True

        def flush(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()
    process._control_frame_writer = lambda stream, payload, _deadline: (
        stream.write(payload),
        stream.flush(),
    )
    results: list[str] = []

    def exchange(command_id: str, *, priority: bool) -> None:
        results.append(
            process._exchange_frame(
                command_id=command_id,
                frame=json.dumps({"command_id": command_id}).encode("utf-8"),
                timeout=1.0,
                terminate_on_timeout=False,
                priority_write=priority,
            )
        )

    active = threading.Thread(
        target=exchange,
        kwargs={"command_id": "active-normal", "priority": False},
    )
    queued_normal = threading.Thread(
        target=exchange,
        kwargs={"command_id": "queued-normal", "priority": False},
    )
    cleanup = threading.Thread(
        target=exchange,
        kwargs={"command_id": "cleanup", "priority": True},
    )
    active.start()
    assert first_write_entered.wait(timeout=1.0)
    queued_normal.start()
    while True:
        with process._waiters_lock:
            if "queued-normal" in process._waiters:
                break
    cleanup.start()
    while True:
        with process._write_condition:
            if process._priority_writers_waiting == 1:
                break
    release_first_write.set()
    for thread in (active, queued_normal, cleanup):
        thread.join(timeout=1.0)
        assert not thread.is_alive()

    assert write_order == ["active-normal", "cleanup", "queued-normal"]
    assert sorted(results) == ["active-normal", "cleanup", "queued-normal"]


def test_cleanup_overtakes_but_does_not_publish_interrupted_queued_frame() -> None:
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    write_order: list[str] = []

    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._retired_command_ids = OrderedDict()
    process._write_condition = threading.Condition()
    process._write_active = False
    process._priority_writers_waiting = 0
    process._state_lock = threading.Lock()
    process._closed = False
    process._validate_response = lambda _raw, command_id: command_id

    class FakeStdin:
        def write(self, payload: bytes) -> None:
            command_id = json.loads(payload)["command_id"]
            write_order.append(command_id)
            if command_id == "active-other-request":
                first_write_entered.set()
                assert release_first_write.wait(timeout=1.0)
            response = json.dumps(
                {"command_id": command_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            assert process._deliver_response(response) is True

        def flush(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()
    process._control_frame_writer = lambda stream, payload, _deadline: (
        stream.write(payload),
        stream.flush(),
    )
    results: dict[str, str] = {}

    def exchange(command_id: str, *, priority: bool) -> None:
        try:
            results[command_id] = process._exchange_frame(
                command_id=command_id,
                frame=json.dumps({"command_id": command_id}).encode("utf-8"),
                timeout=1.0,
                terminate_on_timeout=False,
                priority_write=priority,
            )
        except NodeProcessError as error:
            results[command_id] = error.code

    active = threading.Thread(
        target=exchange,
        kwargs={"command_id": "active-other-request", "priority": False},
    )
    stale = threading.Thread(
        target=exchange,
        kwargs={"command_id": "queued-stale-request", "priority": False},
    )
    cleanup = threading.Thread(
        target=exchange,
        kwargs={"command_id": "cleanup", "priority": True},
    )
    active.start()
    assert first_write_entered.wait(timeout=1.0)
    stale.start()
    while True:
        with process._waiters_lock:
            if "queued-stale-request" in process._waiters:
                break
    assert process.interrupt_command("queued-stale-request", code="request_cancelled")
    cleanup.start()
    while True:
        with process._write_condition:
            if process._priority_writers_waiting == 1:
                break
    release_first_write.set()
    for thread in (active, stale, cleanup):
        thread.join(timeout=1.0)
        assert not thread.is_alive()

    assert write_order == ["active-other-request", "cleanup"]
    assert results == {
        "active-other-request": "active-other-request",
        "queued-stale-request": "request_cancelled",
        "cleanup": "cleanup",
    }
    assert process._retired_command_ids == OrderedDict()


def test_response_publication_is_atomic_with_waiter_interruption() -> None:
    response_put_entered = threading.Event()
    release_response_put = threading.Event()

    class PausingQueue(Queue[object]):
        def put_nowait(self, item: object) -> None:
            if isinstance(item, bytes):
                response_put_entered.set()
                assert release_response_put.wait(timeout=1.0)
            super().put_nowait(item)

    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._retired_command_ids = OrderedDict()
    waiter = PausingQueue(maxsize=1)
    process._waiters = {"racing-command": waiter}
    response = json.dumps(
        {
            "protocol": PROTOCOL,
            "command_id": "racing-command",
            "node_id": "node-a",
            "ok": True,
            "route_ready": False,
            "result": {"value": "response-won"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    delivery_result: list[bool] = []
    interruption_result: list[bool] = []
    interruption_started = threading.Event()

    delivery = threading.Thread(
        target=lambda: delivery_result.append(process._deliver_response(response))
    )

    def interrupt() -> None:
        interruption_started.set()
        interruption_result.append(
            process.interrupt_command("racing-command", code="request_cancelled")
        )

    interruption = threading.Thread(target=interrupt)
    delivery.start()
    assert response_put_entered.wait(timeout=1.0)
    interruption.start()
    assert interruption_started.wait(timeout=1.0)
    # The interrupter cannot publish into or retire the waiter between the
    # response reader's lookup and its queue publication.
    time.sleep(0.01)
    assert interruption_result == []
    release_response_put.set()
    delivery.join(timeout=1.0)
    interruption.join(timeout=1.0)

    assert delivery_result == [True]
    assert interruption_result == [False]
    assert waiter.get_nowait() == response
    assert "racing-command" not in process._retired_command_ids


def test_interrupt_before_waiter_registration_prevents_command_write() -> None:
    process = PhysicalNodeProcess.__new__(PhysicalNodeProcess)
    process._waiters_lock = threading.RLock()
    process._waiters = {}
    process._pending_interruptions = OrderedDict()
    process._retired_command_ids = OrderedDict()
    process._write_condition = threading.Condition()
    process._write_active = False
    process._priority_writers_waiting = 0
    writes: list[bytes] = []

    class FakeStdin:
        def write(self, payload: bytes) -> None:
            writes.append(payload)

        def flush(self) -> None:
            return None

    class FakeProcess:
        stdin = FakeStdin()

        @staticmethod
        def poll() -> None:
            return None

    process._process = FakeProcess()

    assert process.interrupt_command(
        "not-yet-registered",
        code="request_cancelled",
    )
    with pytest.raises(NodeProcessError) as caught:
        process._exchange_frame(
            command_id="not-yet-registered",
            frame=b"{}",
            timeout=1.0,
            terminate_on_timeout=False,
        )

    assert caught.value.code == "request_cancelled"
    assert writes == []
    assert process._pending_interruptions == OrderedDict()
    assert process._retired_command_ids == OrderedDict()


def test_outgoing_frame_limit_is_checked_before_write(tmp_path: Path) -> None:
    process = _process(tmp_path)
    try:
        with pytest.raises(NodeProcessError) as caught:
            process.command("configure", {"value": "x" * MAX_CONTROL_FRAME_BYTES})
        assert caught.value.code == "node_command_too_large"
        assert process.running is True
    finally:
        process.close()


def test_build_command_uses_absolute_paths_and_no_secret_argv(tmp_path: Path) -> None:
    service = tmp_path / "physical_inference_node.py"
    sidecar = tmp_path / "mycelium-iroh-sidecar"
    service.write_text("", encoding="utf-8")
    sidecar.write_bytes(b"binary")
    artifact_root = tmp_path / "artifacts"
    socket_root = tmp_path / "sockets"
    artifact_root.mkdir()
    socket_root.mkdir()

    command = build_physical_node_command(
        python_executable=Path(sys.executable),
        service_script=service,
        run_id="run-1",
        deployment_id="deployment-1",
        node_id="node-a",
        artifact_root=artifact_root,
        socket_root=socket_root,
        sidecar_binary=sidecar,
        sidecar_local_only=False,
        command_timeout_seconds=31.0,
    )

    assert command[0] == str(Path(sys.executable).resolve())
    assert command[1] == str(service.resolve())
    assert "--sidecar-local-only" not in command
    assert "secret" not in " ".join(command).lower()
    assert command[-2:] == ("--command-timeout", "31.0")


def test_build_command_local_only_flag_is_explicit(tmp_path: Path) -> None:
    paths = [
        tmp_path / name
        for name in ("python", "service", "artifacts", "sockets", "sidecar")
    ]
    for path in paths:
        if path.name in {"artifacts", "sockets"}:
            path.mkdir()
        else:
            path.write_bytes(b"x")
    command = build_physical_node_command(
        python_executable=paths[0],
        service_script=paths[1],
        run_id="run-1",
        deployment_id="deployment-1",
        node_id="node-a",
        artifact_root=paths[2],
        socket_root=paths[3],
        sidecar_binary=paths[4],
        sidecar_local_only=True,
    )
    assert command[-1] == "--sidecar-local-only"


def test_real_physical_node_hello_and_stop(
    tmp_path: Path,
    node_agent_sidecar_binary: Path,  # noqa: F811
) -> None:
    root = Path(__file__).resolve().parents[2]
    artifact_root = tmp_path / "artifacts"
    socket_root = tmp_path / "sockets"
    artifact_root.mkdir()
    socket_root.mkdir()
    command = build_physical_node_command(
        python_executable=Path(sys.executable),
        service_script=root / "physical_inference_node.py",
        run_id="run-real-1",
        deployment_id="deployment-real-1",
        node_id="node-a",
        artifact_root=artifact_root,
        socket_root=socket_root,
        sidecar_binary=node_agent_sidecar_binary,
        sidecar_local_only=True,
    )
    with PhysicalNodeProcess(
        command=command,
        node_id="node-a",
        run_id="run-real-1",
        deployment_id="deployment-real-1",
    ) as process:
        hello = process.command("hello")
        assert hello["protocol"] == PROTOCOL
        assert hello["run_id"] == "run-real-1"
        assert hello["deployment_id"] == "deployment-real-1"
        assert hello["node_id"] == "node-a"
        assert hello["state"] == "NEW"
        assert hello["route_ready"] is False
