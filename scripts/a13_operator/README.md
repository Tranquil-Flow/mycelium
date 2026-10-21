# scripts/a13_operator — the two-Mac A13 operator runner (tracked, de-laned)

The operator-side toolchain that drives a **bounded two-Mac A13 preview round**:
build/deploy hand-off → app launch on both Macs → control-surface wait → network
recovery + bootstrap + native hello + compile → work preview/accept + activation →
public test prompt over the live physical route → clean quit.

## Provenance

* **Lane:** `demo/a13-macos` (worktree `playground/mycelium-a13-macos-demo`). The runner
  lived only under the lane's `outputs/a13-macos-evidence/` tree, which the lane's own
  export excludes — so the integrated branch could not run a two-host round from its
  committed bytes alone.
* **Source directory of this copy (READ-ONLY, byte-pinned):**
  `~/Documents/playground/mycelium-a13-macos-demo/outputs/a13-macos-evidence/`
* **Authoritative description of each tool:** `RUNNER_SPEC.md` §1 in that same directory
  (each file's exact commands, assertions, gotchas), §3 (the numbered round sequence),
  §4 (the hardcoded literals this copy parameterizes), §6 (fail-closed conditions),
  §7 (collection/privacy rules). This README does **not** replace it.
* Tracked by work packet `packets/wp5-operator-track.md` (program `mycelium-core-program`);
  verification battery recorded under `mycelium-core-program/runs/wp5-operator-track/`.

The five files `app_rpc.py`, `round14_start_and_activate.py`, `round14_stream.py`,
`packaged_app_daemon.py`, `round15_quit.py` are **byte-identical** to the lane source
(sha256 equality verified; see the run evidence). Everything else carries exactly the
de-laning edits listed in the ledger below, and nothing more. Since then
`packaged_app_daemon.py` also stops its app when its controlling channel is gone
(keepalive line on stdout; a failed write, SIGTERM or SIGHUP quits the app; interval
`A13_DAEMON_KEEPALIVE_SECONDS`, default 15 s), so it is no longer byte-identical.

## Env contract (authoritative)

| Variable | Required? | Meaning / default |
|---|---|---|
| `A13_OWNER_ROOT` | **required** (absolute) | Owner app state root on this Mac (the owner side appends `owner-lhr`; per-round evidence lands in `<A13_OWNER_ROOT>/<A13_RPC_ROUND>/`, mode `0700`) |
| `A13_REMOTE_ROOT` | **required** (absolute) | Laptop-side state root parent — contains `target/`, `preview<V>/`, and round dirs under `target/<A13_RPC_ROUND>/` |
| `A13_REMOTE_HOST` | optional | SSH destination for the laptop · default `member-mac` |
| `A13_DIST_ROOT` | optional | Build-output root holding `0.1.0-preview.<V>/` · default `<repo>/dist` |
| `A13_EVIDENCE_DIR` | optional | Evidence directory (logs, receipts, collection) · default `<repo>/outputs/a13-macos-evidence` |
| `A13_TARGET_PREVIEW` | **required** | Deployed preview tag on the laptop, e.g. `preview61` |
| `A13_RPC_ROUND` | **required** | RPC round directory name, e.g. `rpc-round61` |
| `A13_ROTATE_RELAY` | kept as-is | `1` → rotate the relay origin during bootstrap (else the already-published origin is reused) |
| `A13_LOG_LABEL` | kept as-is | Attempt-log label, independent of the preview tag (tooling-only retries on the same preview) |
| `A13_PYTHON` | optional | Interpreter for the chain driver · default `python3` |
| `A13_OWNER_DAEMON_LOG` / `A13_TARGET_DAEMON_LOG` | optional | That app's daemon log. Quoted as evidence inside `surface_lost:…:daemon=…`, and set by `round15_supervisor.py` for its own runs · default unset |
| `A13_ROUND_SUPERVISED` / `A13_ALLOW_ATTACHED` | optional | The driver's detachment guard: `run_a13_attempt.sh` refuses to spend a physical attempt unless `A13_ROUND_SUPERVISED=1` (set by `round15_supervisor.py`); `A13_ALLOW_ATTACHED=1` is the explicit escape hatch for a deliberate tooling-only run · default `0` |
| `MYCELIUM_CANCEL_PHASE_DIAGNOSTIC_LOG` | kept as-is | Passed to the **remote** node only, via the `env …` prefix in `packaged_target_daemon.py` |

A missing **required** variable fails closed with
`a13_operator_env_required:<NAME>`, raised at import time for both state roots, the
preview tag and the round name. The two state roots must additionally be **absolute
paths** (`A13_OWNER_ROOT`, `A13_REMOTE_ROOT`); the preview tag and round name must be
**non-empty** (they are tags, not paths).

**No default points at the historical lane or any frozen state tree.** The lane
constants (`Path.home()/…/Mycelium A13 Demo/qualification-47587ebe`, the lane root,
`preview32`/`rpc-round10` fallbacks) are gone: both state roots, the preview tag and
the round name are required inputs, and the only surviving default is the SSH host
alias `member-mac` (overridable). Nothing here reads or writes
`~/Library/Application Support/Mycelium A13 Demo`.

## Tools → round steps

Round steps are those of `packets/wp8-round-1.md` ("The round"), which is
version-independent in shape.

| Round step | Tool(s) |
|---|---|
| pre-round · deploy the tested bytes | `deploy_preview_to_laptop.py` (ditto zip → `scp -q -O` → remote `ditto -x -k` → cross-Mac sha256 equality), then `verify_preview_deployed.py` (post-deploy read-only verifier: executable + bundled `a13-runtime-source` digests, fix markers, exec bit, free space) |
| 2 · launch the app on both Macs | `packaged_owner_daemon.py` (OWNER, bounded 1800 s) · `packaged_target_daemon.py` (launches over SSH, blocks on the channel) — both delegate to the generic `packaged_app_daemon.py`; `round15_status.py` waits for each private control surface and reads `/api/status` |
| 3 · activation sequence (bootstrap → compile → accept → activate) | `run_a13_attempt.sh` drives `round14_bootstrap_compile.py` (network recovery/reuse, model-free bootstrap, native hello, `serving/observe`, staging accept/cleanup, `serving/compile`; includes the peer→owner reachability gate) then `round14_start_and_activate.py` (consume the exact `execution_proposal`, `/api/serving/start`, concurrent `/api/work/preview` + `/api/work/accept`, `/api/serving/activate`) |
| 4 · observe qualification | `round14_stream.py` (reads `/api/v1/bootstrap` + `/api/v1/qualification/current` through the real gateway validators, asserts `route_ready` + `evidence_class == physical_qualification`) |
| 5 · submit the frozen request | `round14_stream.py` (`InferenceSubmission('The capital of Germany is', max_new_tokens=8)`, SSE continuity validation, exactly one `completed` terminal, `token_events > 0`) |
| 6 · stop cleanly | `round15_quit.py` (`/api/quit` on the target, then the owner) |
| 0 · **the round itself (mandatory entry point)** | `round15_supervisor.py` — detaches into its own session, starts both daemons, waits for **both control surfaces by name**, runs the chain, then always quits both apps and sweeps both Macs. The driver refuses to spend a physical attempt from anywhere else (exit 5) |
| any step · control-surface liveness | `round14_surface.py` — `require_local` / `require_remote` raise the named `surface_lost:<label>:<state>:daemon=<evidence>`; `round14_rpc.py` calls them on every RPC, so a dead app can no longer be reported as a compile problem |
| after any round · strays on BOTH Macs | `round15_strays.py` — each root's control-surface state + `resume_ready` + strays on both Macs (a daemon behind `ssh` outlives the local ssh client) |
| transport for every step | `round14_rpc.py` (owner RPC in-process via `app_rpc.py`; laptop RPC over the canonical `ssh`/`scp` pattern) |

Per-round (private) evidence lands in `A13_OWNER_ROOT/<A13_RPC_ROUND>/` — never in this
directory. Collection and publication rules stay those of `RUNNER_SPEC.md` §7.

The chain driver refuses to start when `round<label>-attempt.log` already exists
(exit 4) — re-running the same version would truncate the earlier round's only log
copy. Chain exit codes are unchanged: `1` bootstrap_compile, `2` start_and_activate,
`3` stream, and `CHAIN_COMPLETE` is printed only on a full chain.

From 2026-09-16 the driver **also refuses to run outside the detached supervisor**
(`exit 5`, `ROUND_NOT_SUPERVISED`) unless `A13_ALLOW_ATTACHED=1` is set deliberately —
see "Detached rounds are mandatory" below.

The build + packaged-smoke wrapper (`build_preview_and_smoke.py`) is **not** part of
this tracked set: it builds from the owner Mac's `.venv` and its dependencies
(`scripts/build_a13_macos.py`, `tests/a13_macos/test_package.py`) are already in the
branch; see `RUNNER_SPEC.md` §1.1 for its exact command line.

## De-laning edit ledger

Byte-identical under every other character (each diff saved in the run evidence).

| File | Edit |
|---|---|
| `round14_rpc.py` | **the single owner of the path/env contract.** Replaced the lane constants (`OWNER`/`REMOTE` literals, `preview32`/`rpc-round10` defaults) with required `A13_OWNER_ROOT` / `A13_REMOTE_ROOT` (absolute-path checked, `_required`) and required `A13_TARGET_PREVIEW` / `A13_RPC_ROUND` (non-empty checked, `_required_tag`) + optional `A13_REMOTE_HOST`; `PRIVATE.mkdir(..., parents=True)`. Every `'member-mac'` literal in `target()` became `REMOTE_HOST`; the ssh/scp calls are otherwise character-identical (no added escaping or flags — the scp semantics are load-bearing). Docstring notes why the roots are required inputs. |
| `round14_bootstrap_compile.py` | `from round14_rpc import owner,target,PRIVATE,REMOTE`; the local `REMOTE = '…'` literal deleted. Nothing else. |
| `packaged_owner_daemon.py` | `DIST` ← `A13_DIST_ROOT` (default `<repo>/dist`, repo = `parents[2]`); `OWNER` ← required `A13_OWNER_ROOT`; executable existence asserted (`a13_owner_executable_missing:<path>`); prints `LAUNCH <argv>` before delegating; runs `packaged_app_daemon.py` (sibling file) via `sys.executable` with PYTHONPATH/PYTHONHOME/VIRTUAL_ENV scrubbed. |
| `packaged_target_daemon.py` | `REMOTE` ← required `A13_REMOTE_ROOT`; host ← `A13_REMOTE_HOST` (default `member-mac`). Remote-args structure and the `env MYCELIUM_CANCEL_PHASE_DIAGNOSTIC_LOG=…` prefix logic unchanged. |
| `deploy_preview_to_laptop.py` | repo root ← `parents[2]`; `src` ← `A13_DIST_ROOT`; `REMOTE` ← required `A13_REMOTE_ROOT`; host ← `A13_REMOTE_HOST`; evidence dir ← `A13_EVIDENCE_DIR` (default `<repo>/outputs/a13-macos-evidence`). `ditto` / `scp -O` / space-escaping / digest-equality logic unchanged. |
| `verify_preview_deployed.py` | `LANE` → `REPO` (`parents[2]`); `REMOTE` ← required `A13_REMOTE_ROOT`; host ← `A13_REMOTE_HOST`; `dist` ← `A13_DIST_ROOT`. `SOURCE_RELS` / `FIX_MARKERS` / pump markers and every check unchanged. |
| `round15_status.py` | imports `REMOTE_HOST` from `round14_rpc` and uses it in `_remote_control_present` instead of the literal. Everything else unchanged (including the `/tmp/round15-status.json` write). |
| `run_a13_attempt.sh` | `cd "$(dirname "$0")/../.."`; interpreter `${A13_PYTHON:-python3}`; log dir `${A13_EVIDENCE_DIR:-<repo>/outputs/a13-macos-evidence}`; **refuses to run when the attempt log exists** (exit 4); chain paths point at `scripts/a13_operator/…`. Exit codes 1/2/3 and the `CHAIN_COMPLETE` line unchanged. |
| `app_rpc.py`, `round14_start_and_activate.py`, `round14_stream.py`, `packaged_app_daemon.py`, `round15_quit.py` | **unchanged, byte-identical to the lane source.** `round14_stream.py` keeps `Path(__file__).resolve().parents[2]` — from `scripts/a13_operator/` that still resolves to the repo root, which is what it inserts on `sys.path`. |
| `round14_rpc.py` (second edit, 2026-09-16) | Also carries the control-surface contract: `owner()` calls `require_local(OWNER)`; `target()` probes `require_remote(REMOTE, REMOTE_HOST)` once per process (one extra ssh round trip, deliberately outside any timing-sensitive stage window). Every chain step therefore fails closed with `surface_lost:…` instead of a bare `FileNotFoundError` from `app_rpc`. This is the lifecycle fix for attempt 63r1 and it could **not** live in the chain script, which must stay byte-identical to the lane source. |
| `round14_surface.py`, `round15_strays.py`, `round15_supervisor.py` | **New files, added 2026-09-16 after attempt 63r1** (surface liveness · both-Mac stray sweep · detached round supervisor). Not lane source and not de-laning edits; see `mycelium-core-program/runs/wp8-02-useful-two-mac/ERRATA-1.md`. |

## Laptop-side Python 3.9 constraint

Only **`app_rpc.py`** and **`packaged_app_daemon.py`** are ever executed on the laptop,
via its `/usr/bin/python3` (3.9.6). Both are stdlib-only and 3.9-safe, and both are
byte-identical to the lane source in this directory. All other tools run on the owner
Mac under the project interpreter. Do not add a third laptop-side helper without
re-checking this constraint (`RUNNER_SPEC.md` §4).

## Detached rounds are mandatory (from 2026-09-16)

On attempt **63r1** the two app daemons were background children of an interactive chat
session. That session was torn down mid-round (provider exhaustion `402`/`429`, then
`Context length exceeded`, then an interrupt), the session's process group was SIGTERMed,
the round died **mid-compile**, and chain step 2 reported the misleading
`compiler_not_ready` because the app's control surface had simply gone away. Post-mortem:
`mycelium-core-program/runs/wp8-02-useful-two-mac/ERRATA-1.md`.

How the toolchain now prevents it:

* **`round15_supervisor.py` is the only supported way to spend an attempt.** It re-execs
  itself into its own session (macOS has **no `setsid(1)`**, so detachment uses
  `start_new_session=True`), starts both daemons detached with their own logs, waits for
  both control surfaces by name, runs the chain, and then always quits and sweeps.
* **`run_a13_attempt.sh` refuses** to run unless it was started by that supervisor
  (`exit 5`). Set `A13_ALLOW_ATTACHED=1` only for a deliberate tooling-only round.
* **A lost surface is a named failure**: `surface_lost:owner|target:{…}:daemon={…}`
  instead of a bare `FileNotFoundError`. `native-control.json` is unlinked by the app on
  shutdown (`app.py:771`), so its absence means "the app is not running".
* **Sweep both Macs afterwards** (`round15_strays.py`): a daemon behind `ssh` survives the
  local ssh client dying, and a stray blocks the next `--resume` (the daemon helper asserts
  the control file is absent at launch).

## Running it

```bash
# fresh pilot roots — never the frozen ~/Library/Application Support tree
export A13_OWNER_ROOT="$HOME/mycelium-pilot/round-<id>/owner-lhr"
export A13_REMOTE_ROOT="~/mycelium-pilot/round-<id>"
export A13_TARGET_PREVIEW="preview<V>"
export A13_RPC_ROUND="rpc-round<V>"
# the only supported way to run the round: detaches, owns both daemons, sweeps afterwards
python3 -B scripts/a13_operator/round15_supervisor.py <V>
# deliberately attached, tooling-only retry of the same chain (no physical spend intended):
A13_ALLOW_ATTACHED=1 bash scripts/a13_operator/run_a13_attempt.sh <V>
```

The full cycle (build → smoke → deploy → sha256 equality → attempt → collect) is
sequenced by the owner-run wrapper described in `RUNNER_SPEC.md` §2/§3; this directory
supplies every tool that wrapper invokes, minus the build/smoke step. No step here
starts the app on the owner Mac's frozen demo surfaces: the packaged app is launched
with `--headless --state-root <A13_OWNER_ROOT>`/`<A13_REMOTE_ROOT>/target`.


## Round apparatus moved in from /tmp (2026-09-20, commit `da033a0`)

A round must run from this branch's committed bytes alone. Four things the preview.78 pipeline needed did
not: the per-module `pyz_ledger.py` (lived in the program's `tools/`), the criterion-3 ssh audit shim,
and the round's own instruments.

| File | What it is |
|---|---|
| `pyz_ledger.py`, `tree_ledger.py` | `tree_ledger.py` is byte-identical to the copy the pipeline ran; `pyz_ledger.py` was byte-identical until 2026-09-21, when its row set and its needle were fixed (below) — the ledger prints, per module of the archive's real table of contents, which markers are in the PYZ and whether the baseline row shows them changing |
| `ssh-shim/ssh` | the instrument behind `A13_SSH_LOG`: logs one line per ssh invocation, then execs the real ssh. Its own comment names this directory as its home. **`chain-preview78e.sh` now fails closed if it is missing** — without it the calls stop being recorded and criterion 3 reads clean for the wrong reason |
| `soak_interventions.py` | read-only locate/list/read of membership counters and worker processes on both Macs (C5 interventions) |
| `p78r1_evidence.py` | collects the round's evidence from artifacts, never from a console line |
| `soak_baseline_arm.sh`, `soak_head_arm.sh` | the two arms of the transport experiment (0/3 vs 2/3), the reproduction behind ERRATA-22 |
| `run_readiness_gate.sh` | runs the readiness gate; **refuses to overwrite an existing gate log** and takes the label as `$1` |
| `run_vitest.sh` | the canonical TS command, run directly — the shell guard rejects `npm` payloads |
| `lan_sweep.sh`, `regen_and_check.sh`, `local_preflight_p78.sh` | read-only LAN discovery; fixture/manifest regen + drift check; the local half of the round chain |

Evidence produced by a round now lands in the run directory (`$RUN/pyz-ledger-pre78.txt`,
`$EV/criterion3-ssh-calls-p78r1.log`) instead of `/tmp`, so it survives a `/tmp` clean.

**Two determinism fixes found while moving these in:** the ledger's marker order is now sorted (it
iterated a set, so two runs over the same bundle could print the same evidence differently), and
`detach_run.py` refuses to start a round into a log that already has content — a round log is evidence and
interleaved launches would make it ambiguous which launch produced which line.

## The ledger's two blind spots (2026-09-21, lane `lane/pyz-ledger-coverage`)

`pyz_ledger.py` printed `PYZ modules: 1888` and then walked a **hardcoded 9-entry dict** with a
**str-only needle**. Measured consequence: `runs/wp8-02-useful-two-mac/pyz-ledger-pre83.txt` shows
every `vs baseline` delta a dot — not one `+` in nine rows, i.e. "no watched module changed" — while
`mycelium_a13_macos.app` had changed. A full-TOC byte differential over all 1888 modules finds exactly
one differing module, 71255 -> 72286 decompressed bytes, and the R1 fix that made it adds ZERO new str
constants (317 -> 317; every literal it introduces is `bytes`). Both blind spots are now closed:

* the row set is the **archive's real table of contents**, one row per module, and each module is paired
  to source by its own name — there is no curated list left to hide a module;
* markers are **str AND byte-string constants**, the byte ones tested as substrings of the NUL-joined
  bytes constants (the compiler folds adjacent byte literals, so const boundaries are codegen);
* a module with no pairing is printed **UNCOVERED** with the paths tried, a module whose bytecode
  changed with no marker moving is printed **CHANGED-UNATTRIBUTED**, and the baseline sweep is computed
  over the whole TOC so a changed module can never go unreported;
* controls are kept and strengthened: both kinds, and both sides of the differential.

`test_pyz_ledger_coverage.py` pins it in both directions: the pre-fix tool (kept byte-identical in
`fixtures/pyz_ledger_pre_fix_9d68291.py`, sha-checked against git `9d68291`) **must** miss the change —
on the real `.83`/`.82` pair and on a hermetic PYZ fixture — and the fixed tool must report it and state
what it does not cover. Run: `env -u PYTHONPATH /opt/homebrew/bin/python3 -B
scripts/a13_operator/test_pyz_ledger_coverage.py`.
