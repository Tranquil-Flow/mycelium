from __future__ import annotations

import hashlib
import http.cookiejar
import math
import random
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

import scripts.run_a5_benchmark_gate as benchmark_gate


def test_product_session_fork_isolates_data_and_control_cookie_locks() -> None:
    parent = object.__new__(benchmark_gate.ProductSession)
    parent.base_url = "http://127.0.0.1:8792"
    parent._bootstrap = {"session": {"csrf_header": "x-csrf", "csrf_token": "t"}}
    parent._qualification = {"binding": {"deployment_id": "deployment"}}
    parent._cookie_jar = http.cookiejar.CookieJar()
    parent._cookie_jar.set_cookie(
        http.cookiejar.Cookie(
            version=0,
            name="session",
            value="owner",
            port=None,
            port_specified=False,
            domain="127.0.0.1",
            domain_specified=False,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
    )
    parent._opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(parent._cookie_jar)
    )
    parent._control_opener = parent._opener_from_cookies(parent._cookie_jar)

    child = parent.fork()

    assert child._cookie_jar is not parent._cookie_jar
    assert child._opener is not parent._opener
    assert child._control_opener is not child._opener
    assert [(cookie.name, cookie.value) for cookie in child._cookie_jar] == [
        ("session", "owner")
    ]


def test_default_window_collection_budget_exceeds_stream_timeout() -> None:
    assert (
        benchmark_gate.REQUEST_CANCELLATION_AFTER_SECONDS
        < benchmark_gate.STREAM_TIMEOUT_SECONDS
    )
    assert (
        benchmark_gate.WINDOW_COLLECTION_TIMEOUT_SECONDS
        > benchmark_gate.STREAM_TIMEOUT_SECONDS
    )
    assert (
        benchmark_gate.WINDOW_WORKER_CLEANUP_TIMEOUT_SECONDS
        == benchmark_gate.STREAM_TIMEOUT_SECONDS * 2
    )
    assert (
        benchmark_gate.WINDOW_WORKER_CLEANUP_TIMEOUT_SECONDS
        < benchmark_gate.WINDOW_COLLECTION_TIMEOUT_SECONDS
    )


def test_stream_timeout_budget_can_account_for_minimum_window_terminals() -> None:
    timeout_cycles = math.ceil(
        benchmark_gate.MIN_WINDOW_REQUESTS / benchmark_gate.OFFERED_CONCURRENCY
    )
    assert (
        timeout_cycles * benchmark_gate.STREAM_TIMEOUT_SECONDS
        < benchmark_gate.WINDOW_COLLECTION_TIMEOUT_SECONDS
    )


def test_post_window_zero_resource_budget_covers_serialized_cancellation_burst() -> (
    None
):
    assert benchmark_gate.ROUTE_ZERO_RESOURCE_TIMEOUT_SECONDS > (
        benchmark_gate.OFFERED_CONCURRENCY
        * benchmark_gate.REQUEST_CLEANUP_SERIALIZATION_SECONDS
    )


class _SlowSuccessfulSession(benchmark_gate.ProductSession):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0

    def submit(
        self, *, prompt: str, maximum_new_tokens: int, **kwargs
    ) -> dict[str, object]:
        del prompt, maximum_new_tokens, kwargs
        return {"event_path": "/unused"}

    def stream_summary(self, accepted: dict[str, object]) -> tuple[dict[str, int], str]:
        del accepted
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        time.sleep(0.03)
        with self._lock:
            self.active -= 1
        return {"token": 1}, "completed"

    def cancel(self, accepted: dict[str, object]) -> None:
        del accepted

    def fork(self):
        return self


class _StreamTimeoutSession(benchmark_gate.ProductSession):
    def __init__(self) -> None:
        pass

    def submit(
        self, *, prompt: str, maximum_new_tokens: int, **kwargs
    ) -> dict[str, object]:
        del prompt, maximum_new_tokens, kwargs
        return {"event_path": "/unused"}

    def stream_summary(self, accepted: dict[str, object]) -> tuple[dict[str, int], str]:
        del accepted
        raise TimeoutError("socket_read_timeout")

    def cancel(self, accepted: dict[str, object]) -> None:
        del accepted

    def fork(self):
        return self


