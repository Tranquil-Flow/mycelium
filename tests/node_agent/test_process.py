from __future__ import annotations

from pathlib import Path
import stat
import sys
import textwrap
import threading
import time

import pytest

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
                sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\\n")
                sys.stdout.flush()
                if command["command"] == "stop":
                    raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return script


def _process(tmp_path: Path, behavior: str = "ok", *, timeout: float = 1.0) -> PhysicalNodeProcess:
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
    (("wrong_id", "response_command_mismatch"), ("route_ready", "invalid_node_response")),
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


def test_correlated_waiters_allow_out_of_order_concurrent_responses(tmp_path: Path) -> None:
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
        assert process.interrupt_command(
            "request-a-infer-start",
            code="active_transport_failure",
        ) is True
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
    paths = [tmp_path / name for name in ("python", "service", "artifacts", "sockets", "sidecar")]
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
    tmp_path: Path, node_agent_sidecar_binary: Path  # noqa: F811
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
