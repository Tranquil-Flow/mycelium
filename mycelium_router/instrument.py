# SPDX-License-Identifier: AGPL-3.0-only
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

Timing accuracy contract
------------------------
A hop may be reported two ways, and the record always says which one it is:

``transport_source == "socket_spans"``
    The transport bracketed a real socket write and the matching read
    completion.  ``socket_write_at`` is a monotonic timestamp taken immediately
    *before* the write call and ``socket_read_end_at`` one taken immediately
    *after* the read completed, so ``transport_ms`` is the measured round trip
    across the wire.  ``measured_rtt_ms`` repeats that number and names its
    source; ``serialize_ms`` and ``deserialize_ms`` are *separate* spans with
    their own labels and are never merged into ``transport_ms``.

``transport_source == "point_stamp_fallback"``
    No socket span was available (e.g. a caller used the legacy
    ``send()``/``recv()`` pair).  ``transport_ms`` is then the old
    point-stamp difference and ``measured_rtt_ms`` / ``serialize_ms`` /
    ``deserialize_ms`` are ``None`` so that a point stamp can never be mistaken
    for a measurement.

This file is the source of truth for the hop schema; the analyzer
(`mycelium_router.analyze`) reads the same shape.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: Transport-span provenance labels read by consumers and tests.
TRANSPORT_SOURCE_SOCKET_SPANS = "socket_spans"
TRANSPORT_SOURCE_POINT_STAMP_FALLBACK = "point_stamp_fallback"

#: Span labels.  A span label says which operation the timestamps bracketed.
SERIALIZE_SOURCE_ENCODE_FRAME = "encode_frame"
DESERIALIZE_SOURCE_RESPONSE_FRAME = "response_frame"

#: Measurement-source label for a round trip measured across a real socket.
MEASURED_RTT_SOURCE_SOCKET_ROUND_TRIP = "socket_round_trip"

#: Measurement-source label for a round trip measured at a live node-session
#: boundary (a command write plus its correlated response).  Distinct from the
#: socket label on purpose: it includes the peer's processing time.
MEASURED_RTT_SOURCE_LIVE_SESSION_ROUND_TRIP = "live_session_round_trip"

_HOP_FIELDS = (
    "stage_id", "node_id",
    "queue_in_at", "compute_start_at", "compute_end_at",
    "send_start_at", "send_end_at",
    "recv_start_at", "recv_end_at",
    "bytes_sent", "bytes_recv",
    "wall_ms", "compute_ms", "transport_ms", "queue_ms",
)


def _ms(start: float | None, end: float | None) -> float:
    """Milliseconds between two monotonic points, clamped at zero."""

    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start) * 1000.0)


def _new_hop(request_id: str, stage_id: str, node_id: str, hop_id: str,
             causal_predecessor: str | None) -> dict:
    """The closed hop schema. Absent spans are ``None``, never a fake number."""

    return {
        "request_id": request_id,
        "stage_id": stage_id,
        "node_id": node_id,
        "hop_id": hop_id,
        "causal_predecessor": causal_predecessor,
        "queue_in_at": 0.0,
        "compute_start_at": 0.0,
        "compute_end_at": 0.0,
        "send_start_at": 0.0,
        "send_end_at": 0.0,
        "recv_start_at": 0.0,
        "recv_end_at": 0.0,
        "bytes_sent": 0,
        "bytes_recv": 0,
        # -- true socket spans -------------------------------------------
        "serialize_start_at": None,
        "serialize_end_at": None,
        "serialize_ms": None,
        "serialize_source": None,
        "socket_write_at": None,
        "socket_write_after_at": None,
        "socket_write_span_ms": None,
        "socket_read_end_at": None,
        "deserialize_start_at": None,
        "deserialize_end_at": None,
        "deserialize_ms": None,
        "deserialize_source": None,
        "transport_source": TRANSPORT_SOURCE_POINT_STAMP_FALLBACK,
        "measured_rtt_ms": None,
        "measured_rtt_source": None,
        # -- derived ------------------------------------------------------
        "wall_ms": 0.0,
        "compute_ms": 0.0,
        "transport_ms": 0.0,
        "queue_ms": 0.0,
    }


