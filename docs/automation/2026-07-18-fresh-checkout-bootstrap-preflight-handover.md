# Fresh-checkout bootstrap preflight handover

## Scope and immutable boundaries

Worktree: `/Users/evinova-self/Projects/mycelium-wt-bootstrap-preflight`
Branch: `tool/fresh-checkout-bootstrap-preflight`
Base:   `9d65a75832c34f8cb876a9f7a06459ed60373414`

Owned paths:

- `mycelium_bootstrap_preflight/` — package source.
- `tests/bootstrap_preflight/` — focused tests.
- `docs/automation/2026-07-18-fresh-checkout-bootstrap-preflight-handover.md` — this handover.

All existing source, scripts, locks, CI, and docs are read-only. No fetch,
pull, push, PR, package install, network, remote host, credential access, or
physical qualification occurred. The worktree was created from the exact base
listed above; canonical `main` and other worktrees were not modified.

The product is a deterministic, strictly read-only preflight that reports
whether a checkout already contains the pinned sources, lockfiles, tools, and
already-materialized dependencies required to attempt the existing
verification bundle. It performs no setup and never claims a fresh-machine
proof.

## Single CLI

```
python3.14 -m mycelium_bootstrap_preflight --root PATH --json
```

`PATH` is validated through a held `O_DIRECTORY | O_NOFOLLOW` descriptor; an
initial identity mismatch fails closed and emits a canonical report whose
sole blocker is `repository_root_unsafe`. The CLI requires both `--root`
and `--json`. `argparse` echoes only fixed code/option text on rejection
because the `--root` value is converted by Python's `Path` constructor
inside the parser; no private path, environment value, or credential
appears in stderr/stdout under any failure path.

## Output contract

| Field                                  | Value                                         |
| -------------------------------------- | --------------------------------------------- |
| `protocol`                             | `mycelium.bootstrap_preflight.v1`              |
| `preflight_ready`                      | only when this local preflight found none     |
| `route_ready`                          | always `false`                                |
| `release_ready`                        | always `false`                                |
| `fresh_checkout_proven`                | always `false`                                |
| `physical_qualification_evaluated`     | always `false`                                |
| `verification_bundle_executed`         | always `false`                                |

Distinctions:

- `source_lockfile_prerequisites` reflects lockfile presence, format, and
  index-bytes parity.
- `toolchain_availability` reflects local tool probes.
- `dependency_materialization` reflects directory presence only (counts of
  matching packages and crates); it never installs or repairs.
- `gates_requiring_execution` lists every command the verification bundle
  would run, with `cwd` and `executed=false`; nothing is executed.

## Boundaries enforced inside the preflight

- **No shell**, no install, no fetch, no remote command, no SSH. Subprocess
  runner allow-lists the version probes plus an exact set of `git`
  invocations: `rev-parse --show-toplevel`, `rev-parse --verify HEAD`,
  `status --porcelain=v1 -z --untracked-files=all`, and the `ls-files -z
  --stage --` inventory scoped to the lockfile set.
- **No follow** on every directory and lockfile `open`. Identity (dev/ino)
  of the opened lockfile descriptor is matched to a `fstat` over the
  parent directory with `follow_symlinks=False`.
- **Symlinked `npm_modules`/`registry/cache`** is reported as
  `*_unsafe`. The preflight does not crawl across them.
- **Final re-check**: source `git status` and root identity are re-verified
  after every probe; any drift emits `source_checkout_changed_during_preflight`
  or `repository_root_changed`.
- **Canonical JSON**: `sort_keys=True`, `ensure_ascii=False`, `allow_nan=False`,
  `separators=(",", ":")`, exactly one trailing newline.
- **No secret-bearing leaks**: the runner's stderr is never copied into the
  report when the runner exits non-zero for a version probe; malformed
  version output is replaced with a fixed code (`tool_version_malformed:*`);
  reason codes are enumerated, never raw text.

## Tool compatibility sources

| Tool        | Minimum                                              | Source                                    |
| ----------- | ---------------------------------------------------- | ----------------------------------------- |
| python3.14  | `3.14.x`                                             | CLI contract `python3.14` is fixed in the script name |
| cargo       | `1.91.0`                                             | `native/iroh_transport/Cargo.toml` `rust-version = "1.91"` |
| rustc       | `1.91.0`                                             | same as above                             |
| rustfmt     | bundled with the rust toolchain                     | same as above                             |
| cargo-clippy| bundled with the rust toolchain                     | same as above                             |
| node        | `(20, 19)+`, `(22, 12)+`, or `24+`                  | `ui/web/package-lock.json` `engines.node` range repeated in every package |
| npm         | bundled with `node`                                 | `engines.npm` is intentionally absent; bounded by `node` |

