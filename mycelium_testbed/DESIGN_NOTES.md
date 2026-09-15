# mycelium_testbed — design notes (WP2 item 4, WP5 seams)

**Status:** harness delivered on `integration/core-pilot` (base `b9001e6`). Nothing in `mycelium_*`
was modified: the trace subscribes to seams the existing harnesses already expose.

**Trace schema version:** `mycelium.testbed.trace.v1` (`mycelium_testbed.trace.TRACE_SCHEMA_VERSION`).
A change to the file format is a schema-version bump plus an edit to
`tests/testbed/test_trace_schema_version.py`, which pins the literal on purpose.

## 1. What is in the package

| Module | Contents |
|---|---|
| `trace.py` | lifecycle model (`enqueue`, `delivered`, `started`, `finished`, `receipt`, `terminal`), JSONL run directory + `run.json` header, clock-offset estimation with uncertainty, `summarise(run_dir)` and the timeline renderer, `python -m mycelium_testbed <run-dir>` CLI |
| `iroh_e2e.py` | reuse adapters that trace the *existing* `tests/e2e_request_iroh/harness.run_complete_request` run without a product hook |
| `__init__.py` | public surface re-exported from `trace.py` |

File layout of a run (always outside the repository):

```
<run-id>/run.json                 header: run_id, host_id, boot_id, package_digest, git_head,
                                  start_wall_ns/iso, hosts[], reference_host, event_kinds, notes
<run-id>/requests/<request>.jsonl one file per request
<run-id>/commands/<command>.jsonl one file per command
<run-id>/exchanges.jsonl          optional recorded clock exchanges (see §3)
<run-id>/summary.txt              written by the reuse adapter
```

`package_digest` digests the **harness** sources (so a trace says which tracer produced it);
`product_package_digest` digests the product modules under test (`mycelium_router`,
`mycelium_request_gateway`, `mycelium_iroh_sidecar`, `mycelium_qualification`).

Writers use `O_APPEND` with one `os.write` per line, so several hosts — including several
*processes* — can append to the same request file. `fsync` is opt-in per run
(`TraceRunDirectory(..., fsync=True)`); `run.sync()` forces it. `register_recorder` merges the
`run.json` host list under a `flock`, so two child processes cannot clobber each other's record.

## 2. Why the lifecycle is recorded harness-side today

The packet forbids adding a production hook, so every event comes from an artefact the harness
already produces:

| Event | Source (existing harness artefact) |
|---|---|
| `enqueue` | `IrohTransport.send_router_frame` entry (frame digest, destination, message type) |
| `receipt` | the `DeliveryReceipt` that same call already returns (`message_id`, semantics) |
| `delivered` | `IrohTransport._recv` returning a delivery (`source_node_id`, generation, frame) |
| `started` / `finished` | the router runtime port's `execute` / `execute_batch` (`item.placement_id`, `phase`, `token_index`) |
| request `enqueue` / `terminal` | `run_complete_request` boundaries plus `CompleteRequestEvidence` |

Subclassing (`TracedIrohTransport`) is the seam the harness itself already uses
(`_PausedAfterNativeReceive`). `run_traced_complete_request` temporarily replaces two *test-harness*
factories (`harness._make_transport`, `harness._DeterministicStageRuntime`) for exactly one run and
restores them; no `mycelium_*` module is imported-and-patched, and the product evidence
(`accepted_status`, `router_admissions`, digests) is asserted unchanged by the adapter's test.

Consequence, stated plainly: **a trace produced this way is only as trustworthy as the seams.**
It is enough for the loopback/emulation work of WP2; it is not, by itself, proof of what a
physical host did.

## 3. Clock offset: estimated, bounded, never silently aligned

Paired exchange, NTP-style four timestamps, all taken from trace events:

* `t0` sender `enqueue`, `t1` receiver `delivered`, `t2` receiver `enqueue` (the reply),
  `t3` sender `delivered` (the reply arriving).
