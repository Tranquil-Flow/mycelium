# AGENTS.md — mycelium-rs (v0.2 Rust port)

Program root: `~/Projects/mycelium-core-program` (env `MYCELIUM_PROGRAM_ROOT`). Older docs that say
`/Users/mycelium-dev/Documents/mycelium-core-program/...` mean this path; never read or list `~/Documents`, `~/Desktop` or `~/Downloads`.

Read first: `$MYCELIUM_PROGRAM_ROOT/status/V02-PLAN.md` (sections 0, 1, 5 and 7 at least),
then `/Users/mycelium-dev/Projects/mycelium-v02-process/AGENTS.md` and `docs/BUILDING.md` there (adopted pipeline rules).

- One lane = one worktree + one branch `v02/<lane>`; never write another lane's paths. The parent integrates into `main`.
- Zero Python in this repository, its tests, CI or release path. No PyO3. The old Python app is an external black-box oracle only.
- AGPL-3.0-only; SPDX header on every new source file. Commits as Tranquil-Flow <tranquil_flow@protonmail.com>, no trailers. No push, no remote.
- Never delete or loosen a test to get green. Every timeout/duration comes from the typed budget table (lane L1); no literals.
- No silent errors: every fallible result is propagated or recorded with a specific code (lane L4 enforces).
- Heavy commands (`cargo build/test/clippy` beyond a single small crate) run `nice -n 19`. The job count is whatever the foreman sets in
  `CARGO_BUILD_JOBS` (do not pass `-j`; this replaces the old `-j 2` rule), and only when no
  `~/mycelium-demo-run/*.busy` marker exists and no `PARENT-cert-*-when-quiet.sh` wrapper is waiting (v0.1.4 release has priority).
- Fast loop: `nice -n 19 cargo xtask quick`. It takes the crates changed versus `main` (committed, staged, unstaged and untracked),
  adds their reverse dependencies, then runs `cargo fmt --check`, `cargo clippy -p <crates> --all-targets -- -D warnings` and
  `cargo nextest run --profile quick -p <crates>`. `--base REV` changes the base, `--list` prints the selection without running.
  The toolchain is pinned in `rust-toolchain.toml`; nextest profiles (`quick`, `merge`, `nightly`) live in `.config/nextest.toml`.
  A merge needs `cargo nextest run --profile merge`; `nightly` also takes `--run-ignored all`.
- Downloads: crates only, total under 300 MB per lane; no models, no toolchains.
