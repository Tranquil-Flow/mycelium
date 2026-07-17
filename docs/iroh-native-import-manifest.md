# Native iroh import manifest and review

Status: **DO NOT IMPORT YET**

This is a provenance and architecture review only. No implementation was imported, no test was changed, and no native transport test was executed during this review.

Machine-readable source of truth: `docs/iroh-native-import-manifest.json` (`mycelium.iroh_native_import_manifest.v1`).

## 1. Review scope and baseline

- Canonical repository: `/Users/evinova-self/Projects/mycelium`
- Isolated worktree: `/Users/evinova-self/Projects/mycelium-wt-p7-iroh-review`
- Branch: `phase7/native-iroh-review`
- Frozen review baseline: `88af7fc0b4e2bf3ec480cd8e2916bcbc480c1e92`
- Remote evidence host: `evinova@evis-macbook-pro-1`
- Remote native source: `/Users/evinova/Projects/mycelium-iroh-native-spike`
- Local evidence source: `/Users/evinova-self/Projects/mycelium-iroh-activation-spike`

Both evidence roots are unversioned directories, not Git repositories. The remote native root has no license file. Its reviewed provenance is therefore bound only by host, absolute path, byte size, and SHA-256. Import requires a separate source-ownership/reuse attestation and license decision.

The local activation spike is Python evidence/harness code and contains machine-specific identity-key files, membership data, and generated results. It has zero native Rust candidates and is excluded recursively from import.

## 2. Decision

The remote source demonstrates a useful native Iroh benchmark primitive, but it is **not** an implementation of `mycelium_router.ports.TransportPort`.

It provides:

- Iroh 1.0 endpoint creation in direct and N0-relay modes;
- cryptographic Iroh endpoint identities exposed by the connection;
- ALPN negotiation for `mycelium/activation-native/2`;
- fixed binary activation and receipt codecs;
- per-message and persistent bidirectional-stream modes;
- SHA-256 payload integrity checks;
- four-timestamp clock calibration;
- benchmark windowing and asynchronous receipt collection;
- one Iroh connection reused across streams within one fixed benchmark run.

It does not provide:

- exact Router wire compatibility;
- five of six `TransportPort` methods;
- a complete `send_hop` mapping or inbound Router dispatch;
- trusted Mycelium-node to Iroh-endpoint authorization;
- stable persisted native identity;
- production retry, cancellation, operation deadlines, or Router backpressure;
- cross-request connection pooling/recovery;
- a Python/Rust bridge or Rust-native Router integration;
- executed tests against the reviewed hashes.

Result: use reviewed source as bounded reference material for a rewrite. Do not copy `transport.rs` verbatim into production and do not advertise its benchmark ALPN as Router compatibility.

## 3. Candidate native source inventory

Every source/build/test candidate in the remote native tree is listed below. `src/main.rs` is inventoried because it is native source, then explicitly rejected.

