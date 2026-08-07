# Mycelium — Astra autonomous handoff

Status: current cross-host handoff, published 2026-07-30.

This file replaces the local-only prompt copied on 2026-07-24. That prompt and the older
2026-07-22 continuation prompt are retained as `docs/handover/mycelium-handover.md` and
`docs/handover/ORIGINAL_TOMORROW_PROMPT_20260722.md` for provenance, but they contain stale
machine-local paths and status. The 2026-07-24 prompt also names three unverified symbols. Do not
execute either historical prompt verbatim.

## Claim boundary

The recent work and its source plans are now preserved on GitHub. This does **not** mean the
physical multi-device gates passed. G4, G4b, G8, and G9 remain unproven until new evidence comes
from distinct physical hosts.

Recorded integration results from 2026-07-24:

| Repository | Branch | Code checkpoint | Recorded suite result | Work contained |
|---|---|---:|---:|---|
| `Tranquil-Flow/mycelium` | `integration/wave3-mycelium` | `2109b950734031eef281c145f00e7aa647d261bf` | 2,408 passed | A1 heartbeat/endpoint work, A3 optional MLX + NumPy runtime, A4 unified membership, A6 request gateway |
| `Tranquil-Flow/BloomBee` | `integration/wave3-serial` | `8fded03a69fe2c5aff6e67098ef60fc9024001ea` | 2,691 passed | A7 local native-Iroh E2E, Task 2R.5 entrypoints, Task 2R.6 exact stage packs, A9 offline cycle audit |

Both branches descend from `b317566a73d8c7aebafc76bcbcca7f8eb651163e`. They are separate
integration tranches in separate GitHub repositories. No existing branch contains both. A
read-only merge-tree audit found one explicit conflict in
`contracts/contract-manifest.v1.json`; the overlapping Python files otherwise auto-merged. Do
not pretend one checkout contains all work, and do not perform an ad-hoc cross-repository merge
inside a qualification run. Any future unification must regenerate the contract manifest and
pass the complete suite before physical evidence is attempted.

## 1. Create clean, separate checkouts

Do not reuse or reset an existing dirty project directory. On `m4pro`:

```bash
export PATH=/opt/homebrew/bin:$HOME/bin:$PATH
mkdir -p "$HOME/Projects"

# Primary Mycelium integration and all tracked plans.
git clone --branch integration/wave3-mycelium --single-branch \
  https://github.com/Tranquil-Flow/mycelium.git \
  "$HOME/Projects/mycelium-astra-ready"

# Latest entrypoint, stage-pack, cycle, and local native-Iroh E2E tranche.
git clone --branch integration/wave3-serial --single-branch \
  https://github.com/Tranquil-Flow/BloomBee.git \
  "$HOME/Projects/mycelium-dimvp-wave3"
```

Verify provenance before reading or editing:

```bash
MYC="$HOME/Projects/mycelium-astra-ready"
DIM="$HOME/Projects/mycelium-dimvp-wave3"

test -z "$(git -C "$MYC" status --porcelain)"
test -z "$(git -C "$DIM" status --porcelain)"
git -C "$MYC" merge-base --is-ancestor \
  2109b950734031eef281c145f00e7aa647d261bf HEAD
test "$(git -C "$DIM" rev-parse HEAD)" = \
  8fded03a69fe2c5aff6e67098ef60fc9024001ea
```

If HTTPS authentication fails, ensure `/opt/homebrew/bin` is on `PATH` and verify the existing
GitHub CLI credential before changing remotes. Do not print tokens.

## 2. Read this tracked material in order

From `$MYC`:

