# Typed budgets — foundation model

`budgets` is a dependency-free, deterministic policy/model crate. It is not the
Rust port of workers, cryptographic consent verification, SQLite retention,
transport, or physical inference. The only consumer in this slice is the
independent `budget-model` test package.

## API contract

- `BudgetId` and `table::BUDGET_TABLE` are the single declaration surface. Time is
  integral milliseconds (`Span`), storage/memory use `Bytes`, cardinalities use
  `Entries`. Fallible arithmetic is checked; no wrapping or silent saturation.
- `WallExpiry` is an absolute Unix expiry. `MonoInstant` includes a host/boot
  `ClockDomain`. Cross-domain comparison refuses. Adapters must sample and
  translate authenticated remote clocks before creating `WorkerWindow` values;
  this crate does not infer offset/uncertainty or authenticate an expiry.
- `effective_wall` distinguishes owner-local proposals from member reports. An
  unknown **member** gets the source-compatible planning bound, never permission.
  `EffectiveWall::admit` requires a separate authenticated member grant. The owner
  has no synthetic standing grant. Limits are immutable; infeasible work refuses.
- `deadlines` takes each worker's own translated origin and absolute authority
  expiry; a late join cannot restart another worker's clock. `require_deadlines`
  checks an externally proposed schedule. Short assignments without room for the
  existing execution-ceiling lead refuse rather than inventing a shorter lead.
- `LeasePin` translates an unchanged signed expiry only conservatively, never
  extends it after wall rollback, and retires permanently on observed expiry.
  A new pin after retirement requires fresh upstream admission. Identity and
  signature verification remain the caller's responsibility.
- `RelayBudget` models the quick-relay window/whole-window cap. Failed/false
  probes do not renew; expiry/explicit stop cannot revive. This is not a socket,
  process, URL or live-service continuity proof.
- `Reservations` accounts staging overhead and resident/scratch/KV memory,
  rejects partial/duplicate updates, and releases only on an explicit model
  `CleanupProof::Verified`. That enum is **not** cryptographic/native proof;
  the future owner must construct it only after verifying the real receipt.
- `RetentionFootprint` includes fixed never-cleared per-signer history, retained
  response rows, active commands and caller-supplied database/index/WAL overhead.
  It checks accounting, not actual SQLite pruning or Bloom replay semantics.

## Calibration is deliberately blocked

`require_renewal`, `require_stop` and `require_rollover` check structural relations.
`require_p99` and `qualify_entry` separately check phase-tagged measured evidence.
Every operational/progress-window entry without p99 returns `BlockedNoP99`.
An observation is never accepted as p99. `MeasuredP99` is an input seam for L2:
L2 must validate sample adequacy, raw samples, method, model/build/device and host
load at the evidence path. A sample count greater than one is only a basic
malformed-input guard, **not** statistical qualification. Synthetic profiles
exist only in tests and never populate the declared registry.

For wall-dependent entries call `BudgetId::wall_span` and then `require_p99`;
`span` without the required context refuses. `ConfiguredCloseWaitSum` is a sum of
explicit waits, **not** a finite guaranteed end-to-end bound. `GuaranteedClose`
and `CorrectedRestart` remain BLOCKED. No production calibrated restart value is
provided; changing consent or picking a margin to make a test pass is forbidden.

The registry is complete for this first incident-model slice, **not an inventory
of every timeout in the Python repository**. L0/later ports must add any newly
reachable timeout with a stable ID and verified provenance before use.

## Source and policy distinctions

Pinned oracle: `f5bfb70981569c79cb873afe8e0c5be20342db7e`. Each table row carries a
source citation or an explicitly labelled observation/blocker. The derived
lease renewal interval is the adopted R2 `TTL / 3` rule, not a claim that Python
had that exact periodic interval. The 128 MiB mailbox ceiling is the adopted
soak/model policy; Python `managed_offers.MAX_TOTAL` has the same numeric value
but limits **staged offers, not the SQLite database**. The model does not claim
Python enforces a global database cap. Decimal GB consent is not GiB.

## Verification

After the current LANE-COMMON host-priority guard admits execution:

```text
cargo test --offline -p budgets -p budget-model -j 2
cargo clippy --offline -p budgets -p budget-model --all-targets -j 2 -- -D warnings
cargo fmt -p budgets -p budget-model -- --check
cargo run --offline -p budgets --example catalog -j 2
```

Property tests use a fixed recorded seed, independent scalar oracles and bounded
sequences. Numeric scalar expectations in tests are historical observations or
mathematical generator domains, not configured duration arguments. Runtime time
construction reads the registry or generated variables. Compile-fail doctests
prove unit/domain separation. No test starts devices, networks or Python.