| Source | Bytes | SHA-256 | Language | Role | Disposition | Exact proposed destination |
|---|---:|---|---|---|---|---|
| `Cargo.toml` | 573 | `397b5eb408c8d0b7041c2a146edfd52dfaed83ac0da03c28de5dfe1e2fb9586c` | TOML | Package/toolchain/direct dependencies | Adapt | `native/iroh_transport/Cargo.toml` |
| `Cargo.lock` | 106203 | `a4e41a432926da35b236615bd471ca202f22721adc16ca2d793778de912da00f` | Cargo lock | Reviewed transitive resolution | Regenerate after manifest adaptation | `native/iroh_transport/Cargo.lock` |
| `src/lib.rs` | 71 | `fbb5747f0242830065cce32f306bc00dd8afe5cb75a7c2645429dfb41616bc5b` | Rust | Module root | Rewrite | `native/iroh_transport/src/lib.rs` |
| `src/main.rs` | 4891 | `dfbcb16d96ea266de52e6dac5b64d0ef976e04927a4a86f365e725843351ee1d` | Rust | Benchmark CLI/evidence writer | **Must not import** | None |
| `src/clock.rs` | 1805 | `dcdf736ae8065843db6e2e296f06a18e55a05c07ec049e9ced0003861aeca045` | Rust | Clock calibration/arrival estimator | Adapt | `native/iroh_transport/src/clock.rs` |
| `src/protocol.rs` | 8030 | `ff4c2ffe878327573ce775b4170f94b6f0724fc91759ec87d6d44d219d061e39` | Rust | Activation/receipt binary codec | Adapt | `native/iroh_transport/src/protocol.rs` |
| `src/scheduler.rs` | 3455 | `e315c14ec3a9fe38b72347b353eee6aee8025f57dcdcd940e6dc5a9ea53a849b` | Rust | Benchmark send-window/receipt collector | Adapt and rename/redesign | `native/iroh_transport/src/scheduler.rs` |
| `src/transport.rs` | 57336 | `64454f11a4ad597411b7271d6fc2e5ba521e897ce809c81c4fc3e9513c17709d` | Rust | Iroh benchmark endpoint and protocol orchestration | Reference-only rewrite | `native/iroh_transport/src/transport.rs` |
| `tests/contract.rs` | 5732 | `c05b1d18004f9f7a91d281482b54faad551c4687d61b3bca19d83547016eebb8` | Rust | Codec/calibration/window tests | Adapt after implementation | `native/iroh_transport/tests/contract.rs` |
| `tests/loopback.rs` | 1656 | `756209b113efff1f312303c5d4f8185fa0972bd95da505d3473676401e964d25` | Rust | Direct loopback benchmark test | Adapt after implementation | `native/iroh_transport/tests/loopback.rs` |
| `tests/persistent_chain.rs` | 1918 | `df4aa0a1909b6f125ade26d046058a748dc186d2226a450b3698ed7fe1be0c7d` | Rust | Persistent/synthetic-chain benchmark tests | Adapt after implementation | `native/iroh_transport/tests/persistent_chain.rs` |

### Per-file dependencies and implemented contract

`Cargo.toml`

- Toolchain: Rust 1.91, edition 2024.
- Runtime dependencies: `anyhow`, `clap`, `futures`, `hex`, exact `iroh=1.0.0`, `rand`, `serde`, `serde_json`, `sha2`, full `tokio`, and `uuid` v5.
- Development dependency: `tempfile`.
- Contract: package/build declaration only. Production adaptation must remove the benchmark binary and audit apparently benchmark-only or unused dependencies (`clap`, `hex`, `rand`, `serde_json`, `tempfile`).

`Cargo.lock`

- Dependency: complete transitive graph resolved from the reviewed manifest.
- Contract: reproducibility only. Because `Cargo.toml` must change, the lockfile must be regenerated and license/diff reviewed; its current hash cannot be claimed after adaptation.

`src/lib.rs`

- Dependencies: internal `clock`, `protocol`, `scheduler`, and `transport` modules.
- Contract: exports the entire benchmark surface. Production root must expose only the frozen wire/transport API and keep benchmark evidence helpers out of its public API.

`src/main.rs`

- Dependencies: standard socket/path types, native transport module, `anyhow`, `clap`, `serde_json`, and Tokio.
- Contract: benchmark operator CLI (`serve`, `client`, direct loopback, chain modes) plus arbitrary ready/evidence-file writes.
- Decision: do not import. If a diagnostic example is still wanted later, write it separately after production transport exists.

`src/clock.rs`

- Dependencies: `std::time::{SystemTime, UNIX_EPOCH}`.
- Contract: calculate network RTT and server/client offset from four timestamps, select lowest-RTT sample, report half-RTT uncertainty, and estimate one-way arrival-ready latency.
- Caveat: calls `expect` if system time predates Unix epoch. Wall-clock calibration must remain telemetry only; it cannot determine ordering, authorization, expiry, retries, or correctness.

`src/protocol.rs`

- Dependencies: standard error/formatting and `sha2`.
- Contract:
  - `MYA2`, version 2, fixed 112-byte activation header;
  - sequence, 16-byte request ID, 16-byte hop ID, payload length, wall-clock send time, offset, uncertainty, SHA-256 digest;
  - exact payload length and digest validation;
  - 64 MiB maximum payload;
  - `MYR2`, fixed 72-byte receipt with status/sequence/length/digest/ready time/arrival estimate.
