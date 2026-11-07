# Section-7 historical models (L2b)

These are deterministic **MODELS**, not historical-product executions, product ports,
real HTTP/SQLite/Iroh/Cloudflare tests, or measured latency qualification. The same
`model::Config`, `Mode`, `run`, `violations`, scheduler and trace/replay path support
all ten incidents. The original F4/F15/F16 contracts and concrete corpus are retained.

## Verified fault/source crosswalk

All historical revisions below were read with `git show`, without checking out,
modifying or executing the external oracle. The section-7 plan takes precedence.
Each `Incident::obligation_id()` is checked against L0's checked-in registry.

| ID | L0 obligation (prefix `O:path:`) | Historical source / distinguishing fault | Corrected control |
|---|---|---|---|
| F1 | `mycelium_a13_macos/token_owner.py` | `ff8299ac:token_owner.py` (full package path), lines 75–79, 114–122: install policy from current membership expiry; cached pin not refreshed. Repair `a015f2eb`. | Preserve one installed policy and its immutable process cap; authenticated membership/pin renewals every TTL/3. Requests at 400/700 seconds cross the original 300-second pin and serve. Request at the process cap refuses; wall jumps cannot extend it. |
| F2 | `mycelium_a13_macos/managed_control.py` | `ff8299ac`, before `f8061466`; `_record_auth` inserts replay rows without retention. | Bounded transactions preserve high-water, live retries and claimed auth/command records; expired rows feed a non-clearing Bloom history before deletion. Serialize/reopen the actual model state, reject old sequence and old nonce/request at higher sequences, accept fresh traffic. |
| F3 | `mycelium_a13_macos/demo_target.py` | `ff8299ac`, before `c8aa0528`; capability reporting does not account for an owner process restart clearing the inbox. | Owner-incarnation announcement triggers a fresh capability report, using unchanged member identity/generation/grant. Recover without member restart/re-consent; old owner-incarnation and old member-generation reports refuse. |
| F5 | `mycelium_a13_macos/published_status.py` | `ff8299ac`, before `68119358`; app guard held across compile/workflow preparation also gates status. `L5-STATUS.md:17–27,42–53` verifies the lock mechanism and real loopback discriminator. | Detached published progress reads complete while compile/publish barriers remain held; both phases subsequently finish and a request serves. No real HTTP or response-time measurement is claimed here. |
| F13 | `mycelium_a13_macos/public_relay.py` | `ce4ca16df2c9f495043f6e8030229a88fa1ba141:public_relay.py:357`, full package path: old quick-tunnel hard Timer. `L9-RELAY.md:14–20,70`. | Actual expiry event fires in the historical model. Corrected renewal preserves URL/process and consumes whole granted windows, with an immutable cap. False/failed probes and explicit stop cannot revive a closed relay. |
| F14 | `mycelium_a13_macos/layer_split.py` | `d42e4100^` = `2596efc07b4c7b3a12fb90f1d8d293eea58ea9ce`; storage-blind weighted placement selects 2,227,449,134-byte owner share. `L10-SPLIT.md:24–35`. | Filter both recorded complete partitions against each unchanged 2 GB consent, reserve and stage the feasible owner/member shares (1,997,798,421 / 1,735,338,491 bytes), then serve. Too-small or asymmetric caps refuse by name before reservation. Saved 2 GB and fresh 4 GB policies are separate. |
| F15b | `mycelium_a13_macos/demo_chain.py` | `076ffaa4`, repaired by `2513f28a` and included in `52d1b69f`: no reported cap on real owner incorrectly selects the unknown-member 60-second fallback. | Owner-local proposal supplies 1,800 seconds without a standing member grant; member gets 600 seconds. Actual simulated worker expiry gates serving. Unknown joined member still gets 60 seconds; explicit caps clamp either role. |

## Budget/provenance inventory

All scenario time and storage quantities are read from **L1 BudgetIds**. L2b did not
change the budget table. L2c adds opt-in instrumentation to runtime accessors, not
policy values: v2 traces pin exact per-mode read sets and full declarations, including
transitive formula reads. The immutable v1 full-registry traces remain as migration
inputs; see TRACE-MIGRATION.md. Source citations remain in `crates/budgets/src/table.rs`.

- F1: `MembershipTtl`, `LeaseRenewalInterval`, `WorkerDefaultWall`; finite authority
  comes from Config. Early renewal cannot expand the policy/process cap.
- F2: `ReplayKeep`, `ReplayCapacity`, `RetentionBatch`, `ReplayHistoryPerSigner`,
  `ReplayPage`, `MailboxCeiling`, `ControlTtl`, `ControlClockSkew`, `OwnerPoll`,
  `RetiredCommandAge`. **Synthetic accounting:** each auth/command row is charged
  one ReplayPage (512 bytes), plus the fixed history allocation. This is an explicit
  typed storage-charge workload, not a claim about actual SQLite row/page geometry,
  filesystem bytes, or process RSS. One signer, monotonic unique typed IDs, bounded
  bursts, and a finite two-retirement-age scenario authority. Authenticated input is
  assumed at this seam; the keyed deterministic four-probe hash preserves Bloom
  no-false-negative semantics, not production SHA-256 identity/false-positive rates.
  The model snapshot persists indexes, high-water and history, not a fresh empty DB.
