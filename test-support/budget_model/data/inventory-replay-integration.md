<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Inventory growth and simulation replay

The inventory branch rebased onto `8a2c0f0d1f3110c55645e0357aa9b0420cafe159`
contains L2c's recorded budget-read mechanism. The earlier four
`budget_snapshot_mismatch` failures are resolved by that integration, not by
loosening assertions or modifying budgets.

At rebased inventory commit `0512cdf`, the complete workspace suite returned
**214 passed, 0 failed, 0 ignored**, including doctests. The formerly failing
persisted-corpus, remaining-persisted-corpus and review-regressions targets ran.

All ten v1 traces were independently re-derived using the existing Rust CLI:

```text
cargo run --locked --offline -j 2 -p sim -- migrate \
  test-support/sim/traces/<ID>-v1.json \
  test-support/sim/fixtures/legacy-outcomes/<ID>-outcomes.json \
  <inventory-commit> <actual-Cargo.lock-SHA256> <fresh-scratch-output>
```

Every migration compared both complete historical and corrected outcomes with
the immutable baseline export. The re-derived trace equals the checked-in v2
trace in **every field except producer build_id**. The lock pin, read snapshots,
config, commands, outcomes, failure sets and execution/profile pins agree.
Consequently no replacement of already-correct L2c fixtures is necessary.

| Incident | Historical reads | Corrected reads |
|---|---:|---:|
| F1 | 4 | 4 |
| F2 | 8 | 10 |
| F3 | 2 | 2 |
| F4 | 9 | 9 |
| F5 | 4 | 4 |
| F13 | 7 | 7 |
| F14 | 6 | 6 |
| F15 | 11 | 11 |
| F15b | 4 | 4 |
| F16 | 11 | 10 |

L2c's `budget_reads` and `trace_migration` regressions retain the exact used
value/declaration identity, missing/extra-read and baseline-outcome fences.
Unrelated inventory entries are not execution dependencies. This is simulation
compatibility evidence, not whole-inventory closure or measured calibration.