- Caveat: `Unauthorized` is a declared receipt status, but reviewed transport never emits it. This is not authorization.

`src/scheduler.rs`

- Dependencies: standard timing, `futures::FuturesUnordered`, and Tokio selection.
- Contract: enforce benchmark windows of 1, 2, or 4; begin replacement send after send setup completes; poll receipts concurrently; restore result order.
- Caveat: this is not Router `HopScheduler` behavior. It has no byte/work budget, deadline, cancellation, retry-after response, or bounded receipt lifetime.

`src/transport.rs`

- Dependencies: standard Arc/timing; `anyhow`; `clap::ValueEnum`; Iroh endpoint/address/relay/connection presets; `serde`; `sha2`; Tokio semaphore/channel/task set; UUID v5; internal clock/protocol/scheduler.
- Contract:
  - ALPN `mycelium/activation-native/2`;
  - direct endpoint or N0 relay endpoint;
  - calibration stream sequence;
  - per-message or persistent activation streams;
  - asynchronous fixed receipts and done handshake;
  - direct, external, and synthetic three-crossing benchmark modes;
  - JSON ready/evidence output.
- Caveat: fixed benchmark count and positional stream purpose are part of protocol state. There is no generic message discriminator, long-lived service loop, Router receiver callback, or peer manager.

`tests/contract.rs`

- Dependencies: native clock/protocol/scheduler, `sha2`, Tokio.
- Contract checked by source: codec integrity/corruption rejection, receipt round trip, lowest-RTT calibration, allowed windows, receipt polling while another send is pending, and receipts not gating later sends.

`tests/loopback.rs`

- Dependencies: native transport and Tokio.
- Contract checked by source: one direct local Iroh run, digest/length validation, separate arrival-ready and delayed acknowledgement metrics, and calibration sample accounting.

`tests/persistent_chain.rs`

- Dependencies: native transport and Tokio.
- Contract checked by source: fixed-count activations on one persistent stream and synthetic alternating three-crossing metrics.

These test contracts were reviewed statically, not executed.

## 4. Router `TransportPort` mapping

Current Python port (`mycelium_router/ports.py`) requires six synchronous operations.

| Router operation | Native coverage | Review |
|---|---|---|
| `send_hop(HopHeader, payload)` | Partial primitive only | Native can send raw bytes with a benchmark `ActivationHeader`; it cannot encode/decode Router `HopHeader`, resolve destination placement, or dispatch inbound work. |
| `send_manifest_delta(ManifestDelta)` | None | No message type, codec, entry routing, or receiver dispatch. |
| `send_manifest_locked(ManifestLocked)` | None | No participant fanout, path registration, entry confirmation, codec, or receiver dispatch. |
| `send_failure_report(FailureReport)` | None | Benchmark receipt status lacks Router failure scope/reason/placement/edge/node/path-attempt semantics. |
| `send_token_event(TokenEvent)` | None | No token fields, codec, entry routing, or dispatch. |
| `send_prefill_chunk_completed(PrefillChunkCompleted)` | None | No chunk fields, codec, entry routing, or dispatch. |

### `send_hop` field gap

Router `HopHeader` requires:

- `request_id` (arbitrary string);
- `path_id` and `path_attempt`;
- `phase`;
- `token_index` and `hop_index`;
- source and destination placement IDs;
- topology version;
- idempotency key;
- progressive-prefill chunk token count.

Native `ActivationHeader` provides only sequence, forced UUID-v5 request ID, non-Router hop ID, payload length/digest, and timing calibration fields. Timing fields can supplement telemetry; they cannot replace Router routing/idempotency fields.

### Wire incompatibility

Python Router wire (`mycelium.router_wire.v1`):

1. Four-byte big-endian JSON-header length.
2. Canonical JSON envelope containing protocol, message type, message body, payload length, and payload SHA-256.
3. Raw payload.
4. Maximum header: 1 MiB; maximum payload: 256 MiB.

Native benchmark wire:

1. Fixed 112-byte `MYA2` v2 activation header.
2. Raw payload.
3. Fixed 72-byte `MYR2` receipt.
4. Maximum payload: 64 MiB.

