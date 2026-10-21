# SPDX-License-Identifier: AGPL-3.0-only
"""A14 class D: the entry node's prefill wait on a degraded relay link.

The wait used to be a fixed ``min(command_timeout x 0.8, 240 s)`` that only
watched ``transport.fatal_error``.  A chunk that stopped crossing the relay
was therefore reported - 240 s later - as the generic
``prefill_completion_timeout``, and a long prompt on a slow but healthy link
was refused by the same fixed number.

These tests run the real ``PhysicalNodeService._infer_start`` with production
policy numbers on a *virtual* clock (the node module's ``time`` is replaced),
so hundreds of link-seconds take milliseconds.  The transport and Router are
fakes that expose exactly the surface the wait reads.

Link model used throughout (unmeasured link => policy floors):
goodput 4096 B/s, p95 RTT 1 s; one chunk frame = 8 tokens x 896 x 4 B +
4096 B envelope = 32768 B.

* stall window  S = clamp(8 x 1 + 32768 / 4096 x 4, 30, 120)      = 40 s
* watchdog      W = (max_resends 2 + 1) x S + 4 x 1                = 124 s
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import physical_inference_node as node_module
from physical_inference_node import NodeCommandError, PhysicalNodeService


FRAME_BYTES = 8 * 896 * 4 + 4096
STALL_WINDOW_S = 40.0
WATCHDOG_S = 3 * STALL_WINDOW_S + 4 * 1.0
LEGACY_WAIT_S = 240.0


class _VirtualTime:
    """Drop-in for the node module's ``time``: ``sleep`` advances the clock."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def monotonic_ns(self) -> int:
        return int(self.now * 1e9)

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    def time_ns(self) -> int:
        return int(self.time() * 1e9)

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.0)


class _Router:
    def __init__(self, clock: _VirtualTime, *, completes_at: float | None) -> None:
        self.clock = clock
        self.completes_at = completes_at
        self.sink: Any = None
        self.emitted = False

    def start_distributed_prefill(self, request, sink, **_kwargs: Any) -> str:
        self.sink = sink
        return request.request_id

    def request_status(self, _request_id: str) -> str:
        if self.completes_at is not None and self.clock.now >= self.completes_at:
            if not self.emitted:
                self.emitted = True
                self.sink.emit(0, 42)
            return "DECODING"
        return "LOCKED"

    def get_request(self, _request_id: str):
        hop = SimpleNamespace(placement_id="placement-0")
        manifest = SimpleNamespace(path_id="path-0", path_attempt=0, ordered_hops=(hop,))
        return SimpleNamespace(manifest=manifest)


class _Transport:
    """Progress source: confirmed chunks at fixed virtual times."""

    fatal_error = None

    def __init__(
        self,
        clock: _VirtualTime,
        *,
        chunk_offsets: list[float],
        failure: tuple[float, str] | None = None,
    ) -> None:
        self.clock = clock
        self.started_at = clock.now
        self.chunk_times = [clock.now + offset for offset in chunk_offsets]
        self.failure = None if failure is None else (clock.now + failure[0], failure[1])
        self.watches: list[tuple[str, float]] = []
        self.released: list[str] = []

    def watch_prefill(self, request_id: str, *, deadline_monotonic: float) -> None:
        self.watches.append((request_id, deadline_monotonic))

    def release_prefill_watch(self, request_id: str) -> None:
        self.released.append(request_id)

    def prefill_progress(self, _request_id: str) -> dict[str, Any]:
        done = [moment for moment in self.chunk_times if moment <= self.clock.now]
        failure_code = None
        if self.failure is not None and self.clock.now >= self.failure[0]:
            failure_code = self.failure[1]
        return {
            "started_at": self.started_at,
            "last_progress_at": max(done, default=self.started_at),
            "progress_events": len(done),
            "bytes_confirmed": len(done) * FRAME_BYTES,
            "max_frame_bytes": FRAME_BYTES if done else 0,
            "stall_resends": 0,
            "failure_code": failure_code,
        }

    def link_estimate(self, _destination_node_id: str | None = None):
        return SimpleNamespace(
            goodput_bytes_per_second=4096.0,
            rtt_p95_seconds=1.0,
            goodput_source="unmeasured",
            rtt_source="unmeasured",
        )

    def prefill_hop_diagnostic(self) -> dict[str, Any]:
        return {
            "scoped_failure_code": None if self.failure is None else self.failure[1],
            "remote_frames_sent": 1,
            "remote_frames_received": 1,
            "router_frames_dispatched": 1,
            "duplicate_frames": 0,
        }


