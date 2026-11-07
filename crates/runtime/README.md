<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Runtime lane slice

**Event-driven composition seams, not a running app or full L9 acceptance.**
The `iroh-adapter` tests now launch exact-owned local Rust children and actual
authenticated loopback QUIC endpoints. No models, public relays or devices run.
Oracle: `f4e366e98a1f705dfaf182897044500d290dbd7f`.

## Implemented

- `Session` composes L5 signed receipts with router inbox/attempt/terminal ownership.
  Pull backpressure is checked before producer IO, but cannot hide revocation.
  Native completion never supplies duplicate tokens; queued progress is rechecked
  before classifying native failure/missing terminal. Explicit cancellation closes
  registrations without inventing cleanup or a terminal. Callers must demultiplex
  authenticated native results to the exact request; these are trusted events.
- `Forwarding` composes L5 verified ACK/retirement decisions with fallible storage,
  reader-close, outbound-clear and native-ACK effects. Retirement never reaches
  native receipt eviction, retains pending data and requires a separate native
  executed/reconciliation disposition for ordinary command ACK. Persistence and
  clear failures remain retryable. Durable recovery remains L6 work.
- `shutdown`: cancel chats while admission is fenced, close listener, drain owned
  request gateway, then close registry/router, attempt target cleanup, and only
  then announce stopped. Drain failure still attempts later cleanup; errors remain
  phase-specific. All operational deadlines use L1 budgets capped by the original
  outer deadline. Effect success must mean completion, not task dispatch.
- `discovery`: exact L17 serving-read exception allowlist, independently live and
  admitted runtime required. Reports failed observation, not successful serving.
  HTTP/policy/write/unknown/stale/dead/native-pump failures remain fatal.
- `mode`: default normal, saved next-run choice and explicit on/off environment
  precedence, with strict saved shape. Persistence, role/active-run UI guards and
  actual backend dispatch remain L6/L10 integration work.
- `QuickRelay`: existing L1 bounded-renewal state drives an already-owned resource,
  with immutable origin/resource through renewal. Probe completion resamples time;
  a late probe cannot renew an expired window. Failed probe, final cap and stop
  fence new admission; cleanup errors remain explicit. Fake resource only.
- `worker`: L8 StageBackend sessions fence binding/state generation/input position
  and output generation/committed position. Cancel/synchronize/release/close each
  run despite preceding failure; consumed release errors remain sticky. L5 signed
  quorum, actual L6 Supervisor observation and current exact Ticket all gate proof.
  `settle_worker` calls real SQLite Core::cleaned, whose persisted revision CAS
  fences stale owners after reopen; this does not settle request publication.
  Tests use real Core and interfaces with deterministic backend/environment effects.
- `iroh_wire`: exact Router transfer generation prefix, L5 framing and empty ACK
  correlation; wrong channel/kind/message/payload refuses. `iroh-adapter` adds an
  established-connection-only Iroh carrier with authenticated endpoint/ALPN and
  independently supplied generation fences, absolute operation deadlines, fail-closed
  transport retirement and no automatic infer-start retry. Only Router ALPN is supported; NodeControl
  and ArtifactChunk require their separate protocols. Closing this exclusive
  carrier is transport cleanup, not request/native cleanup proof.
- `iroh_host::LoopbackHost`: owns a local-only endpoint, rejects non-loopback or
  relay addresses, disables relays/address lookup and retains the original close
  task across timeout. Runtime drain/drop is still needed for UDP socket release.
- `iroh_receive::RouterReceiver`: authenticated membership fence, remote transfer
  generation prefix, shared L5 Router-wire decoding and explicit dispatch ACK.
  A held `Delivery` applies backpressure; `acknowledge` is called only after the
  host's dedup/dispatch owner confirms completion. Queue/replay refusal bytes are
  source-derived. This is NOT a replacement for the sidecar's bounded replay table,
  reconnect/retry scheduler, per-stream cancellation or native command receipts.