These are not byte- or contract-compatible. Shared SHA-256 validation does not create compatibility. Production must either carry exact Router wire frames as an authenticated typed payload or reproduce that contract exactly in Rust with cross-language golden fixtures. It also needs an explicit request/control message discriminator.

### Sync/async gap

Python `TransportPort` methods are synchronous. Native implementation is Tokio async. A production import must choose and test one bounded bridge:

- Rust-native Router integration;
- a narrow FFI boundary;
- or an authenticated local IPC boundary.

No such bridge exists in reviewed source. An unbounded background queue would violate Router delivery/error and backpressure semantics.

## 5. Control-plane/data-plane boundary

### Gossip control plane

Allowed:

- membership;
- topology and availability summaries;
- capacity/load summaries;
- non-sensitive routing telemetry.

Forbidden:

- activation tensors or prefill payloads;
- prompt token IDs or generated token payloads;
- model weights;
- protected edits/deltas;
- request-scoped manifests;
- secrets/private keys.

### Router request control plane

`ManifestDelta`, `ManifestLocked`, `FailureReport`, `TokenEvent`, and `PrefillChunkCompleted` are request-scoped Router messages. They belong on authenticated point-to-point Router transport, not Gossip.

### Data plane

`HopHeader` plus activation/prefill payload, and `ProgressivePrefillMessage` plus raw payload, belong on authenticated point-to-point Iroh transport.

### Static confirmation

For reviewed source paths:

- remote Rust `src/*.rs` has no Gossip reference;
- `mycelium_router` has no `mycelium_gossip` import/reference;
- `mycelium_gossip` has no `mycelium_router` import/reference;
- reviewed `mycelium_gossip` source has no tensor, prompt, checkpoint, protected-edit, or activation field/reference.

Therefore no reviewed static code path sends tensors or protected edits into Gossip. This is a static reachability conclusion, not an end-to-end runtime claim. Future adapter tests must actively reject data-plane/protected-edit submission to Gossip.

## 6. Native transport behavior review

### Authentication and peer identity

- Iroh supplies cryptographic endpoint identity and exposes `connection.remote_id()`.
- Server performs no application authorization; any peer reaching endpoint and negotiating ALPN can be accepted.
- Client reads mutable ready JSON and checks schema, mode, and ALPN. It does not bind `endpoint_id` to trusted Mycelium membership/topology.
- Native builder receives no persisted secret key, so source does not provide stable node identity across runs.
- `ReceiptStatus::Unauthorized` exists but is never emitted.

Production requirement: persist/load approved Iroh identity, map trusted Mycelium node ID to endpoint ID, verify outbound target and inbound peer, and fail closed before decoding/dispatching Router data.

### ALPN

Current value: `mycelium/activation-native/2`.

It identifies benchmark framing only. It must not be reused as a production Router compatibility signal. Freeze a production ALPN only after the Router wire and authorization contract are complete.

### Stream framing

- Calibration: one bidirectional stream per sample; 16-byte `MYC2` request then EOF; 32-byte `MYS2` response then EOF.
- Per-message activation: one bidirectional stream; 112-byte header, exact payload, then EOF; 72-byte receipt, then EOF.
- Persistent activation: one bidirectional stream per fixed run; repeated header+payload frames and repeated fixed receipts; external count determines termination.
- Done: separate bidirectional stream with 8-byte `MYD2` request and 8-byte `MYE2` response.
- Purpose is inferred from protocol order, not a generic typed stream preface.

### Retry

None. Connect, accept, stream, codec, payload, receipt, and done errors abort the run.

### Cancellation

None. There is no cancellation token, Router path-cancel message, stream-reset policy, or cancellation acknowledgement.

### Timeouts

Present:

- 45 seconds waiting for relay endpoint online;
- 60 seconds for external client connect;
- best-effort 5-second wait for connection close.

Missing:

- server accept and incoming handshake;
- calibration read/write;
- activation read/write;
- receipt read/write;
- done handshake;
- whole-operation/request deadline.

### Backpressure

Per-message mode:

- client send setup window is restricted to 1, 2, or 4;
- server semaphore limits concurrent activation processing;
- permit is released after payload validation and before optional delayed receipt write;
- receipt collection does not gate later send setup.

Persistent mode:

- sequential writer;
- channels sized to fixed benchmark count;
- no Router work/byte limit or retry-after outcome.

Iroh/QUIC provides implicit flow control, but application policy is not exposed. This does not satisfy Router's bounded pending-hop/pending-byte and `retry_after_seconds` semantics.

### Connection reuse

- One Iroh connection is reused for calibration, all activation streams, and done handshake within one benchmark run.
- Per-message mode opens a fresh bidirectional stream per activation.
- Persistent mode uses one bidirectional activation stream.
- No reuse across independent Router requests or benchmark runs.
- No connection pool, reconnect, health check, idle expiry, or concurrent peer manager.

## 7. Files that must not be imported

1. `/Users/evinova-self/Projects/mycelium-iroh-activation-spike/**`
   - Entire tree excluded.
   - Contains Python spike code, runtime/identity private keys, membership files, generated results, and machine-specific evidence.
2. `/Users/evinova/Projects/mycelium-iroh-native-spike/src/main.rs`
   - Benchmark CLI/evidence writer, not production transport.
3. `/Users/evinova/Projects/mycelium-iroh-native-spike/evidence/**`
   - Generated evidence, including tensor-named outputs; not source and not proof that current reviewed hashes were executed.
4. `/Users/evinova/Projects/mycelium-iroh-native-spike/target/**`
   - Build output and dependency cache.
5. Any `.key`, private-key, ready, stdout, result, membership, or machine-specific identity file from either source root.

No source copying, remote write, package installation, or implementation import occurred during review.

## 8. Collision analysis

Proposed namespace: `native/iroh_transport/`.

Exact path checks:

- Baseline `88af7fc0b4e2bf3ec480cd8e2916bcbc480c1e92`: zero exact collisions; zero tracked files under proposed namespace; 211 tracked files total.
- Read-only live canonical observation at `4da5430e2fe7aa41766fa023ed8dc2260091fb35`: zero exact collisions; zero tracked files under proposed namespace.

Semantic collisions remain:

- Rust `protocol.rs` conflicts with `mycelium_router/wire.py` contract.
- Rust `scheduler.rs` name suggests Router scheduling but implements only benchmark send windowing.
- Native receipt indicates payload validation, while `LoopbackSocketTransport` waits for same-process Router dispatch completion and propagates dispatch errors.
- Native 64 MiB and Router 256 MiB payload ceilings disagree.
- Benchmark ALPN does not represent production Router compatibility.

Build/integration risks:

- repository has no current Cargo workspace/native namespace;
- Rust 1.91 and edition 2024 add toolchain requirements;
- `native/iroh_transport/target/` must be ignored before any future build;
- Python/Rust bridge and packaging remain unresolved.

## 9. Exact proposed destination paths

Only after blockers close:

```text
native/iroh_transport/Cargo.toml
native/iroh_transport/Cargo.lock
native/iroh_transport/src/lib.rs
native/iroh_transport/src/clock.rs
native/iroh_transport/src/protocol.rs
native/iroh_transport/src/scheduler.rs
native/iroh_transport/src/transport.rs
native/iroh_transport/tests/contract.rs
native/iroh_transport/tests/loopback.rs
native/iroh_transport/tests/persistent_chain.rs
```

`src/main.rs` has no destination.

Except for provenance comparison, proposed source files are adaptation/rewrite inputs, not approved verbatim copies.

## 10. Staged import order

### Stage 0: provenance and production contract gate

1. Obtain ownership/reuse attestation and choose license.
2. Re-read remote bytes and require every listed byte size/SHA-256 to match.
3. Freeze production ALPN.
4. Freeze trusted node-ID to Iroh-endpoint-ID mapping and inbound authorization.
5. Freeze Router request/control framing, payload ceiling, completion semantics, retry taxonomy, cancellation, deadlines, and backpressure.
6. Choose Python/Rust bridge or Rust-native Router architecture.
7. Stop if any reviewed hash or provenance fact differs.