def derive_hop_metrics(hop: dict) -> None:
    """Compute every derived field from the recorded monotonic points.

    Shared by the context-manager path (``_RequestTrace.hop``) and the
    inline transport path (``HopRecorder.record_hop``) so both emit one schema.
    """

    wall = _ms(hop["queue_in_at"], hop["recv_end_at"])
    compute = _ms(hop["compute_start_at"], hop["compute_end_at"])
    if hop["socket_write_at"] is not None and hop["socket_read_end_at"] is not None:
        # A real socket span: measured round trip from immediately before the
        # write to immediately after the read completed.
        hop["transport_source"] = TRANSPORT_SOURCE_SOCKET_SPANS
        transport = _ms(hop["socket_write_at"], hop["socket_read_end_at"])
        hop["socket_write_span_ms"] = round(
            _ms(hop["socket_write_at"], hop["socket_write_after_at"]), 3
        )
        hop["measured_rtt_ms"] = round(transport, 3)
        hop["measured_rtt_source"] = MEASURED_RTT_SOURCE_SOCKET_ROUND_TRIP
    else:
        hop["transport_source"] = TRANSPORT_SOURCE_POINT_STAMP_FALLBACK
        transport = _ms(hop["send_start_at"], hop["recv_end_at"])
        hop["socket_write_span_ms"] = None
        hop["measured_rtt_ms"] = None
        hop["measured_rtt_source"] = None
    if hop["serialize_start_at"] is not None and hop["serialize_end_at"] is not None:
        hop["serialize_ms"] = round(
            _ms(hop["serialize_start_at"], hop["serialize_end_at"]), 3
        )
    else:
        hop["serialize_ms"] = None
    if (
        hop["deserialize_start_at"] is not None
        and hop["deserialize_end_at"] is not None
    ):
        hop["deserialize_ms"] = round(
            _ms(hop["deserialize_start_at"], hop["deserialize_end_at"]), 3
        )
    else:
        hop["deserialize_ms"] = None
    hop["wall_ms"] = round(wall, 3)
    hop["compute_ms"] = round(compute, 3)
    hop["transport_ms"] = round(transport, 3)
    hop["queue_ms"] = round(max(0.0, wall - compute - transport), 3)


@dataclass(frozen=True)
class HopSample:
    """An inline-measured hop, for transports that hold the spans themselves.

    Every timestamp is a caller-supplied ``time.monotonic()`` reading taken at
    the moment named by the field.  A field left as ``None`` means the span was
    not captured — the record then says so instead of inventing a number.
    """

    request_id: str
    stage_id: str
    node_id: str
    queue_in_at: float | None = None
    compute_start_at: float | None = None
    compute_end_at: float | None = None
    serialize_start_at: float | None = None
    serialize_end_at: float | None = None
    serialize_source: str | None = None
    socket_write_at: float | None = None
    socket_write_after_at: float | None = None
    socket_read_end_at: float | None = None
    deserialize_start_at: float | None = None
    deserialize_end_at: float | None = None
    deserialize_source: str | None = None
    bytes_sent: int = 0
    bytes_recv: int = 0
    #: Which boundary produced the round-trip span.  Defaults to the socket
    #: source; a caller measuring at another boundary (e.g. a live node
    #: session) names it, and the hop's ``transport_source`` follows.
    measured_rtt_source: str = MEASURED_RTT_SOURCE_SOCKET_ROUND_TRIP


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
        # from epoch in the derived transport_ms).  This is a point stamp, and
        # derive_hop_metrics labels it as one: measured_rtt_ms stays None.
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

    # -- true socket spans ------------------------------------------------
    # Each mark_* call takes its own time.monotonic() reading inside the method
    # so the label and the reading can never drift apart.  A caller that already
    # holds a reading (because it must stamp immediately before a syscall it
    # does not own) uses HopSample + HopRecorder.record_hop instead.

    def mark_serialize_start(self, source: str) -> None:
        self._hop["serialize_source"] = source
        self._hop["serialize_start_at"] = time.monotonic()

    def mark_serialize_end(self) -> None:
        if self._hop["serialize_start_at"] is None:
            raise RuntimeError("serialize span ended before it started")
        self._hop["serialize_end_at"] = time.monotonic()

    def mark_socket_write_before(self, *, bytes_out: int = 0) -> None:
        """Stamp immediately *before* the socket write call."""

        now = time.monotonic()
        self._hop["socket_write_at"] = now
        self._hop["bytes_sent"] = int(bytes_out)
        # Keep the legacy pair monotonic for readers of the old fields.
        if self._hop.get("send_start_at", 0.0) == 0.0:
            self._hop["send_start_at"] = now

    def mark_socket_write_after(self) -> None:
        now = time.monotonic()
        self._hop["socket_write_after_at"] = now
        self._hop["send_end_at"] = now

    def mark_socket_read_end(self, *, bytes_in: int = 0) -> None:
        """Stamp immediately *after* the read completed."""

        now = time.monotonic()
        self._hop["socket_read_end_at"] = now
        self._hop["recv_end_at"] = now
        self._hop["recv_start_at"] = self._hop["recv_start_at"] or now
        self._hop["bytes_recv"] = int(bytes_in)

    def mark_deserialize_start(self, source: str) -> None:
        self._hop["deserialize_source"] = source
        self._hop["deserialize_start_at"] = time.monotonic()

    def mark_deserialize_end(self) -> None:
        if self._hop["deserialize_start_at"] is None:
            raise RuntimeError("deserialize span ended before it started")
        self._hop["deserialize_end_at"] = time.monotonic()


