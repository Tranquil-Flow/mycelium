# L2c adapter fidelity slice

Foundation test-support adapters, **not production ports**. Owner: L2 / future
host-storage and transport port owners. No real node, relay, Iroh/QUIC or Metal
execution occurs here. There is no madsim dependency: the required pinned turmoil
slice compiles and executes on the assigned macOS/Rust toolchain.

## Pins and intake

- `turmoil =0.7.2`, MIT, checksum
  `0061c28f1469e94771bb8aa626ce6b29265c2fb5034115046a170b0cba2e33d2`.
- `tokio =1.53.1`, MIT; `rusqlite =0.32.1`, MIT, with cached
  `libsqlite3-sys =0.30.1` (MIT binding, SQLite public-domain amalgamation 3.46.0).
- Exact transitive resolution is in the root `Cargo.lock`; complete added-package
  versions/licences/checksums and newly acquired archive sizes are in
  `dependencies.json`. Every added dependency offers MIT/Apache-2.0 terms.
- Turmoil was absent from the local crate cache. The crates.io REST API returned
  403; the sparse index and checksum-verified static archive worked. Dependency
  acquisition was reserved in the shared download ledger before the first fetch.

## Minimal-driver comparison (ran in the integration tests)

`tests/turmoil_comparison.rs` runs the minimal `sim::World` and the pinned
Turmoil/Tokio adapter against **independent literal expected observations**:

| Case | Expected received message IDs | Turmoil mechanism |
|---|---|---|
| Healthy | `[1,2,3]` | simulated UDP sockets, positive delivery and recovery marker |
| Drop | `[2,3]` | one-way partition drops message 1's delivery window, then repair |
| Duplicate | `[1,1,2,3]` | typed adapter emits two actual simulated datagrams |
| Reorder | `[2,1,3]` | typed adapter defers message 1 until message 2 is delivered |
| Partition | `[3]` | bidirectional partition drops first two messages, then repair |

Two pinned seeds (17 and 89), with a repeated run for each network case, establish
repeatability of these **observable projections**, not identical internal event
logs across two different schedulers. No sorting or deduplication hides ordering
or duplicate errors. The receiver continues draining after the recovery marker.
Every send/receive error propagates. Both source and destination socket readiness
are explicit; all waiting is virtual Tokio time. Epoch, seed, tick, min/max
latency, failure/repair rates and simulator duration are explicitly set; every
duration is supplied by an existing typed BudgetId (`OwnerPoll`,
`MemberRestartGrace`, `MemberChosenWall`). These are synthetic scheduling slots,
not measured networking latency. Duplicate/reorder are application-message fault
injection at the typed seam; no nonexistent turmoil packet-reorder API is claimed.

The disk comparison uses `turmoil/unstable-fs` with explicit capacity from
`ReplayPage`, no random syncing/errors, and seed-pinned execution. It fills enough
of a simulated disk to make the next write fail; verifies old committed bytes and
absence of failed bytes; commits a subsequent fitting write; crashes/bounces the
host; requires explicit file and directory sync for surviving data; removes files
and verifies the simulated directory is empty. The minimal driver's comparison
projects the corresponding failure/preservation/recovery observations and drained
completion queue. Its queue-drain check is **not** a file-deletion model; actual
file deletion is an additional Turmoil assertion. Real adapter cleanup is tested
separately below.

### Pinned-source compatibility finding

`turmoil 0.7.2/src/fs/mod.rs:1091–1095` returns the exact string
`No space left on device`; `src/fs/shim/std/fs/mod.rs:824–829` maps it with
`std::io::Error::other`. It does **not** expose `raw_os_error() == Some(28)`.
The first disk probe's errno assertion failed. Its corrected oracle pins all
three observed/source-supported properties: raw errno `None`, kind `Other`,
exact message. It does not accept arbitrary I/O failure. The initial failure
and old→new assertion mapping remain in the lane evidence. File-content,
failed-write, recovery, crash and cleanup assertions were not weakened.

Source patterns used: upstream `tests/net/udp.rs` (partition/hold/UDP),
`tests/fs/durability.rs:12–61` (sync_dir, crash/bounce, phase notification),
`src/builder.rs` (explicit options). The `unstable-fs` API is deliberately pinned;
upgrades require rerunning this compatibility slice. No production stack switch
or claim of Tokio real-I/O determinism follows from this test.

## Shared fake / real storage corpus

`Store` is a small atomic byte-map seam, with an explicit logical-value-byte cap.
The **same `corpus()` assertions** run against:

1. `Fake`: in-memory staged map, atomic publish, closed/cleanup state.
2. `Filesystem`: real OS files in an owned temporary root, staged JSON image,
   file sync and atomic rename, fresh file reads on reopen.
3. `Sqlite`: actual bundled SQLite 3.46.0, persistent on-disk database, transactions,
   explicit rollback, DELETE journal mode, FULL synchronous, close/reconnect.

The common corpus covers missing keys; successful writes; duplicate operations;
update and graceful reopen; an atomic multi-write failure; post-failure progress;
capacity refusal; idempotent deletion; maximum u64 key and empty values; explicit
cleanup failure; closed authority before cleanup finishes; repair/retry and
idempotent cleanup. An additional common boundary corpus requires exact-capacity
acceptance, one-byte-over refusal, unchanged data after reopen and last-write
semantics. Test roots are created exclusively; existing roots/foreign data cannot
be silently claimed or erased.

Faults are not faked success codes on the real implementations:

- Filesystem commit: an actual rename over a directory fails after staging;
  pending data is removed and the previous image remains unchanged.
- SQLite commit: a real trigger aborts the second key after the first SQL write;
  the explicit transaction rollback must restore the original value.
- Cleanup: a foreign sentinel makes actual `remove_dir` fail. Cleanup still
  closes authority and attempts all named owned resources. It never recursively
  deletes unknown files. Clearing the test fault permits a retry; the corpus
  verifies no root/database/journal/pending file remains afterward.
- Two deliberately lying fake wrappers (hidden commit failure and false cleanup
  success) are rejected by the unchanged common corpus.

Obligation links: `O:path:mycelium_physical_runner/state_store.py` and
`O:path:mycelium_physical_runner/private_fs.py` (checked against L0). These are
foundation prerequisites, not completion claims for those full production ports.

## Boundaries / next consumers

The storage cap is logical value bytes, **not SQLite file geometry, filesystem
quota, ENOSPC qualification or F2's bounded-retention accounting proof**. The real
filesystem corpus proves atomic visibility and graceful reopening, not crash or
power-loss durability: parent-directory fsync is deliberately not claimed. The
Turmoil crash test is a simulated durability model, not a physical storage test.
SQLite's injected SQL abort is not an actual hardware disk failure. Permissions,
symlink adversaries, process-level cancellation, multi-process writers and corrupt
store recovery need their production adapter contracts. Transport and worker
fake/real production corpora are still future work; only storage is started here.
No fake authorization/crypto/GPU behavior is qualified.

Run under the host-priority guard, nice 19, two jobs and owned `TMPDIR`:
`cargo test --locked --offline -j 2 -p sim-adapters --tests`.
