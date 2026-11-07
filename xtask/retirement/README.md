# R6 test retirement (`xtask retire check`, `xtask behaviours check`)

**Schema 2 (RU-042, TB-5; the default).** The unit of proof is the *behaviour*, not the Python test id.
`cargo xtask retire check --ledger <registry>/evidence.jsonl` resolves the 1,337 Python **test groups**
against `registry/BEHAVIOURS.tsv`, `registry/FAULTS.tsv` and the evidence ledger; `cargo xtask behaviours
check --ledger <registry>/evidence.jsonl` validates the two catalogues. The TSVs stay the source of truth
(read as they are, no JSON copy). Both commands exit 0 only when nothing is open or found, 1 while a group,
behaviour or fault is unresolved or a finding exists, 2 for unreadable or malformed input. `--structure-only`
(no ledger) loads and checks the files but proves nothing and says so; the release cell `r6-retirement`
refuses it, and refuses `--schema` (the id-level schema 1 is not an R6 release pass). `--full` lists everything.

Inputs (found from `--registry-dir`, default the directory of `--ledger`; every path has an override flag):

| File | Role |
|---|---|
| `BEHAVIOURS.tsv` (12 columns, exact header) | behaviour catalogue; `tb4/*-new-behaviours.tsv` proposals are added unless `tb4/NEW-ID-MAP.tsv` says they joined it (groups naming the NEW id are translated) |
| `FAULTS.tsv` (5 columns) | fault catalogue |
| `TEST-GROUP-DISPOSITIONS.final.tsv` (TB-4c, consolidated; `--dispositions FILE` overrides) | the **only** source of group dispositions: one row per group, header has `group_id path n_tests disposition b_ids reason_or_reachability source_step confidence` (extra columns allowed). `TEST-GROUP-DISPOSITIONS.tsv` (TB-3) and `tb4/P<n>.tsv` are provenance and are **not read**. A group listed twice is a conflict (`group_duplicate`, resolves nothing); a `disposition` that is not `evidence_for` or a retirement category, `needs_review` included, is a finding (`disposition_invalid`); an inventory file with no row stays in the report as an open group (`inventory_file_without_group`) |
| `xtask/retirement/fault-catches.tsv` (this repo) | how each fault was caught or adjudicated (below) |
| `xtask/retirement/groups.json` (this repo) | rollback commit, allowed approvers, default approver and date, per-group owner overrides |
| `xtask/retirement/registry.json` (schema 1) | per-id rows stay valid evidence: a group resolves when **every** id of it is mapped |

**A behaviour is proven** when it lists Rust tests (`rs:<path>.rs::<fn>`, found by `syn`) and evidence, every
test is shown `ok` by a **cited PASS gate** log (same rule as schema 1), its catalogue row has no finding, and
every fault it lists in `must_catch` **or that lists it in `must_break`** is closed. The `status` column is never
trusted; `status proven` the evidence does not back is a finding (`status_overclaim`).

**A fault is closed** by one row in `fault-catches.tsv` (`fault status evidence caught_by reason approver`):
* `caught`: `caught_by` is an existing Rust test of a behaviour the fault must break, and a cited PASS gate
  log has the line `FAULT-CAUGHT <fault-id> by <test name>` (a mutation script prints it only after the mutant
  made that test fail for the intended reason; a harness crash prints nothing);
* `adjudicated`: `reason` is `equivalent|unviable|timeout|not-run: <text>`, `evidence` names ledger records,
  `approver` is allowed. A positive-control fault can only be caught.

### Fault catch lines (for lanes that write mutation scripts)

A mutation run proves a catch by printing, in the log of a **PASS** gate record (`xtask gate` / `v02.py gate`),
one line per caught fault:

```
FAULT-CAUGHT <fault-id> by <test name>
```

