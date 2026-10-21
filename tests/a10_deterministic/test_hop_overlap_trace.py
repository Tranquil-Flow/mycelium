# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV5 overlap-trace instrument: per-hop send / receive / execute / ack instants.

The A10 device run needs to *show* the current serialisation and the gain from
acknowledging on receipt.  ``HopOverlapTrace`` records, per hop and per node, the
monotonic instants of ``send_requested`` / ``send_started`` / ``send_confirmed``
(sender) and ``received`` / ``exec_start`` / ``exec_end`` / ``ack_sent``
(receiver).  Every overlap question is answered on one node's clock, so no
cross-host offset is involved.  The records ride the transport's existing
evidence (``TransportEvidence.hop_overlap_trace``) and therefore the signed node
snapshot's ``details["transport"]``.

Failure modes this suite catches (one per test, by name):

  test_trace_is_off_by_default
      The instrument recording (and costing) anything in production.
  test_receiver_trace_shows_serialisation_with_the_default_ack
      A trace that cannot tell "B waited for A's execute-ack" from overlap, or
      records whose ack mode is wrong.
  test_receiver_trace_shows_overlap_with_ack_on_receipt
      The gain not being visible: B received/acknowledged while A executes.
  test_sender_trace_shows_the_second_send_blocked_behind_the_first
      A sender-side serialisation (shared send slot held until the remote ack)
      that the trace would miss.
  test_summary_is_decided_on_each_nodes_own_clock
      A summary that mixes hosts' clocks or counts same-request hops as overlap.
  test_trace_is_bounded_and_first_reading_wins
      Unbounded growth, or a replayed hop rewriting its own history.
  test_unknown_events_and_non_hop_frames_are_refused_or_ignored
      A typo'd event silently recorded, or a token event polluting the trace.
  test_env_switch_enables_the_trace
      The device run having no way to turn the instrument on without code.
