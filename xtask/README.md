# Rust foundation policy tools

## Run

From the workspace root, while the integrator-owned root Cargo alias is pending:

```sh
export CARGO_ALIAS_XTASK='run --offline -j 1 -p xtask --'
cargo xtask lint
cargo xtask preflight
cargo xtask preflight --group xtask-unit --group lint-policy
cargo test --offline -p xtask -j 1
cargo clippy --offline -p xtask --all-targets -j 1 -- -D warnings
cargo fmt -p xtask --check
```

The repository's `.cargo/config.toml` defines the alias, so plain `cargo xtask lint`
and `cargo xtask preflight` work from the workspace root. No user-global Cargo config
is modified. Direct invocation always works: `cargo run --offline -p xtask -- lint`.
The workspace lockfile is integrator-owned; regenerate it offline after merging.
Dependencies are crates.io-only, and no plugins or toolchain downloads are used.

Both subcommands accept `--root PATH`; the default is the workspace containing
the built xtask crate. Unknown arguments fail. Exit codes: **0** policy clean /
selected mandatory groups admitted, **1** violations / mandatory blocked groups,
**2** malformed configuration, unreadable input, source parse error, or usage.
Preflight is readiness only, never a test runner or a PASS receipt.

## Syntax policy

Every `.rs` under the root is read, including tests, build scripts, untracked
sources, and cfg-disabled code. `.git`, `target` build outputs, and exactly
`xtask/fixtures/**` are excluded. Other crates' fixtures are checked. Symlinks
outside those excluded paths refuse inventory rather than silently omit sources.
Each finding prints `workspace-relative-file:line: RULE: explanation`.

| Code | Rejected shape | Accepted idiom |
|---|---|---|
| SE001 | `let _ = call()` / method call / awaited call, including typed wildcard | `operation()?`; retain a value; explicitly record best-effort error |
| SE002 | discarded `.ok();` expression statement | retain or return the `Option`; propagate the original error |
| SE003 | empty/comment-only/unit `Err(..)` arm or `if let Err(..)` body, including let-chains | propagate or call a named diagnostic recorder |
| SE004 | statement `std::thread::spawn`, `thread::spawn`, `tokio::spawn`, `tokio::task::{spawn,spawn_blocking}` | retain the handle and observe `join` / `await` |
| SE005 | a cleanup error branch falls through to explicit unconditional success in the same block; or directly returns success from the error handler | propagate the cleanup error before success; do not claim best-effort cleanup is proven |
| TM001 | numeric literals in `Duration::from_{secs,millis,secs_f64,secs_f32,micros,nanos}` | inject the typed budget or measured value |
| TM002 | numeric literal at a locally declared parameter whose name contains `timeout`, `deadline` or `ttl`; name-based fallback for external time-named APIs | pass a budget value; numeric byte/count/measurement arguments remain valid |
| CR001 | hand-written cryptography (RU-010, CL-L4-10): an integer literal equal to a published SHA-1/SHA-2/ChaCha/Poly1305/Curve25519/Keccak constant in any base or suffix (also inside macros), the ChaCha sigma text, the first AES S-box bytes in a row, a `fn` named like a primitive internal (`quarter_round`, `sha256_compress`, `fe_mul`, ...), a `fn hmac*`/`hkdf*` (exactly the primitive's name, e.g. `hmac_sha256`) whose body names no admitted crate, or a function body holding both HMAC pad bytes | call the admitted crates (`hmac`, `hkdf`, `sha2`, `chacha20poly1305`, `x25519-dalek`, ...); published constants in comments, docs and strings are not code |
| CG001 | constant guard in test code (CL-L4-11): `if`/`while`/match-guard/`assert!` condition decided by a literal (`false && ..`, `true \|\| ..`, `!true`), a literal operand of `&&`/`\|\|`, or a file-local `const X: bool = <literal>` used as the condition. Test code = files under `tests`/`test-support`/`test_support` and items under `#[cfg(test)]`/`#[test]` | a condition that depends on state |

CR001 and CG001 exceptions use the same `xtask/allowlist.json` (owner, reason, expiry, replacement plan,
exact source line). Fixtures: `fixtures/{crypto,guards}/{reject,accept}`; the published constants in the
reject files are cited in each file.

## Fixture provenance (CL-L4-09, WARN level)

`lint` also inventories expected-value fixtures. A fixture is any file below a crate whose path (relative to
the crate) has a directory named `fixtures`, `testdata`, `golden` or `goldens`, or starts with `tests/data/`.
Each needs a row in that crate's `fixtures/PROVENANCE.tsv` (header, kinds and hash rules in
`src/provenance.rs`): `path`, `fixture_id` (`FX-<lane>-<name>`), `kind` (`oracle|spec|recorded|input`),
`probe`, `probe_sha256`, `oracle_sha` (40-hex commit for `oracle`), `case_count`, `fixture_sha256`.
Findings FP001 (no row, listed one per file and counted per crate), FP002 (malformed), FP003 (stale sha),
FP004 (row names no fixture), FP005 (duplicate id/path), FP006 (symlink). They print as `WARN` and do not
change the exit status; `lint --strict-provenance` makes them violations. This crate's own rows are
regenerated by `probes/xtask-provenance.sh`.

