<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Iroh carrier parity boundary — L9 round 4

Reference: read-only v0.1.4 `f4e366e98a1f705dfaf182897044500d290dbd7f`.
All source references below are at that pin. This is a local carrier slice,
**not sidecar retirement, complete owner/member provisioning, or release parity**.

## Available additive APIs

With `runtime/iroh-adapter`:

- `LoopbackHost::bind_with_secret(runtime, clock, deadline, secret)` preserves a
  caller-owned endpoint identity. The host still owns private persistent storage.
  Ordinary `bind` and `bind_with_secret` remain Router-only.
- `bind_protocols(..., secret, &[Channel::NodeControl, Channel::ArtifactChunk])`
  explicitly enables other services. Empty selection refuses. `connect_channel`
  selects one ALPN. Hosts remain Minimal, no discovery, relay disabled, and only
  direct IPv4 localhost addresses; none of these APIs enable public networking.
- `NodeControlClient::new(host, clock, capacity)` and `call(ControlRequest,
  &mut ControlAuthority)` carry raw command/result bytes. Supply a current
  `ConnectionFence` and watch channel; cancellation is `None`, revocation is a
  changed/closed watch. One deadline covers capacity, connect, open, write, read.
- `NodeControlReceiver::new(connection, runtime, clock, fence, current)` binds
  endpoint, ALPN and current authority. `receive` borrows the receiver until its
  delivery is consumed; `bytes()` is **not authenticated command acceptance**.
  Dispatch must validate signed policy/provisioning, replay and durable effects
  before `respond`. The receiver does not duplicate L5/L6 command policy.
- `ChunkSession` borrows the runtime and clock, owns one authenticated artifact
  connection and reuses it across requests. `fetch` takes the signed request,
  expected `ChunkRequest`, admitted L5 `Grant`, and caller deadline. It verifies
  source/recipient endpoint binding and the signed receipt/digest. Any failed
  fetch permanently excludes this session, matching the source acquisition's
  failed-source policy; it does not retry the same request.
- `ArtifactService<ArtifactObjects>` accepts artifact connections asynchronously.
  It applies separate total/per-peer/blocking permits, serial stream dispatch,
  transport-recipient precheck, L5 grant/request authentication, admission,
  bounded pacing, signed receipt, framing and write. Blocking work retains its
  permit through cancellation. An artifact service does not consume Router
  membership slots or use the Router allow-list as artifact authority.
- `ArtifactObjects::verified_size` and `admit_and_read` are an explicit **unmet
  L6/L7 production-storage seam**, not a new in-memory replay store. Implement
  source journal genesis, atomic replay/charge, fsync-before-read, poisoning,
  expiry retention and content-addressed verified reads. The test implementation
  uses actual files/fsync but is intentionally only a fixture, not that journal.

### Bounds and integration prerequisites

Native source-specific registry IDs are missing in preview 3. Consequently there
is no convenient/default constructor silently replacing them with equal-valued
Python budgets. `ArtifactWireLimits` / `ArtifactServeLimits` and NodeControl slot
capacity must be supplied from reviewed L1 entries. Tests use reduced explicit
capacity and input-derived byte caps; this does **not** qualify shipped defaults.

Requested L1 definitions:
- `sidecar.rs:54,66,68-69,87,100,106,1715`: stream 60 s, connect 5 s,
  retry 100 ms doubling to 5 s, enqueue lifetime 300 s, control concurrency 4,
  local sessions 16, seen-limit `max(saturating(capacity*8),64)`.
- `artifact_net.rs:59-61,68-71,87-94`: request 262144 B, header 65536 B,
  IO/connect 30 s each, connections 16, per-peer 2, blocking 4,
  maximum pace 10 s, read 10 s, idle 20 s, replay fence 65536.

The existing Python sidecar-client registry IDs used for NodeControl's 4-MiB
payload and 60-s ceiling refer to that same ControlCall contract, not arbitrary
same-valued budgets. No lock/package/version/root-manifest changes were needed;
Tokio's already locked `sync` and `macros` features are enabled at the runtime edge.

SLICE-1 may consume the raw control and artifact carrier boundaries. Its current
JSON control handler needs an adapter, not a new wire format: retain its signed
payload/dispatch and durable admission, route native control on NodeControl, and
route artifact requests separately. Enrollment/invitation/application provisioning
must not be relabeled complete merely because a signed token-path policy crossed
QUIC. No SLICE-1 file or live app was modified in this lane.

## What v0.1.4 actually ships

### Router connection pool and retry

`native/iroh_transport/src/sidecar.rs:1991-2054,2137-2242,2530-2707`:

1. Pool key is `(EndpointId, generation)`. Connect is bounded at 5 s, verifies
   `remote_id`, and closes a duplicate newly opened connection with code 8,
   reason `duplicate`, while returning the existing one.
2. Each outbound item has one enqueue-time 300-s lifetime. A queue item does not
   acquire a renewed lifetime on retry. Missing primary binding waits on peer
   change/cancel/100-ms tick/deadline. Routed absent destination refuses rotation.
3. One outbound worker uses FIFO items and one attempt at a time. Attempts use a
   fresh bidirectional stream on the pooled connection. Open/write/read each have
   60-s caps inside the item's outer lifetime. Retry delays start at 100 ms,
   double saturating, and cap at 5 s; peer change/cancellation/deadline interrupt
   waiting. There is no fixed retry-count limit replacing that time bound.
4. Connect/open/write/FIN/read/decode/wrong-message-ID failures evict the pool
   entry (close code 8, `retry`) and retry. Empty matching ACK succeeds.
   `peer_rotated` and `replay_collision` are distinct terminal outcomes.
   Other valid responses, including `queue_full`, retry **without eviction**.