class _WatchdogSession(benchmark_gate.ProductSession):
    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def submit(
        self, *, prompt: str, maximum_new_tokens: int, **kwargs
    ) -> dict[str, object]:
        del prompt, maximum_new_tokens, kwargs
        return {"event_path": "/unused"}

    def stream_summary(self, accepted: dict[str, object]) -> tuple[dict[str, int], str]:
        del accepted
        assert self.cancelled.wait(timeout=1.0)
        return {}, "cancelled"

    def cancel(self, accepted: dict[str, object]) -> None:
        del accepted
        self.cancelled.set()

    def fork(self):
        return self


def test_product_session_cancel_uses_delete_on_control_channel() -> None:
    session = object.__new__(benchmark_gate.ProductSession)
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

    def request(method, path, **kwargs):
        captured.update(method=method, path=path, kwargs=kwargs)
        return _Response()

    session.request = request

    session.cancel({"cancel_path": "/v1/inference/request-1"})

    assert captured == {
        "method": "DELETE",
        "path": "/v1/inference/request-1",
        "kwargs": {"mutate": True, "timeout": 10.0, "control": True},
    }


def test_stream_reader_closes_on_authoritative_terminal_event() -> None:
    session = object.__new__(benchmark_gate.ProductSession)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

        def __iter__(self):
            yield b"event: token\n"
            yield b"event: cancelled\n"
            raise AssertionError("stream reader advanced beyond terminal")

    session.request = lambda *args, **kwargs: _Response()

    counts, terminal = session.stream_summary({"event_path": "/events"})

    assert counts == {"token": 1, "cancelled": 1}
    assert terminal == "cancelled"