- F3: `MemberRestartGrace`, `OwnerPoll` are synthetic event spacing, not measured
  restart latency. Owner inbox is volatile; member identity and consent are durable.
- F5: synthetic held-work scenarios use the recorded 40–150-second range endpoints:
  `WorkerStartup + WorkerCleanupReserve` and `RolloverMargin + WorkerStartup`.
  **These are compositions of existing L1 slots for fault injection, NOT derivations
  of compile/publish performance from worker/rollover policy.** `OwnerPoll` supplies
  the 500 ms observation deadline, numerically the discriminator reported in
  `L5-STATUS.md:42`; it is not a calibrated device response budget. Snapshot read
  completion is a zero-cost typed event, not a claimed zero-millisecond HTTP result.
  The measured-p99 status-response budget remains **BLOCKED**; L1 should name separate
  status/held-work IDs when calibration data is admitted.
- F13: `RelayWindow`, `RelayMinimumWindow`, `QuickRelayMaximumWindow`,
  `RelayGrantedWindowCap`, `RelayRenewalLead`, `RenewalDivisor`, `RelayPoll`, `OwnerPoll`.
  Early renewals consume full windows: default window reaches 86,400 seconds granted
  but closes at 83,580 seconds elapsed, consistent with L9 (not a 24-hour guarantee).
- F14: `SavedStorageConsent`, `FreshStorageConsent`, `HistoricalOwnerShare`,
  `CorrectedOwnerShare`, `MemberChosenWall`, `OwnerPoll`. The two owner candidates
  are **planner estimates**, not physical staged files. The member estimates are
  retained in `fixtures/f14-member-estimates.json` from `L10-SPLIT.md:24–33`:
  historical 1,505,687,777 and corrected 1,735,338,491 bytes. The fixture values are
  workload sizes, not new consent budgets. Both sides traverse reservation/staging;
  no full layer-cost search or physical allocation is claimed. The member's
  2 GB/600-second input and owner-without-standing-grant shape remain.
- F15b: `WorkerDefaultWall`, `MemberChosenWall`, `UnknownMemberWall`, `ObservedStart`.
  L13's approximate single 160-second startup is used as a **synthetic held-start
  schedule** to expose the 60-second worker expiry; it is not a measurement of F15b
  or a p99. The later request is scheduled one unknown-member window after readiness.

No model above fabricates a measured p99. The policy-model profile id is explicitly
`section7-policy-model-v1-NOT-MEASURED`; it is not a latency-profile cohort. F4/F15/F16
continue validating their imported L13 close profile unchanged. All measured-p99
admission, real-adapter shared corpus and production-transition substitution gates
remain separate foundation/release blockers.

## Executable contracts and replay

Use the lane's host-priority guard before **every** Cargo/rustc command, offline,
nice 19, two build jobs, and a lane-owned scratch TMPDIR. Commands below omit that
operational prefix; they do not authorize running a historical application.

```text
cargo test --locked --offline -p sim --tests section7_ -j 2 -- --nocapture
SIM_MODEL=historical cargo test --locked --offline -p sim --tests section7_ -j 2 --no-fail-fast -- --nocapture
cargo test --locked --offline -p sim -p latency-profiles -p budgets -p obligations -j 2
cargo clippy --locked --offline -p sim --all-targets -j 2 -- -D warnings
cargo xtask lint
```

Historical mode intentionally exits 101 at the same section-7 contract assertion.
The normal suite independently checks source values, positive work, per-incident
reached actions, no-Begin and AlwaysRefuse controls, missing permissions, replay
protection, storage/default boundaries, probe/stop/cap boundaries, and clock jumps.

`sim record INCIDENT BUILD_ID LOCK_SHA OUTPUT` saves minimized concrete commands,
expected outcome/failure, budget/config/profile/PRNG pins. The minimizer requires
that incident's reached transitions and exact semantic failure set, rather than
requiring a worker-stop transition irrelevant to mailbox/report/placement incidents.
`sim replay TRACE BUILD_ID LOCK_SHA PROFILES historical|corrected` never regenerates
commands. CLI replay first verifies the recorded full outcome/event order and
failure set, then evaluates the historical or corrected contract. Corrupted golden
observations are replay errors, not incident REDs. Retained traces pin the producer
executable; a future executable is not byte-identical merely because it replays the
same model outcome. The caller supplies independently established executable/lock
identities; this is not executable self-attestation.

## Review corrections

The interrupted candidate lacked the other F14 member's staged/reserved observation,
retired all eligible F2 commands instead of one `RetentionBatch`, and CLI replay did
not compare the recorded event/outcome log. Behavioral regressions reproduced each
gap before repair. Command pruning now uses the oracle's retirement/update ordering
and batch bound. Original assertions remain intact; no historical fixture is treated
as production qualification. F5's real HTTP/measured-budget requirement and F16's
measured-p99 requirement remain BLOCKED outside these typed-seam models.