def _service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PhysicalNodeService:
    monkeypatch.setattr(
        node_module,
        "derive_local_run_scoped_identity",
        lambda run_id: (f"host-{run_id}", f"boot-{run_id}"),
    )
    service = PhysicalNodeService(
        run_id="run-prefill-stall",
        deployment_id="deployment-prefill-stall",
        node_id="node-0",
        artifact_root=tmp_path,
        socket_root=tmp_path / "socket",
        sidecar_binary=Path("/usr/bin/false"),
        sidecar_local_only=True,
        # The canonical route's node command timeout.
        command_timeout=900.0,
    )
    service.state = "RUNNING"
    service.runtime = SimpleNamespace(decode_mode="stage_local_kv")
    service._bind_router_config_to_runtime(service.runtime)
    service.graph = SimpleNamespace(
        hidden_size=896,
        activation_bytes=4,
        stages=(SimpleNamespace(), SimpleNamespace()),
    )
    service._signed_result = (  # type: ignore[method-assign]
        lambda event, details: {"event": event, "details": details}
    )
    return service


def _payload(request_id: str, prompt_tokens: int) -> dict[str, Any]:
    return {
        "request": {
            "request_id": request_id,
            "prompt_token_ids": list(range(1, prompt_tokens + 1)),
            "max_new_tokens": 4,
            "expected_new_tokens": 4,
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1_000.0,
            "target_tpot_ms": 1_000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 17,
            "generation_config_digest": "sha256:" + "c" * 64,
        }
    }


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualTime:
    virtual = _VirtualTime()
    monkeypatch.setattr(node_module, "time", virtual)
    return virtual


# -- (a) chunk 0 confirmed, then the relay blackholes ---------------------------


def test_blackhole_after_chunk_zero_fails_as_prefill_transport_stalled_at_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _service(monkeypatch, tmp_path)
    service.router = _Router(clock, completes_at=None)  # type: ignore[assignment]
    transport = _Transport(clock, chunk_offsets=[0.0])
    service.transport = transport  # type: ignore[assignment]
    started = clock.now

    with pytest.raises(NodeCommandError) as caught:
        service._infer_start(_payload("request-blackhole", 74))

    elapsed = clock.now - started
    assert caught.value.code == "prefill_transport_stalled"
    # Fails at the progress watchdog, measured from the last progress (t=0),
    # not at the legacy fixed 240 s wait.
    assert WATCHDOG_S - 0.5 <= elapsed <= WATCHDOG_S + 0.5, elapsed
    assert elapsed < LEGACY_WAIT_S
    assert transport.released == ["request-blackhole"]
    diagnostic = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("MYCELIUM-PREFILL-STALL-DIAG ")
    ]
    assert len(diagnostic) == 1
    fields = dict(token.split("=", 1) for token in diagnostic[0].split()[1:])
    assert fields["code"] == "prefill_transport_stalled"
    assert fields["stall_window_ms"] == "40000"
    assert fields["watchdog_ms"] == "124000"
    assert fields["goodput_source"] == "unmeasured"
    assert fields["progress_events"] == "1"


@pytest.mark.parametrize(
    ("code", "reported_at"),
    (
        ("prefill_transport_stalled", 90.0),
        ("delivery_deadline_exceeded", 50.0),
    ),
)
def test_transport_verdict_for_the_request_is_reported_at_once_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
    code: str,
    reported_at: float,
) -> None:
    """A hop the transport gave up on surfaces its own code, not the generic one."""

    service = _service(monkeypatch, tmp_path)
    service.router = _Router(clock, completes_at=None)  # type: ignore[assignment]
    transport = _Transport(clock, chunk_offsets=[0.0, 20.0], failure=(reported_at, code))
    service.transport = transport  # type: ignore[assignment]
    started = clock.now

    with pytest.raises(NodeCommandError) as caught:
        service._infer_start(_payload(f"request-{code}", 74))

    assert caught.value.code == code
    assert reported_at <= clock.now - started <= reported_at + 0.1