def test_stream_reader_enforces_absolute_budget_across_heartbeats(
    monkeypatch,
) -> None:
    session = object.__new__(benchmark_gate.ProductSession)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

        def __iter__(self):
            yield b": heartbeat\n"
            raise AssertionError("stream reader advanced beyond absolute deadline")

    session.request = lambda *args, **kwargs: _Response()
    monkeypatch.setattr(benchmark_gate, "STREAM_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(benchmark_gate.GateError, match="http_get_stream_deadline"):
        session.stream_summary({"event_path": "/events"})


def test_worker_cleanup_joins_all_workers_against_one_shared_deadline() -> None:
    first_finished = threading.Event()
    second_finished = threading.Event()
    workers = [
        threading.Thread(target=lambda: first_finished.wait(0.03)),
        threading.Thread(target=lambda: second_finished.wait(0.04)),
    ]
    for worker in workers:
        worker.start()

    assert benchmark_gate._join_workers_until(workers, deadline=time.monotonic() + 1.0)
    assert not any(worker.is_alive() for worker in workers)


def test_harness_serializes_cancel_calls_but_overlaps_terminal_waits(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        benchmark_gate,
        "REQUEST_CLEANUP_SERIALIZATION_SECONDS",
        0.2,
    )
    driver = benchmark_gate._WindowDriver(_SlowSuccessfulSession(), random.Random(165))
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    terminal_observed = [threading.Event(), threading.Event()]
    cancel_started_at: list[float] = []

    class _CancellationSession:
        def __init__(self, index: int) -> None:
            self.index = index

        def cancel(self, accepted: dict[str, object]) -> None:
            nonlocal active, maximum_active
            del accepted
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                cancel_started_at.append(time.monotonic())
            time.sleep(0.02)
            with lock:
                active -= 1

    workers = [
        threading.Thread(
            target=driver._cancel_and_wait,
            args=(_CancellationSession(index), {}, terminal_observed[index]),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=1.0)

    assert not any(worker.is_alive() for worker in workers)
    assert maximum_active == 1
    assert len(cancel_started_at) == 2
    assert cancel_started_at[1] - cancel_started_at[0] < 0.1


def test_product_session_cancel_fails_closed_on_control_error() -> None:
    session = object.__new__(benchmark_gate.ProductSession)

    def request(*_args, **_kwargs):
        raise benchmark_gate.GateError("http_delete_unavailable")

    session.request = request

    with pytest.raises(benchmark_gate.GateError, match="http_delete_unavailable"):
        session.cancel({"cancel_path": "/v1/inference/request-1"})


def test_request_shapes_are_seeded_and_uniform_within_bucket() -> None:
    left = benchmark_gate._WindowDriver(_SlowSuccessfulSession(), random.Random(165))
    right = benchmark_gate._WindowDriver(_SlowSuccessfulSession(), random.Random(165))

    left_shapes = [left._sample_request(index) for index in range(1, 101)]
    right_shapes = [right._sample_request(index) for index in range(1, 101)]

    assert left_shapes == right_shapes
    for shape in left_shapes:
        bucket = next(
            item for item in benchmark_gate.BUCKETS if item["name"] == shape.bucket_name
        )
        assert (
            bucket["prompt_token_min"]
            <= shape.target_prompt_tokens
            <= bucket["prompt_token_max"]
        )
        assert (
            bucket["output_token_min"]
            <= shape.maximum_new_tokens
            <= bucket["output_token_max"]
        )
    for bucket_name in benchmark_gate.BUCKET_NAMES:
        sampled_outputs = {
            shape.maximum_new_tokens
            for shape in left_shapes
            if shape.bucket_name == bucket_name
        }
        assert len(sampled_outputs) > 1


def test_raw_stream_timeout_is_counted_and_releases_in_flight() -> None:
    driver = benchmark_gate._WindowDriver(_StreamTimeoutSession(), random.Random(165))
    driver._in_flight = 1
    driver._measured_start = time.monotonic() - 1

    driver._submit_one(1)

    assert driver._measured_outcomes["timeout"] == 1
    assert driver._measured_terminals == 1
    assert driver._in_flight == 0


def test_client_stream_timeout_does_not_suppress_queued_server_cancel(
    monkeypatch,
) -> None:
    class Session(_StreamTimeoutSession):
        def __init__(self) -> None:
            self.cancelled = threading.Event()

        def cancel(self, accepted: dict[str, object]) -> None:
            del accepted
            self.cancelled.set()

    monkeypatch.setattr(benchmark_gate, "REQUEST_CANCELLATION_AFTER_SECONDS", 0.0)
    session = Session()
    driver = benchmark_gate._WindowDriver(session, random.Random(165))
    driver._in_flight = 1
    driver._measured_start = time.monotonic() - 1
    driver._cancellation_lock.acquire()
    worker = threading.Thread(target=driver._submit_one, args=(1,))
    try:
        worker.start()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert not session.cancelled.is_set()
    finally:
        driver._cancellation_lock.release()

    assert session.cancelled.wait(timeout=1.0)


def test_request_watchdog_cancels_before_socket_timeout(monkeypatch) -> None:
    monkeypatch.setattr(benchmark_gate, "REQUEST_CANCELLATION_AFTER_SECONDS", 0.01)
    session = _WatchdogSession()
    driver = benchmark_gate._WindowDriver(session, random.Random(165))
    driver._in_flight = 1
    driver._measured_start = time.monotonic() - 1

    driver._submit_one(1)

    assert session.cancelled.is_set()
    assert driver._measured_outcomes["cancelled"] == 1
    assert driver._in_flight == 0


def test_window_resumes_arrivals_after_concurrency_saturation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(benchmark_gate, "MIN_WINDOW_REQUESTS", 3)
    monkeypatch.setattr(benchmark_gate, "WARMUP_SECONDS", 0)
    monkeypatch.setattr(benchmark_gate, "OFFERED_CONCURRENCY", 1)
    monkeypatch.setattr(
        benchmark_gate._WindowDriver,
        "_next_arrival_delay",
        lambda self: 0.001,
    )
    session = _SlowSuccessfulSession()

    result = benchmark_gate._WindowDriver(session, random.Random(1)).run(
        duration_after_measure=0.5
    )

    assert result["request_count"] >= 3
    assert session.maximum_active == 1


def test_each_benchmark_window_opens_fresh_browser_ownership(
    monkeypatch,
) -> None:
    sessions: list[object] = []
    drivers: list[object] = []

    class _Session:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url
            sessions.append(self)

    class _Driver:
        def __init__(self, session: object, rng: random.Random) -> None:
            self.session = session
            self.rng = rng
            drivers.append(self)

        def run(self) -> dict[str, object]:
            return {"request_count": 60}

    monkeypatch.setattr(benchmark_gate, "ProductSession", _Session)
    monkeypatch.setattr(benchmark_gate, "_WindowDriver", _Driver)
    rng = random.Random(165)

    first = benchmark_gate._run_isolated_window("http://route.invalid", rng)
    second = benchmark_gate._run_isolated_window("http://route.invalid", rng)

    assert first == {"request_count": 60}
    assert second == {"request_count": 60}
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert [driver.session for driver in drivers] == sessions
    assert all(driver.rng is rng for driver in drivers)


def test_main_seals_private_source_bound_failure_observation(
    monkeypatch,
    tmp_path,
) -> None:
    manifest = tmp_path / "source-manifest.v1.json"
    manifest.write_bytes(b'{"protocol":"test.source_manifest.v1"}\n')
    source_digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    output = tmp_path / "a5-benchmark.v1.json"
    failure = tmp_path / "a5-benchmark-failure.v1.json"

    monkeypatch.setattr(
        benchmark_gate,
        "run_benchmark",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            benchmark_gate.GateError("route_not_alive")
        ),
    )
    monkeypatch.setattr(
        benchmark_gate,
        "public_json",
        lambda base_url, path: (
            {
                "protocol": "mycelium.live_route_status.v1",
                "route_alive": False,
                "route_identity_digest": "sha256:" + "b" * 64,
                "deployment_id": "deployment-a",
                "counters": {
                    "frames_sent": 10,
                    "frames_received": 9,
                    "applied_operation_count": 8,
                    "fatal": "node_process_exited",
                },
                "replica_loss_placement_ids": ["placement-002"],
                "incidents": [
                    {"reason": "node_process_exited"},
                    {"reason": "node_process_exited"},
                ],
            }
            if path == "/__mycelium/live-status"
            else {
                "protocol": "mycelium.runtime_admission_status.v1",
                "requests": [
                    {"terminal_state": "completed"},
                    {"terminal_state": None},
                ],
                "placements": [
                    {"active_reservations": 2},
                    {"active_reservations": 1},
                ],
            }
        ),
    )
    monkeypatch.setattr(
        benchmark_gate.sys,
        "argv",
        [
            "run_a5_benchmark_gate.py",
            "--output",
            str(output),
            "--failure-output",
            str(failure),
            "--source-manifest",
            str(manifest),
            "--source-manifest-digest",
            source_digest,
        ],
    )

    with pytest.raises(benchmark_gate.GateError, match="route_not_alive"):
        benchmark_gate.main()

    document = benchmark_gate.json.loads(failure.read_text("utf-8"))
    assert not output.exists()
    assert failure.stat().st_mode & 0o777 == 0o600
    assert document == {
        "benchmark_artifact_created": False,
        "benchmark_protocol_digest": benchmark_gate.protocol_digest(
            benchmark_gate.PROTOCOL
        ),
        "captured_at_unix_ms": document["captured_at_unix_ms"],
        "error_type": "GateError",
        "live_observation": {
            "active_reservations": 3,
            "deployment_id": "deployment-a",
            "incident_reason_counts": {"node_process_exited": 2},
            "live_status_protocol": "mycelium.live_route_status.v1",
            "nonterminal_requests": 1,
            "replica_loss_placement_ids": ["placement-002"],
            "route_alive": False,
            "route_counters": {
                "applied_operation_count": 8,
                "fatal": "node_process_exited",
                "frames_received": 9,
                "frames_sent": 10,
            },
            "route_identity_digest": "sha256:" + "b" * 64,
            "runtime_status_protocol": "mycelium.runtime_admission_status.v1",
            "total_requests": 2,
        },
        "promotion_authorized": False,
        "protocol": "mycelium.a5_benchmark_failure.v1",
        "qualification_claim": False,
        "reason_code": "route_not_alive",
        "source_manifest_digest": source_digest,
        "workload_manifest_digest": benchmark_gate.workload_manifest_digest(),
    }


def test_sigterm_seals_source_bound_failure_before_exit(tmp_path) -> None:
    manifest = tmp_path / "source-manifest.v1.json"
    manifest.write_bytes(b'{"protocol":"test.source_manifest.v1"}\n')
    source_digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    output = tmp_path / "a5-benchmark.v1.json"
    failure = tmp_path / "a5-benchmark-failure.v1.json"
    driver = f"""
import os
import signal
import sys
import scripts.run_a5_benchmark_gate as gate

def terminate(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGTERM)
    raise AssertionError("SIGTERM handler returned")

gate.run_benchmark = terminate
gate.public_json = lambda *args, **kwargs: {{}}
sys.argv = [
    "run_a5_benchmark_gate.py",
    "--base-url", "http://127.0.0.1:1",
    "--output", {str(output)!r},
    "--failure-output", {str(failure)!r},
    "--source-manifest", {str(manifest)!r},
    "--source-manifest-digest", {source_digest!r},
]
try:
    gate.main()
except gate.ExternalTermination as error:
    raise SystemExit(error.exit_code)
"""

    result = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=benchmark_gate.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 143
    assert not output.exists()
    assert failure.stat().st_mode & 0o777 == 0o600
    document = benchmark_gate.json.loads(failure.read_text("utf-8"))
    assert document["protocol"] == "mycelium.a5_benchmark_failure.v1"
    assert document["qualification_claim"] is False
    assert document["promotion_authorized"] is False
    assert document["benchmark_artifact_created"] is False
    assert document["source_manifest_digest"] == source_digest
    assert document["reason_code"] == "external_sigterm"
    assert document["error_type"] == "ExternalTermination"
    assert document["termination_signal"] == "SIGTERM"
    assert document["termination_signal_number"] == 15


def test_sigterm_during_success_seal_writes_failure_receipt(tmp_path) -> None:
    manifest = tmp_path / "source-manifest.v1.json"
    manifest.write_bytes(b'{"protocol":"test.source_manifest.v1"}\n')
    source_digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    output = tmp_path / "a5-benchmark.v1.json"
    failure = tmp_path / "a5-benchmark-failure.v1.json"
    driver = f"""
import os
from pathlib import Path
import signal
import sys
import scripts.run_a5_benchmark_gate as gate

real_atomic_json = gate.atomic_json

def terminate_success_seal(path, document):
    if Path(path).resolve() == Path({str(output)!r}).resolve():
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler returned")
    real_atomic_json(path, document)

gate.run_benchmark = lambda *args, **kwargs: {{"protocol": "unused-success"}}
gate.public_json = lambda *args, **kwargs: {{}}
gate.atomic_json = terminate_success_seal
sys.argv = [
    "run_a5_benchmark_gate.py",
    "--base-url", "http://127.0.0.1:1",
    "--output", {str(output)!r},
    "--failure-output", {str(failure)!r},
    "--source-manifest", {str(manifest)!r},
    "--source-manifest-digest", {source_digest!r},
]
try:
    gate.main()
except gate.ExternalTermination as error:
    raise SystemExit(error.exit_code)
"""

    result = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=benchmark_gate.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 143
    assert not output.exists()
    document = benchmark_gate.json.loads(failure.read_text("utf-8"))
    assert document["reason_code"] == "external_sigterm"
    assert document["error_type"] == "ExternalTermination"
    assert document["termination_signal"] == "SIGTERM"
    assert document["termination_signal_number"] == 15


def test_main_derives_source_digest_from_manifest_bytes(monkeypatch, tmp_path) -> None:
    manifest = tmp_path / "source-manifest.v1.json"
    manifest.write_bytes(b'{"protocol":"test.source_manifest.v1"}\n')
    expected = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    output = tmp_path / "a5-benchmark.v1.json"
    captured: dict[str, str] = {}

    def run_benchmark(*args, source_manifest_digest: str, **kwargs):
        del args, kwargs
        captured["source_manifest_digest"] = source_manifest_digest
        return {"protocol": "test.benchmark.v1"}

    monkeypatch.setattr(benchmark_gate, "run_benchmark", run_benchmark)
    monkeypatch.setattr(
        benchmark_gate.sys,
        "argv",
        [
            "run_a5_benchmark_gate.py",
            "--output",
            str(output),
            "--source-manifest",
            str(manifest),
        ],
    )

    assert benchmark_gate.main() == 0
    assert captured == {"source_manifest_digest": expected}


def test_main_rejects_claimed_digest_mismatch_before_workload(
    monkeypatch, tmp_path
) -> None:
    manifest = tmp_path / "source-manifest.v1.json"
    manifest.write_bytes(b'{"protocol":"test.source_manifest.v1"}\n')
    output = tmp_path / "a5-benchmark.v1.json"
    called = False

    def run_benchmark(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("workload must not start after source mismatch")

    monkeypatch.setattr(benchmark_gate, "run_benchmark", run_benchmark)
    monkeypatch.setattr(
        benchmark_gate.sys,
        "argv",
        [
            "run_a5_benchmark_gate.py",
            "--output",
            str(output),
            "--source-manifest",
            str(manifest),
            "--source-manifest-digest",
            "sha256:" + "0" * 64,
        ],
    )

    with pytest.raises(
        benchmark_gate.GateError, match="source_manifest_digest_mismatch"
    ):
        benchmark_gate.main()
    assert called is False
    assert not output.exists()
