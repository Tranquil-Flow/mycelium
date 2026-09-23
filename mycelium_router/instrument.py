# SPDX-License-Identifier: AGPL-3.0-or-later
"""mycelium_router.instrument — per-hop timing capture for distributed inference.

A read-only, opt-in recorder. When disabled (the default), every context manager
is a no-op (the disabled fast-path is verified by test_instrument.py:ZeroCost
< 200 ms for 10k no-op traces). When enabled, each `trace_request` emits one
JSONL line containing a complete per-hop breakdown.

Design goals:
  * zero product change (a single env var flips it on)
  * monotonic-per-hop ordering
  * every hop has the same closed schema
  * wall_ms = compute_ms + transport_ms + queue_ms (recomputed in the recorder)

This file is the source of truth for the hop schema; the analyzer
(`mycelium_router.analyze`) reads the same shape.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_HOP_FIELDS = (
    "stage_id", "node_id",
    "queue_in_at", "compute_start_at", "compute_end_at",
    "send_start_at", "send_end_at",
    "recv_start_at", "recv_end_at",
    "bytes_sent", "bytes_recv",
    "wall_ms", "compute_ms", "transport_ms", "queue_ms",
)


class _Hop:
    """One stage hop. Records wall-time monotonic points and computes derived fields."""

    __slots__ = ("_owner", "_hop", "_compute_end_at", "_send_start_at",
                 "_send_end_at", "_recv_start_at", "_recv_end_at")

    def __init__(self, owner: "_RequestTrace", hop: dict) -> None:
        self._owner = owner
        self._hop = hop

    def queue_in(self) -> None:
        self._hop["queue_in_at"] = time.monotonic()

    def compute_start(self) -> None:
        self._hop["compute_start_at"] = time.monotonic()

    def compute_end(self) -> None:
        self._compute_end_at = time.monotonic()
        self._hop["compute_end_at"] = self._compute_end_at

    def send(self, *, bytes_out: int = 0) -> None:
        now = time.monotonic()
        # If no dedicated start hook, use `now` for both start and end so the
        # timestamps stay monotonic (no zero-default sentinel that subtracts
        # from epoch in the derived transport_ms).
        if self._hop.get("send_start_at", 0.0) == 0.0:
            self._hop["send_start_at"] = now
        self._send_end_at = now
        self._hop["send_end_at"] = now
        self._hop["bytes_sent"] = int(bytes_out)

    def recv(self, *, bytes_in: int = 0) -> None:
        now = time.monotonic()
        if self._hop.get("recv_start_at", 0.0) == 0.0:
            self._hop["recv_start_at"] = now
        self._recv_end_at = now
        self._hop["recv_end_at"] = now
        self._hop["bytes_recv"] = int(bytes_in)


class _RequestTrace:
    """One request. Owns a list of hops and the received_at / finished_at markers."""

    __slots__ = ("_recorder", "_trace")

    def __init__(self, recorder: "HopRecorder", request_id: str) -> None:
        self._recorder = recorder
        self._trace = {
            "request_id": request_id,
            "received_at": time.monotonic(),
            "hops": [],
        }

    @contextmanager
    def hop(self, stage_id: str, node_id: str) -> Iterator[_Hop]:
        if not self._recorder.enabled:
            yield _DisabledHop()  # type: ignore[arg-type]
            return
        hop: dict = {
            "stage_id": stage_id,
            "node_id": node_id,
            "queue_in_at": 0.0,
            "compute_start_at": 0.0,
            "compute_end_at": 0.0,
            "send_start_at": 0.0,
            "send_end_at": 0.0,
            "recv_start_at": 0.0,
            "recv_end_at": 0.0,
            "bytes_sent": 0,
            "bytes_recv": 0,
            "wall_ms": 0.0,
            "compute_ms": 0.0,
            "transport_ms": 0.0,
            "queue_ms": 0.0,
        }
        h = _Hop(self, hop)
        try:
            yield h
        finally:
            self._finalize_hop(hop)
            self._trace["hops"].append(hop)

    def _finalize_hop(self, hop: dict) -> None:
        """Compute wall_ms / compute_ms / transport_ms / queue_ms from timestamps."""
        ms = lambda a, b: max(0.0, (b - a) * 1000.0)  # noqa: E731
        wall = ms(hop["queue_in_at"], hop["recv_end_at"])
        compute = ms(hop["compute_start_at"], hop["compute_end_at"])
        transport = ms(hop["send_start_at"], hop["recv_end_at"])
        queue = max(0.0, wall - compute - transport)
        hop["wall_ms"] = round(wall, 3)
        hop["compute_ms"] = round(compute, 3)
        hop["transport_ms"] = round(transport, 3)
        hop["queue_ms"] = round(queue, 3)


class _DisabledHop:
    """Returned by the disabled recorder. Every method is a no-op."""

    __slots__ = ()

    def queue_in(self) -> None: pass
    def compute_start(self) -> None: pass
    def compute_end(self) -> None: pass
    def send(self, *args, **kwargs) -> None: pass
    def recv(self, *args, **kwargs) -> None: pass


class _DisabledTrace:
    __slots__ = ()

    @contextmanager
    def hop(self, stage_id: str, node_id: str) -> Iterator[_DisabledHop]:
        yield _DisabledHop()


class HopRecorder:
    """Reads env var ``MYCELIUM_ROUTER_INSTRUMENT`` to decide on/off at construction.

    Off by default. When on, requires ``sink_path`` (a writable JSONL file) — the
    recorder never invents a path.
    """

    __slots__ = ("enabled", "sink_path", "records")

    ENV_VAR = "MYCELIUM_ROUTER_INSTRUMENT"

    def __init__(self, *, enabled: bool | None = None,
                 sink_path: str | Path | None = None) -> None:
        if enabled is None:
            enabled = bool(os.environ.get(self.ENV_VAR))
        self.enabled = bool(enabled) and sink_path is not None
        self.sink_path = Path(sink_path) if (self.enabled and sink_path) else None
        self.records: list[dict] = []
        if self.enabled and self.sink_path is not None:
            self.sink_path.parent.mkdir(parents=True, exist_ok=True)
            self.sink_path.write_text("")  # truncate; we append

    @contextmanager
    def trace_request(self, request_id: str) -> Iterator[_RequestTrace | _DisabledTrace]:
        if not self.enabled:
            yield _DisabledTrace()
            return
        t = _RequestTrace(self, request_id)
        try:
            yield t
        finally:
            t._trace["finished_at"] = time.monotonic()
            t._trace["ttft_ms"] = round(
                (t._trace["finished_at"] - t._trace["received_at"]) * 1000.0, 3)
            self._append(t._trace)

    def _append(self, trace: dict) -> None:
        self.records.append(trace)
        if self.sink_path is None:
            return
        with self.sink_path.open("a") as fh:
            fh.write(json.dumps(trace, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        # No buffered writer today; method exists so the public API matches the
        # README and analyzer's expectations.
        return


def is_enabled() -> bool:
    """Quick check used at hot-path call sites to skip even constructing traces."""
    return bool(os.environ.get(HopRecorder.ENV_VAR))
