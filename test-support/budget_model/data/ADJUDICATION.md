<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Manual adjudication (RU-042, L1b round 13)

The mechanical levers of the inventory are exhausted, so the rows that are still pending are decided by a
reviewer: one verdict per row, in one TSV per partition. The engine
(`src/inventory/adjudication.rs`) rebuilds the reviewed row from the verdict and validates it against the
pinned oracle (v0.1.4 `f4e366e98a1f705dfaf182897044500d290dbd7f`, read-only). A stale or wrong citation fails
the gate.

## Files (a partition lane writes ONLY its own three)

| file | owner | content |
|---|---|---|
| `python-adjudication-partitions.tsv` (+ `.sha256`) | round 13, frozen | `key partition basis` for every row without a `python-bounds.tsv` row (703 rows: A 369, B 334) |
| `python-adjudication-a.tsv`, `python-oracle-line-pins-a.tsv` | lane A (L1b) | verdicts, and the oracle lines they cite |
| `python-adjudication-b.tsv`, `python-oracle-line-pins-b.tsv` | lane B | same, for partition B |
| `python-launch-graph.tsv` | round 13, frozen | RU-046 static launch graph for all 725 scoped files |
| `tests/inventory_adjudication_a.rs` | lane A | A's pinned counts; lane B adds `inventory_adjudication_b.rs` |

Nothing else is shared: both files and both pin files are already wired into the engine, `python-bounds.tsv` is
not touched, and new registry ids go between the lane's own marker comments in `crates/budgets/src/table.rs`
(`partition A ids` / `partition B ids`, separated by a spacer).

## Verdict row (`python-adjudication-<a|b>.tsv`, tab separated, header `key verdict target citations rationale reviewer`)

* `key`: a census candidate key assigned to this file's partition.
* `verdict` and `target`:
  * `bounded`: `target` is a numeric registry id; the row takes its unit and value from the registry;
  * `derived`: `target` is a numeric registry id; the row carries it in `relation` and no value;
  * `not-a-bound`, `fixture-only`, `test-only`, `UI-cosmetic`: `target` is `-`;
  * `not-executed`: `target` is `launch-graph`; the row's file must be `unreached` in `python-launch-graph.tsv`.
  * `source-unbounded` (round 15, RU-033): `target` is the row's OWN `Value::Blocked` registry id (see below); the older
    family tables (`python-source-unbounded.tsv`, `python-unbounded-propagation.tsv`) keep their prefixes and are untouched.
* `citations`: ` | `-joined `path:line @ f4e366e9 sha256=<hex of that exact oracle line>`. The row's own line must
  be cited; for `bounded`/`derived` the registry provenance (`path:line @ f4e366e9|ef9ae61d|f5bfb709`) must name
  one of the cited lines. Every cited line must be in the lane's pin file with the same digest (sorted by path,
  line; the file is exactly the set of lines the lane cites; the digest is SHA-256 of the line text split on `\n`,
  `\r` removed) and inside the scoped file.
* `rationale`: at least 24 characters, no tab or newline; state predicate direction and strictness for a bound.
* `reviewer`: your lane id and round.

## How to add verdicts

1. Read the oracle source (never execute it); decide the verdict from the code, not from the forecast class.
2. Append the verdict rows; add the cited lines to your pin file; add a registry id only for a value v0.1.4
   enforces that has no id yet, with `Source("path:line @ f4e366e9")` equal to a row's own path:line.
3. `cargo test -p budget-model --test inventory_adjudication --test inventory_adjudication_<a|b>` (the pin
   tests re-derive every pinned line from the oracle blobs); update your partition test's counts.
4. Never edit another lane's files, the partitions table or the launch graph.

The global report (`cargo run -p budget-model --example inventory_gate`; a report since RU-053, formerly a gate that had to exit 0) reads `python-bounds.tsv` plus both
verdict files (`inventory::all_reviewed`); pending = candidates minus those rows.

## Round 14 (L1b, partition A)

* Census gaps: six `manual-bound` rows were appended to `python-candidates.tsv` (and to the partitions table with a
  `module:` basis, `python-scope.tsv` counts bumped): `standing_consent.py:102` (60..3600 s wall cap) and `:53`
  (512 GiB memory cap), `model_resources.py:406` (storage consent ceiling), `token_provisioning.py:148` (1800 s
  process ceiling), `demo_chain.py:836` (member wall cap 60..3600) and `physical_inference_node.py:6576` (0.05 s
  progress wait slice). General rule: any v0.1.4 bound found while reviewing gets a census row.
* Verdict reviewer id `L1b-r14`. A new registry id's `Source` line must be the line of one of its own verdict rows
  (`python_inventory` orphan rule), so a computed ceiling is registered at the first row that enforces it and the
  other rows are `derived` from it.
* Values that are computed rather than literal come from EXECUTING the oracle (Python 3.14, `-B`, oracle tree clean
  before and after): `fx-l1b-storage-consent-ceiling.tsv` (`storage_consent_ceiling()` over the registered models),
  `fx-l1b-model-resource-ceilings.tsv` (`whole_model_member` / `model_resource_proposal`) and
  `fx-l1b-node-computed-ceilings.tsv` (`COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS`, the router lease ceiling);
  `round_14_ids_equal_the_oracle_values_and_the_executed_probe_records` pins registry == FX.

## Round 15: the `source-unbounded` verdict kind (RU-033)

For a row on which v0.1.4 enforces NO ceiling on any path (RU-044: if any path bounds it, the row is `bounded` or
`derived`). Target: a `Value::Blocked` id registered in `table.rs` between the lane's marker comments, one id per row (two
rows may not share it), whose provenance starts `v0.1.4 ships no ceiling at <path>:<census line> @ f4e366e9;` and
contains `V03` (the ceiling is designed in v0.3, never here).

The negative proof is a second per-lane table, `python-unbounded-proofs-<a|b>.tsv`, header `key kind path line needle verdict`:

| kind | path / line | needle | verdict |
|---|---|---|---|
| `hop` | a cited line of the flow | `-` | `origin` (where the value enters), `carrier`, or `read` (exactly one, at the row's own line; at least one `origin`) |
| `scan` | a scoped file or `*` (every scoped file); `line` is the COUNT of oracle lines holding the needle | literal text | `-` |
| `occurrence` | one oracle line holding a scanned needle | the scan's needle | `read`, `declare`, `write`, `floor`, `type-guard`, `relative`, `other-document` (there is no `ceiling`) |

The engine refuses (codes in `adjudication.rs`): a missing proof, no origin, a read elsewhere, a scan without occurrences
(or the converse), a count that differs, a proof line the verdict does not cite, and a citation that is not a proof line.
`tests/inventory_adjudication_unbounded_real.rs` re-derives every scan from the oracle blobs, so an oracle line holding the
needle that is missing from the table fails; the digest of every cited line is re-derived by the pin tests as before.