## RED → GREEN evidence

### Focused suite (`tests/bootstrap_preflight/`)

```
$ python3.14 -m pytest -q tests/bootstrap_preflight
........................................................                 [100%]
56 passed in 0.20s
```

### Full Python suite (`python3.14 -m pytest -q`)

```
1218 passed, 2 skipped, 117 subtests passed in 57.07s
```

The focused suite covers:

- happy-path prerequisite readiness for a fully-materialized repository,
  including exact-byte lockfile index matching;
- absent / file / symlink / symlink-parent repository roots;
- descriptor closure when the initial `fstat` raises on a real root;
- a malicious `git rev-parse --show-toplevel` that points elsewhere is
  refused as `repository_root_mismatch` and the false path is not echoed;
- a dirty checkout (with a private name in `git status`) is refused and
  the private name is omitted from the report;
- lockfile mutations — missing, symlink, directory, hardlinked, oversized,
  total-hash-budget exceeded, duplicate normalized path, non-canonical
  path component, oversized bytes / component / depth, digest alias,
  type alias, lone surrogate string, partially raising iterable, mode
  mismatch (`100755`), stage mismatch, symlink index entry, mismatched
  index object bytes, duplicate JSON key, malformed version string;
- `npm`/`cargo` lockfile format and dependency-inventory invariants —
  `lockfileVersion:3` / `version:4` exact ints, only
  `registry+` source, only `[A-Za-z0-9_-]+` names, only `[0-9A-Za-z.+-]+`
  versions, only `[0-9a-f]{64}` checksums;
- node_modules and rust cache invariants — required package/archive
  names must be present under canonical paths, indices/archives
  must not be symlinked, a `node_modules` symlink chains to outside
  must be reported as `unsafe`;
- tool probe handling — every probe is allowed to fail closed, time out,
  return version output via `stderr`, exceed the output budget, or
  return an incompatible version; the codes are all
  `tool_<kind>:<name>` and never include the runner's stderr text;
- a `git status` mutation observed mid-run is detected as
  `source_checkout_changed_during_preflight` and the late private name
  is omitted;
- canonical JSON is a perfect round-trip under `json.loads`, contains no
  absolute root path, no `tmp_path` markers, no `timestamp` substring,
  and no `Traceback`;
- the gate inventory lists every requested verification command with
  `executed=false` and the matching fragments appear in the rendered JSON;
- the `default_runner` accepts only the exact read-only probes and raises
  `ValueError("command is not an allowed read-only probe")` for
  `("npm", "install")`, `("git", "fetch")`, and
  `("python3.14", "--root", "../escape")`;
- the CLI rejects `--json`-less runs and emits stable canonical JSON for
  a missing private root with the marker substring absent from stdout
  and stderr.

### Other gates in this worktree

| Command                                                      | Exit | Observed result                                |
| ------------------------------------------------------------ | ---- | ---------------------------------------------- |
| `python3.14 -m compileall -q .`                              | 0    | No diagnostics.                                |
| `git diff --check`                                           | 0    | No whitespace errors.                           |
| `ruff check mycelium_bootstrap_preflight tests/bootstrap_preflight` | 0    | All checks passed.                              |
| `python3.14 scripts/contract_audit.py`                       | 0    | `contract audit OK: 14 contracts`              |
| `python3.14 scripts/release_security_audit.py`               | 0    | `release security audit OK: 386 tracked files` |
| `python3.14 scripts/claim_boundary_audit.py`                 | 0    | `claim boundary audit OK: 150 source files`    |
| `cd native/iroh_transport && cargo fmt --check`              | 0    | Clean.                                          |
| `cd native/iroh_transport && cargo clippy --all-targets --all-features -- -D warnings` | 0 | `Finished dev profile [unoptimized + debuginfo]` |
| `cd native/iroh_transport && cargo test`                     | 0    | 21 passed (3 integration tests + 18 unit tests, 0 doctests) |
| `cd ui/web && npm run check`                                 | 0    | typecheck, build, `npm test` (vitest), and `npm run test:contracts` all passed; only Vite's existing chunk-size advisory |

For `npm run check` only, `ui/web/node_modules` was materialized from
the canonical sibling (`/Users/evinova-self/Projects/mycelium/ui/web/node_modules`)
through `cp -al` (no install). The copy was removed before staging the
preflight commit. No network, fetch, publish, or private upload
occurred.

