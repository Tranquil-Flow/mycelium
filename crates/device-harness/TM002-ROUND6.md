<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Round 6 TM002 classification and correction

Baseline is preview-three merge `c684bbb` (preview `e3773ea3`), not the later
registry workaround. All 12 findings are **resolver false positives**: the
second argument is `keeper::check`'s observed clock `now`, not a duration budget.
`runtime::iroh_receive::RouterReceiver::check(&self, deadline: MonoInstant)` was
indexed by bare `check` and its UFCS receiver offset contaminated argument 1 of
an unrelated free function. No genuine duration literal occurs in these findings.

| Baseline path (under `crates/device-harness/tests`) | Line | Observed clock | Classification |
|---|---:|---:|---|
| keeper.rs | 137 | 200000 | false positive, `now` |
| keeper.rs | 144 | 300000 | false positive, `now` |
| keeper.rs | 153 | 21600000 | false positive, `now` |
| keeper.rs | 211 | 2000000 | false positive, `now` |
| keeper.rs | 229 | 2000000 | false positive, `now` |
| keeper.rs | 245 | 100 | false positive, `now` |
| keeper.rs | 256 | 100 | false positive, `now` |
| keeper.rs | 262 | 100 | false positive, `now` |
| keeper.rs | 276 | 100 | false positive, `now` |
| native_session.rs | 427 | 100 | false positive, `now` |
| native_session.rs | 459 | 100 | false positive, `now` |
| native_session.rs | 483 | 100 | false positive, `now` |

The corrective increment restores all twelve original clock literals and removes
three mistakenly introduced synthetic-clock registry IDs from `67a0b03`.
The seven independent, previously requested L1 policy/count declarations remain;
their source provenance and exact-value tests are unrelated to this lint defect.

Resolver signatures now have crate/module/type ownership, use/type aliases,
lexical import scopes, typed receiver lookup and correct UFCS offsets. Unresolved
receivers retain conservative same-crate/visible-import checks; external time-name
fallback and Duration literal detection remain. Cyclic/unresolved glob expansion
is fenced. No rule, allowlist, fixture oracle, or exception policy was disabled.
Syntax-resolution limits remain explicit in `xtask/README.md`.

Receipts: program `runs/v2/v02/l12-receipts/round6-*.{log,json}`.
- `01-lint-before`: exit 1; exact summary `lint: 514 source files, 12 violations`.
- `08-resolver-red`: exit 101; collision and module/alias/receiver tests fail,
  genuine-deadline controls pass. `10-lexical-red`: lexical leakage fails.
- `14-resolver-all-tests`: budgets/device-harness/xtask tests and doctests pass.
- `20-resolver-tests`: all xtask tests pass, including seven new resolver cases;
  existing accept/reject fixture line/code assertions unchanged.
- `21-lint-corrected`: exit 0; `lint: 517 source files, 0 violations` (includes
  the untracked evidence-input adapter draft present during this intermediate run).
- `22-resolver-clippy`: all targets of xtask/device-harness/budgets pass `-D warnings`.
- Intermediate compiler duplicate-visitor and unresolved-glob stack-overflow
  failures are retained (`12`, `15`); neither was counted as acceptance.

## Verification of WIP `b0234ac` (resume run, 2026-10-28)

The provider-limit WIP commit was re-gated unchanged: `cargo fmt --check` clean;
`xtask lint` **522 source files, 0 violations** (before: 514 files, 12 TM002);
`cargo test -p device-harness -p budgets -p xtask` 227 passed / 0 failed /
0 ignored; device-harness with `serde_json/arbitrary_precision` 186 passed / 0
failed; `clippy --all-targets -D warnings` for xtask, budgets and device-harness
clean. With the genuine foundation-exit manifests (read-only; native 40a9980d…,
oracle 9e2d4dec…) `xtask preflight --group native-adapters --group oracle-journeys`
selects both groups and `foundation_discovery` reports four native resources with
`native_execution:false` and `release_qualified:false`. TM002 is unchanged and
no allowlist or fixture was edited.
