# SPDX-License-Identifier: AGPL-3.0-only
"""The node's native ``snapshot`` response must not outgrow the 256 KiB authority row.

Measured on the live A13 demo (2026-10-21): every inference request added 45-100 KB to
the ``snapshot`` observation (three per-request histories: the A10 stage-compute
intervals, the transport's outbound frame trace and its scoped receipt events), the
response reached 253 KB after four 48-token chats and the owner's control pump died
with ``authority_state_write_failed`` because the pending command is persisted whole
in a row capped at ``authority._MAX_PRIVATE_JSON_BYTES`` (256 KiB).

The per-history deques were bounded (256 entries each) but their sum alone is about
330 KB, so the cap was never a bound on the *response*.  These tests drive the real
stage runtimes and the real ``TransportEvidence`` shape through ``_snapshot`` for
N = 200 requests and require a fixed response bound plus honest counters.

FAKE (labelled): ``_GrowingTransport`` stands in for ``IrohTransport``; it serves the
exact evidence/trace shapes the live node produced (read-only copy of the owner
mailbox row), bounded by the same 256-entry deques the real transport uses.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import itertools
import json
from collections import deque

import physical_inference_node as node_module
from mycelium_a13_macos.authority import _MAX_PRIVATE_JSON_BYTES
from mycelium_router.a10_live_publication import (
   MAXIMUM_INTERVALS,
   pseudonym,
   validate_publication,
)
from mycelium_router.transports.iroh import TransportEvidence
from tests.a10_deterministic.test_a10_live_publication import _run_request
from tests.a10_deterministic.test_runtime_target_verifier import mlx_case  # noqa: F401

REQUESTS = 200
TOKENS_PER_REQUEST = 48
RESPONSE_BOUND = 64 * 1024


_RUNS = itertools.count()


class _GrowingTransport:
   def __init__(self) -> None:
      # The mlx fixture is shared: request ids (and the cancelled-path markers they
      # leave behind) must be unique per test.
      self.prefix = f"run{next(_RUNS)}-request"
      self.trace: deque[str] = deque(maxlen=256)
      self.events: deque[dict] = deque(maxlen=256)
      self.sequence = 0
      self.fatal_error = None
      self.worker_threads_alive = 2
      self.dispatcher_phase = "idle"
      self.last_dispatch_error = None
      self.pending_delivery_count = 0
      self.last_cancellation = None

   def serve_request(self, index: int, *, failure_at: int | None = None) -> None:
      for token in range(TOKENS_PER_REQUEST):
         message = f"{index:04d}{token:04d}" + "a" * 24
         self.trace.append(
            "DeliveryReceipt->peer:remote:"
            + json.dumps(
               {
                  "delivery_receipt_sha256": "sha256:" + "8" * 64,
                  "message_id": message,
                  "peer_endpoint_id": "c" * 64,
                  "peer_generation": 1,
                  "router_protocol": "mycelium.router_wire.v1",
                  "semantics": "remote_router_dispatch_ack",
               },
               sort_keys=True,
               separators=(",", ":"),
            )
         )
         self.sequence += 1
         failed = failure_at == token
         self.events.append(
            {
               "protocol": "mycelium.iroh_scoped_transport_event.v1",
               "sequence": self.sequence,
               "event": "failure" if failed else "receipt",
               "request_id": f"startup-{index:04d}",
               "path_id": "path-a5644358-d1c6-4c32-99a1-781794a573a8",
               "path_attempt": 0,
               "peer_node_id": "remote-mac-7e86e6899bc7025f84c314d5",
               "peer_generation": 1,
               "code": "peer_unreachable" if failed else None,
            }
         )

   @property
   def outbound_trace(self) -> tuple[str, ...]:
      return tuple(self.trace)

   def evidence(self) -> TransportEvidence:
      return TransportEvidence(
         local_node_id="local",
         local_endpoint_id="a" * 64,
         peer_node_id="remote-mac-7e86e6899bc7025f84c314d5",
         peer_endpoint_id="b" * 64,
         peer_generation=1,
         remote_frames_sent=self.sequence,
         remote_frames_received=self.sequence,
         router_frames_dispatched=self.sequence,
         duplicate_frames=0,
         scoped_events=tuple(dict(event) for event in self.events),
      )

   def cancellation_cleanup_complete(self, request_id, path_id) -> bool:
      return True


def _service(case, transport):
   service = object.__new__(node_module.PhysicalNodeService)
   service.state = "RUNNING"
   service.runtime = case.ports[0]
   service.transport = transport
   service.sidecar = None
   service.capacity = None
   service._last_cancellation = None
   service._host_resources = lambda: {}
   service._signed_result = lambda event, details: {"event": event, "details": details}
   return service


def _size(document) -> int:
   return len(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _finish_request(case, request_id):
   """Run one request and release its KV state, as a served request does.

   The live node frees a finished request's KV; leaving it would grow the ``runtime``
   block with the *active* paths, which is a separate (admission-bounded) quantity.
   """
   _run_request(case, request_id, (1, 2, 3))
   for port in case.ports:
      port.cancel(f"path:{request_id}")


def _drive(case, transport, *, requests=REQUESTS, failure_request=None):
   for index in range(requests):
      _finish_request(case, f"{transport.prefix}-{index}")
      transport.serve_request(
         index, failure_at=7 if index == failure_request else None
      )


def test_snapshot_stays_below_a_fixed_bound_after_200_requests(mlx_case):
   transport = _GrowingTransport()
   service = _service(mlx_case, transport)
   _drive(mlx_case, transport)

   signed = service._snapshot({})

   size = _size(signed)
   assert size <= RESPONSE_BOUND, size
   # The bound is a real margin under the authority row, not a coincidence.
   assert RESPONSE_BOUND * 3 < _MAX_PRIVATE_JSON_BYTES
   # The row holds the response *and* the command envelope; the node's own budget
   # for the details is a published constant below the response bound.
   assert node_module.SNAPSHOT_DETAILS_MAX_BYTES < RESPONSE_BOUND
   assert _size(signed["details"]) <= node_module.SNAPSHOT_DETAILS_MAX_BYTES


def test_size_does_not_grow_with_the_number_of_requests(mlx_case):
   transport = _GrowingTransport()
   service = _service(mlx_case, transport)
   _drive(mlx_case, transport, requests=60)
   sizes = [_size(service._snapshot({}))]
   for batch in range(3):
      for index in range(60 + batch * 50, 110 + batch * 50):
         _finish_request(mlx_case, f"{transport.prefix}-{index}")
         transport.serve_request(index)
      sizes.append(_size(service._snapshot({})))
   assert max(sizes) <= RESPONSE_BOUND
   # Saturated: more requests no longer move the size by more than rounding.
   assert max(sizes) - min(sizes) < 4 * 1024, sizes


def test_publication_stays_valid_recent_and_counts_what_it_left_out(mlx_case):
   transport = _GrowingTransport()
   service = _service(mlx_case, transport)
   _drive(mlx_case, transport)
   recorded = service.runtime._a10_publication._recorded

   details = service._snapshot({})["details"]

   publication = details["a10_publication"]
   assert validate_publication(publication) == publication
   intervals = publication["intervals"]
   assert 0 < len(intervals) <= MAXIMUM_INTERVALS
   # Counters, not silence: published + dropped is everything ever recorded.
   assert len(intervals) + publication["dropped_interval_count"] == recorded
   # The window is the most recent one: the last request's interval is in it.
   assert intervals[-1]["request_ref"] == pseudonym("request", f"{transport.prefix}-{REQUESTS - 1}")
   assert [i["interval_ref"] for i in intervals] == sorted(
      (i["interval_ref"] for i in intervals), key=lambda ref: int(ref.rsplit(":", 1)[1])
   )


def test_transport_histories_are_a_recent_window_with_counters(mlx_case):
   transport = _GrowingTransport()
   service = _service(mlx_case, transport)
   _drive(mlx_case, transport)
   held_trace, held_events = len(transport.trace), len(transport.events)

   details = service._snapshot({})["details"]

   trace = details["transport_outbound_trace"]
   events = details["transport"]["scoped_events"]
   assert 0 < len(trace) < held_trace
   assert 0 < len(events) < held_events
   # Newest entries survive, in order.
   assert trace == list(transport.trace)[-len(trace):]
   assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)
   assert events[-1]["sequence"] == transport.sequence
   bounds = details["snapshot_bounds"]
   assert bounds["max_bytes"] == node_module.SNAPSHOT_DETAILS_MAX_BYTES
   assert bounds["transport_outbound_trace"] == {
      "published": len(trace),
      "dropped": held_trace - len(trace),
   }
   assert bounds["scoped_events"]["published"] == len(events)
   assert bounds["scoped_events"]["dropped"] == held_events - len(events)
   # Cumulative frame counters (the aggregates consumers read) are untouched.
   assert details["transport"]["remote_frames_sent"] == transport.sequence


def test_failure_events_outlive_the_receipt_window(mlx_case):
   """A liveness failure is rarer and costlier to miss than a receipt."""
   transport = _GrowingTransport()
   service = _service(mlx_case, transport)
   _drive(mlx_case, transport, failure_request=REQUESTS - 3)

   events = service._snapshot({})["details"]["transport"]["scoped_events"]

   failures = [event for event in events if event["event"] == "failure"]
   assert [event["request_id"] for event in failures] == [f"startup-{REQUESTS - 3:04d}"]
   assert events[-1]["event"] == "receipt"
   assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)


def test_details_budget_holds_even_when_each_entry_is_oversized(mlx_case):
   """The window counts are a first cut; the byte budget is the guarantee."""
   transport = _GrowingTransport()
   service = _service(mlx_case, transport)
   _drive(mlx_case, transport, requests=30)
   fat = "x" * 3000
   transport.trace.extend(fat for _ in range(256))
   for event in transport.events:
      event["code"] = fat

   signed = service._snapshot({})

   assert _size(signed["details"]) <= node_module.SNAPSHOT_DETAILS_MAX_BYTES
   assert _size(signed) <= RESPONSE_BOUND