### Stage 1: crate scaffold

1. Adapt `native/iroh_transport/Cargo.toml`.
2. Add production-only `src/lib.rs`.
3. Add target ignore rule.
4. Regenerate/review `Cargo.lock`.
5. Do not add benchmark CLI.

### Stage 2: wire and clock primitives

1. Adapt `src/protocol.rs` to exact Router contract.
2. Keep `src/clock.rs` telemetry-only and non-panicking.
3. Adapt `tests/contract.rs`.
4. Add Python/Rust golden frames and malformed-frame parity tests.

### Stage 3: bounded authenticated async engine

1. Redesign `src/scheduler.rs` around work/byte limits, deadlines, cancellation, retry-after, and bounded receipt lifetime.
2. Rewrite `src/transport.rs` around stable identities, peer authorization, typed messages, inbound Router dispatch, operation deadlines, cancellation, retry classification, and connection pooling.
3. Implement all six `TransportPort` operations.
4. Remove deterministic benchmark payloads/IDs, simulated delay, JSON evidence writes, fixed-count lifecycle, and synthetic three-crossing flow.

### Stage 4: native and Router integration tests

1. Adapt loopback and persistent tests to real Router messages.
2. Add direct and relay execution.
3. Add unauthorized peer, malformed frame, timeout, cancellation, retry, backpressure, reuse/reconnect, and Gossip-boundary cases.
4. Run real native tests.
5. Only then claim transport behavior that those tests demonstrate.

## 11. Verification commands

### Review artifacts now

```bash
git add --intent-to-add -- \
  docs/iroh-native-import-manifest.md \
  docs/iroh-native-import-manifest.json
python3 -m json.tool docs/iroh-native-import-manifest.json >/dev/null
git diff --check -- \
  docs/iroh-native-import-manifest.md \
  docs/iroh-native-import-manifest.json
git status --short
git diff --name-only 88af7fc0b4e2bf3ec480cd8e2916bcbc480c1e92 -- \
  docs/iroh-native-import-manifest.md \
  docs/iroh-native-import-manifest.json
```

### Re-verify source before future import

```bash
ssh -i ~/.ssh/id_ed25519_m4pro_to_laptop -o BatchMode=yes \
  evinova@evis-macbook-pro-1 \
  'cd ~/Projects/mycelium-iroh-native-spike && \
   shasum -a 256 \
     Cargo.toml Cargo.lock \
     src/lib.rs src/main.rs src/clock.rs src/protocol.rs \
     src/scheduler.rs src/transport.rs \
     tests/contract.rs tests/loopback.rs tests/persistent_chain.rs'

git ls-tree -r --name-only \
  88af7fc0b4e2bf3ec480cd8e2916bcbc480c1e92 \
  -- native/iroh_transport
```

### Verify after future implementation import

```bash
cargo fmt --manifest-path native/iroh_transport/Cargo.toml -- --check
cargo clippy --manifest-path native/iroh_transport/Cargo.toml \
  --all-targets --all-features -- -D warnings
cargo test --manifest-path native/iroh_transport/Cargo.toml --all-targets
python3 -m pytest -q \
  test_router_wire.py \
  test_router_socket_transport.py \
  test_router_end_to_end.py \
  test_router_backpressure.py
git diff --check
git status --short
```

Required future assertions:

- all six `TransportPort` methods round-trip;
- Python and Rust accept identical golden Router frames and reject identical malformed frames;
- untrusted endpoint ID cannot deliver or receive Router messages;
- deadlines/cancellation terminate blocked connect/read/write/receipt paths;
- work/byte limits surface retry-after without unbounded buffering;
- connection is reused across independent requests and recovers after closure;
- no tensor, prompt payload, protected edit, private key, or request-scoped manifest enters Gossip.

## 12. Test claim boundary

Native tests executed during this review: **no**.

Reason: native source existed only on the read-only remote evidence machine. Running Cargo there would write `target/`; source copying and package installation were forbidden. Existing evidence JSON was not accepted as proof that current hashed source passes.

Therefore this review does **not** claim native transport works. It claims only that the reviewed source statically contains the contracts and gaps documented above.