1. `docs/handover/ASTRA_CURRENT.md` — this current handoff.
2. `docs/handover/MVP_COMPLETION_PLAN.md` — execution plan snapshot from 2026-07-23.
3. `docs/handover/mycelium-demo-plan.md` — latest Desktop plan snapshot and RED cases.
4. `docs/handover/NEXT_SESSIONS_AND_DEVICE_PREP_2026-07-23.md` — device preparation and staged prompts.
5. `docs/handover/UNRESTRICTED_SESSION_PROMPTS_2026-07-23.md` — complete lane prompt archive.
6. `docs/handover/OVERNIGHT_LANE_BRIEF.md` — original lane operating contract.
7. `docs/handover/SESSION_PROMPTS_A2_A7_A8.md` — earlier interactive session prompts.
8. `docs/handover/mycelium-handover.md` and `ORIGINAL_TOMORROW_PROMPT_20260722.md` — historical prompts.
9. `docs/handover/a8-reviews/` — both durable A8 review reports referenced by the prompts.
10. `docs/handover/tools/run_full_suite_locked_20260723.py` — exact historical suite-lock runner.
11. `docs/handover/ASTRA_SOURCE_MANIFEST.sha256` — checksums of previously local-only sources.
12. `REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md` — Router invariants.
13. `FAULT_TOLERANT_LAYER_REPLANNER.md` — failure authority and control loops.
14. `GOSSIP_PROTOCOL.md`, `BROADCAST_PROTOCOL.md` — evidence and peer discovery.
15. `ALLOCATOR.md`, `LAYER_PLANNER_PRODUCT_V1.md` — placement authority.
16. `QUESTIONS.md` — unresolved decisions; do not guess around them.

The plan and prompt snapshots predate the two integration heads above. They retain their original
machine-local paths and status for provenance; do not execute those paths on `m4pro`. Their
specifications and RED cases remain useful, but this file's branch map, commands, and status
supersede stale path, privacy, or completion instructions. The historical suite-lock runner also
retains its original coordinator-only lock path and is reference code, not a cross-host command.

## 3. First message to Astra

Send this concise status before touching another device:

```text
Mycelium handoff is now on GitHub in two verified integration tranches, with the previously
local-only plans tracked in the Mycelium branch. Physical multi-device inference remains
unproven.

Please enable only the devices you consent to use:

1. Astra MacBook
   - Send `whoami`, `hostname`, and the current Tailscale IP.
   - Turn on System Settings → General → Sharing → Remote Login.
   - Keep it awake, plugged in, and connected to Tailscale.

2. Pixel 8 Pro, if included
   - Turn on Developer Options → Wireless Debugging.
   - Keep it unlocked.
   - Send the current pairing endpoint/code and the separate connection endpoint when asked.
```

Do not install, reconfigure, or wake someone else's device without explicit consent.

## 4. Host and connectivity preflight

First prove the current machine is the intended M4 Pro:

```bash
printf 'user='; whoami
printf ' host='; hostname
printf ' mem='; sysctl -n hw.memsize
```

Expected baseline: the operator-confirmed account and host for the 48 GiB M4 Pro. Stop if any
field differs; do not copy account identifiers into tracked reports.

For the second Mac, use the operator-provided current Tailscale identity rather than a stale IP:

```bash
ping -c 1 <astra-tailscale-ip>
ssh -o BatchMode=yes -o ConnectTimeout=8 \
  <astra-user>@<astra-tailscale-ip> \
  'printf "user="; whoami; printf " host="; hostname; uname -m'
```

For Pixel Wireless Debugging, pairing and connection use different rotating ports. Discover or
use the operator-provided current endpoints. Do not run bare `adb connect 100.126.233.4` and do
not trust `adb connect` exit status alone; require `adb devices -l` to show state `device`.

## 5. Relevant tracked code

Primary Mycelium checkout (`$MYC`):

- `two_process_inference_qualification.py` — 1,644-line local reference architecture.
- `physical_pixel_host_stage.py` — bounded Pixel host-stage path.
- `physical_inference_qualification.py` and `distributed_inference_qualification.py` — existing qualification surfaces.
- `runtime_loader.py`, `numpy_runtime.py`, `mycelium_router/mlx_runtime.py` — runtime selection and backends.
- `mycelium_router/transports/iroh.py` — production `IrohTransport` implementation.
- `docs/qualification/physical-qualification-preflight.md` — exact preflight contract and claim boundary.
- `docs/qualification/request-iroh-e2e.md` — production-path local Iroh evidence and its limits.
- `docs/runtime-backend-selection.md` and `docs/request-gateway-api-contract.md` — A3/A6 contracts.

DIMVP checkout (`$DIM`):

- `mycelium_node/__main__.py`, `mycelium_node/process.py` — latest Task 2R.5/A7 node closure.
- `mycelium_seed/__main__.py`, `mycelium_seed/http.py` — latest seed entrypoint closure.
- `stage_pack.py` and its model/ownership tests — latest Task 2R.6 closure.
- `tests/seed_coordinator/test_local_two_node.py` and
  `tests/seed_coordinator/native_process_harness.py` — latest local native-Iroh E2E tranche.