## `gate` (CL-L4-12): evidence records in Rust

```sh
cargo xtask gate <LANE> <NAME> [--kind test|check] [--registry DIR] [--guard PATH] -- <command...>
cargo xtask gate parse <LOG> --exit N [--kind test|check]
```
Replaces `tools/v02.py gate`. Runs `bash lane-guard.sh` first (its exit code is returned verbatim; a missing
guard is an error, not a skipped guard), then `nice -n 19 <command>` in the lane worktree with the lane's
`CARGO_TARGET_DIR`, `CARGO_INCREMENTAL=0`, no debug info and no `PYTHONPATH`, stdout+stderr to a never-overwritten
`registry/logs/<LANE>/<time>-<NAME>.log`. It parses libtest `Running`/`Doc-tests`/`test result:` lines and cargo
`--message-format=json` messages, and appends one JSON row to `registry/evidence.jsonl` (id assigned under an
exclusive file lock) with exactly the fields of the prototype: `argv at commit exit failed_tests id kind lane load
log name targets total verdict wall_s`. Verdicts: `PASS`, `FAIL`, `COMPILE-ERROR`, `RAN-NOTHING`,
`EXIT-NONZERO-NO-TEST-FAILURE`, `ZERO-TESTS`, `NO-TEST-OUTPUT`, `KILLED`. The process exits 1 for every verdict
except `PASS`, so a command that executed no test cannot be a green gate. `--kind` defaults to `test` for
`cargo test|t|nextest|bench` and `check` otherwise.

Only **TM001/TM002** are exempt under exactly `crates/budgets/**`. The exemption
does not apply to similarly prefixed directories or disable silent-error rules.
Parentheses, negative literals and casts are inspected. Workspace signatures are
collected before calls with crate/module paths, including methods and UFCS
receiver offsets. `crate`/`self`/`super`, `use` renames, module/glob imports,
type aliases, lexical import scopes and explicitly typed receivers are resolved.
An unrelated crate's same-named UFCS method cannot reclassify a free function's
parameters. Unknown receivers conservatively check same-crate and visibly
imported method types; unresolved free functions check same-crate free signatures
only. Direct Duration literals and external time-name fallback remain enforced.
Ordinary expression-list and
statement macro inputs are inspected with source spans; comments/strings are not
code. Rust compiler/Clippy remain separate required checks, with inherited
`unused_must_use`, `let_underscore_must_use`, `let_underscore_future` denied.

