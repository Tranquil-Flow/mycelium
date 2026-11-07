<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Python bound inventory — L1b WIP, not full closure

Discovery is pinned to **ef9ae61de0222c78d83c8133f8f709619394bf51**. Scope is
exactly L0's 725 `packaged_python_source` paths, including packaged scripts.
The reviewed subset may retain provenance at
`f5bfb70981569c79cb873afe8e0c5be20342db7e`; this is explicit, not a relabeling of
old evidence. The baseline L0 manifest remains at its original identity.

## Re-pin and artifacts

The scoped `git diff f5bfb709 ef9ae61d` changed only
`mycelium_a13_macos/managed_runtime.py`. Only that file was rescanned; unchanged
census hashes/discovery were retained. `python-repin.json` records both hashes,
the changed path, and discovery counts. `python-repin-relocations.tsv` records
the reviewed 15-second join moving from old line 525 to final line 540 without
rewriting its reviewed key or SHA. The final worker-owner-stop receipt's
`max_attempts` at line 333 is derived from the unique 3-attempt declaration at
line 33 (manually supplemented because `REPLACEMENTS` escaped the keyword
scanner); its line-334 timestamp is an observation, not another wait.

* `python-scope.tsv`: all 725 source hashes, line counts and candidate counts,
  including zero-candidate paths.
* `python-candidates.tsv`: broad discovery queue, **not** proven enforceable
  bounds. Identity is `path:line:column:kind:symbol` (one-based lines, zero-based
  AST columns); `scope` is the lexical class/function path.
* `python-bounds.tsv`: reviewed subset, retaining exact expressions, units,
  value or disposition, provenance, authority and review reasoning. Numeric
  values use integral milliseconds/bytes/entries, never guessed conversions.
* `python-binding-edges.tsv`: reviewed source-to-declaration links. These prove
  lexical provenance, **not** that a caller always uses the default or that no
  runtime mutation is possible. Derived rows deliberately have no numeric value.
* `legacy-budget-ids.txt`: the original 70 stable IDs, retained unchanged.
* `inventory-categories.md`: predicates, examples, category counts and tests.
* `inventory-relations.md`: existing source-derived arithmetic and hazards.

Current checkpoint: **7,241 candidates, 3,610 reviewed, 3,631 pending**.
Reviewed sites comprise 1,235 direct mappings, 404 derived dispositions,
1,792 non-bounds, 146 fixture-only and 33 test-only rows. The registry has
1,263 IDs, including all original 70. These are not whole-product bound totals;
unreviewed expressions and possible discovery misses remain.

## Review discipline

Scratch-only AST analysis parses pinned blobs without importing or executing
Python. The final scope hashes were independently rechecked with Git blobs.
No extraction script is committed or called by the Rust build/runtime.

Category claims are executable: the validator binds a claim to the exact
checked-in candidate and runs the named predicate. Category tests exercise real
source rows and reject unrelated waits. Numeric claims use exact integer
conversion, reject fractional canonical units/overflow and distinguish retry
counts from durations, and propagation speed from milliseconds. A separate
Decimal cross-check was used before writing literal declarations.

Binding review counts stores across branches and rejects parameter reassignment,
import shadowing, ambiguous declarations and cross-file guesses. It distinguishes
same-scope parameters, earlier same-scope assignments, unique module constants
and constructor fields. Casts are **not** generic aliases (`int()` may truncate).
Two additional rules admit only same-scope builtin normalizations whose literal
default is unchanged; fractional truncation and integer-to-float rounding are
rejected. Shadowed builtins were checked against source. Source/target identity,
units, BudgetId relationship and acyclic edges are tested.
A derived default remains configurable; no nominal default becomes a runtime
upper bound merely because it has an ID.

Operator scripts are not automatically tests. Only three explicit packaged
unittest modules receive the test-only predicate. Fixture-data rules exclude
executable timed positional calls and timeout/poll keywords. Windows support is
**not** declared out of scope; only specific path grammar, identifiers, platform
flags and HTTP status mappings are non-bound data. Embedded executable strings
and unmatched policy/duration strings remain pending.

## Admission catalogue (CL-L1-07)

`src/inventory/epochs.rs` holds the one ordered admission catalogue. `EPOCHS`
lists, in admission order, every cohort a `tests/common/*_admission.rs` helper
or `tests/inventory_r52_lane_a.rs` projects (r19, r21, r23-r32, r36, r50, r52
lane a), then the RU-053 epoch. Data:

* `admission-catalogue.tsv` (`epoch kind identity number sha256`): per epoch
  its `manifest` (file, data rows, sha256 of the bytes), its ordered `key`s
  (each with the sha256 of its exact review record line), the `census` keys a
  discovery cohort added (sha256 of the census line), the exact `pin`s it added
  (oracle lines its records cite that no earlier review cited; R52 and RU-053
  name one `pins` file instead), the registry `id`s it added, and, on the
  RU-053 epoch, the pinned pre-RU-053 pending `base` (count, sha256 of the
  sorted keys). It was generated once from the sealed manifests and the
  checked-in tables; the library re-derives every fact and refuses a missing or
  extra pin, a dropped key or a flipped record digest with its own code.
* `ru053-admissions.tsv`: one RU-053 admission record per census key (`key site
  site_sha256 carrier carrier_token consumer consumer_calls behaviour_test
  parity_test`). The site is the key's own v0.1.4 line; `site_sha256` is that
  line's sha256 in the ledger encoding (the line without its terminator, no
  strip()); the carrier is a Rust `crates/<crate>/src/...:line` holding the
  token; `consumer` is the hop list `rust/path.rs:Item=v014/path:line` joined
  by ` > ` (a reviewed root first, the carrier's fn last, mirroring the site;
  at least two hops, none twice); `consumer_calls` names, per consecutive hop
  pair, the reviewed edge rows that join them (their `path:line` sites joined
  by `,`, lists joined by ` > `; no row listed twice in the whole field);
  `behaviour_test` / `parity_test` are `package::target::fn` (joined by
  ` | `; parity `-` when none exists). A list names a test at most once: a
  repeated exact identity within `behaviour_test`, or within `parity_test`,
  is refused `ru053-duplicate-row` before any pin is checked (the same test
  named once in each list is not a repeat). Empty
  here: this task admits no row, so the closure stays 58 (55 + 3 guarded).