- `tests/integration/test_cycle_search_offline.py` — A9 synthetic directed-cycle evidence.

The old local prompt named `IrohSocketMesh`, `LoopbackSocketMesh`, and `_MLXWorkerProxy` as if
those exact symbols were established. A current source search did not find them. Use the actual
production `IrohTransport` and existing harness contracts; do not invent replacement classes to
match stale prose.

## 6. Execution sequence

### Phase A — qualify both clean checkouts

1. Confirm required interpreter and dependencies without changing system state unexpectedly.
2. Run the project-declared focused tests for the files you will use.
3. Run the complete suite on an unrestricted macOS host before changing transport or physical
   qualification code. Seatbelt/sandbox results do not replace native-Iroh proof.
4. Record exact command, HEAD, pass/fail counts, and any exclusions.

### Phase B — prepare physical hosts

1. Pin `microsoft/DialoGPT-small` to revision
   `49c537161a457d5256512f9d2d38a87d81ae0f0e` outside the qualification run.
2. Build and health-check the native Iroh sidecar for each host architecture.
3. Generate a canonical operator plan and run `mycelium_physical_preflight` on each host.
4. Treat the preflight as inert. It validates and generates a plan; it does not execute physical
   inference and always reports physical/release/route readiness false.
5. Stage only explicit repository-relative source files. Never transfer credentials, local
   identity directories, model cache paths, or private prompts.

### Phase C — run the physical proof

Use distinct physical hosts with separate hostname, boot identity, PID space, and non-loopback
address. Exercise production Router/Iroh paths, not an in-process mesh. Require:

- real pinned model layers loaded across participating hosts;
- prefill plus at least eight greedy decode steps;
- exact token-ID comparison against an independent monolithic reference;
- per-stage latency and observed transport path;
- signed load, assignment, endpoint, and cleanup evidence;
- all participants carrying actual activation traffic;
- one run, one writer, one seal; and
- no source, tolerance, golden-file, or timeout edits during the run.

A Pixel-only host stage is fallback/partial evidence unless it uses the production transport and
meets the plan's exact gate. Do not relabel it as G8 or heterogeneous production proof.

### Phase D — report

Report:

- exact repo URLs, branches, and HEADs used;
- host identities and consented roles;
- model revision and layer allocation;
- first 20 generated token IDs/text;
- per-stage and end-to-end latency;
- cleanup/resource audit;
- evidence paths and digests; and
- the highest claim-ladder rung actually proven.

## 7. Blocked protocol

If any phase blocks:

1. Stop at the failed gate; do not fabricate a substitute result.
2. Record exact command, exit status, stderr, host identity, branch, and HEAD.
3. Distinguish code failure, environment failure, unavailable device, missing credential, and
   missing model cache.
4. Do not raise timeouts or weaken validation to make a run pass.
5. Do not claim physical, route, release, privacy, or performance readiness from local tests.

## 8. Preserved unfinished work

These branches preserve every dirty Mycelium worktree found during the 2026-07-30 publication
audit. They are **archives, not reviewed integration bases**:

| Branch | Commit | Preserved state |
|---|---|---|
| `archive/astra-handoff-20260730/a5-planner-red-tests` | `18661c089be5db428689672d72b4fee361edbd1c` | A5 planner-placement RED tests |
| `archive/astra-handoff-20260730/durable-heartbeat-stale` | `da9e1addb6604e8a52e9070b1529f32343988729` | older stale heartbeat edits, superseded by later A1 work |
| `archive/astra-handoff-20260730/membership-contracts-stale` | `1dd455d706b001b88706912d0cbf8ca884ce8ac0` | stale membership-contract draft |
| `archive/astra-handoff-20260730/physical-mvp-unfinished` | `e96e68ee32b840c579d1ab3b8bfeb37c380db3fe` | unfinished physical MVP code/tests/docs |
| `archive/astra-handoff-20260730/a7-e2e-v1-unfinished` | `e0b6e7ebdae125066fe3f6f2fc14b8902d12e246` | first-pass A7 assertion edits superseded by the v2 tranche |

Do not merge archive branches wholesale. Inspect them only when recovering a specific missing
idea, then transplant through RED → GREEN tests and full review.