5. Terminal compare-exchange is first-wins. Cancel removes the exact token/queued
   item, signals cancellation and releases its permit before worker traversal.
   Write/read cancellation resets send and stops receive with **code 7**, without
   closing the shared connection. Peer replacement fences active work and closes
   outbound pools; it is not a reconnect/membership recovery grant.

Round 4 ported the exact response classifier and wire outcomes. **Round 6
adds the scheduler as a deterministic state machine,
`runtime::sidecar_pool`** (no socket, task or clock; the QUIC driver passes
explicit `MonoInstant`s and attempt outcomes): `(EndpointId, generation)` pool
registry with duplicate/evict/rotation-close decisions, FIFO outbound queue with
one enqueue-time lifetime per item, the 100-ms-doubling-to-5-s wait (first wait
unclamped, later ones clamped, never beyond the item deadline), binding waits,
first-wins terminals, `cancel` that removes the exact token/queued item and
releases capacity before worker traversal, per-attempt `Cancelled` that leaves
the shared connection pooled (the driver resets send / stops receive with code 7),
io failures evicting (code 8, `retry`) vs valid-but-not-ACK responses retrying
without eviction, and `ConfigurePeers` rotation fencing. All bounds come from
caller-supplied `PoolLimits` (native L1 IDs are still requested). **Round 7 adds the
QUIC pooled driver, `runtime::iroh_pool::PooledRouter`** (feature `iroh-adapter`;
see "Pooled QUIC driver (round 7)" below). The older exclusive `IrohAdapter`
closes its dedicated connection on cancellation; that is not pooled cancellation
parity. No new
recovery behavior was added: there is still no retry-count limit, no membership
recovery on rotation and no persisted replay.

### Router replay and local delivery

`sidecar.rs:1532-1589,1672-1715,1948-1984,2137-2407,2786-2902`:

- Outbound active ID plus same payload digest shares its control; changed bytes
  collide. Active outbound tokens are removed at terminal, not kept as a durable
  permanent replay ledger.
- Inbound current generation and rotated-ID fencing precede capacity admission.
  Same-ID/same-digest active duplicates wait on the same dispatch completion;
  completed duplicates receive ACK; changed digest is `replay_collision`.
- Completed and rotated ID history is FIFO-bounded by the seen limit. It is
  ephemeral process state, not L6 persistent replay or unlimited exactly-once.
- Receive moves a pending item to a local session's inflight queue. ACK must name
  that session's inflight ID; only completed dispatch creates completed history
  and allows the remote ACK. Local hangup redelivers inflight items to the queue
  front in original order. Rotation clears pending/inflight and marks IDs rotated.
- A 60-s wait for dispatch completion that expires resets/stops that stream; it
  does not silently perform the effect or acknowledge it. Invalid outer framing
  resets streams; valid outer framing with wrong generation returns `peer_rotated`
  ahead of inner-frame validation. Inner invalid bytes return `invalid_frame`.

Round 6 ports the history/session half in `runtime::sidecar_pool`
(`admit_inbound`, `receive` into per-session inflight, session-bound `ack`,
`redeliver_session` to the queue front in original order, FIFO-bounded completed
and rotated history, rotation fence). Round 7 implements the 60-s dispatch-completion
wait that resets/stops the stream, the generation-before-inner-frame ordering
and the ACK/`queue_full`/`replay_collision`/`peer_rotated`/`invalid_frame`
reply mapping in `PooledRouter` (below). The existing `iroh_receive` receiver is
unchanged.

### NodeControl is not Router pooling

`sidecar.rs:88-104,1428-1476,1749-1817` and
`native/mobile_core/src/node_control.rs:53-60`:

- Remote ALPN is `mycelium.node_control.v1`. Remote bytes are exactly the raw
  command JSON until FIN, then raw observation JSON until FIN. No Router header,
  generation prefix or artifact length prefix is added.
- The sidecar's **local IPC** ControlCall payload has destination bytes + u64-BE
  timeout + command. The Rust direct API replaces that local IPC hop; it does
  not send that local record wrapper onto QUIC.
- Four separate control slots; 4-MiB command/result cap; caller timeout at most
  60 s. One attempt, one call-owned connection, no Router retry or replay policy.
- All waits share one deadline. Authority is checked after capacity, connect and
  response. Biased watch interruption and final changed check reject same-turn
  stale settlement. Dropping the call closes the connection code 7 with reason
  `control call ended`, including success, failure, hangup and revocation.
- Transport possession is not signed command authorization. Receiving native
  service owns controller pinning, command validation, replay and execution.

### Artifact acquisition and durable admission

`native/mobile_core/src/artifact_net.rs:118-258,292-299,360-474,540-609`:

- ALPN `mycelium.artifact_chunk.v1`; one canonical signed request per
  bidirectional stream. Response is u32-BE header byte length, canonical JSON
  `{protocol,status,receipt|code}`, then raw bytes. Receipt binds the source key,
  grant, request, object, range and exact received-content digest.
- Acquisition caches live connections by endpoint. Closed cached connections are
  redialed before a later fetch. Each attempt creates a random 12-byte nonce and
  incrementing `chunk-N-<first-four-random-bytes-hex>` request ID. Sources are tried
  in configured order; **any failure excludes that source for the acquisition**,
  removes its connection and proceeds to the next. No exponential Router retry.
- This round supplies one-source session reuse/exclusion and signed wire service.
  Multi-source selection, cryptographic nonce generation/request-ID sequencing,
  connect-cache replacement and acquisition orchestration remain a caller seam.