"""

from __future__ import annotations

import threading

import pytest

from mycelium_router.contracts import TokenEvent
from mycelium_router.hop_overlap_trace import (
    ACK_MODE_AFTER_EXECUTE,
    ACK_MODE_ON_RECEIPT,
    HOP_OVERLAP_TRACE_PROTOCOL,
    HopOverlapTrace,
    summarize_hop_overlap,
)
from mycelium_router.wire import encode_frame
from tests.a10_deterministic.test_hop_ack_on_receipt import (
    _GatedHopRouter,
    _hop,
    _Node,
    _on,
    _transport,
    _wait,
)
from test_router_iroh_transport import _Hub


def _recv(records, request):
    return next(
        item
        for item in records
        if item["role"] == "recv" and item["request_id"] == f"req-{request}"
    )


def _send(records, request):
    return next(
        item
        for item in records
        if item["role"] == "send" and item["request_id"] == f"req-{request}"
    )


def test_trace_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYCELIUM_HOP_OVERLAP_TRACE", raising=False)
    with _Node() as node:
        node.deliver("m-a", _hop("a"))
        node.router.release_event("a").set()
        _wait(lambda: node.router.executed == ["req-a#1"])
        assert node.transport.hop_overlap_records() == ()
        assert node.transport.evidence().hop_overlap_trace == ()


def test_receiver_trace_shows_serialisation_with_the_default_ack() -> None:
    with _Node(hop_overlap_trace=True) as node:
        first = node.deliver("m-a", _hop("a"))
        assert node.router.started_event("a").wait(5)
        second = node.deliver("m-b", _hop("b"))
        node.router.release_event("a").set()
        assert node.router.started_event("b").wait(5)
        node.router.release_event("b").set()
        _wait(lambda: node.acked(first) and node.acked(second))
        _wait(lambda: len(node.transport.hop_overlap_records()) == 2)
        records = node.transport.evidence().hop_overlap_trace
        a, b = _recv(records, "a"), _recv(records, "b")
        assert a["protocol"] == HOP_OVERLAP_TRACE_PROTOCOL
        assert a["ack_mode"] == b["ack_mode"] == ACK_MODE_AFTER_EXECUTE
        assert a["events"]["ack_sent"] >= a["events"]["exec_end"]
        # B was not even taken from the sidecar until A had been acknowledged.
        assert b["events"]["received"] >= a["events"]["ack_sent"]
        summary = summarize_hop_overlap(records)
        assert summary["verdict"] == "serialized"
        assert summary["totals"]["recv_overlapped_pairs"] == 0


def test_receiver_trace_shows_overlap_with_ack_on_receipt() -> None:
    with _Node(**_on(), hop_overlap_trace=True) as node:
        node.deliver("m-a", _hop("a"))
        assert node.router.started_event("a").wait(5)
        second = node.deliver("m-b", _hop("b"))
        _wait(lambda: node.acked(second))
        node.router.release_event("a").set()
        assert node.router.started_event("b").wait(5)
        node.router.release_event("b").set()
        _wait(lambda: len(node.router.executed) == 2)
        _wait(lambda: "exec_end" in _recv(node.transport.hop_overlap_records(), "b")["events"])
        records = node.transport.hop_overlap_records()
        a, b = _recv(records, "a"), _recv(records, "b")
        assert a["ack_mode"] == b["ack_mode"] == ACK_MODE_ON_RECEIPT
        assert a["events"]["exec_start"] < b["events"]["received"] < a["events"]["exec_end"]
        assert b["events"]["ack_sent"] < a["events"]["exec_end"]
        assert b["events"]["exec_start"] >= a["events"]["exec_end"]
        summary = summarize_hop_overlap(records)
        assert summary["verdict"] == "overlapped"
        assert summary["totals"]["recv_overlapped_pairs"] == 1


def test_sender_trace_shows_the_second_send_blocked_behind_the_first() -> None:
    hub = _Hub()
    hub.block_confirmed_send = True
    transport = _transport(hub, hop_overlap_trace=True, delivery_timeout_seconds=5.0)
    transport.bind_router(_GatedHopRouter())
    transport.start()
    errors: list[BaseException] = []

    def send(name: str) -> None:
        try:
            transport.send_router_frame(
                encode_frame(_hop(name), b"x"), destination_node_id="peer-node"
            )
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    try:
        first = threading.Thread(target=send, args=("a",))
        first.start()
        assert hub.confirmed_send_entered.wait(5)
        second = threading.Thread(target=send, args=("b",))
        second.start()
        _wait(lambda: len(transport.hop_overlap_records()) == 2)
        hub.release_confirmed_send.set()
        first.join(5)
        second.join(5)
        assert errors == []
        records = transport.hop_overlap_records()
        a, b = _send(records, "a"), _send(records, "b")
        assert a["events"]["send_requested"] <= a["events"]["send_started"]
        assert a["events"]["send_started"] <= a["events"]["send_confirmed"]
        assert b["events"]["send_requested"] < a["events"]["send_confirmed"]
        assert b["events"]["send_started"] >= a["events"]["send_confirmed"]
        summary = summarize_hop_overlap(records)
        assert summary["totals"]["send_blocked_pairs"] == 1
        assert summary["verdict"] == "serialized"
    finally:
        hub.release_confirmed_send.set()
        transport.close()


def _record(node, role, request, **events):
    return {
        "protocol": HOP_OVERLAP_TRACE_PROTOCOL,
        "node_id": node,
        "role": role,
        "request_id": request,
        "events": dict(events),
    }


def test_summary_is_decided_on_each_nodes_own_clock() -> None:
    # Node x's clock is ~1000 s ahead of node y's; no pair may span the two.
    records = [
        _record("x", "recv", "a", received=1000.0, exec_start=1000.0, exec_end=1002.0, ack_sent=1000.1),
        _record("x", "recv", "b", received=1001.0, exec_start=1002.0, exec_end=1003.0, ack_sent=1001.1),
        _record("y", "recv", "c", received=1.0, exec_start=1.0, exec_end=2.0, ack_sent=2.0),
        # Same-request hop inside the window is a dependency, never overlap.
        _record("x", "recv", "a", received=1000.5, exec_start=1003.0, exec_end=1004.0, ack_sent=1000.6),
    ]
    for item in records:
        item["ack_mode"] = ACK_MODE_ON_RECEIPT if item["node_id"] == "x" else ACK_MODE_AFTER_EXECUTE
    summary = summarize_hop_overlap(records)
    assert summary["nodes"]["x"]["recv_overlapped_pairs"] == 1
    assert summary["nodes"]["y"]["recv_overlapped_pairs"] == 0
    assert summary["verdict"] == "overlapped"
    assert summarize_hop_overlap([])["verdict"] == "no_concurrent_hops"


def test_trace_is_bounded_and_first_reading_wins() -> None:
    clock = iter(float(i) for i in range(1000))
    trace = HopOverlapTrace(node_id="n", enabled=True, capacity=2, clock=lambda: next(clock))
    for name in ("a", "b", "c"):
        trace.mark("recv", _hop(name), "received")
    assert [r["request_id"] for r in trace.records()] == ["req-b", "req-c"]
    trace.mark("recv", _hop("c"), "received")  # a replay of the same hop
    assert _recv(trace.records(), "c")["events"]["received"] == 2.0


def test_unknown_events_and_non_hop_frames_are_refused_or_ignored() -> None:
    trace = HopOverlapTrace(node_id="n", enabled=True)
    with pytest.raises(ValueError):
        trace.mark("recv", _hop("a"), "executed")
    with pytest.raises(ValueError):
        trace.mark("sideways", _hop("a"), "received")
    trace.mark(
        "recv",
        TokenEvent(
            request_id="r", path_id="p", path_attempt=0, token_index=0, token_id=1, sampling_counter=1
        ),
        "received",
    )
    assert trace.records() == ()
    disabled = HopOverlapTrace(node_id="n")
    disabled.mark("recv", _hop("a"), "received")
    assert disabled.records() == ()


def test_env_switch_enables_the_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYCELIUM_HOP_OVERLAP_TRACE", "1")
    transport = _transport(_Hub())
    assert transport.hop_overlap_trace_enabled is True
    assert _transport(_Hub(), hop_overlap_trace=False).hop_overlap_trace_enabled is False