**Deliberate first-slice limits:** this is not rustc type resolution, an arbitrary
macro expander, whole-program control-flow analysis, or a proof that every result
is observed. Conventional Cargo source paths determine crate names (hyphens
become underscores); explicit Cargo library-name/dependency remaps, generated
proc-macro/DSL output, arbitrary re-export chains and computed function pointers
need compiler/adapter-specific follow-up. Recursive/unresolved import expansion
is cycle-fenced rather than an unbounded search. An external function
with an uninformative name and unavailable signature cannot have its parameter
names inferred. SE005 recognizes direct cleanup calls with a name component
`stop`, `close`, `cleanup`, `teardown` or `join`, and explicit `Ok`,
`record_success`, `mark_stopped`, `mark_cleanup_complete` successors. It does not
infer recovery proofs, state aliases, arbitrary success fields or interprocedural
cleanup. Diagnostics' code/kind/source-chain/phase and privacy still need typed
product interfaces and behavioral tests; a nonempty handler is not that proof.

## Exceptions

`allowlist.json` starts empty. Version 1 entries require all of:

- `path`, positive `line`, known `rule`, and **exact** `source_line`;
- `owner`, `reason`, `expiry` (`YYYY-MM-DD`), `replacement_plan`.

No globs or parent traversal. Expiry is exclusive: an entry is expired at the
start of its expiry date in **UTC**, using the real system clock. Invalid dates,
unknown fields, duplicates, empty ownership text, stale source and entries that
no longer suppress a real diagnostic fail the command. No inline `allow` comment
can bypass this policy. The fixtures are syntax data, never compiled or executed;
reject annotations are independent expected rule/line oracles.

## Read-only prerequisite and quarantine manifests

`prerequisites.json` names tools, detector shapes and groups (unit, doctest,
custom, native, journey). Detectors only inspect filesystem metadata:

- `path_executable`: bare program in absolute PATH directories, regular file and
  executable permission on Unix; **does not execute it**, validate version or ABI;
- `file`: literal workspace-relative regular file;
- `env_file`: path from a named environment variable; values are not printed.

Group fields are explicit: `id`, `kind`, `mandatory`, `needs`, `after`, `command`.
`command` is a declared selection recipe, not executed by preflight. `after`
expands transitive prerequisites and propagates blocked status. Repeated
`--group ID` limits the selection, but malformed/expired policy anywhere still
fails. Empty manifests, unknown references and cycles fail. This is initial
explicit selection, not changed-file/transitive-consumer inference.

Every selected group prints **RUN** or **NOT_RUN(BLOCKED: reason)**. RUN means
inputs were located, not that tests ran or native/package identities were checked.
Mandatory blocked groups force exit 1. Native and oracle groups intentionally
remain blocked until their crate manifests and externally admitted input
manifests exist. No fixture acquisition, builds, network, shell probes, native
execution, app launch or device activity occurs. Automatic verified setup and
candidate/ABI/content validation remain later integration work, not green skips.

`quarantine.json` starts empty. Entries require `group`, `owner`, `reason`,
`expiry` and nonempty `replacement_coverage` (existing distinct, nonquarantined
group IDs). Unknown fields, duplicate/unknown groups, invalid/expired dates and
missing coverage fail. Valid quarantine is still visibly NOT_RUN/BLOCKED and
cannot make a mandatory selection green; replacement readiness is not a passing
replacement-test receipt.

## Required real evidence inputs (not generated by preflight)

Keep these **private, operator-admitted** files outside the repository. Set each
variable to its actual regular-file path; unset/empty values, missing files and
directories are mandatory `NOT_RUN(BLOCKED)` with exit 1, not skipped passes.
Never use an empty JSON file, this README, a Cargo manifest or a synthetic test
receipt to clear a real qualification gate.

- **`MYCELIUM_NATIVE_INPUT_MANIFEST`**: the L8/L11/integrator-admitted inventory
  of the exact candidate's native inputs. Required evidence identifies candidate
  source/lock/toolchain, target architecture, compiler/SDK and minimum OS;
  official mlx-c/MLX revisions; the header-tree hash and the paths, sizes and
  SHA-256 identities of `libmlxc.dylib`, `libmlx.dylib`, `libjaccl.dylib` and
  `mlx.metallib`; license notices; selected model/checkpoint/profile and fixture
  provenance; and links to actual native test receipts. Bind the packaged
  resources and executable to the same candidate, with relocation and minimum-OS
  receipts separately required for package qualification (integration seam S14).
  `backend-mlx/build.rs` validates `MLXC_INCLUDE_DIR` / `MLXC_LIB_DIR` directly
  when `metal` is enabled; this preflight variable does **not** configure them.
  Checkpoint variables and selected native commands are documented by L8.
