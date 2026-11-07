# Deterministic foundation simulation (MODEL only)

`sim` is a synchronous Rust event driver and faithful historical fault-model
laboratory. It is not a product port, network emulator or device qualification.
No sockets, threads, OS clocks or sleeps are used by the driver or models.

All ten section-7 incidents now have historical-model contracts. The seven L2b
additions, exact source/obligation/budget crosswalk and model limitations are in
[INCIDENTS.md](INCIDENTS.md). The original three lifecycle models follow below.

## Driver contract

- `Scheduler<E>` orders events by `(monotonic milliseconds, explicit task ordinal,
  insertion ordinal)`; nodes/partitions/persisted entries use ordered collections.
- Wall jumps do not move the monotonic clock. Clock and ordinal arithmetic is
  checked. Keyed SplitMix64 finalization is pinned as `splitmix64-keyed-v1`;
  `(seed, entity, stream, counter)` choices do not share mutable RNG state.
- `Io` is the typed simulated storage/transport command/completion seam. It
  supports symmetric partitions/heal, drop, duplicate, delayed reordering, disk
  failure, crash and restart. Writes commit only on successful completion.
  Restart restores committed data; old-boot completions cannot mutate a successor.
- Whole-world serde snapshots include the pending queue, sequence ordinal, clock,
  node boot generations, durable and volatile state, partitions and event log.
- The fast completion slot uses L1 `OwnerPoll`; reordering uses
  `MemberRestartGrace`. These are **synthetic scheduling slots**, not measured
  transport/storage latencies. No real-adapter fidelity is claimed.

## Historical fault crosswalk and independent observations

| Section-7 row | L0 obligation | Faithful fault / corrected model |
|---|---|---|
| F4 (`V02-PLAN.md:224`) | `O:path:mycelium_a13_macos/worker.py` | Holds the first worker to `WorkerDefaultWall` (1800 s), then attempts replacement before cleanup proof; corrected stops with `EffectiveRolloverLead`, waits for proof and creates a new generation. |
| F15 (`V02-PLAN.md:228`) | `O:path:mycelium_a13_macos/demo_chain.py` | Owner-only deadline ignores `MemberChosenWall` (600 s), with owner start staggered by a synthetic `WorkerStartup` slot; corrected uses the earliest absolute per-worker expiry. |
| F16 (`V02-PLAN.md:230`) | `O:path:mycelium_a13_macos/chain_compile.py` | `HistoricalOuterStop` (30 s) expires before `ObservedStop` (~33 s) and hides the error; corrected waits only to the unchanged worker deadline minus `WorkerCleanupReserve`, requires a receipt and surfaces missing proof. |

Tests resolve these real L0 ids and escape rows. Models use only L1 duration ids:
`WorkerDefaultWall`, `MemberChosenWall`, `WorkerStartup`, `ObservedStart`,
`ObservedStop`, `HistoricalOuterStop`, `EffectiveRolloverLead`,
`WorkerCleanupReserve`, `OwnerPoll`, `ControlClockSkew`; seam reordering also uses
`MemberRestartGrace`. There is no second timeout table. Fixture authority is
explicit: genuine owner-local permission, a separately granted member and one
immutable monotonic authority cap (eight `WorkerDefaultWall` windows). It is a
finite **synthetic scenario input**, not a newly invented product consent policy.
Missing permission or insufficient remaining authority refuses before a worker.

Independent assertions require two automatic fresh-run transitions, three
successful serving chats, two explicitly interrupted rollover-spanning requests,
cleanup proof before the original reserve boundary and distinct generations with
unchanged per-worker limits. Always-refuse and missing-receipt models cannot pass.
The final observation horizon ends after the post-second-rollover chat; this is
not an indefinite availability claim. Failures check reached actions as well as
semantic invariants, not just seed counts. A 100-seed seam bank additionally
reaches all four delivery modes and compares full replay state.

Sources are the section-7 faithful recipes, `L12-WALL.md:12-29,67` and
`L13-ROLLSTOP.md:14-34,61-65`. L1 carries the exact source citations at `f5bfb709`:
`model_resources.py:95`, `test_consented_wall_limits.py:75`, `worker.py:35-36`,
`demo_chain.py:247,268,1140,1240-1253`, `chain_compile.py:480`,
`managed_control.py:97` (all package-qualified in `budgets::BUDGET_TABLE`).
The close delay is the L13 approximate route-stop observation used as an injected
close-path proxy, not an independently measured native close p99.

## Replay and minimization

Trace v2 includes build id, Cargo.lock SHA256, simulator/PRNG versions, seed,
configuration, profile id, exact runtime budget read sets with full declarations
(value, unit, purpose, provenance), concrete commands, expected semantic failures
and full historical AND corrected outcome/event logs. Recursive formula reads are
instrumented too. Unrelated registry growth is allowed; missing/changed used budgets
and added or removed runtime reads fail closed. See [TRACE-MIGRATION.md](TRACE-MIGRATION.md)
for the explicit v1→v2 migration and preserved baseline outputs. Deletion
minimization preserves the exact failure set and incident-specific reached transitions; a no-op or
setup error is not a smaller failure. Concrete command replay never regenerates
inputs from the seed. Committed minimized fixtures remain regression data when
the generator changes. Identity checking requires the caller's known build/lock
pins; it is not binary attestation. Use the recorded source revision for an
original-build reproduction. Future revisions must retain the fixture outcome
or explicitly version the simulator; do not silently re-record goldens.

After the host-priority guard described in the lane brief, examples:

```text
cargo test --offline -p sim -p latency-profiles -j 2
SIM_MODEL=historical cargo test --offline -p sim --tests section7_ -j 2 --no-fail-fast -- --nocapture
cargo run --offline -p sim -j 2 -- record F16 BUILD_ID LOCK_SHA NEW_TRACE.json
cargo run --offline -p sim -j 2 -- replay TRACE.json BUILD_ID LOCK_SHA PROFILES.json historical
cargo run --offline -p sim -j 2 -- replay TRACE.json BUILD_ID LOCK_SHA PROFILES.json corrected
```

The historical contract selection **must fail ten intended assertions** (use
Cargo's `--no-fail-fast` before the test-binary `--` to run both integration files). Normal
tests assert those mutants are killed and therefore remain green. CLI historical
replay also fails the invariant assertion (nonzero), never turns a RED into success.
The profile check binds the injected delay to the imported, explicitly reported
or synthetic L13 profile and its L1 duration; no fabricated measured p99 enters it.

## Qualification boundary

Corrected controls at 1x/2x/3x cover **close-path-only** 33/66/99 s load with the
same 110 s consent-bounded wait. They do not prove simultaneous loaded startup,
continuous service or R3 admission. Full restart's observation floor is already
396 s versus the unchanged 120 s margin; p99 calibration remains BLOCKED.
All ten incident rows have model contracts. L2c adds a pinned turmoil comparison
and the first shared fake/real filesystem/SQLite corpus in
[`sim-adapters`](../sim-adapters/README.md). Transport/worker production corpora
remain later work. See INCIDENTS.md for the seven
additional typed-seam models and their explicit non-production boundaries.
Actual Iroh/QUIC, storage durability, GPU/Metal, native workers and authenticated
consent require their own adapters and production-code gates after L5+.

`evidence/resolved-workspace.lock` is the historical L2 lock receipt. The current
root Cargo.lock pins L2c adapter dependencies. L2c adds opt-in budget-access
instrumentation; it does not change declarations or L0 obligations.