class _RequestTrace:
    """One request. Owns a list of hops and the received_at / finished_at markers."""

    __slots__ = ("_recorder", "_trace", "_hop_count", "_last_hop_id")

    def __init__(self, recorder: "HopRecorder", request_id: str) -> None:
        self._recorder = recorder
        self._hop_count = 0
        self._last_hop_id: str | None = None
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
        self._hop_count += 1
        hop = _new_hop(
            self._trace["request_id"], stage_id, node_id,
            f"{stage_id}:{node_id}:{self._hop_count}",
            self._last_hop_id,
        )
        h = _Hop(self, hop)
        try:
            yield h
        finally:
            derive_hop_metrics(hop)
            self._last_hop_id = hop["hop_id"]
            self._trace["hops"].append(hop)

    def _finalize_hop(self, hop: dict) -> None:
        """Compute wall_ms / compute_ms / transport_ms / queue_ms from timestamps."""

        derive_hop_metrics(hop)


class _DisabledHop:
    """Returned by the disabled recorder. Every method is a no-op."""

    __slots__ = ()

    def queue_in(self) -> None: pass
    def compute_start(self) -> None: pass
    def compute_end(self) -> None: pass
    def send(self, *args, **kwargs) -> None: pass
    def recv(self, *args, **kwargs) -> None: pass
    def mark_serialize_start(self, *args, **kwargs) -> None: pass
    def mark_serialize_end(self, *args, **kwargs) -> None: pass
    def mark_socket_write_before(self, *args, **kwargs) -> None: pass
    def mark_socket_write_after(self, *args, **kwargs) -> None: pass
    def mark_socket_read_end(self, *args, **kwargs) -> None: pass
    def mark_deserialize_start(self, *args, **kwargs) -> None: pass
    def mark_deserialize_end(self, *args, **kwargs) -> None: pass


class _DisabledTrace:
    __slots__ = ()

    @contextmanager
    def hop(self, stage_id: str, node_id: str) -> Iterator[_DisabledHop]:
        yield _DisabledHop()


class HopRecorder:
    """Reads env var ``MYCELIUM_ROUTER_INSTRUMENT`` to decide on/off at construction.

    Off by default. When on, requires ``sink_path`` (a writable JSONL file) — the
    recorder never invents a path.  ``from_environment`` is the switch a live run
    uses: it reads ``MYCELIUM_ROUTER_INSTRUMENT`` plus
    ``MYCELIUM_ROUTER_INSTRUMENT_SINK`` so instrumentation needs no code edit.
    """

    __slots__ = ("enabled", "sink_path", "records")

    ENV_VAR = "MYCELIUM_ROUTER_INSTRUMENT"
    SINK_ENV_VAR = "MYCELIUM_ROUTER_INSTRUMENT_SINK"

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

    @classmethod
    def from_environment(cls) -> "HopRecorder":
        """Build the recorder a live run uses, from the environment alone.

        Enabling the env var without a sink yields a disabled recorder: this
        class never invents an output path.
        """

        sink = os.environ.get(cls.SINK_ENV_VAR)
        return cls(
            enabled=bool(os.environ.get(cls.ENV_VAR)),
            sink_path=Path(sink) if sink else None,
        )

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

    def record_hop(self, sample: HopSample) -> dict | None:
        """Emit one inline-measured hop as its own single-hop trace.

        Transports hold their own spans (they must stamp immediately around a
        syscall they do not own), so they pass a :class:`HopSample` instead of
        using the context manager.  The emitted hop goes through the same
        ``derive_hop_metrics`` path as ``trace_request`` hops, so both readers
        see one schema.  Returns ``None`` when the recorder is disabled.
        """

        if not self.enabled:
            return None
        hop = _new_hop(
            sample.request_id, sample.stage_id, sample.node_id,
            f"{sample.stage_id}:{sample.node_id}:1", None,
        )
        for field in (
            "queue_in_at", "compute_start_at", "compute_end_at",
            "serialize_start_at", "serialize_end_at", "serialize_source",
            "socket_write_at", "socket_write_after_at", "socket_read_end_at",
            "deserialize_start_at", "deserialize_end_at", "deserialize_source",
        ):
            value = getattr(sample, field)
            if value is not None:
                hop[field] = value
        hop["bytes_sent"] = int(sample.bytes_sent)
        hop["bytes_recv"] = int(sample.bytes_recv)
        derive_hop_metrics(hop)
        if (
            hop["measured_rtt_ms"] is not None
            and sample.measured_rtt_source != MEASURED_RTT_SOURCE_SOCKET_ROUND_TRIP
        ):
            # A round trip measured at another boundary still names its source,
            # and the hop's transport label follows it so a reader is never told
            # "socket" for a span that was not one.
            hop["measured_rtt_source"] = sample.measured_rtt_source
            hop["transport_source"] = sample.measured_rtt_source
        received_at = time.monotonic()
        self._append({
            "request_id": sample.request_id,
            "received_at": received_at,
            "finished_at": received_at,
            "ttft_ms": 0.0,
            "hops": [hop],
        })
        return hop

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
