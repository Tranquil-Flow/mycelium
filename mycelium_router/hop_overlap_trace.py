# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-hop overlap trace for the live iroh transport (PV5 measurement).

Records, for each router hop a node sends or receives, the monotonic instants the
A10 device run needs to show whether concurrent requests serialise at a hop:

``send`` role (the node forwarding the hop)
    ``send_requested``  the hop reached the transport's send path
    ``send_started``    the send slot was held and the frame went to the sidecar
    ``send_confirmed``  the remote sidecar's acknowledgement came back

``recv`` role (the node the hop is addressed to)
    ``received``        the sidecar handed the frame to this process
    ``exec_start``      the dispatcher began executing the hop
    ``exec_end``        the dispatch returned (a cap-1 hop has run; a queued
                        batch hop has been stored for its window)
    ``ack_sent``        this process acknowledged the frame to the sidecar

Every instant is a ``time.monotonic()`` reading on the recording node, so each
overlap question below is answered on one clock and needs no cross-host offset:

* accepted-while-executing: a *different* hop's ``received`` (and ``ack_sent``)
  falls inside this hop's ``[exec_start, exec_end]`` window on the same node;
* send serialisation: a hop was ``send_requested`` while another was in flight
  but did not become ``send_started`` until that one was ``send_confirmed``.

