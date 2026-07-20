from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from mycelium_interactive.runtime import (
    InferenceRecord,
    InteractiveRuntime,
    InteractiveRuntimeError,
)
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


def test_runtime_requires_each_target_peer_session_despite_lifetime_imbalance(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    stop = threading.Event()
    errors: list[BaseException] = []
    threads = [_worker(runtime, stop, errors), _worker(runtime, stop, errors)]
    try:
        deadline = time.monotonic() + 2.0
        while runtime.status()["ready_peer_count"] != 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        with runtime.swarm._condition:  # noqa: SLF001 - scheduler regression oracle
            peers = list(runtime.swarm._peers.values())  # noqa: SLF001
            peers[0].completed_jobs = 0
            peers[1].completed_jobs = 10
        record = runtime.infer(
            prompt="moonlit swarm",
            max_new_tokens=2,
            required_distinct_peers=2,
            timeout_seconds=5,
        )
        assert record["required_distinct_peers"] == 2
        assert record["observed_distinct_peers"] == 2
        assert len(record["peer_ids"]) == 2
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=1)
        runtime.close()
    assert errors == []


def test_runtime_freezes_exact_peer_cohort_after_target_is_reached(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    stop = threading.Event()
    errors: list[BaseException] = []
    threads = [_worker(runtime, stop, errors) for _ in range(3)]
    try:
        deadline = time.monotonic() + 2.0
        while runtime.status()["ready_peer_count"] != 3:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        record = runtime.infer(
            prompt="moonlit swarm",
            max_new_tokens=3,
            required_distinct_peers=2,
            timeout_seconds=5,
        )
        assert record["required_distinct_peers"] == 2
        assert record["observed_distinct_peers"] == 2
        assert len(record["peer_ids"]) == 2
        completed = sorted(peer["completed_jobs"] for peer in runtime.status()["peers"])
        assert completed == [0, 1, 2]
    finally:
        stop.set()
        for thread in threads:
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


def test_runtime_parent_request_cancel_reaches_active_token_stage_and_status_stays_live(
    tmp_path: Path,
) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    invite = runtime.swarm.create_invite(public_origin="http://127.0.0.1:8787")
    grant = runtime.swarm.exchange_invite(invite.token)
    inference_errors: list[str] = []

    def infer() -> None:
        try:
            runtime.infer(
                prompt="cancel moonlit swarm",
                max_new_tokens=8,
                timeout_seconds=5,
                request_id="operator-request",
            )
        except InteractiveRuntimeError as exc:
            inference_errors.append(exc.code)

    inference = threading.Thread(target=infer, daemon=True)
    inference.start()
    try:
        work = runtime.swarm.poll_work(
            peer_id=grant.peer_id,
            session_token=grant.session_token,
            timeout_seconds=2,
        )
        assert work is not None
        assert work["request_id"] == "operator-request:token-0"

        status_result: list[dict[str, Any]] = []
        status_done = threading.Event()

        def read_status() -> None:
            status_result.append(runtime.status())
            status_done.set()

        status_reader = threading.Thread(target=read_status, daemon=True)
        status_reader.start()
        assert status_done.wait(0.5), "status must remain responsive during inference"
        assert status_result[0]["active_request_count"] == 1

        assert runtime.cancel_request("operator-request") is True
        inference.join(timeout=2)
        status_reader.join(timeout=1)
        assert not inference.is_alive()
        assert inference_errors == ["request_cancelled"]
        assert runtime.get_record("operator-request") is None
        assert runtime.status()["active_request_count"] == 0
        assert runtime.cancel_request("operator-request") is False
    finally:
        runtime.cancel_request("operator-request")
        inference.join(timeout=2)
        runtime.close()


def test_completed_record_cannot_be_cancelled_during_return_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    stop = threading.Event()
    errors: list[BaseException] = []
    worker = _worker(runtime, stop, errors)
    committed = threading.Event()
    release = threading.Event()
    original = InferenceRecord.public_document

    def block_after_commit(record: InferenceRecord) -> dict[str, Any]:
        if record.request_id == "terminal-race":
            committed.set()
            assert release.wait(2)
        return original(record)

    monkeypatch.setattr(InferenceRecord, "public_document", block_after_commit)
    inference = threading.Thread(
        target=lambda: runtime.infer(
            prompt="moonlit swarm",
            request_id="terminal-race",
            timeout_seconds=5,
        ),
        daemon=True,
    )
    inference.start()
    try:
        assert committed.wait(2)
        assert runtime.cancel_request("terminal-race") is False
    finally:
        release.set()
        inference.join(timeout=2)
        stop.set()
        worker.join(timeout=1)
        runtime.close()
    assert not inference.is_alive()
    assert errors == []


def test_cancel_before_dispatch_creation_stops_stage_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    entered = threading.Event()
    release = threading.Event()
    original = runtime.swarm.dispatch
    outcome: list[str] = []

    def blocked_dispatch(**kwargs: Any) -> Any:
        entered.set()
        assert release.wait(2)
        return original(**kwargs)

    monkeypatch.setattr(runtime.swarm, "dispatch", blocked_dispatch)

    def run() -> None:
        try:
            runtime.infer(prompt="cancel", request_id="pre-dispatch", timeout_seconds=0.3)
        except InteractiveRuntimeError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        assert runtime.cancel_request("pre-dispatch") is True
    finally:
        release.set()
        thread.join(timeout=2)
        runtime.close()
    assert not thread.is_alive()
    assert outcome == ["request_cancelled"]


def test_cancellation_wins_timeout_edge_after_acceptance(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    entered = threading.Event()
    release = threading.Event()
    base = time.monotonic()
    calls = 0

    def controlled_now() -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            return base
        if calls == 2:
            entered.set()
            assert release.wait(2)
        return base + 1

    runtime.swarm._clock = controlled_now  # noqa: SLF001 - timeout race oracle
    outcome: list[str] = []
    cancel_result: list[bool] = []

    def infer() -> None:
        try:
            runtime.infer(prompt="race", request_id="timeout-edge", timeout_seconds=0.1)
        except InteractiveRuntimeError as exc:
            outcome.append(exc.code)

    infer_thread = threading.Thread(target=infer, daemon=True)
    infer_thread.start()
    assert entered.wait(2)
    cancel_thread = threading.Thread(
        target=lambda: cancel_result.append(runtime.cancel_request("timeout-edge")),
        daemon=True,
    )
    cancel_thread.start()
    deadline = time.monotonic() + 2
    while not runtime._cancel_events["timeout-edge"].is_set():  # noqa: SLF001
        assert time.monotonic() < deadline
        time.sleep(0.001)
    release.set()
    infer_thread.join(timeout=2)
    cancel_thread.join(timeout=2)
    runtime.close()
    assert cancel_result == [True]
    assert outcome == ["request_cancelled"]


def test_queued_inference_cancellation_settles_without_lock_release(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    outcome: list[str] = []
    runtime._infer_lock.acquire()  # noqa: SLF001 - deterministic queue regression oracle

    def run() -> None:
        try:
            runtime.infer(prompt="queued", request_id="queued-request")
        except InteractiveRuntimeError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    settled_while_locked = False
    try:
        deadline = time.monotonic() + 2
        while runtime.status()["active_request_count"] != 1:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert runtime.cancel_request("queued-request") is True
        thread.join(timeout=0.5)
        settled_while_locked = not thread.is_alive()
    finally:
        runtime._infer_lock.release()  # noqa: SLF001
        thread.join(timeout=2)
        runtime.close()
    assert settled_while_locked is True
    assert outcome == ["request_cancelled"]


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