* The line starts with `FAULT-CAUGHT ` (leading/trailing blanks are ignored), then the fault id as in
  `FAULTS.tsv`, the three characters ` by `, then the test name. Only the first blank-free word after ` by `
  counts; anything after it is ignored (so a note may follow).
* `<test name>` is the libtest name: the part of the `rs:` id after `.rs::` (`<mods>::<fn>`, or only `<fn>` for
  a top-level test). A longer module path ending with it is also accepted.
* Print it **only** after the planted fault made that very test fail for the intended reason. A harness
  crash, a compile error, a timeout or an unrelated failing test prints nothing. The mutated source is
  restored byte-exact and the process group reaped before the line is printed.
* The line alone is not enough. `fault-catches.tsv` needs a row `fault caught <EV-id> rs:<path>.rs::<test>`
  (the other columns empty) whose `EV-id` is a PASS gate record **whose log contains that line**, and the test
  must be a test of a behaviour the fault is listed to break (`must_break` / `must_catch`). Otherwise the
  fault stays open (`catch_test_unknown`, `catch_not_proved`, `catch_by_unrelated_test`).
* No line, no catch: a fault that cannot be exercised is adjudicated instead
  (`adjudicated`, reason `equivalent|unviable|timeout|not-run: <why>`, evidence, allowed approver; a
  positive-control fault cannot be adjudicated).

**A group resolves** by one of:
* `evidence_for`: it names B-ids, each exists and is proven;
* a retirement category (`not_shipped`, `python_only`, `structure_only`, `superseded_tooling`, `not_executed`):
  a reason (`reason_or_reachability`), its file in the archived inventory (rollback = the file at
  `groups.json` `rollback_sha`), an allowed approver and a real date (default `integrator-provisional`,
  RU-034); `not_executed` also needs a reachability argument (>= 40 chars, names code, states a verdict;
  the owner reviews the arguments); `superseded_tooling` names existing `B-REL-*` behaviours (plan section 5);
* schema 1: every test id of it is resolved by a schema-1 row.

Findings include (all reported, none fatal): `row_columns_invalid`, `id_invalid|duplicate`, `area_unknown|prefix_mismatch`,
`column_empty`, `v014_source_invalid` (needs `<file>:<lines>` and ` @ f4e366e9`), `kind_invalid` (V U M S J N D joined
by `+`), `oracle_invalid` (executed-v0.1.4 / independent-model / spec, or `author-reference golden` for NUM rows only, RU-050), `key_cases_invalid` (items split on `;`
outside brackets; `[a,b)` balances), `must_catch_unknown_fault`, `fault_back_reference_missing`,
`must_break_unknown_behaviour`, `behaviour_back_reference_missing`, `positive_control_missing`,
`rust_test_malformed|unknown`, `evidence_malformed|unknown|not_pass_gate`, `status_invalid|overclaim`,
`group_*`/`disposition_*`/`n_tests_*`/`inventory_file_without_group`, `catch_*`.
Limits: whether the Rust tests cover every harvested **key case** is not machine-checked (no mapping exists
between a key case and a test); the owner's sample review covers it.

---

# Schema 1: the per-test-id registry (kept as evidence)

R6 replaces the precursor count pins (41/42) with a diff of **test IDs and obligations**. Counts never
enter the decision: a Rust suite may be larger or smaller than the Python one.

| Side | ID | Source |
|---|---|---|
| old (Python v0.1.4) | `py:<file>::<test name>` | every definition in `test-support/obligations/data/test-groups.json` (static discovery, **not** pytest-collected; parametrised IDs are unknown, see its README) |
| new (Rust) | `rs:<path>::<module path>::<fn>` | every `#[test]` / `tokio::test` function found by `syn` over the workspace (macro-generated tests are invisible) |

`registry.json` (this directory) is the approved old -> new mapping:

```json
{"schema": 1,
 "baseline": {"sha256": "<sha256 of test-groups.json bytes>", "source_sha": "<commit the inventory was cut from>"},
 "approvers": ["integrator", "owner"],
 "rows": [
   {"old": "py:test_x.py::test_a", "disposition": "mapped", "new": ["rs:crates/x/tests/x.rs::a"],
    "obligation": "O:path:test_x.py", "approver": "integrator", "approved": "2026-10-28", "evidence": ["EV-20261028-0004"]},
   {"old_file": "test_y.py", "disposition": "external_oracle", "reason": "...", "obligation": "...", "approver": "owner", "approved": "...", "evidence": ["EV-..."]},
   {"old": "py:test_x.py::test_b", "disposition": "retired", "reason": "...", "rollback": "tests/test_x.py @ <sha> (archived)",
    "obligation": "...", "approver": "...", "approved": "...", "evidence": ["EV-..."], "mutation_verified": false}
 ]}
```

* A row names exactly one of `old` (one ID) or `old_file` (every test of that file). Two rows may not claim the same ID.
* `mapped` needs >= 1 `new` Rust test that exists now (consolidation = several rows, one `new`). `retired` needs `reason` and `rollback`. `external_oracle` needs `reason`.
* Every row needs an `obligation` present in `obligations.json`, an `approver` from `approvers`, a real `approved` date and >= 1 `EV-` evidence id (checked against the ledger when `--ledger` is given). `mutation_verified: true` needs a `kill_matrix` EV id; otherwise the retirement is not labelled mutation-verified (PLAN section 9.4).
* A row with any finding resolves nothing; a wrong pin resolves nothing at all.

```sh
cargo xtask retire check --schema 1 --ledger <registry>/evidence.jsonl   # id-level: exit 1 while any old ID is unresolved
cargo xtask retire check --schema 1 --structure-only                    # rows well-formed; NOT a release pass
```

Exit codes: 0 resolved, 1 findings or unresolved IDs, 2 unreadable/malformed input. The release profile's
`r6-retirement` cell refuses a record whose argv has `--structure-only`.

**Evidence must show the test ran.** With `--ledger`, a `mapped` row resolves only if a cited EV is a `PASS` record of kind `gate` whose log has a `test <name> ... ok` line for every `new` test (an integration test must appear under its own `Running tests/<file>.rs` target; a unit test by its module path in any target). A FAILED/ignored line, another target, a FAIL record, a non-gate record or an unreadable log proves nothing (`new_test_not_proved`; the row resolves nothing).

State (measured 2026-10-28 round 12, this Mac, `--ledger registry/evidence.jsonl`): 10,334 old IDs in 1,185 test files, **24 rows resolve 24 IDs** (8 `tests/a13_macos/test_install_command.py` -> `crates/desktop/tests/install_command.rs`, proved by EV-20261028-0230; 12 `tests/swarm_directory/test_directory_core.py` + 4 `test_directory_write_budgets.py` -> `crates/api/tests/swarm_directory.rs`, proved by EV-20261028-0406), 10,310 unresolved; 2,229 Rust tests discovered. Rows were taken only where the Rust test is a named transcription of the Python test (same name, or a longer name that contains it; the file header of each Rust file cites the Python source @ f4e366e9). Assertion-by-assertion equivalence was NOT re-audited by L12 (spot check: the Python delete/replay assertions are all present in `delete_by_owner_retires_the_id_...`); that is the integrator's approval. The 8 directory tests whose Python assertions I could not match one-to-one (core: unsigned/forged order, heartbeat status expiry, grant deposit, canonical bytes (the Python float/bell refusals are not asserted in Rust), signed-read budget x2; write budgets: unauthorised heartbeats, signed queries; that is 6 of 18 core + 2 of 6 write-budget tests) are deliberately left unresolved.
The `approver` of these rows is the provisional role `integrator` (R6 approvals: integrator/owner accepted for now): L12 wrote them from the evidence above and the integrator has not signed them individually; confirm or strike. The inventory is cut from `f5bfb709`, not the oracle `f4e366e9`; rebinding it is CL-L0-02.