The recorder is disabled by default and costs one attribute test per call when
off.  It never stores payloads, only the hop's own identifiers and instants, and
keeps at most ``capacity`` hops (oldest dropped).  ``summarize_hop_overlap`` is a
pure function over the exported records so a harness can analyse a snapshot
captured from a device without importing the transport.
"""

from __future__ import annotations

from collections import OrderedDict
import threading
import time
from typing import Any, Callable, Iterable, Mapping

HOP_OVERLAP_TRACE_PROTOCOL = "mycelium.hop_overlap_trace.v1"
HOP_OVERLAP_SUMMARY_PROTOCOL = "mycelium.hop_overlap_summary.v1"

SEND_EVENTS = ("send_requested", "send_started", "send_confirmed")
RECV_EVENTS = ("received", "exec_start", "exec_end", "ack_sent")
ROLE_EVENTS = {"send": SEND_EVENTS, "recv": RECV_EVENTS}

#: ``ack_mode`` values: when the receiver acknowledged the frame to the sidecar.
ACK_MODE_AFTER_EXECUTE = "after_execute"
ACK_MODE_ON_RECEIPT = "on_receipt"

DEFAULT_CAPACITY = 512
ENV_ENABLE = "MYCELIUM_HOP_OVERLAP_TRACE"


def _hop_of(message: object) -> Any | None:
    """The ``HopHeader`` a decoded message carries, or ``None``."""

    if message is None:
        return None
    if hasattr(message, "idempotency_key") and hasattr(message, "hop_index"):
        return message
    header = getattr(message, "header", None)
    if header is not None and hasattr(header, "idempotency_key"):
        return header
    return None


class HopOverlapTrace:
    """Bounded, thread-safe, opt-in recorder of per-hop transport instants."""

    def __init__(
        self,
        *,
        node_id: str,
        enabled: bool = False,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.node_id = node_id
        self.enabled = bool(enabled)
        self._capacity = capacity
        self._clock = clock
        self._lock = threading.Lock()
        self._records: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def mark(
        self,
        role: str,
        message: object,
        event: str,
        *,
        at: float | None = None,
        **extra: Any,
    ) -> None:
        """Record ``event`` for the hop in ``message`` (no-op when disabled)."""

        if not self.enabled:
            return
        if event not in ROLE_EVENTS.get(role, ()):
            raise ValueError(f"unknown hop trace event {role}/{event}")
        header = _hop_of(message)
        if header is None:
            return
        instant = self._clock() if at is None else at
        key = (role, header.idempotency_key)
        with self._lock:
            record = self._records.get(key)
            if record is None:
                record = {
                    "protocol": HOP_OVERLAP_TRACE_PROTOCOL,
                    "node_id": self.node_id,
                    "role": role,
                    "request_id": header.request_id,
                    "path_id": header.path_id,
                    "path_attempt": header.path_attempt,
                    "phase": header.phase,
                    "token_index": header.token_index,
                    "hop_index": header.hop_index,
                    "events": {},
                }
                self._records[key] = record
                while len(self._records) > self._capacity:
                    self._records.popitem(last=False)
            # The first reading of an event is the true one: a replayed or
            # re-driven hop reuses the key and must not rewrite history.
            record["events"].setdefault(event, instant)
            for name, value in extra.items():
                record.setdefault(name, value)

    def records(self) -> tuple[Mapping[str, Any], ...]:
        """Detached copies of the retained records, oldest first."""

        with self._lock:
            return tuple(
                {**record, "events": dict(record["events"])}
                for record in self._records.values()
            )


def _window(record: Mapping[str, Any], start: str, end: str) -> tuple[float, float] | None:
    events = record.get("events", {})
    first, last = events.get(start), events.get(end)
    if first is None or last is None or last < first:
        return None
    return float(first), float(last)


def summarize_hop_overlap(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Answer the PV5 question from exported records, per node, on one clock each.

    ``recv_overlapped_pairs`` counts ordered pairs (A, B) of hops of *different*
    requests on one node where B was received inside A's execute window: the gain
    signal, impossible while the receiver takes no frame until the current one is
    acknowledged.  ``recv_ack_after_execute`` counts hops acknowledged only once
    their execute finished (the serialising behaviour).  ``send_blocked_pairs``
    counts sender pairs where B was requested while A was in flight but started
    only after A was confirmed.
    """

    by_node: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for record in records:
        node = str(record.get("node_id"))
        roles = by_node.setdefault(node, {"send": [], "recv": []})
        roles.setdefault(str(record.get("role")), []).append(record)

    nodes: dict[str, Any] = {}
    totals = {
        "recv_overlapped_pairs": 0,
        "recv_ack_after_execute": 0,
        "send_blocked_pairs": 0,
    }
    concurrent_requests = False
    for node, roles in sorted(by_node.items()):
        recv = roles.get("recv", [])
        send = roles.get("send", [])
        overlapped = after_execute = blocked = 0
        ack_wait_ms: list[float] = []
        exec_ms: list[float] = []
        blocked_ms: list[float] = []
        modes: set[str] = set()
        for record in recv:
            waited = _window(record, "received", "ack_sent")
            if waited is not None:
                ack_wait_ms.append((waited[1] - waited[0]) * 1000.0)
            ran = _window(record, "exec_start", "exec_end")
            if ran is not None:
                exec_ms.append((ran[1] - ran[0]) * 1000.0)
            mode = record.get("ack_mode")
            if isinstance(mode, str):
                modes.add(mode)
            ack_at = record.get("events", {}).get("ack_sent")
            if mode == ACK_MODE_AFTER_EXECUTE and ran is not None and ack_at is not None:
                if ack_at >= ran[1]:
                    after_execute += 1
        for a in recv:
            ran = _window(a, "exec_start", "exec_end")
            if ran is None:
                continue
            for b in recv:
                if b is a or b.get("request_id") == a.get("request_id"):
                    continue
                concurrent_requests = True
                received = b.get("events", {}).get("received")
                if received is not None and ran[0] <= received < ran[1]:
                    overlapped += 1
        for a in send:
            flight = _window(a, "send_started", "send_confirmed")
            if flight is None:
                continue
            for b in send:
                if b is a:
                    continue
                events = b.get("events", {})
                requested, started = events.get("send_requested"), events.get("send_started")
                if requested is None or started is None:
                    continue
                if requested < flight[1] <= started:
                    blocked += 1
                    blocked_ms.append((started - requested) * 1000.0)
        nodes[node] = {
            "recv_hops": len(recv),
            "send_hops": len(send),
            "ack_modes": sorted(modes),
            "recv_overlapped_pairs": overlapped,
            "recv_ack_after_execute": after_execute,
            "send_blocked_pairs": blocked,
            "mean_received_to_ack_ms": _mean(ack_wait_ms),
            "mean_exec_ms": _mean(exec_ms),
            "mean_send_blocked_ms": _mean(blocked_ms),
        }
        totals["recv_overlapped_pairs"] += overlapped
        totals["recv_ack_after_execute"] += after_execute
        totals["send_blocked_pairs"] += blocked
    if totals["recv_overlapped_pairs"] > 0:
        verdict = "overlapped"
    elif totals["send_blocked_pairs"] > 0 or (
        concurrent_requests and totals["recv_ack_after_execute"] > 0
    ):
        verdict = "serialized"
    else:
        verdict = "no_concurrent_hops"
    return {
        "protocol": HOP_OVERLAP_SUMMARY_PROTOCOL,
        "verdict": verdict,
        "totals": totals,
        "nodes": nodes,
    }


def _mean(values: list[float]) -> float | None:
    return None if not values else round(sum(values) / len(values), 3)
