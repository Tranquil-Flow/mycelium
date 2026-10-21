# Hop acknowledgement on receipt (PV5)

Backlog row PV5 (`V2-BACKLOG.md`): *with batch cap 1 the receiver acks a hop only after
executing it, and the sender takes no new frame on that stream until acked; ack on receipt
(ownership transfer) would let concurrent requests overlap across stages. Measure first
(A10 overlap trace); keep delivery/loss semantics.*

Status: **instrument and opt-in implementation landed on `lane/rel2-ack`, default OFF.**
Nothing here is evidence of a gain. The flag stays off in V2 until the device A10 trace
(section 8) shows the serialisation and the gain. Line numbers below are at the base
`99d24e17` (`rel1.1/hardening`) unless marked *(now)*.

## 1. Verdict

* The serialisation is real and sits in three stacked places, all of which clear when the
  receiver acknowledges earlier: the Python receive loop, the Rust sidecar's per-connection
  accept loop, and the sender's single ordered forward worker.
* A change that acknowledges **plain hop frames** on receipt, keeps every other frame as it
  is, and keeps `_inflight_received` (cleanup, drain, withdrawal) keyed on EXECUTED is about
  150 lines in one file and needs **no wire or sidecar change**. That is what is built.
* It is **not** loss-neutral. After RECEIVED, the sender no longer learns of a stage that
  dies or hangs before executing from its own failed/timed-out send. Parity needs a third
  signal, EXECUTED sent back to the sender, which is a wire change (section 6). That is
  deliberately not built; it is a reason to keep the flag off until the gain justifies it.

## 2. Today's sequence per hop (cap 1)

Sender node X (stage k) to receiver node Y (stage k+1), one hop of request A:

| # | Step | Where |
|---|---|---|
| 1 | X's dispatcher executes A's hop for stage k, builds the next `HopHeader`, calls `transport.send_hop` | `relay.py:1379` (batch path; `1816` on the synchronous `receive_hop`) execute, then `send_hop` |
| 2 | `_send_or_dispatch`: a hop produced inside an inbound dispatch goes to the single ordered forward queue (`defer_remote_forward`) | `iroh.py:4569`, worker `_forward_loop` `iroh.py:3495` |
| 3 | forward worker sends with `_send_router_frame`; `send_confirmed` **blocks until Y's sidecar returns an ack** | `iroh.py:1602`, `iroh.py:1763`, `iroh.py:2029` |
| 4 | Y sidecar accept loop: reads one stream, admits the frame, then `control.wait_for_terminal()` **before it accepts the next stream** (60 s `STREAM_IO_TIMEOUT`) | `sidecar.rs:2799`, `sidecar.rs:2860` |
| 5 | Y receive loop takes the frame only if `_inflight_received` is empty, records it, queues it on the dispatch queue | `iroh.py:3167`, `3259`, `3268` |
| 6 | Y dispatcher: `_dispatch` -> `enqueue_hop` -> (cap 1) `_queue_hop_for_batch` pumps at once, so the hop **executes inside `_dispatch`** | `iroh.py:3333`, `3907`, `3975`, `3732` |
| 7 | only then `_ack_after_dispatch` -> receive thread `client.ack` -> sidecar `ack_inbound` finishes the terminal -> step 4 returns the remote ack | `iroh.py:3338`, `3599`, `3622`, `sidecar.rs:2372` |
| 8 | Y pops `_inflight_received`, records `_seen`, counts `router_frames_dispatched`; receive loop may take the next frame | `iroh.py:3341` |
| 9 | X's forward worker is free; request B's queued frame goes out | `iroh.py:3495` |

Consequences for two concurrent requests A, B on a 2-stage route: stage compute already
overlaps (X runs B's stage-0 hop while Y runs A's stage-1 hop). What serialises is the
**hand-off**: B's frame cannot leave X (step 3) or be taken by Y (steps 4, 5) until A has
finished executing on Y, so Y idles for B's transfer, admission and queueing after every
request switch, and X's forward worker is head-of-line blocked for every other destination.
The sender's shared data client (`_send_operation_lock`, `iroh.py:1718`) adds the same hold
for entry-originated sends.