- Receiver first compares signed grant recipient text to QUIC `remote_id`, before
  expensive signature or disk work. Source global/per-peer/work budgets are
  independent; one advertised bidirectional stream and serial request handling
  prevent per-connection fan-out. Busy connections close code 2; wrong ALPN code
  1; completed service closes code 0. Heavy verification/read uses blocking work.
- Admission hashes `grant_id + NUL + request_id + NUL + request_nonce` into a
  replay key. Under one lock it checks ledger availability/genesis, prunes only
  expired request fences, refuses live-capacity/replay/byte exhaustion, appends
  one complete JSON line and fsyncs, then charges and reads. Failed read/send
  remains charged. Append failure poisons the ledger until rebind.
- Reopen compacts still-live grant rows via tmp + fsync + rename + directory
  fsync. Only a torn last non-newline record is ignored; other corruption refuses.
  Grants older than ledger genesis refuse after reset. Rate pacing derives from
  charged bytes and first-serve instant; transport caps each sleep at 10 s.

No durable restart/pruning/poisoning claim follows from this round's test fixture.

## Qualification and the 42-versus-32 distinction

`transport::qualification::ROUTER_NATIVE_CORPUS_TSV` exports **42 outer native
frames**, imported from L5 `571c0436`. Every row crosses actual authenticated
QUIC between two Rust OS processes and checks exact codec/outcome values. This
is separate from the already qualified **18 positive + 14 negative inner Router
vectors**. Counts are not alternative totals for the same layer.

New two-process tests cover signed token-path control and tamper refusal,
per-call control connection disposal, empty/oversized result refusal, held-result
cancellation/rotation, signed artifact grant/request/receipt, exact raw payload,
connection reuse, replay refusal via actual fixture fsync, transport-recipient
precheck precedence, signature failure, delayed admission, truncated/wrong-protocol/
trailing/tampered responses and failed-source exclusion. Every fixture reaps its
exact child and verifies socket release. Seven semantic mutants are retained in
external `L9-evidence/r4-m-*` evidence; mutation failures are assertion failures
after compilation, not the separate host-admission hold.

This is not an invocation of the historical sidecar executable, a physical Mac
run, a full NodeControl dispatcher, loaded-model Iroh evidence, or an L2/L13 gate.

## Exact future two-Mac / relay checklist — BLOCKED, NOT RUN

Requires separately authorized idle devices, non-demo private state/ports and
reviewed default budgets. Do not run against either live v0.1.4 app/demo. Before
calling any carrier retired:

1. Freeze both source/build/lock/toolchain and fixture hashes, exact executable
   identities, persistent signing/transport key fingerprints, budgets, model
   profile and approved devices. Record read-only baseline binary provenance;
   test Rust/Rust and both baseline/Rust directions, not only shared-code loops.
2. Run direct LAN, relay-only, direct-to-relay and relay-to-direct on explicitly
   approved relay configuration. Observe actual selected path, remote ID, ALPN,
   RTT, reconnect/path-change counters and bytes. Unknown path is not relay proof.
   Do not enable N0/discovery merely to make an unavailable route pass.
3. Repeat all 42 outer and 18+14 inner frames, control and artifact positive/
   negative suites. Test wrong key, ALPN, generation, message ID, signature,
   recipient, expired grant, range/digest and malformed/truncated/oversized bytes.
4. Saturate exact source-default queue, total/per-peer/control/work/history caps.
   Hold dispatch and disk completion with explicit barriers. Show control,
   artifact and Router do not starve each other's independent capacity; memory
   remains bounded and no ACK/receipt/token precedes authoritative acceptance.
5. Implement/qualify source pool first: multiple frames reuse one identity+generation
   connection; valid retry responses retain it, IO/decode/ID failures evict it.
   Inject dropped ACK, peer outage and delayed binding; observe 100-ms-to-5-s
   backoff and original enqueue-time 300-s deadline without late delivery.
6. Cancel one stream while a sibling uses the same pool: verify RESET/STOP code 7,
   sibling continuity, first-wins terminal and immediate permit release. Separately
   verify whole-call NodeControl code-7 close and no retry. Rotation must interrupt
   held IO and forbid stale settlement, without introducing v0.3 recovery.
7. Restart source/owner separately: persist endpoint keys and document F10 address/
   retained-peer limitations. For artifacts crash at append/fsync/read/write,
   reopen and prove replay/budget retention, torn-tail/corruption rules, poisoned
   journal refusal, cap refusal and genesis fencing. Do not credit refunds on
   failed sends or eviction of live fences.
8. Compose real L6/L7 signed enrollment/provisioning, selected assignments, L8
   activation/token execution, native cancellation and cleanup, then L2 transitions.
   Preserve token sequence, generations, exact terminal ownership and descriptor
   failure semantics; socket closure alone is not worker/GPU disappearance proof.
9. Qualify the **separate HTTP quick-relay F13 lifecycle**: same URL and process
   through rollover/renewal, original immutable grant cap, failed/false serving
   probe and final stop refusal/cleanup. Iroh relay path evidence cannot substitute.
10. Record real stop/close/TTFT and throughput samples (not invented p99), both
    hosts' non-secret diagnostics, exact-owned PID/socket/directory cleanup and
    immutable evidence seals. Missing authorization, path, oracle or peer is
    BLOCKED, never a simulated green or a reason to touch the live demo.

## Pooled QUIC driver (round 7)

`runtime::iroh_pool::PooledRouter` applies `runtime::sidecar_pool` to real Iroh
connections on the host's local-only endpoint (direct IPv4 loopback addresses; no
relay, discovery or n0). Tasks run on the host's runtime and progress while it is
driven. All bounds are caller-supplied in `DriverLimits` (`PoolLimits` plus
`connect` and `stream_io`); the shipped 5 s and 60 s values still have no native L1
ID, so no default exists. Source `sidecar.rs @ f4e366e9`:

- Outbound (`reusable_connection`, `send_outbound_once`, `outbound_worker`): one
  worker, FIFO, one attempt at a time; pool key `(EndpointId, generation)`; dial
  capped by `connect`, `remote_id` verified (`identity` close), a concurrent
  duplicate is closed with code 8 `duplicate` and the existing connection used;
  open/write/read each capped by `stream_io`, all inside the item's single
  enqueue-time deadline; cancellation resets send and stops receive with code 7 and
  leaves the pooled connection open; io/decode/id failures evict (code 8 `retry`),
  valid non-ACK responses retry without eviction; `peer_changed`, cancellation, the
  backoff/binding tick and the deadline wake waits; rotation closes the pool with
  code 8 `peer_rotated` and ends held items as `PeerRotated`.
- Inbound (`incoming_worker`, `handle_remote_connection`): connection slots =
  queue capacity; `unconfigured`/`identity` closes (code 8); a peer-table publish
  closes every inbound connection (`peer_rotated`, code 8); streams on one
  connection are served **strictly serially**, so a stream waiting for its local
  dispatch blocks later streams on that connection (and, as in the sidecar, an
  item whose wait expired stays pending and keeps its capacity). Wrong generation
  is answered `peer_rotated` before the inner frame is judged; invalid inner frame
  `invalid_frame`; then `admit_inbound` outcomes map to ACK / `queue_full` /
  `replay_collision` / `peer_rotated`. An admitted item answers only after the
  local `ack` (or a rotation fence); if the wait (`stream_io`) expires the stream is
  reset/stopped with code 7 and nothing is acknowledged.
- Local surface: `enqueue`/`cancel`/`wait_terminal`/`outbound_free`, `receive`/
  `try_receive`/`ack`/`redeliver_session`, `configure_peers`, `stats` (dial and
  dial-failure counters), `shutdown` (must finish and the router be dropped before
  `LoopbackHost::close`).
- Round 7's one divergence (a dial finishing after rotation was closed instead of
  parked under the dead key) was **removed in round 8**: see "Round 8" below.
- Round 7 did not port transport observations, the artifact-ALPN branch or local IPC
  framing; round 8 does (below). Still no retry-count limit, no membership recovery,
  no persisted replay (unchanged).

Tests: `tests/iroh_pool_process.rs` (12 incl. the fixture entry; two OS processes
over loopback QUIC, an exact-owned child as scripted acceptor/sender/mute peer).
Verified: pooled reuse across frames and `queue_full` retries without eviction with
the doubling wait observed; io failure -> code 8 `retry` + redial; cancel -> stream
STOP code 7, immediate permit release, sibling item on the same connection; enqueue
-time lifetime with no later attempt; connect bound (not stream bound) caps dials;
rotation -> code 8 `peer_rotated`, held item `PeerRotated`; inbound reply order,
ACK only after the local ack, completed re-ACK, `replay_collision`, serial stream
behaviour, `queue_full` by held capacity; dispatch-wait expiry -> reset code 7, no
ACK, retransmit joins the pending item exactly once; `unconfigured`/`identity`
closes; inbound rotation fence; an idle inbound connection is closed (code 8 `peer_rotated`) when the table is republished (the survivor of mutant M15 in `r7-pool-mutations.txt`, killed by this test). The 60-s wait is exercised at a caller-supplied
1.5 s: the literal 60 s path is the same code with a different `Span`.

## Round 8: rotated-dial ruling, artifact ALPN, local IPC, telemetry

**Rotated-dial ruling (integrator): ported exactly, wire-observable.** v0.1.4
(`sidecar.rs:2027-2048`) inserts a dial that completes after its binding was rotated
away into the pool under the dead `(EndpointId, generation)` key and leaves it open;
only the next effective rotation (`close_outbound_connections`, `peer_rotated`,
`:2109-2114`, which takes the WHOLE map) or shutdown closes it. A peer therefore sees
an open, idle connection (holding one of its inbound connection slots) instead of an
immediate code-8 `peer_rotated`: close frame, close timing and peer slot use all
differ, so round 7's early close is gone. The race is a `tokio::select!` between the
item's cancellation and the dial (random branch order, same in v0.1.4), so it cannot
be forced from outside; `iroh_pool::tests::v03_a_dial_finishing_after_rotation_is_parked_under_the_dead_key_and_stays_open`
forces the ordering through the driver's private `reusable_connection` and observes
the real second endpoint: connection parked (`pooled_connections()==1`), peer
`closed()` does not fire in 600 ms, then the next rotation closes it `code=8
reason=peer_rotated`. V03: parking under a dead key is a leak a later generation
should close. RED `r8-red-v03-divergence.log`, GREEN `r8-green-v03-divergence.log`;
re-adding the binding re-check is mutant M23b (killed). Rotation now closes the whole
pool map, not only registry keys (mutant M23x is equivalent: the registry mirrors the
map).

**Artifact-ALPN branch** (`sidecar.rs:2731-2744`; `Services::artifact`,
`ConnectionService`, `ArtifactService<S>` implements it). On the artifact ALPN the
Router connection slot is released at once (a held slot would starve Router peers);
with a service the connection is handed over whole (the service enforces its own
limits and closes), without one it is closed code 8 `alpn`; Router frames written on
an artifact stream are never admitted. Any other ALPN is closed `alpn`: v0.1.4 would
run its Router admission on it, but it can only be reached there if the endpoint
registers an extra ALPN, which `bind_endpoint` (sidecar.rs:834-868) never does;
rejecting is the safe reading of an unreachable path. Tests: 6 + 1 artifact
connections with half-open request streams on a 2-slot driver never take a Router slot
and a configured Router peer is still admitted and ACKed
(`artifact_connections_go_to_the_artifact_service_...`); `the_artifact_alpn_with_no_service_...`.
Mutants M16 (slot kept) and M17 (branch removed) killed.

