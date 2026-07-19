from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from mycelium_interactive.runtime import InteractiveRuntime, InteractiveRuntimeError
from mycelium_interactive.swarm import matrix_digest
from mycelium_mobile.pixel_stage import PixelStage


def _worker(runtime: InteractiveRuntime, stop: threading.Event, errors: list[BaseException]) -> threading.Thread:
    invite = runtime.swarm.create_invite(public_origin="http://127.0.0.1:8787")
    grant = runtime.swarm.exchange_invite(invite.token)
    stage = PixelStage.from_document(grant.stage_pack)

    def loop() -> None:
        try:
            while not stop.is_set():
                work = runtime.swarm.poll_work(
                    peer_id=grant.peer_id,
                    session_token=grant.session_token,
                    timeout_seconds=0.1,
                )
                if work is None:
                    continue
                output = stage.execute(
                    request_id=work["request_id"],
                    assignment_id=work["assignment_id"],
                    stage_id=work["stage_id"],
                    hidden=work["hidden"],
                )
                runtime.swarm.submit_result(
                    peer_id=grant.peer_id,
                    session_token=grant.session_token,
                    document={
                        "protocol": "mycelium.browser_stage_result.v1",
                        "job_id": work["job_id"],
                        "request_id": work["request_id"],
                        "assignment_id": work["assignment_id"],
                        "stage_id": work["stage_id"],
                        "pack_digest": work["pack_digest"],
                        "input_digest": work["input_digest"],
                        "output": output,
                        "output_digest": matrix_digest(output),
                        "route_ready": False,
                    },
                )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def test_runtime_executes_prompt_through_joined_browser_stage(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    stop = threading.Event()
    errors: list[BaseException] = []
    thread = _worker(runtime, stop, errors)
    try:
        record = runtime.infer(prompt="moonlit swarm", max_new_tokens=2, timeout_seconds=5)
        assert record["protocol"] == "mycelium.interactive_inference_record.v1"
        assert record["route_ready"] is False
        assert record["local_evidence_only"] is True
        assert len(record["generated_tokens"]) == 2
        assert len(record["generated_labels"]) == 2
        assert record["max_intermediate_error"] < 1e-6
        assert record["max_logit_error"] < 2e-6
        assert all(item["route_ready"] is False for item in record["token_records"])
        assert runtime.get_record(record["request_id"]) == record
        status = runtime.status()
        assert status["route_ready"] is False
        assert status["local_evidence_only"] is True
        assert status["completed_request_count"] == 1
        serialized_status = str(status).lower()
        assert "moonlit swarm" not in serialized_status
        assert "hidden" not in serialized_status
        assert "session_token" not in serialized_status
    finally:
        stop.set()
        thread.join(timeout=1)
        runtime.close()
    assert errors == []


def test_runtime_fails_without_joined_worker(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    try:
        with pytest.raises(InteractiveRuntimeError, match="dispatch_timeout"):
            runtime.infer(prompt="alone", max_new_tokens=1, timeout_seconds=0.05)
    finally:
        runtime.close()


def test_runtime_can_restart_with_same_explicit_state_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    first = InteractiveRuntime(root=root, run_id="first-run")
    first_digest = first.stage_pack["pack_digest"]
    first.close()

    second = InteractiveRuntime(root=root, run_id="second-run")
    try:
        assert second.stage_pack["pack_digest"] != first_digest
        assert (root / "runs" / "first-run" / "deployment").is_dir()
        assert (root / "runs" / "second-run" / "deployment").is_dir()
        assert second.status()["route_ready"] is False
    finally:
        second.close()


def test_status_captures_swarm_and_request_state_under_one_runtime_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    original_status = runtime.swarm.status
    original_lock = runtime._lock  # noqa: SLF001 - consistency regression oracle
    lock_entered = False

    class CheckedLock:
        def __enter__(self) -> None:
            nonlocal lock_entered
            original_lock.acquire()
            lock_entered = True

        def __exit__(self, *_args: object) -> None:
            nonlocal lock_entered
            lock_entered = False
            original_lock.release()

    def checked_status() -> dict[str, Any]:
        assert lock_entered is True
        return original_status()

    monkeypatch.setattr(runtime, "_lock", CheckedLock())
    monkeypatch.setattr(runtime.swarm, "status", checked_status)
    try:
        status = runtime.status()
        assert status["peer_count"] == 0
        assert status["completed_request_count"] == 0
    finally:
        runtime.close()