## Observed blockers on the live checkout

The preflight, run against this worktree, returns:

```
{
  "protocol": "mycelium.bootstrap_preflight.v1",
  "blockers": [
    "node_dependencies_not_materialized",
    "rust_cache_not_materialized",
    "source_checkout_changed_during_preflight",
    "source_checkout_not_clean"
  ],
  "dependency_materialization": {
    "node_modules": {"materialized": false, "required": 196, "present": 146},
    "rust_cache":   {"materialized": false, "required": 414, "present": 398}
  },
  "fresh_checkout_proven": false,
  "physical_qualification_evaluated": false,
  "verification_bundle_executed": false,
  "route_ready": false,
  "release_ready": false,
  "gates_requiring_execution": [
    {"code": "bootstrap_preflight_tests", "command": "python3.14 -m pytest -q tests/bootstrap_preflight", "cwd": ".", "executed": false},
    {"code": "full_python_tests", "command": "python3.14 -m pytest -q", "cwd": ".", "executed": false},
    {"code": "contract_audit", "command": "python3.14 scripts/contract_audit.py", "cwd": ".", "executed": false},
    {"code": "python_compileall", "command": "python3.14 -m compileall -q .", "cwd": ".", "executed": false},
    {"code": "git_diff_check", "command": "git diff --check", "cwd": ".", "executed": false},
    {"code": "ruff", "command": "ruff check mycelium_bootstrap_preflight tests/bootstrap_preflight", "cwd": ".", "executed": false},
    {"code": "release_security_audit", "command": "python3.14 scripts/release_security_audit.py", "cwd": ".", "executed": false},
    {"code": "claim_boundary_audit", "command": "python3.14 scripts/claim_boundary_audit.py", "cwd": ".", "executed": false},
    {"code": "rust_fmt", "command": "cargo fmt --check", "cwd": "native/iroh_transport", "executed": false},
    {"code": "rust_clippy", "command": "cargo clippy --all-targets --all-features -- -D warnings", "cwd": "native/iroh_transport", "executed": false},
    {"code": "rust_tests", "command": "cargo test", "cwd": "native/iroh_transport", "executed": false},
    {"code": "ui_check", "command": "npm run check", "cwd": "ui/web", "executed": false}
  ]
}
```

The blockers reflect genuine local evidence: this checkout owns uncommitted
preflight files, the dirty `git status` reads back across the preflight
lifetime (`source_checkout_changed_during_preflight`), the local
`~/.cargo/registry/cache` happens to have 398/414 of the locked crates
materialized and `ui/web/node_modules` has 146/196 of the locked
packages present from prior work. Each partial count is reported
honestly — the preflight does not move on, repair, or run the bundle.

## Strict distinction between preflight readiness and fresh-machine proof

| Question                                              | What this tool answers                                          |
| ----------------------------------------------------- | -------------------------------------------------------------- |
| Do tracked lockfiles match the git index byte-for-byte? | Yes / no.                                                      |
| Are the listed tools present and version-compatible?  | Yes / no per tool.                                             |
| Are `node_modules` and the cargo registry present in a usable form for the listed dependency set? | Yes / no, with present-vs-required counts. |
| Is the source checkout clean and stable during the preflight? | Yes / no with `source_checkout_changed_during_preflight`.      |
| Will the verification bundle pass once executed here?  | The tool never claims this. It enumerates the gates for the user to execute; `verification_bundle_executed` is `false`. |
| Is the route qualified on a physical two-host topology? | Out of scope; `route_ready=false`, `physical_qualification_evaluated=false`. |
| Is the release approved?                              | Out of scope; `release_ready=false`.                            |
| Has the fresh-machine bootstrap been proven?           | Out of scope; `fresh_checkout_proven=false`.                    |

The preflight is the upstream gate for the verification bundle, not a
substitute for it. A clean exit here means only: "this checkout can
attempt the verification bundle without setup or mutation". Acceptance
of the bundle's output (or its absence) remains a downstream decision.

## Commits

| SHA    | Subject                                                     |
| ------ | ----------------------------------------------------------- |
| `efaa340` | feat(preflight): deterministic read-only bootstrap prerequisite CLI |

The handover is committed in a separate commit. No merge, push, fetch,
pull, package install, or remote-host action occurred.

## Claim boundary (recap)

`preflight_ready` reflects only this local deterministic preflight.
`route_ready`, `release_ready`, `fresh_checkout_proven`, and
`physical_qualification_evaluated` remain false. The preflight does not
accept physical evidence and does not perform fresh-machine setup,
qualification, or release promotion.