* Mapping-only check (RU-053 re-cut, owner INBOX 2026-11-04 04:06). No Rust
  source is parsed and nothing is inferred beyond the listed rows. Four
  reviewed tables, read through the workspace `Tree`, share one pin grammar:
  identity column(s), then `path line sha256 text` = a repo-relative path, the
  sha256 of that file's bytes, a 1-based line (canonical decimal) and the exact
  bytes of that line (split on `\n` only; no tab or `\r`; non-empty). A file
  must end in `\n` and start with its exact header.
  * `ru053-reviewed-roots.tsv` (`root path line sha256 text`): product roots,
    `crates/<c>/src/<file>.rs:Item` pinned at the line declaring `fn <name>(`
    in that same product file. Unique by root and by site.
  * `ru053-reviewed-edges.tsv` (`from to path line sha256 text`): `from`
    reaches `to` on the pinned line of `from`'s own file; a carrier row has `to`
    equal to its own `path:line`. Unique by site and by `(from, to)`.
  * `ru053-reviewed-tests.tsv` (`test path line sha256 text`): a test
    `package::target::fn` pinned at its `fn <name>(` line; the line above must
    be exactly `#[test]`. Unique by test and by site.
  * `ru053-reviewed-crates.tsv` (`crate path line sha256 text`): per crate
    `crates/<c>`, its `Cargo.toml` and `src/lib.rs` (whole-file pins; the line
    names the fact relied on: the dependency or the `pub mod` line).
  The checker only (a) recomputes each used row's sha256 and compares the exact
  line bytes, (b) requires the chain to be contiguous by exact endpoint identity
  from a root to the carrier row of the last hop, with every crate the record
  touches pinned in the crates table, and (c) requires each test to be actually
  collected: exactly one `cargo metadata --format-version 1 --no-deps
  --offline` target of that package and name, `kind = ["test"]`, `test = true`,
  `src_path` the pinned file (no other target on it), no `required-features`;
  the package manifest parsed as TOML (`toml =1.1.6`) whatever the table
  spelling: a `[[test]]` entry naming the target or its path may hold only
  `name`, `path` (equal), `harness = true`, `test = true` or empty
  `required-features`, and `autotests` is not false; the test file holds no
  inner attribute (`#`, `!`, `[`). Codes, one per class:
  `ru053-malformed-row`, `ru053-duplicate-row` (tables, whole catalogue; a
  record's repeated `consumer_calls` row or repeated test within one list);
  `ru053-unresolved-carrier` (carrier line lacks the token),
  `ru053-missing-consumer-mapping`, `ru053-unresolved-consumer-hop` (hop not
  a reviewed identity), `ru053-consumer-not-product`,
  `ru053-duplicate-consumer-hop`, `ru053-consumer-carrier`,
  `ru053-carrier-outside-endpoint` (no carrier row from the last hop),
  `ru053-unmapped-edge` (a listed site, root or pair with no reviewed row),
  `ru053-unconnected-consumer-hop` (rows not contiguous), `ru053-stale-pin`
  (file, sha256 or line bytes differ), `ru053-missing-behaviour-test`,
  `ru053-unresolved-parity-test`, `ru053-uncollected-test`,
  `ru053-unmapped-crate`; and `ru053-guarded-key` for the three guarded stats
  rows. Unused rows are shape-checked only; an edit to any pinned file voids
  every record that uses it until the rows are re-reviewed. The checked-in
  rows hold the validator controls (`ChunkSession::fetch` > `fetch_once` >
  protocol `verify_chunk_receipt`, carrier `!data.is_empty()` at
  `artifact_grant.rs:806`, behaviour test `protocol::artifact_refusals::...`)
  and the reviewed root `ArtifactWireLimits::wait`; they admit nothing in
  production.
* `ru053-admission-pins.tsv` (`path line sha256`): the exact oracle line pins
  the RU-053 records add (lines no checked-in pin table holds yet).

`as_of(epoch)` is the live view (census, reviews, citation pins, registry ids,
with validated RU-053 admissions counted as reviews) with every later cohort
removed; `added_after(epoch)` is what those cohorts added. `report()` counts an
RU-053 row as reviewed only when the whole catalogue, its record included,
validates; a refused catalogue admits nothing and is reported as a violation.
The live closure must equal `projected_closure()`: the pinned base minus the
validated RU-053 keys (`tests/inventory_ru053_admissions.rs`).

## Gates

`inventory::validate` checks structural consistency, exclusive mapping/disposition,
real typed IDs/units/values, allowed source pins, category predicates, numeric
predicates and declaration edges. Changed-file provenance also rejects both
backdating new receipt fields and relabeling the preserved historical row. Tests
preserve old invalid-input refusals and add real-candidate category, relationship
and source-pin tampering controls.

`require_complete` also rejects **every** pending candidate and every reviewed
row absent from discovery. Its result is shown by the `inventory_gate` example,
which under RU-053 is a **report, not a gate**: it prints the inventory counts and
a closure status line (PASS, or the violation count and the first few violations)
and exits 0 whenever the report was produced. It exits non-zero only for real
errors such as unreadable or malformed inputs. Run it with the required guard:

```sh
while ls ~/mycelium-demo-run/*.busy >/dev/null 2>&1 || pgrep -f 'PARENT-cert-.*-when-quiet' >/dev/null; do sleep 120; done
nice -n 19 cargo run -j 2 --locked --offline -p budget-model --example inventory_gate
```

**This checkpoint must fail the completion gate.** Passing consistency tests is
not whole-Python closure. No blanket waiver or ignored test closes the queue.

> RU-053 note: the paragraph above records the checkpoint when the example was a
> gate that had to fail until the queue closed; it is kept as recorded. The example
> is a report now and exits 0 either way. "Passing consistency tests is not
> whole-Python closure" still holds: read the closure line, not the exit code.

Owned crates: 73 passing tests including two doctests. A no-fail-fast workspace
run has 184 passed / 4 failed / 0 ignored, including three doctests. All four failures are
`budget_snapshot_mismatch` in sim's `persisted_corpus`,
`remaining_persisted_corpus` and two `review_regressions` tests after the L2b
merge. They are the same whole-registry identity-fence dependency, not four
unrelated defects. L2c owns the redesign; **no sim file or assertion was edited**.

Unknown phases remain `Phase::Unclassified`; no measured p99, empirical
qualification, guaranteed close or caller authority was invented. Remaining
review includes computed/remaining deadlines, runtime/config inputs, ambiguous
bindings, duration strings, sub-millisecond values, retention slices, and
potentially unbounded operations. A complete candidate queue alone would still
not prove that lexical discovery found every actual source bound.