- `iroh_cleanup::close_endpoint_and_native`: attempts independent native cleanup
  even after endpoint failure, keeps errors visible and exposes a native proof
  only after observed process/resource cleanup and deadline checks. Tests use a
  real nonforking Rust child and owned directory, not an MLX/device worker.
- `rollover::DescriptorGate`: one-shot descriptor-finalization seam; one missing
  usable report with otherwise unchanged member pins fails with
  `rollover_members_changed`. The failure is sticky; no retry was added.
  Characterization is tagged **V03-PLAN lane 3**. Earlier collection error policy
  and the owner-run/public-state driver remain host responsibilities.
- `Published`: effects run outside the published-snapshot lock. Barrier tests read
  progress during blocked work and fence an old generation's delayed publication.
  This is not an HTTP latency or L10 public status-schema claim.
- `simulated::Sim`: concrete scripted implementation of L5 `Transport`, exercising
  its framing/fence/correlation/deadline/cancel/close corpus. Tests are adapted
  from L5 `tests/simulated_corpus.rs`; test assertions are retained. This is **not
  authenticated network IO or deterministic Iroh/QUIC**.

## Integration requests / limitations

1. Foundation exit still blocks main integration. L6-L8 real owner/member,
   provisioning, native worker, planner/artifact and compute paths are not linked.
2. L6 must supply atomic current membership/claim/replay checks, signed retirement
   responses from the retained tuple, durable forwarding restore/clear and actual
   local/remote cleanup observations. No command row alone restores a tombstone.
3. L10 must wire gateway drain before registry teardown, publish independent status
   snapshots and preserve cancellation/terminal SSE ordering.
4. Router recovery and `StageAttempt::replay_prefill` now compose full-context
   replay with L8 identity checks; live scheduling, checkpoint authenticity/storage,
   qualification/fencing and publication handoff remain host integration obligations.
   L1 now supplies the existing two-attempt registry bound. Native infer-start is
   never retried implicitly by the transport.
5. Optional `iroh = "=1.0.2"` (default features off, `tls-ring`) and
   `tokio = "=1.53.1"` (`rt,time,io-util`) compile and link offline in the pinned
   worktree after preview-2 admitted its cached lock; feature default is off.
   Actual two-process local QUIC is exercised with 18 positive / 14 negative L5
   Router-wire vectors plus source-derived transport fault/dispatch cases. This
   is not historical-executable or full sidecar/application interoperability.
   Use a host-owned runtime
   with IO/time enabled, outside a nested Tokio runtime; retain exclusive connection
   ownership until explicit close. Keep IO/time pumping during slow dispatch:
   blocking a single-thread runtime on stdin/native work starves QUIC ACK/close.
   Endpoint close can fail honestly at the caller's original deadline; never
   restart that allowance or treat a later close attempt as the earlier receipt.
   The public relay path is NOT_RUN/BLOCKED by this round's local-only scope.
6. **F10 remains:** a restarted quick relay may have a different URL; retained
   identity does not automatically relocate/reconnect peers to that URL. Existing
   explicit signed endpoint recovery/owner acknowledgement is not replaced by an
   automatic discovery redesign.
7. Relay subprocess terminate/kill/wait/reader-close defaults need L1 IDs and an
   admitted real adapter. The resource trait is not proof of process cleanup.

## Source anchors at the pinned oracle

`token_path_owner.py:117-182`; `token_progress_pump.py:118-135,212-223`;
`token_receipt_mailbox.py:139-219,318-341`; `demo_chain.py:1326-1390`;
`supervisor.py:4443-4459`; `demo_target.py:254-291`; `fast_mode.py:22-37`;
`public_relay.py:387-407`. L14/L15/L16/L17 external reports describe the historical
composition faults; this lane's fault controls are Rust seam/model controls, not
executions of the historical Python app or real device incident reproductions.
