# RED-by-design suites: default collection policy and operator opt-in

**Scope.** Five lane paths use the historical `scaffold_red` opt-in policy. A10/A11/A12/A14b
exercise unimplemented behaviour; A7's in-memory contract core now passes, but its runtime and
physical gates remain unimplemented. A marker is not a verdict. This
note is the single place that says how they are kept out of ordinary runs and how an operator runs them
deliberately.

**Provenance.** Repository `/Users/evinova-self/Documents/playground/mycelium-core-pilot`, branch
`integration/core-pilot`, original policy base commit `349845b`. The policy is registered in `pytest.ini` and applied by
one `conftest.py` inside each suite directory. The original collection-policy change edited no test
assertion or guard. The A7 import adds refusal regressions and strengthens the deterministic
core, including the terminal-state refusal; it does not change the marker mechanism or claim runtime closure.

## The mechanism

| Piece | Where |
| --- | --- |
| Marker registration + default deselect | `pytest.ini` (`markers =`, `addopts = -m "not scaffold_red"`) |
| Marker applied to each suite | `tests/<suite>/conftest.py` (`pytest_collection_modifyitems`, `tryfirst`) |

Default runs therefore **collect and then deselect** every item carrying `scaffold_red`, so an ordinary
full-suite run stays green with respect to these five suites. An explicit `-m` on the command line
overrides the `addopts` default, which is what makes the opt-in below work.

Two operational consequences:

- **A CLI `-m` replaces the default filter.** `addopts` is a default only, so a `-m <expr>` you pass is used
  *instead* of it. If you filter a full run by marker yourself, carry the policy term along
  (`-m "not slow and not scaffold_red"`) or the five suites come back into that run. `-k`, `--ignore=`,
  `--deselect=` and plain path arguments do not have this effect: the default filter stays in force.
- **Deselection happens after collection.** The scaffolding modules are still imported on a default run
  (collecting them is what the marker then filters), so this policy removes their failures from the result,
  not their import cost.

## The suites, and what they contribute

| Suite | RED test modules | Collected items |
| --- | --- | --- |
| `tests/a10_scaffold` | both modules | 79 |
| `tests/a11_scaffold` | all three modules | 57 |
| `tests/a12_scaffold` | all three modules | 59 |
| `tests/a14b_acceptance` | both modules | 88 |
| `tests/a7_acceptance` | `test_kv_successor_contract.py`, `test_kv_successor_boundaries.py` — deterministic core GREEN, not runtime closure | 41 of 51 |

`tests/a7_acceptance/test_inventory.py` (10 items) is the already-landed frozen inventory and stays in
default collection: only the two KV-successor modules are marked. In the 2026-09-21
closure-train candidate after the terminal-state repair, all A7 tests passed 51,
and default collection passed the same 10 inventory tests with 41 deselected.
The five paths collected 324 marked items (334 total); these are measured snapshot counts,
not a fixed contract. Parent logs: `runs/20260921-closure-train/a7/parent-final-verification/`
under the external program root. Earlier 40/50 and 323/333 snapshots remain historical,
before the terminal-state regression was added. No A7 runtime seam is wired.

## Operator invocation

Run the five suites, and only them:

```sh
cd /Users/evinova-self/Documents/playground/mycelium-core-pilot   # or your worktree of this branch
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /opt/homebrew/bin/python3.14 \
  -m pytest -q -p no:cacheprovider -m scaffold_red \
  tests/a10_scaffold tests/a11_scaffold tests/a12_scaffold \
  tests/a14b_acceptance tests/a7_acceptance
```

`pytest --markers` also lists `scaffold_red`, so the opt-in is discoverable from the tool itself. A
repo-wide opt-in (`... -m pytest -q -p no:cacheprovider -m scaffold_red`) works too, but must add
`--ignore=scripts` while `scripts/a13_operator/test_bootid_guard.py` calls `sys.exit()` at import and
aborts collection of the whole tree (pre-existing, unrelated to this policy).

## What this policy does NOT prove

- **Excluding a suite does not make it pass.** Four suites still describe missing functionality.
  A7's deterministic core passes explicitly; its boundary tests also check intentional unimplemented
  exceptions. Those exceptions are weak evidence of absent runtime behavior, not executed recovery.
- **A green default run no longer implies the scaffolding suites ran.** It implies the authoritative
  selected surface ran and passed. Report the actual invocation and deselection count; neither
  default green nor opt-in green establishes full product or physical closure.
- The deselection is a *collection* policy: it is not a skip, not an `xfail`, and it is not evidence that
  the missing gates were implemented.