- **`MYCELIUM_ORACLE_MANIFEST`**: the L0/L3-admitted external oracle target
  manifest. The runner's actual JSON keys are `implementation`, `source_sha`,
  `executable` (absolute packaged executable path) and `executable_sha256`.
  Use the accepted oracle identity allowed by the integrated journey runner,
  not an illustrative digest or a moving checkout. Retain its certification,
  package/device provenance, immutable golden corpus and actual command/result
  receipts alongside it; those receipts must state unqualified scenarios. The
  crate is named `journey-tests` but its manifest is
  `test-support/journeys/Cargo.toml`. See that crate's README for the supported
  `--target`, `--receipt`, `--golden` and scenario arguments. An environment
  variable alone does not pass `--target` to the runner.

**Enforcement boundary:** `env_file` currently checks metadata only. No shared
native evidence JSON schema or candidate/content authenticator is implemented
here; the native fields above are the required evidence handoff, not invented
parser keys or an enforced schema. The oracle runner and native builder do
additional independent validation. `RUN` cannot certify a document's contents.
The commands in `prerequisites.json` are descriptive initial selections: the
native recipe lacks `metal`, and the historical `real-defaults` journey recipe
is not the implemented runner's invocation. They must be reconciled by their
owners before any execution scheduler uses them; preflight never runs them.
No approved real manifests are produced or substituted by unit tests. Missing
inputs leave foundation/native/oracle acceptance open even when Rust tests pass.

## F16 provenance

These are **MODEL syntax negative controls**, not execution of the Python app.
`L13-ROLLSTOP.md:15` records the historical 30-second stop/join waits and swallowed
stop exception. The pinned source `f5bfb709` already contains the repair:

- `mycelium_a13_macos/demo_chain.py:160-166 @ f5bfb709`: stop/stop-before seam.
- `mycelium_a13_macos/demo_chain.py:1350-1384 @ f5bfb709`: records stop/late-stop
  failures, retains original deadline, then requires target cleanup proof.
- `mycelium_a13_macos/chain_compile.py:415-443 @ f5bfb709`: joins and rechecks final
  workflow errors before declaring stopped.
- `mycelium_a13_macos/chain_compile.py:480 @ f5bfb709`: retained legacy
  `stop_timeout=30.0` override, anchoring the literal negative control.

`fixtures/reject/f16_stop.rs` injects `let _ = stop(route)`, an empty error arm
around join, and `Duration::from_secs(30)`. The accepted model propagates stop and
join failures and requires receipts before returning success. No measured p99,
physical timing result, product-port GREEN, or full foundation exit is claimed.

## Release state machine (`xtask release`, round 11)

`cargo xtask release candidate <sha> --manifest FILE (--attempt ID | --resume ID)` runs
preflight -> package -> {cert, device} -> inventory -> set-aside -> proof -> promotion over the
**records**: `registry/evidence.jsonl`, `registry/CLAIMS.tsv` and `xtask/release-profile.json`
(claim -> cells; draft 1: cells for evidence no tool emits yet name the record they need).
A candidate is ACCEPTED only when every cell of every mandatory claim is green on its exact bytes:
records carry a `candidate` object (`xtask gate --candidate MANIFEST` writes it after checking the
worktree HEAD and the exe/bundle digests before and after the command); the latest bound, fresh
record decides; zero-test, stale, wrong-digest, unbound or log-disagreeing records are red. The
claim table's status is never evidence (only `blocked` blocks). Attempt roots hold `state.json`,
`events.jsonl`, `set-aside.json`, `promotion-receipt.json` (cites EV ids and log sha256);
a receipt that stops being true is renamed, never deleted. `release status` is the read-only
per-cell view; `release publish` is a refusing stub (exit 3) that needs an owner token file.
`xtask retire check` is the R6 test-ID check: see `retirement/README.md`.
