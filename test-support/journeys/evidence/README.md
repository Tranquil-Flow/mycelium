# Retained lane verification

Outputs are real command captures. Only the absolute lane worktree prefix was
replaced with `$WORKTREE`; counts, verdicts and elapsed times were not changed.
The host-priority guard ran before every cargo invocation; no downloads occurred
(`--offline`). No fixture/mocked response below is credited as product serving.

- `tests.txt`: `nice -n 19 cargo test --offline -j 2 -p journey-tests -p budgets -p obligations`; exit 0, 44 tests/doctests at that checkpoint, real 9.07 s.
- `final-journey-tests.txt`: after adding the early-child-exit lifecycle control,
  `/usr/bin/time -p nice -n 19 cargo test --offline -j 2 -p journey-tests`; exit 0,
  17 tests, real 1.81 s. The unchanged dependency tests remain in `tests.txt`;
  do not sum both logs as independent case counts.
- `final-clippy.txt`: `nice -n 19 cargo clippy --offline -j 2 -p journey-tests --all-targets -- -D warnings`; exit 0.
- `final-lint.txt`: `CARGO_BUILD_JOBS=2 nice -n 19 cargo xtask lint`; exit 0,
  `lint: 40 source files, 0 violations`.
- `clippy.txt` and `lint.txt`: preceding green checkpoint, retained rather than replaced.
- `resolved-workspace.lock`: exact offline dependency resolution used for verification.
  Parent-owned lockfile integration remains necessary; this is NOT permission to
  overwrite another lane's additive lock changes. Root Cargo.toml/Cargo.lock are
  unchanged in this lane commit.

External immutable product receipts stay in the private program evidence directory:
`runs/v2/v02/L3-JOURNEYS-run-{01,02,03}.json`. The first is a retained FAILED harness
attempt. The second captured the golden. The third ran the strengthened teardown
and unchanged assertions against that golden:

```text
"PASS":observations=20:launches=5:elapsed_ms=15091
real 16.27
user 10.28
sys 1.11
PASS:observations_match:20
```

The last line is a separate real `journey-tests compare run-02.json run-03.json`
invocation, exit 0. Five child exits were 0; all five listeners closed; no recorded
PIDs or scratch homes survived. Every launch rehashed the certified executable.
Golden SHA-256: `7cf9117328db53f2c9e2e129a30ba3d070a20a425e26837643a8b4127eead114`.

The comparator's controls mutate all 68 retained fields and all 20 statuses, plus
missing/extra observations and always-refuse. Golden observations are path-level
L0-tagged; none closes an entire incident F-ID.

The measured final crate tests plus product journey command sum to 18.08 s on this
host, excluding a cold dependency build, host-priority waiting and future excluded
journeys. This is NOT a timing qualification of the full combined F14/F15/F15b
regression. No statistical p99 is inferred from these runs.
