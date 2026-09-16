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
  `/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo/outputs/a13-macos-evidence/`
* **Authoritative description of each tool:** `RUNNER_SPEC.md` §1 in that same directory
  (each file's exact commands, assertions, gotchas), §3 (the numbered round sequence),
  §4 (the hardcoded literals this copy parameterizes), §6 (fail-closed conditions),
  §7 (collection/privacy rules). This README does **not** replace it.
* Tracked by work packet `packets/wp5-operator-track.md` (program `mycelium-core-program`);
  verification battery recorded under `mycelium-core-program/runs/wp5-operator-track/`.

The five files `app_rpc.py`, `round14_start_and_activate.py`, `round14_stream.py`,
`packaged_app_daemon.py`, `round15_quit.py` are **byte-identical** to the lane source
(sha256 equality verified; see the run evidence). Everything else carries exactly the
de-laning edits listed in the ledger below, and nothing more.

## Env contract (authoritative)

| Variable | Required? | Meaning / default |
|---|---|---|
| `A13_OWNER_ROOT` | **required** (absolute) | Owner app state root on this Mac (the owner side appends `owner-lhr`; per-round evidence lands in `<A13_OWNER_ROOT>/<A13_RPC_ROUND>/`, mode `0700`) |
| `A13_REMOTE_ROOT` | **required** (absolute) | Laptop-side state root parent — contains `target/`, `preview<V>/`, and round dirs under `target/<A13_RPC_ROUND>/` |
| `A13_REMOTE_HOST` | optional | SSH destination for the laptop · default `mycelium-laptop` |
| `A13_DIST_ROOT` | optional | Build-output root holding `0.1.0-preview.<V>/` · default `<repo>/dist` |
| `A13_EVIDENCE_DIR` | optional | Evidence directory (logs, receipts, collection) · default `<repo>/outputs/a13-macos-evidence` |
| `A13_TARGET_PREVIEW` | **required** | Deployed preview tag on the laptop, e.g. `preview61` |
| `A13_RPC_ROUND` | **required** | RPC round directory name, e.g. `rpc-round61` |
| `A13_ROTATE_RELAY` | kept as-is | `1` → rotate the relay origin during bootstrap (else the already-published origin is reused) |
| `A13_LOG_LABEL` | kept as-is | Attempt-log label, independent of the preview tag (tooling-only retries on the same preview) |
| `A13_PYTHON` | optional | Interpreter for the chain driver · default `python3` |
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
alias `mycelium-laptop` (overridable). Nothing here reads or writes
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
| transport for every step | `round14_rpc.py` (owner RPC in-process via `app_rpc.py`; laptop RPC over the canonical `ssh`/`scp` pattern) |

Per-round (private) evidence lands in `A13_OWNER_ROOT/<A13_RPC_ROUND>/` — never in this
directory. Collection and publication rules stay those of `RUNNER_SPEC.md` §7.

The chain driver refuses to start when `round<label>-attempt.log` already exists
(exit 4) — re-running the same version would truncate the earlier round's only log
copy. Chain exit codes are unchanged: `1` bootstrap_compile, `2` start_and_activate,
`3` stream, and `CHAIN_COMPLETE` is printed only on a full chain.

The build + packaged-smoke wrapper (`build_preview_and_smoke.py`) is **not** part of
this tracked set: it builds from the owner Mac's `.venv` and its dependencies
(`scripts/build_a13_macos.py`, `tests/a13_macos/test_package.py`) are already in the
branch; see `RUNNER_SPEC.md` §1.1 for its exact command line.

## De-laning edit ledger

Byte-identical under every other character (each diff saved in the run evidence).

| File | Edit |
|---|---|
| `round14_rpc.py` | **the single owner of the path/env contract.** Replaced the lane constants (`OWNER`/`REMOTE` literals, `preview32`/`rpc-round10` defaults) with required `A13_OWNER_ROOT` / `A13_REMOTE_ROOT` (absolute-path checked, `_required`) and required `A13_TARGET_PREVIEW` / `A13_RPC_ROUND` (non-empty checked, `_required_tag`) + optional `A13_REMOTE_HOST`; `PRIVATE.mkdir(..., parents=True)`. Every `'mycelium-laptop'` literal in `target()` became `REMOTE_HOST`; the ssh/scp calls are otherwise character-identical (no added escaping or flags — the scp semantics are load-bearing). Docstring notes why the roots are required inputs. |
| `round14_bootstrap_compile.py` | `from round14_rpc import owner,target,PRIVATE,REMOTE`; the local `REMOTE = '…'` literal deleted. Nothing else. |
| `packaged_owner_daemon.py` | `DIST` ← `A13_DIST_ROOT` (default `<repo>/dist`, repo = `parents[2]`); `OWNER` ← required `A13_OWNER_ROOT`; executable existence asserted (`a13_owner_executable_missing:<path>`); prints `LAUNCH <argv>` before delegating; runs `packaged_app_daemon.py` (sibling file) via `sys.executable` with PYTHONPATH/PYTHONHOME/VIRTUAL_ENV scrubbed. |
| `packaged_target_daemon.py` | `REMOTE` ← required `A13_REMOTE_ROOT`; host ← `A13_REMOTE_HOST` (default `mycelium-laptop`). Remote-args structure and the `env MYCELIUM_CANCEL_PHASE_DIAGNOSTIC_LOG=…` prefix logic unchanged. |
| `deploy_preview_to_laptop.py` | repo root ← `parents[2]`; `src` ← `A13_DIST_ROOT`; `REMOTE` ← required `A13_REMOTE_ROOT`; host ← `A13_REMOTE_HOST`; evidence dir ← `A13_EVIDENCE_DIR` (default `<repo>/outputs/a13-macos-evidence`). `ditto` / `scp -O` / space-escaping / digest-equality logic unchanged. |
| `verify_preview_deployed.py` | `LANE` → `REPO` (`parents[2]`); `REMOTE` ← required `A13_REMOTE_ROOT`; host ← `A13_REMOTE_HOST`; `dist` ← `A13_DIST_ROOT`. `SOURCE_RELS` / `FIX_MARKERS` / pump markers and every check unchanged. |
| `round15_status.py` | imports `REMOTE_HOST` from `round14_rpc` and uses it in `_remote_control_present` instead of the literal. Everything else unchanged (including the `/tmp/round15-status.json` write). |
| `run_a13_attempt.sh` | `cd "$(dirname "$0")/../.."`; interpreter `${A13_PYTHON:-python3}`; log dir `${A13_EVIDENCE_DIR:-<repo>/outputs/a13-macos-evidence}`; **refuses to run when the attempt log exists** (exit 4); chain paths point at `scripts/a13_operator/…`. Exit codes 1/2/3 and the `CHAIN_COMPLETE` line unchanged. |
| `app_rpc.py`, `round14_start_and_activate.py`, `round14_stream.py`, `packaged_app_daemon.py`, `round15_quit.py` | **unchanged, byte-identical to the lane source.** `round14_stream.py` keeps `Path(__file__).resolve().parents[2]` — from `scripts/a13_operator/` that still resolves to the repo root, which is what it inserts on `sys.path`. |

## Laptop-side Python 3.9 constraint

Only **`app_rpc.py`** and **`packaged_app_daemon.py`** are ever executed on the laptop,
via its `/usr/bin/python3` (3.9.6). Both are stdlib-only and 3.9-safe, and both are
byte-identical to the lane source in this directory. All other tools run on the owner
Mac under the project interpreter. Do not add a third laptop-side helper without
re-checking this constraint (`RUNNER_SPEC.md` §4).

## Running it

```bash
# fresh pilot roots — never the frozen ~/Library/Application Support tree
export A13_OWNER_ROOT="$HOME/mycelium-pilot/round-<id>/owner-lhr"
export A13_REMOTE_ROOT="/Users/evinova/mycelium-pilot/round-<id>"
export A13_TARGET_PREVIEW="preview<V>"
export A13_RPC_ROUND="rpc-round<V>"
bash scripts/a13_operator/run_a13_attempt.sh <V>          # the bounded attempt only
```

The full cycle (build → smoke → deploy → sha256 equality → attempt → collect) is
sequenced by the owner-run wrapper described in `RUNNER_SPEC.md` §2/§3; this directory
supplies every tool that wrapper invokes, minus the build/smoke step. No step here
starts the app on the owner Mac's frozen demo surfaces: the packaged app is launched
with `--headless --state-root <A13_OWNER_ROOT>`/`<A13_REMOTE_ROOT>/target`.