**Local IPC framing** (`runtime::sidecar_local`, default features, pure): record kinds
1-19, 30-byte header + HMAC-SHA256 tag, `SequenceGuard` (no wrap), HMAC handshake
(client/server proofs, HKDF session keys), `parse_send` (plain / confirmed / routed
payloads and their `invalid_peer` / `invalid_generation` codes), delivery payloads,
`{"code":..}` error payloads, `ControlCall` parse in the sidecar's check order,
`validate_router_ingress`. HMAC/HKDF are built over `protocol`'s SHA-256, so there is
**no dependency or lock change**. Expected values come from an independent stdlib-only
generator (`L9-evidence/r8-vector-generator.py`, fixture
`fixtures/sidecar-local-vectors.json` sha256 in the evidence seal), not from this code:
38 record vectors, handshake proofs/keys (the oracle's own test inputs), 3 HMAC
vectors (RFC 4231 case 1/2 shape + a key longer than a block), negative records, 6
control-call cases, 17 error payloads. Two oracle facts pinned that a reader would not
guess: a bad tag is `invalid_proof` (the `invalid_record_tag` code exists but decode
never produces it), and an unknown *kind* with a valid tag still consumes its sequence.
**Round 9 supersedes the "Python fixture" provenance above and ports the rest of the process
boundary; see "Round 9" below.**

**Telemetry** (`runtime::sidecar_telemetry` + driver): per-`(peer, generation)`
observation state (attempts counted at attempt start, failures at eviction, opens and
reconnects, frames/bytes/elapsed on an empty matching ACK, selected path class and
RTT window of 64, cold RTT), the byte-exact observation/envelope/admission documents
(struct field order, `serde_json` floats), `transport_connection_opened` and
`peer_connect_deadline_exceeded` event lines (keys sorted: the oracle's `json!` has no
`preserve_order`), identity-rejection and first-admission counters (an `unconfigured`
close is not a rejection; a duplicate joining a pending item is not an admission).
Wall time (`measured_at_unix_ms`) is read from `SystemTime` like the sidecar, so
driver tests keep a Clock that refuses wall time. Tests: 4 telemetry/counter
two-process tests; mutants M18-M22 killed. A dial cut off by the item's own lifetime
counts an attempt but no failure (loss ratio below 1 on a mute peer), as in v0.1.4.

## Round 9: oracle-executed vectors, M23x, control hook, session loop, process boundary

**Vector provenance.** The round-8 vectors came from an independent Python generator, which
can share a misreading with the port. Round 9 builds the oracle's own crate offline
(`native/iroh_transport @ f4e366e9` copied to scratch, never modified; a `vector-dump` binary
added to the COPY calls the oracle's real `hmac`/`derive_session_keys`/`encode_record`,
`handle_local_session`, `process_local_record`, `control_call`, `read_bootstrap_secret`,
`bind_secure_uds`, `authorize_loopback_client`, `bind_endpoint`, ...) and records every
input and output. `fixtures/sidecar-oracle-vectors.json` + `sidecar-oracle-events.txt` are
that dump; harness sources, build log and the byte-for-byte comparison with the Python
fixture are `L9-evidence/r9-oracle-*`. The Python fixture is now only the last cross-check
test (`sidecar_oracle_vectors`, 7 tests). The comparison found the HMAC, handshake proofs,
session keys, record encodings and observation documents equal.

**M23x was NOT equivalent** (round 8 had called it so). A dial finishing between the state
rotation and the pool-map close left the registry holding a key the map lacked. Reproduced
deterministically (`#[cfg(test)]` seam between the two steps, `r9-red-m23x.log`); fixed by
making rotation and eviction one critical section (lock order connections -> state, the
order `reusable_connection` already uses). Seeded randomized dial / evict / rotate
interleavings assert registry keys == map keys at every point
(`m23x_registry_and_map_hold_the_same_keys_across_randomized_dial_evict_rotate_interleavings` and the deterministic `m23x_a_dial_landing_between_...`, green 3 times).

**Non-Router ALPN.** v0.1.4's Router-admission path for a non-Router ALPN is unreachable on
the wire: `bind_endpoint` registers only the Router ALPN (and the artifact ALPN when
configured), and the oracle's executed negotiation shows an unregistered ALPN fails the
handshake before any admission code runs (`alpn_negotiation` in the dump; two-process test
`an_alpn_the_host_did_not_register_fails_the_handshake_exactly_like_the_v014_endpoint`). The code-8 `alpn` close is kept
for the registered-but-unserved case only.

**`Services::control` (SLICE-1 hook; NOT a v0.1.4 behaviour).** v0.1.4 never served NodeControl
inbound; v0.2 runs NodeControl over Iroh, so the host may register a `ConnectionService` for
the NodeControl ALPN. The connection goes whole to the service after the permit is dropped:
no Router slot, no peer-table gate (the service authorizes callers by signed payload, like the
artifact service). Unserved -> code-8 `alpn`. RED-first two-process tests
(`control_connections_go_to_the_control_service_...`, `a_registered_but_unserved_control_alpn_...`; RED `r9-red-control-alpn.log`).

**Local-IPC session loop and dispatcher** (`runtime::sidecar_session`, iroh-adapter): the
`handle_local_session` + `process_local_record` port over `PooledRouter`: handshake with the
2 s window, authenticated records, send/confirmed/routed, receive polling, configure-peers,
transport observation / admission-counter requests, ping, `ControlCall`. The session replays
57 oracle requests byte for byte (`sidecar_session::tests`), including handshake failures,
post-handshake ends and the pending-ControlCall endings (`peer_rotated` on rotation, carrier
connection closed code 7 `control call ended` on rotation and client hangup, slot freed).
`ControlCall` executes over the pooled NodeControl carrier (`enable_control_carrier`, 4
concurrent, 4 MiB response; before enabling it answers `carrier_unavailable`).
Caller-supplied: server nonce, `SessionLimits` (shipped 2 s / 250 ms / control ceiling).

**Process boundary** (`runtime::sidecar_process`, `runtime::sidecar_listener`; safe code, no
lock change beyond rustix features): bootstrap secret over an inherited pipe (exactly 32
bytes then EOF; consumes and closes the descriptor), endpoint-secret file rules (0600, owned,
regular, no symlink), owner-only Unix socket (parent forced 0700, stale own socket replaced,
foreign path refused, socket 0600), peer-uid check, socket cleanup guard, loopback listener
(127.0.0.1, ephemeral port) with pid-owner authorization (`MIB_TCPTABLE_OWNER_PID` and
`netstat` row decoders; any lookup failure refuses), POSIX/loopback ready lines, `address_changed`
lines, the bounded address-readiness gate and the republisher (never repeats the value `ready`
carried), and both listener loops (a session slot is taken BEFORE accepting; rejected peers
are dropped before any handshake byte). Vectors executed from the oracle
(`sidecar_process`, 7 tests) plus two-process tests: an exact-owned child is served over the
owner-only socket; a child receives its bootstrap secret over its inherited stdin pipe.
Mutants M30-M34 (no session cap, no loopback authorization, no destination-point check, wrong
refused-frame code, trailing bytes accepted) killed (`r9-listener-mutations.txt`).
Divergences (fail-closed): the oracle ABORTS (exit 134, std IO-safety) on a bootstrap fd
number that is not open (`r9-oracle-closed-fd.txt`); `read_bootstrap_secret` takes an
`OwnedFd`, so raw-number adoption is **seam S-L9-2** for `node::fds` (the audited-unsafe crate;
`runtime` may not carry unsafe code, `spawn_boundary_ownership`): `adopt_inherited_descriptor(fd)
-> io::Result<OwnedFd>` returning an error for a negative or not-open descriptor. Secrets are
wiped best-effort (no `zeroize`). Listener loops end on a caller future, not Ctrl-C.
**Still not ported:** `run_sidecar` itself (endpoint binding order, ready write, shutdown
choreography: SLICE-1 owns it) and the artifact-source validation (`artifact_source_invalid`).

**L1b ids.** The 16 sidecar ids are committed on `v02/l1b-inventory` (`b610e2c`, tip
`f2b2322`) but NOT on this branch: cherry-picking conflicts in the shared
`crates/budgets/src/table.rs` and the branch also lacks L1b's 12 SLICE-1 ids. `DriverLimits`
/ `PoolLimits` / `SessionLimits` / `ControlCallLimits` therefore stay caller-supplied; the
id -> field mapping is `L9-evidence/r9-l1b-id-mapping.txt` (a one-line switch after integration).

**Lint and result handling (downstream-merge finding).** The head `4cbe29d` carried ~25 xtask
violations that were fixed after it and had not been committed (SE001 discarded results,
SE004 discarded `JoinHandle`s, SE003 empty `Err` arm, TM001 literal `Duration`). Fixed in
code, no allows: dial results in the M23x test are counted and the sum asserted;
`enable_control_carrier` returns whether it enabled (callers assert it); phone-side QUIC
stream operations whose failure is expected when the sidecar already ended the call go
through `observed(..)` which prints the error to the test log; sessions are joined and
asserted `Err(Disconnected)`; per-connection phone tasks are held in a `JoinSet`; the
watch senders / republisher task are `expect`ed; the stale-socket match is restructured so no
`Err` arm is empty; the handshake bound comes from `SessionLimits`. xtask lint: 678 files,
0 violations (`r9-gate-lint.log`).

**"parent socket leaked" was not a leak (round-9 finding).** `iroh_pool_process.rs` proved
release by re-binding the freed port; on this shared host an unrelated process took the
port (a rebind probe in a process with no children at all fails 5 of 430k and 2 of 226k
trials: `r9-leak-foreign-squatter-experiment.txt`; 2 of ~13 loaded runs of the real binary
failed with an EMPTY `lsof` at failure time and no fixture child of this process, i.e. the
holder was transient and foreign). The fork-to-exec residual cannot explain it: the probe
took the same fork lock `Peer::spawn` holds across `fork`+`exec`, so no child of this
process is ever between the two while the probe runs, and `confine_descriptors` marks every
inherited descriptor close-on-exec. The probes are replaced by kernel descriptor tables
(`lsof -p`): a fixture child may hold no inet socket except its own announced one (checked at
readiness, while the parent's sockets are open), and "released" means THIS process's own
table no longer holds the port. Both are exact and immune to other processes. Tests:
`an_unconfined_spawn_leaks_a_parent_socket_and_the_child_table_check_sees_it` (a bare spawn
inherits a gap-state parent socket and the check names it; the confined spawn inherits
nothing), `the_old_rebind_probe_called_a_squatter_a_leak_but_the_table_check_does_not`
(deterministic false positive: one unrelated squatter on one freed port). Mutant: production
`Peer::spawn` unconfined -> 21 of 22 tests fail on `child inherited sockets`
(`r9-red-unconfined-peer-spawn.log`). The lsof calls cost CPU next to the wall-clock
assertions of the round-7 timing tests (`the wait must grow`, `an attempt began after the
enqueue-time deadline`), which fail under host load averages above ~14 with or without the
check; the checks were batched to one `lsof` per test to limit that.

**Timing tests made deterministic (round 9).** `lifetime_is_the_enqueue_time_bound_...` and
`one_pooled_connection_...back_off...` failed 2/10 and 3/15 at host load averages of 14-31 because
they compared timestamps taken by the CHILD process (its scheduling delay lands inside the
driver's bound) and upper-bounded waits on wall-clock sleeps. The driver now records, on its
own injected clock, every attempt decision (`PooledRouter::attempt_decisions()`,
`AttemptDecision { decided_at, deadline, backoff_before }`, last 256): `decided_at` is the
reading the state machine saw when it chose to attempt, `deadline` the item's ENQUEUE-time
deadline, `backoff_before` the pool's planned wait (`wake_at` minus the failure's reading).
The tests now assert exactly: every decision has `deadline.remaining(decided_at)` Ok (the
state machine's own predicate), one identical deadline for all retries (no renewal),
deadline = first decision's enqueue + lifetime, planned waits equal the doubling schedule
(200 ms then the 400 ms cap), each decision follows the previous by at least its wait (a
timer never fires early), and no attempt is decided after the terminal. Only lower bounds on
real elapsed time remain (`elapsed >= 1499 ms`), which load cannot violate. An attempt that is
already decided still begins and is counted if the scheduler delays it past the deadline
(`timeout_at(deadline, send_once)`; v0.1.4 behaves the same), so "no attempt STARTS after
the deadline" is a statement about decisions, which is what the pure pool tests
(`tests/sidecar_pool.rs`) pin and this records through the real loop. Mutants M35 (first wait
is the cap) and M36 (`advance` ignores the deadline) killed (`r9-decisions-mutations.txt`).

## Child-process descriptor inheritance (round 6; consolidated in round 7)

**v0.1.4 did not have this exposure; the Rust port does.** Python's
`subprocess` defaults to `close_fds=True` (the node worker passes an explicit
`pass_fds`; the relay/service/mobile spawns use the default), so no child keeps a
parent socket even when it is forked inside the `socket()` / `FD_CLOEXEC` gap
that macOS cannot close atomically, and, in practice, CPython's GIL serializes a Python-level close against
fork. `std::process::Command` has neither guard: a child forked inside the gap
keeps that socket for its whole life (closed port stays bound, rebind fails
`AddrInUse`, a closed accepted connection sends no FIN), and a child forked
while a socket is open holds a copy until `exec` (the S23 release window).

Measured (`L9-evidence/r6-*`): while a thread creates and drops UDP sockets
continuously, 120 spawned children are inspected with `lsof` (the kernel's own
descriptor table; an earlier rebind-probe count was discarded because unrelated
processes' sockets on the same ephemeral ports produced false positives). 20
runs each, `r6-natural-leak-20.log`: unconfined children held leaked UDP
descriptors in 13/20 runs (43 total); children spawned with `confine_descriptors`
in 0/20 (about 250k socket creations per run). Deterministic pins: a socket in
the gap state is still bound after the parent closes it when the production
`NativeChild` spawns a plain `Command`; it is released with the helper (UDP and
TCP listener).

Round 6 shipped `runtime::child_fds::confine_descriptors`. **Round 7 removed it**
(integrator ruling): `node::process::confine_descriptors` (L6, rustix, fails closed
when marking fails, applied by `NativeChild::spawn` before any owned resource
exists) is the single canonical spawn boundary and `runtime` has no production
spawn site, so a second `pre_exec` implementation would only drift. The guard is
`tests/spawn_boundary_ownership.rs` (scans `runtime/src` for `pre_exec`,
`confine_descriptors`, `FD_CLOEXEC`, `allow(unsafe_code)`; RED
`r7-red-dedupe.log`, GREEN `r7-green-dedupe.log`); the descriptor tests in
`tests/child_fd_inheritance.rs` now drive `NativeChild` with a plain `Command`
and pin the exposure against bare `std::process::Command::spawn`. The helper
cannot shorten the fork-to-exec interval, so code that rebinds immediately after
closing must still be ordered against spawns (as the fixtures are, via the S23
fork lock; the two-process pool tests also confine their children with the
canonical function).

Production spawn sites (read-only for crates this lane does not own):

| Site | Owner | Sockets open in-process? | Exposure | Status |
|---|---|---|---|---|
| `node::process::NativeChild::spawn` | L6 | callers: yes (iroh endpoint, listeners) | permanent leak + release window | seam S-L6-1 (patch validated) |
| `node::recovery::GroupChild::spawn` (`process_group(0)`) | L6 | yes | same | seam S-L6-1 |
| `node::guardian::PersistentChild::spawn` -> guardian -> `GroupChild` | L6 | yes; guardian then spawns the worker | guardian AND worker inherit | seam S-L6-1 |
| `backend_mlx::process::OwnedWorker::spawn` (`command.spawn()` on a supervisor thread) | L8 | yes (multi-threaded caller) | same | seam S-L8-1 |
| `node-service/src/native.rs` member / owner-leaf `Command::new(current_exe)` | SLICE-1 | yes: TcpListener, accepted sockets, iroh wire host UDP | same | seam S-SLICE1-1 |
| `node-service/src/native.rs::string_command` (`.output()`) | SLICE-1 | yes | short-lived (ms); release window only | seam S-SLICE1-1 |
| `device-harness/src/native.rs` | device lane | no sockets (budgets-only crate) | none observed | none |
| `desktop/src/platform.rs::probe` (`sw_vers`) | L11 | startup probe | short-lived | optional S-L11-1 |
| `runtime`/`router` | L9 | - | no production spawn site; no confinement code (round 7) | none |

Exact requests: `L9-evidence/r6-seam-requests.json` (S-L6-1 is satisfied by L6's own
`df56b8d`, merged through integration preview 5); validated L6 patch:
`L9-evidence/r6-seam-l6-node-descriptor-confinement.patch` (RED/GREEN logs
`r6-seam-l6-red.log` / `r6-seam-l6-green.log`, full `-p node` suite green).

## Round 13: HMAC crates, entry point, quick-tunnel resource

- **CR001 / RU-010.** `sidecar_local::hmac_sha256` and the tag check are `hmac` 0.12.1 + `sha2` 0.10.9 (`Hmac<Sha256>`, `verify_slice`); the hand-written HMAC, `tags_equal`, `sha256`/`nibble` helpers are gone. Edge-only lock change (`lock_edge_check`: OK). Vectors unchanged (oracle vectors, 256 flipped tag bits, wrong lengths).
- **EV-0295 root cause.** The test client wrote the body of an oversized hello after the server had rejected the length prefix (`read_prefixed` checks the size before the body, as `local.rs` does); under load the server's end was dropped first and the client's `write_all` got `BrokenPipe`. Fixed in the test (the broken pipe of that one write is the rejection arriving first; the no-reply cases are judged after the session ended instead of a 600 ms window). No product deadline changed.
- **`run_sidecar` / `mycelium-rs-sidecar`** (`sidecar_main.rs`, `src/bin/`). Exit codes and stderr events equal the executed oracle on 36 recorded cases (`fixtures/sidecar-cli-oracle.json`). Stated differences, all fail-closed: only `--local-only` is served (the production-relay and `--force-relay` modes end `fatal`, RU-022); a closed `--bootstrap-fd` is `bootstrap_rejected` where the oracle aborts; a valid `--artifact-*` configuration is served only through an `ArtifactBackend` (the bare binary has none: durable-journal seam L6/L7) and otherwise ends `fatal`; `--help`/`--version` text is the Rust program's.
- **Quick-tunnel resource** (`relay_process.rs`). `NativeRelay: RelayResource` over `NativeChild`; `stop` proves pid reaped + root/home/base removed, residue is reported not deleted; argv/env/`provider_event` pinned to the oracle. The tunnel client logs to `--logfile` (the spawn boundary nulls stdio), a stated difference. The real `cloudflared` is never started by tests.

## Round 14: durable artifact journal, relay modes, closed bootstrap descriptor

- **Closed `--bootstrap-fd` is parity, not a divergence.** The r9/r13 note "the oracle aborts (SIGABRT)" came from a DEBUG build (std's owned-fd check follows the caller's debug assertions). The shipped binary is the RELEASE build (`.build-native/release/mycelium-iroh-sidecar`); built that way (`L9-evidence/r14-build-oracle-release.sh`, opt-level 0) the oracle ends a closed descriptor with `{"event":"bootstrap_rejected"}`, exit 1, no socket (`fixtures/sidecar-artifact-oracle.json` case `closed_bootstrap_fd`). The Rust entry already did exactly that; `sidecar_main_cli.rs` now pins it against the fixture. No V03 tag needed.
- **Durable artifact-admission journal** (`artifact_journal.rs`, `sidecar_artifact.rs`; `artifact_net.rs:118-260,391-433 @ f4e366e9`). `<state>/source-ledger.jsonl` is byte-compatible with v0.1.4's: genesis header (keys sorted: `genesis_unix_ms`, `protocol`), one `{"e","f","g","n","x"}` line per admission, replay + compaction at open (tmp, fsync, rename, directory fsync), torn final line dropped, any other malformed line / header / non-file path fatal, append fsynced under the fence lock before a byte is read, a failed append poisons the journal, a grant older than genesis is refused, live fence entries never evict. Evidence: 26 executed journal states (`sidecar_artifact_journal.rs`, RED stub first `r14-journal-red.log`), the oracle's two unit tests transcribed (executed green, `r14-oracle-journal-unit-tests.log`), and an 18-request scenario with a mid-scenario restart run against the live release oracle AND the Rust sidecar with identical outcomes and journal lines (`oracle_artifact_interop.rs`, recorded in `fixtures/sidecar-artifact-serve-oracle.json`). Not executed against the oracle: `artifact_source_busy` at 65 536 live entries (only the oracle's unit test); a disk-full append. Seam SE-L9-3: `ArtifactMaxFence` (`artifact_net.rs:94`) has no registry id, so `DurableArtifactBackend::shipped()` refuses and the bare binary still ends `fatal` on a valid `--artifact-*` configuration (pinned).
- **Production-relay and `--force-relay`** (`iroh_host.rs` `NetworkMode`/`Reach`, `run_sidecar`). `presets::N0` (+ `clear_ip_transports` for force-relay), the live address watch bridged from `Endpoint::watch_addr`, `Endpoint::online` as the relay confirmation, the named `ready_address_incomplete` event when the window expires, the oracle's `local_only` peer rule only in local-only mode. Executed oracle (release, Seatbelt with all non-local outbound denied, `fixtures/sidecar-relay-oracle.json`): `ready_address_incomplete` at 3046/3049 ms, `ready` published with `addrs: []` (force-relay) or the interface addresses (production, never loopback), exit 0 on SIGINT. The Rust sidecar reproduces that against one unreachable local relay with no address lookup (`Reach::Hermetic`) and a registry span as the window. Not run: the relay-ONLINE path end to end (needs a relay), Router frames across a relay, two physical hosts (RU-022). Seam SE-L9-3: `READY_ADDRESS_WINDOW` (`ready_address.rs:45`, 3 s) has no registry id, so the binary's `shipped_ready_window()` is `None` and the relay modes stay refused (`fatal`, no socket) until the id exists.
- Edge-only: no dependency added. Requests: `L9-evidence/r14-requests.md`.
