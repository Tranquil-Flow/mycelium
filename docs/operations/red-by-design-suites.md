# RED-by-design suites: default collection policy and operator opt-in

**Scope.** Five delivered lane suites exercise behaviour the corresponding gate has **not implemented
yet**. They fail on purpose: they are the honest, executable statement of missing functionality. This
note is the single place that says how they are kept out of ordinary runs and how an operator runs them
deliberately.

**Provenance.** Repository `/Users/evinova-self/Documents/playground/mycelium-core-pilot`, branch
`integration/core-pilot`, base commit `349845b`. The policy is registered in `pytest.ini` and applied by
one `conftest.py` inside each suite directory. No test assertion, expectation or guard was changed, and
no lane test module was edited.

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
| `tests/a7_acceptance` | `test_kv_successor_contract.py`, `test_kv_successor_boundaries.py` (37) | 37 of 47 |

`tests/a7_acceptance/test_inventory.py` (10 items) is the already-landed frozen inventory and stays in
default collection: only the two KV-successor RED modules are marked.

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

- **Excluding a suite does not make it pass.** The five suites are still failing; the default run simply
  stops claiming to have exercised them. Their red state is unchanged and visible via the opt-in above.
- **A green default run no longer implies the scaffolding suites ran.** It implies the authoritative
  surface ran and passed. Anyone reading a green full-suite result must know that ~320 items were
  deselected by design.
- The deselection is a *collection* policy: it is not a skip, not an `xfail`, and it is not evidence that
  the missing gates were implemented.