# -- (b) slow but progressing ----------------------------------------------------


def test_four_kilobyte_per_second_link_completes_a_long_prompt_under_the_scaled_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
) -> None:
    """256 tokens = 32 chunks at 4 KiB/s: 9 s per chunk, 289 s in total.

    That is longer than the legacy fixed 240 s wait, but every chunk makes
    progress well inside the 40 s stall window, and the total deadline scales
    with the bytes still to move:
    clamp(32 x 28672 / 4096 x 3 + 32 x 4 x 1, 60, 900 x 0.8) = 720 s.
    """

    service = _service(monkeypatch, tmp_path)
    per_chunk = FRAME_BYTES / 4096.0 + 1.0
    offsets = [per_chunk * (index + 1) for index in range(32)]
    service.router = _Router(  # type: ignore[assignment]
        clock, completes_at=clock.now + offsets[-1] + 1.0
    )
    transport = _Transport(clock, chunk_offsets=offsets)
    service.transport = transport  # type: ignore[assignment]
    started = clock.now

    result = service._infer_start(_payload("request-slow", 256))

    assert result["details"]["status"] == "DECODING"
    assert result["details"]["output"]["token_ids"] == [42]
    assert clock.now - started > LEGACY_WAIT_S
    assert transport.watches, "the wait deadline must be registered with the transport"
    request_id, first_deadline = transport.watches[0]
    assert request_id == "request-slow"
    assert first_deadline - started == pytest.approx(720.0, abs=0.5)
    assert transport.released == ["request-slow"]


def test_progressing_link_slower_than_the_first_estimate_extends_with_bytes_remaining(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
) -> None:
    """Ten chunks, one every 35 s (inside the 40 s window): 350 s in total.

    The first estimate for ten chunks is clamp(10 x 28672/4096 x 3 + 10 x 4,
    60, 720) = 250 s.  Each confirmed chunk re-derives the deadline from the
    bytes still to move, so steady progress is never refused; the hard cap
    stays command_timeout x 0.8.
    """

    service = _service(monkeypatch, tmp_path)
    offsets = [35.0 * (index + 1) for index in range(10)]
    service.router = _Router(  # type: ignore[assignment]
        clock, completes_at=clock.now + offsets[-1] + 1.0
    )
    transport = _Transport(clock, chunk_offsets=offsets)
    service.transport = transport  # type: ignore[assignment]
    started = clock.now

    result = service._infer_start(_payload("request-steady", 74))

    assert result["details"]["status"] == "DECODING"
    assert transport.watches[0][1] - started == pytest.approx(250.0, abs=0.5)
    # Every registered deadline is inside the hard cap and never moves earlier.
    deadlines = [deadline for _, deadline in transport.watches]
    assert deadlines == sorted(deadlines)
    assert max(deadlines) - started <= 720.0 + 1e-6


def test_progressing_link_that_outruns_the_hard_cap_is_a_completion_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
) -> None:
    """Steady but slower than command_timeout x 0.8 allows: not a stall."""

    service = _service(monkeypatch, tmp_path)
    offsets = [35.0 * (index + 1) for index in range(32)]
    service.router = _Router(clock, completes_at=None)  # type: ignore[assignment]
    service.transport = _Transport(clock, chunk_offsets=offsets)  # type: ignore[assignment]
    started = clock.now

    with pytest.raises(NodeCommandError) as caught:
        service._infer_start(_payload("request-too-slow", 256))

    assert caught.value.code == "prefill_completion_timeout"
    assert clock.now - started == pytest.approx(720.0, abs=0.5)


def test_short_command_timeout_keeps_the_command_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
) -> None:
    """When command_timeout x 0.8 is below the 60 s floor the ceiling wins."""

    service = _service(monkeypatch, tmp_path)
    service.command_timeout = 30.0
    service.router = _Router(clock, completes_at=None)  # type: ignore[assignment]
    service.transport = _Transport(  # type: ignore[assignment]
        clock, chunk_offsets=[float(offset) for offset in range(0, 40)]
    )
    started = clock.now

    with pytest.raises(NodeCommandError) as caught:
        service._infer_start(_payload("request-short", 74))

    assert caught.value.code == "prefill_completion_timeout"
    assert clock.now - started == pytest.approx(24.0, abs=0.1)