* Frame identity is `sha256(frame)` (`frame_digest`); the reply leg is chosen by *within-host*
  ordering only (`t1 <= t2` measured on the receiver's own clock), so a skewed clock cannot
  corrupt the pairing. `t2` also has to be the earliest reply after `t1`, and `RTT >= 0`.
* `offset = ((t1 - t0) + (t2 - t3)) / 2`, `RTT = (t3 - t0) - (t2 - t1)`, `uncertainty = RTT/2`
  (plus the spread across samples for the reported median).
* A four-timestamp estimate is causally safe: the corrected receive stamp is always
  `sender_send + (forward + backward)/2`, i.e. strictly after the sender's stamp, for any constant
  skew. That is why the two-process test can assert causal order and not just proximity.
* Wall offsets are estimated the same way from the wall stamps, so a host with a wrong wall clock
  is corrected in the wall frame too — and its *uncorrected* wall order is still wrong.
* A one-way sample without a caller-supplied `one_way_bound_ns` raises `ClockOffsetUnavailable`.
  `merge_timeline` refuses to interleave a host with no estimate; `summarise` then prints
  per-host sections and says **"hosts are NOT interleaved"** rather than inventing an alignment.

Limitation found in the real iroh run: the reply leg of a request carries real work
(decode ~1-40 ms), which inflates `RTT` and therefore the reported uncertainty (`±40 ms` on a
loopback run). The estimate stays inside its own bound; the bound is simply honest about a
request/response shape that is not a dedicated time exchange.

## 4. Seams needed from product code (for WP5)

None of these are required for the WP2 loopback work; each would replace a harness-side
observation with a stamp taken in the owning process, which is what a *physical* run needs.

1. **Command lifecycle observer.** `started`/`finished` per `(request, placement, phase,
   token_index)` are currently observed by wrapping the runtime port. Required: an opt-in observer
   (or scoped event) on the router's command lifecycle, stamped where the command is executed.
2. **Transport send/receive stamps.** `enqueue`/`delivered`/`receipt` come from a transport
   subclass because `TransportEvidence` and the scoped-event log carry counters, codes and
   digests but **no timestamps**. Required: a monotonic stamp and the local host identity on the
   transport's own send/receive evidence.
3. **Gateway request boundaries.** Request-level `enqueue` and `terminal` are stamped by the
   driver around `run_complete_request`. Required: gateway/service stamps for accepted, first
   token, terminal (and the named failure) so the request timeline survives into a physical run.
4. **A dedicated clock exchange.** Cross-host pairing works only where a reply frame exists per
   request. Physical hosts (17-minute DoH/relay budget, sleeps, App Nap) need a bounded
   ping/pong exchange — e.g. on the mailbox long-poll or the sidecar handshake — carrying its four
   stamps into the trace. Until then, treat a host's corrected order as `±uncertainty`.
5. **Host identity on the wire.** Nothing on the wire currently identifies the *sending host's*
   clock or boot id, so a reader must trust the trace file's own header for `host_id`/`boot_id`.
   A per-host boot id in the frame header (or in the run handshake) would let a merge detect a
   reboot mid-run.

If none of 1–5 land, the trace is still producible for a physical run by running the tracer inside
the owning process and subclassing the same three seams — but then the trace inherits the same
"as trustworthy as the seam" caveat as §2, and the offset estimate stays bounded by §3.

## 5. Known limitations and observations

* `O_APPEND` gives line atomicity for normal-sized lines on local filesystems; a multi-megabyte
  event line could in principle interleave between processes. Event lines are bounded by the
  payload digests we record, not by frame size.
* `sequence` is per *recorder* (one per host per process); the loader de-duplicates the same event
  seen in both its request and its command file by `(host_id, recorder_id, sequence)`.
* Unattributed placements are a loud failure: an executed item whose `placement_id` is not in the
  harness graph raises rather than being dropped.
* Observed on the traced iroh e2e run: harness teardown can race a deferred forward, producing a
  `terminal`-kind event with `status=send_failed:transport_closed` **after** the request reached
  its `completed` terminal. The trace records it; `summarise` prints a `request outcome:` line
  saying the failure did not stop the request.
  **Discriminating check (pre-existing, not caused by tracing):** running the same e2e with no
  tracing at all (`runs/wp2-2/check_teardown_without_tracing.py`) shows node-c's own
  `TransportEvidence.scoped_events` recording `event=failure, code=transport_closed` at
  sequence 11, while node-a records none, and `remote_frames_sent/received = 10/9` for node-c
  versus `9/10` for node-a. So the asymmetry is in the existing harness teardown on this base
  (the untraced evidence dataclass never surfaces it). Repairing it is out of WP2's scope, so it
  is reported here rather than fixed.