For cap > 1 the transport already acknowledges a queued hop before executing it
(`_queue_hop_for_batch` docstring, `iroh.py:3732`); this change makes cap 1 behave like that,
with a bounded window.

## 3. What the ack certifies today

For a plain hop frame the sidecar ack means *dispatched*, and under cap 1 dispatched means
**executed** (runtime ran, next hop handed to the forward queue, token event sent or queued).
It does not mean durable: `PROCESS_LIFETIME_LIMITATION` already states that confirmation and
replay history are process-local. It does not certify cleanup. `DELIVERY_SEMANTICS` is
`remote_router_dispatch_ack`; the string is unchanged so membership and qualifier checks
(`mycelium_node/membership.py:940`, `mycelium_qualification/qualifier.py:1664`) keep passing.
Those checks cover assignment receipts, which are not hop frames.

## 4. Who relies on it

| Consumer | What it needs | Effect of RECEIVED-ack |
|---|---|---|
| Cleanup receipts / withdrawal / drain | no frame of the request still held: `inflight_received_count`, `pending_delivery_count`, `forward_count` (`iroh.py:1133`, `1060`, `route.py:3339`) | **Kept**: a RECEIVED hop stays in `_inflight_received` until EXECUTED, so the count is non-zero until it ran |
| Dispatch evidence | `router_frames_dispatched`, `remote_frames_received`, `_seen` replay table | **Kept**: all updated at EXECUTED, not at receipt |
| Sender delivery failure (`delivery_not_confirmed`, `delivery_deadline_exceeded`, `peer_rotated`, `ack_failed`) feeding A6 liveness and the A7 `forward_failure_observer` (`route.py:1613`, `mycelium_a7_node_wiring.py:369`, `iroh.py:3483`) | a send that returns means Y took the frame; a send that fails means it did not | **Changed**: a frame lost between RECEIVED and EXECUTED no longer fails the sender's send. See section 6 |
| A6 replay / redrive | idempotent hop key; Router `_hop_results` and pending cache return the cached result and never execute twice (`relay.py:1694`, `2098`); the decode driver never re-sends the outstanding hop (`entry.py:936`, `1165`) | **Unchanged**: a re-sent hop is `duplicate_pending` or `COMPLETED` |
| A7 shadow acks (`KVCheckpointAck`, `KVSuccessorCutoverReceipt`) | stage-level signed acks sent after a stage executed; `_a7_await_acknowledged` gates the next decode | **Unchanged**: separate non-hop frames, still acknowledged after dispatch |
| Backpressure / admission | sidecar inbound slots, `adapter_queue_full`, Router scheduler `BackpressureError` | **Kept and extended**: `hop_ack_window` bounds RECEIVED-not-EXECUTED hops; when full the receiver stops taking frames and the sidecar queue (and so the sender's bounded stream) absorbs it |
| Peer rotation fence | `fence_inbound_for_rotation` finishes active controls `PeerRotated` | **Kept** for unacked frames; an already-acked frame is past it |

## 5. The split

Two signals, per hop frame, at the receiver:

* **RECEIVED**: the frame is authenticated, generation-checked, replay-checked, decoded, and
  held in the bounded dispatch queue. Ownership transfers: the sender's send returns and its
  forward worker takes the next frame. Implemented as the sidecar ack, issued by the receive
  thread right after the queue put (`iroh.py` *(now)* receive loop, `early_ack`).
* **EXECUTED**: today's meaning. `_dispatch` returned. Everything in section 4 marked
  *Kept* keys on this: `_inflight_received` is popped, `_seen` and counters updated, only then.

Rules:

1. Only `HopHeader` frames are early-acknowledged (`IrohTransport._early_ack_kinds`).
   Progressive prefill keeps its synchronous lock handshake; manifests, token events,
   failure reports, cancellations, A7 control and assignment frames keep ack-after-dispatch.
2. The window: the receive loop takes a new frame only while no frame still awaits its
   dispatch ack **and** fewer than `hop_ack_window` hops are RECEIVED-not-EXECUTED
   (`_receiver_must_wait_locked`). Default 2, range `1..queue_capacity`. Window 1 is today's
   throughput with an earlier ack.
3. FIFO is preserved: one receive thread, one dispatch queue, one dispatcher. Per-request
   order and one-hop-at-a-time execution on a stage are unchanged.
4. A hop that fails after RECEIVED takes the existing path: scoped failure event plus a
   `FailureReport` to the sender; the dispatcher does not ack a second time.
5. Flag: constructor `hop_ack_on_receipt` or `MYCELIUM_HOP_ACK_ON_RECEIPT=1`; default OFF;
   no node-config plumbing was changed. Window: `hop_ack_window`.

Not changed: wire format, `ROUTER_WIRE_PROTOCOL`, the sidecar and its Python client, the
Router, the entry, A6/A7 modules, `physical_inference_node.py`.

## 6. The loss-detection gap (why the flag stays off)

With the ack at RECEIVED, three failure shapes behave differently for the sender:

| Shape | Today | With flag |
|---|---|---|
| Y dies before the frame arrives | send fails, observer fires | same |
| Y executes and the hop fails | `FailureReport` to sender | same |
| Y process dies after RECEIVED, before EXECUTED | X's send never gets its ack; deadline/rotation failure -> observer -> A7 successor re-send, A6 liveness | X's send already returned. Recovery then comes only from node-level detection: liveness / A4 process-exit claim -> `accept_source_lost` / `cut_over_lost_source`, which re-sends the pending step |
| Y alive but its dispatcher hangs after RECEIVED | X's send times out (`ack_dispatch_timeout`) | detected by liveness / `dispatcher_phase` age and the request deadline, not by the hop |

So a dead stage is still recovered (the A4/A7 path does not depend on the hop ack), but
later for a transport-only observation, and a hung dispatcher loses its per-hop deadline.
Idempotency makes the eventual re-send safe: no token is emitted twice, none is lost.

Full parity needs EXECUTED to travel back: a small `HopExecuted` router frame from Y to X,
with X keeping a bounded received-not-executed ledger that fails entries as
`delivery_not_confirmed` on peer rotation, sidecar loss or a deadline. That adds a message
type to `contracts.py` and `wire.py`, golden vectors, a capability gate (an old peer treats
an unknown type as fatal; members run one pinned closure, so this is a release question, not
a mixed-fleet one) and a second pinned-file change. It is larger than this lane and unjustified
before the measurement, so it is **Phase 2, only if section 8 shows a gain worth the cost**.

## 7. Overlap-trace instrument (built, default off)

`mycelium_router/hop_overlap_trace.py`, wired into `IrohTransport`; switch with
`hop_overlap_trace=True` or `MYCELIUM_HOP_OVERLAP_TRACE=1`. It is independent of the ack
flag so the baseline run is traced too. Per hop, monotonic instants on the recording node:

* sender: `send_requested`, `send_started` (send slot held, frame to sidecar),
  `send_confirmed` (remote ack back);
* receiver: `received`, `exec_start`, `exec_end`, `ack_sent`, plus `ack_mode`
  (`after_execute` or `on_receipt`).

Export: `transport.hop_overlap_records()` and `TransportEvidence.hop_overlap_trace`, which
`physical_inference_node` already serialises into the signed snapshot
`details["transport"]`, together with `hop_ack_on_receipt`. Bounded to 512 hops, first reading
wins, payloads never stored.

Every overlap question is answered on one node's clock, so no cross-host offset enters:

* serialisation signature: a receiver whose hops have `ack_mode=after_execute` and
  `ack_sent >= exec_end`, and a sender whose `send_started` of B equals A's `send_confirmed`
  (`send_blocked_pairs > 0`);
* gain signature: B's `received` and `ack_sent` fall inside A's `[exec_start, exec_end]`
  (`recv_overlapped_pairs > 0`).

`summarize_hop_overlap(records)` returns `verdict` in `serialized | overlapped |
no_concurrent_hops` plus per-node means. Exec start/end bracket `_dispatch`; under cap 1
that is the stage execute, under cap > 1 it is the enqueue (the batch pump is not bracketed).
This is a transport-level seam, not the A10 correlated stage-clock trace
(`a10_overlap_trace.py`), and says so: it shows hand-off serialisation, not physical compute
overlap.

## 8. A10 device measurement to run (parent decides when)

Route: two Macs, a 2-stage Qwen2.5-0.5B route, batch cap 1 (the demo policy), same build both
members, `MYCELIUM_HOP_OVERLAP_TRACE=1` on both.

1. **Baseline** (flag off): two concurrent requests, same prompt length and `max_new_tokens`
   (>= 64 so decode dominates). Record total tokens / wall time per request and combined
   tok/s; collect both nodes' `details["transport"]["hop_overlap_trace"]`.
2. **Candidate** (`MYCELIUM_HOP_ACK_ON_RECEIPT=1`, window 2): identical load.
3. Repeat 3 times each, alternating; single-request run as the control (should not change).
4. Read: `summarize_hop_overlap` per run (expect `serialized` then `overlapped`), per-node
   `mean_received_to_ack_ms` (large baseline, near zero candidate), `mean_send_blocked_ms`,
   and the tok/s ratio. Token ids must equal the baseline's (greedy) for every request.
5. Optional later fault probe (needs a device round): kill the stage-1 process after its
   `received` stamp and before `exec_end`; record time to A6/A7 recovery, flag off vs on, and
   confirm no duplicate and no missing token. This quantifies the section 6 gap.

Decision rule (proposed): enable only if combined tok/s improves by more than run-to-run
spread, greedy outputs are identical, the single-request control does not regress, and the
probe in step 5 recovers within the existing A6/A7 budget; otherwise leave off or build
Phase 2.

## 9. Tests (written before the code; not yet run)

`tests/a10_deterministic/test_hop_ack_on_receipt.py` and `test_hop_overlap_trace.py`, driving
the production `IrohTransport` with the in-memory sidecar client and a gated cap-1 router.

| Requirement | Test |
|---|---|
| (a) today serialises; after, B accepted while A executes | `test_default_off_hop_frame_waits_for_the_previous_execute_ack` (passes on base), `test_on_receipt_accepts_the_next_request_while_the_first_executes` (RED on base) |
| (b) loss semantics | `test_execute_failure_after_received_reports_the_same_failure_once` (both modes), `test_replayed_hop_after_received_is_executed_once`, `test_executed_counters_do_not_move_at_receipt`. The sender-side half (process death after RECEIVED) is **not** covered; see section 6 |
| (c) per-request order | `test_per_request_hop_order_is_preserved`, `test_on_receipt_does_not_execute_two_hops_at_once` |
| (d) bounded window with backpressure | `test_in_flight_window_applies_backpressure_when_full`, `test_window_and_flag_validation` |
| (e) cleanup waits for EXECUTED | `test_cleanup_state_counts_received_hops_until_executed` |
| scope | `test_non_hop_frames_keep_acknowledge_after_dispatch`, `test_only_plain_hop_frames_are_early_acknowledged` |
| instrument | `test_trace_is_off_by_default`, `..._shows_serialisation_...`, `..._shows_overlap_...`, `test_sender_trace_shows_...`, summary and bounds cases |

No pytest has been run (device round in progress). Before enabling anywhere, run these two
files, `test_router_iroh_transport.py`, `tests/a10_deterministic/test_a10_live_hop_batching.py`
and `tests/path_cancellation_adversarial/test_iroh_transport.py`, plus the physical node
snapshot tests that read `details["transport"]` (the evidence gained two defaulted fields).

## 10. Open items

* Phase 2 `HopExecuted` (section 6) if the measurement warrants it.
* The Router's bounded scheduler already rejects with `backpressure:*`; under cap > 1 the
  window does not bound the Router queue because the dispatcher pops the in-flight entry at
  enqueue (unchanged behaviour).
* Pinned files touched and the unpinned new file are listed in the lane report; no manifest
  was re-pinned.
