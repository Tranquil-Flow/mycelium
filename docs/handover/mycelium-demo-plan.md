# Mycelium Physical Multi-Device Inference Demo Implementation Plan

> **For Hermes:** Use `subagent-driven-development` task-by-task. Keep physical orchestration, hardware access, evidence sealing, commits, and pushes in the primary integration driver. Use strict RED → GREEN → REFACTOR for every source change.

**Goal:** Produce one honest, reproducible run in which a real pretrained GPT-2-family model generates coherent text by executing different layer ranges on two distinct physical Macs through the production Router and native Iroh transport, then preserve qualifier-accepted evidence of that exact run — and then, in Phase 3B, replace the hand-frozen placement with real capacity-driven planner placement over a joinable swarm, so the deliverable is a working product rather than a fixed two-node pipe.

**Architecture:** A seed coordinator owns the model snapshot, membership database, placement decision, and signed control records. A node agent on each device joins through a signed single-use invitation bundle, advertises a measured capacity profile, receives only its assignment-bound stage pack, and wraps the existing `PhysicalNodeService`; activations travel only through production `Router` + `IrohTransport`. The existing sealer and sole qualification authority consume signed observations from the same run identity. Placement is frozen by hand for the first physical proof only, then handed to the existing gossip → planner → LayerBuilder pipeline. Quantization starts only after the FP16 physical route is accepted.

**Tech Stack:** Python 3.14, MLX, native Rust `iroh-sidecar`, Ed25519 evidence signing, SQLite, stdlib HTTP control plane, existing Mycelium Router/relay/runtime/planner/gossip/qualification modules, pytest, Node 22 UI gates.

**Plan location:** `/Users/evinova/Desktop/mycelium-demo-plan.md` — private/local only. Do not add this file or internal handover prose to Git.

---

## 0. Amendment log

| Date | Change |
|---|---|
| 2026-07-22 ~06:10 | Original reconstruction against `88688f5`. |
| 2026-07-22 ~11:xx | §1.1 verification pass against `MYCELIUM_MVP_SYNTHESIS_HANDOVER.md`; added §6.6–§6.8; amended Tasks 2.2A/2.2B/2.3A/3.2/3.4/3.6. |
| 2026-07-22 ~13:00 | **Product-scope review.** Added §1.2 (product-scope audit), §1.3 (docs the handover under-represents), §6.9–§6.12, Task 2.4, Phase 3B (Tasks 3B.1–3B.4), gate G4b/G8. Filled in missing Files/RED/Commit fields on Tasks 3.3–3.8. Pinned the model snapshot. Sections changed by this pass are marked **[2026-07-22 product pass]**. |
| 2026-07-22 ~19:40 | **Model fetch.** Downloaded the pinned DialoGPT-small revision (operator-authorised); recovered-feature gate went 18 passed/9 skipped → **61 passed/1 skipped**. Recorded a latent wall-clock `stage-worker load timed out` flake (60 s deadline, trips on cold concurrent 5-way load) as a Task 3.4 hazard with an explicit "inconclusive, not failure" rule. Snapshot still absent on all physical peers. |
| 2026-07-22 ~19:15 | **Hygiene pass.** Refreshed §2's worktree map to all eight worktrees (three agent scratch worktrees had appeared), flagged the active agent worktree as untouchable, recorded that uncommitted files in absorbed worktrees are superseded duplicates rather than work to rescue, and reclaimed ~8 GB of cargo caches from the four absorbed worktrees only. Corrected §2's stale full-suite figures to `1973 passed / 11 skipped` at `22ae8a9`. |
| 2026-07-22 ~18:45 | **Swarm-tactics pass.** Added §7.1 — parallel-subagent execution strategy: the file-disjointness test, a lane table naming four tasks safe to fan out *right now* (2R.5, 2R.6, 3B.5 steps 1–3, 3.2 skeleton) alongside in-flight 2R.4, what each subagent must be handed, what subagents must never do (no physical hardware, no `integration/*` pushes, no contract edits without owning the contract, no evidence writes, **no plan edits**), and merge discipline. Operator direction: parallelize aggressively. No other section touched — 2R.4's write surface deliberately left alone. |
| 2026-07-22 ~14:00 | **As-built reconciliation + operator decisions.** Tasks 0.1–0.2, 2.1A, 2.1B, 2.2A, 2.2B, 2.3A and most of 3.1 **landed** on a branch this plan did not name. Added §1.4 (as-built path map — the plan's `Files:` lists were wrong and would have caused duplicate modules), rewrote §2 baseline, marked landed tasks, added **Phase 2R** (seven carry-forward gaps the as-built code does not yet satisfy). Recorded four operator decisions: Task 2.4 = **extend**, target = **genuinely heterogeneous**, device roster, frozen-first confirmed. Added §6.13 (the MLX/Linux blocker this creates) and promoted Task 4.5 out of Phase 4. Marked **[2026-07-22 as-built pass]**. |
| 2026-07-22 ~22:15 | **Execution closure for parallel-safe lanes.** Phase 2R plus canonical local E2E landed through `132e7c3`; Task 3B.5 steps 1–3 landed as `4fbe48f`; the fail-closed Task 3.2 controller skeleton landed as `b317566`. Final Main-native gate: **2031 passed / 3 skipped / 121 subtests** plus contract, fixture, manifest, claim-boundary, compile, Ruff, and diff checks. All delegated workers returned HTTP 402 before editing, so the primary driver completed the lanes. Physical launch/seal remains blocked and `route_ready=false`. Pixel 8 Pro is tailnet `100.126.233.4` but offline; ADB/bridge unavailable. |

---

## 1. Reconstruction status and provenance

This plan is reconstructed, not claimed byte-for-byte identical to the missing overnight file.

Exact recovered facts:

- Working branch: `integration/mycelium-live-demo` at `88688f54dced2b573153898c0e1b2523369ec884`.
- Phase 1 landed: local pretrained Safetensors sources, arbitrary layer-count/node splits, GPT-2 byte-level BPE, coherent monolithic decode, and exact greedy-token parity across 2/3/5 local process shards.
- Phase 2.1 landed: Ed25519-signed, expiring, single-use invitation tokens.
- `QUESTIONS.md` explicitly names Phase 2.2 node agent and Phase 2.3 seed coordinator as next.
- The older tracked physical-qualification plan survives in commit `41bf01a^` and explains the existing signer, sealer, deterministic deployment, physical node service, lifecycle authority, and physical evidence requirements.
- Existing strongest execution proof is same-host only: two node subprocesses over native Iroh and local process-sharded pretrained decode. This is not physical multi-machine inference.
- No `mycelium_node_agent/`, `mycelium_seed/`, or `physical_inference_qualification.py` exists at the recovered HEAD.
- Current model preparation accepts `float16` and `float32`; there is no implemented weight quantization contract.
- Existing Pixel proof is explicitly `stdin/http/stdin`, not production Router transport, and remains `route_ready=false`.

Unknown and intentionally not invented:

- The original meaning of the old plan's Task 2.4. (The **new** Task 2.4 added by the 2026-07-22 product pass is unrelated code-grounded work; it does not claim to recover the lost one.)
- Exact prose or task granularity from the missing Desktop file.

This replacement supersedes the unknown Task 2.4 with code-grounded work needed for the physical proof.

### 1.1 Verification pass against `MYCELIUM_MVP_SYNTHESIS_HANDOVER.md` (2026-07-22, 11:xx CEST)

This plan was re-checked line-by-line against the full canonical synthesis handover (`~/Projects/.audit/MYCELIUM_MVP_SYNTHESIS_HANDOVER.md`) to confirm it builds on the architecture actually scoped there, not a drifted simplification. Findings, each directly verified against the live repository (not recalled from memory):

- **Gossip lane is green, not red.** The handover's snapshot (2026-07-17) recorded `57 passed, 17 failed` in `tests/gossip`, all failing on a `shutdown_timeout_seconds` constructor mismatch. That parameter now exists in `mycelium_gossip/service.py:182`, and a direct run on `integration/mycelium-live-demo` HEAD (`88688f5`) confirms `108 passed, 2 skipped` (the 2 skips are the documented optional Zenoh path). The Phase-0 gossip-hardening exit gate from the handover's §7 Phase 0 is satisfied in this branch. Do not re-open this as a task.
- **Gossip is already wired into planning, not bypassed.** `mycelium_router/layer_builder.py` and `mycelium_layer_planner/gossip_adapter.py` both import `mycelium_gossip`. The canonical pipeline (`GossipService EvidenceBundle -> PlannerInputAdapter -> RoutePlanV2 -> ... -> LayerBuilder -> ExecutionGraphV1`, handover §6) exists in production source for the planning path. Task 2.3A's "freeze a deterministic two-node DialoGPT split for the first physical proof" is a **scoped, temporary bypass of that existing pipeline for the first proof only** — see §6.8 below — not evidence the pipeline is missing.
- **Confirmed real gap: transport peer authentication is a static allowlist.** `mycelium_router/transports/iroh.py` authenticates peers by exact `expected_endpoint_id` string match only (e.g. line 297: `if actual != self.expected_endpoint_id`). There is no signature, no epoch binding, no revocation check anywhere in that file or in `mycelium_qualification/`. This is precisely the condition the handover's §4.5 calls out as `production_ready=false` for the iroh-activation-spike: *"Production must consume a signed, deployment/epoch-scoped EndpointID membership snapshot with validity and revocation state from the evidence/control plane."* The original Phase 2/3 task list did not carry this requirement forward explicitly — §6.6 and Task 2.2A/3.2 below now do.
- **Confirmed real gap: no heartbeat/receipt liveness logic exists in source.** `grep` for `heartbeat`/`keepalive` across `mycelium_router/` and `mycelium_gossip/` returns nothing. (The only `heartbeat` hits in the tree are SSE keep-alive frames in `mycelium_gateway/` and `mycelium_ui_gateway/`, which are an unrelated HTTP concern.) The handover's cross-session decisions #18–19 (§5) and Gate H (§8) require that valid activation/receipt traffic suppress redundant heartbeats, that idle loss still be caught by keepalive, and that one missing receipt produce *scoped* hop/edge failure evidence rather than an immediate peer-death verdict. `HeartbeatV1` in Task 2.2A currently only carries the wire shape, not this suppression/scoping behavior — §6.7 and the amended Task 2.2A/3.6 below now carry it forward as explicit RED cases.
- **Live coordination alert:** a sibling branch, `integration/mycelium-membership-contracts-mac` (git worktree `.git/worktrees/mycelium-membership-contracts`), sits one commit ahead of this plan's recorded HEAD. Its tip commit `debb4f4` ("fix: restore clean multi-device baseline", 2026-07-22 07:08 — **after** this plan's `88688f5` reconstruction point at 06:10) already implements **Task 0.1** (rewrites `test_router_docs.py` to read `docs/request-streaming-session-lifecycle.md` instead of the deleted private plan) and most of **Task 0.2** (adds `deviceLabClient.ts` to `_ALLOWED_PRODUCT_ACTION_CLIENTS` and extends the audit test). This is very likely another agent already executing against this same plan. **Before starting Phase 0, check whether that branch has moved further and reconcile/merge rather than redoing Task 0.1/0.2 from scratch** — see the amended Task 0.1/0.2 below. Verified 2026-07-22 13:00: `git diff --stat integration/mycelium-live-demo integration/mycelium-membership-contracts-mac` is still exactly 5 files / +86 / −3, i.e. that branch has not moved past Tasks 0.1–0.2.

### 1.2 Product-scope audit (2026-07-22, 13:00 CEST) **[2026-07-22 product pass]**

The 11:xx pass verified that this plan is *consistent with* the handover. This pass asks a different question: **does executing this plan produce the product we actually want** — devices joining a swarm, layers served to the right peers by measured capacity, routing path chosen by cost — or only a sealed proof that two named Macs can pass tensors?

Answer as the plan stood before this amendment: **only the latter.** Findings, each verified against the live tree:

- **The plan's physical path is structurally locked to exactly two nodes.** Phase 3 builds on `mycelium_qualification/physical_deployment.py`, whose `build_execution_graph` raises `execution_graph_requires_two_assignments` at line 156 for any input that is not exactly 2 assignments + 2 proofs. It also hardcodes `topology_version=1`, hardcodes `StageCost(1.0, 1.0, 32)`, and emits exactly one forward edge and one loopback edge. No amount of seed-side generality changes that while Phase 3 depends on this function.
- **The general N-stage builder already exists and is unused by this plan.** `mycelium_router/layer_builder.py:347 build_execution_graph(tranche, load_proofs, *, manifest, runtime_endpoints, topology_version, token_envelope_bytes)` accepts arbitrary assignment counts, validates a full control-plane tranche, and consumes a real gossip `EvidenceBundle` (`evidence_bundle_from_dict`). It is already imported by `mycelium_qualification/qualifier.py` and `two_process_inference_qualification.py`.
- **Capacity-driven placement and cost-based path selection already exist and are unused by this plan.** `mycelium_layer_planner/planner.py:76 plan_snapshot(snapshot) -> RoutePlanV2`, fed by `mycelium_layer_planner/gossip_adapter.py:164 planner_snapshot_from_evidence_bundle(...)`, backed by `network_cost.py` (`transfer_time_ms`, `phase_edge_costs`, geodesic propagation floor), `flow.py` (`assign_flow`, `_shortest_complete_loop`), `cycle_search.py`, `phase_score.py`, and `primary_plan.py`. This is exactly the "assign layers by capacity, pick the efficient path" capability the README advertises as working.
- **A swarm control plane already exists.** `mycelium_interactive/swarm.py:287 SwarmCoordinator` implements one-time invitations (`create_invite:418`, `exchange_invite:443`), capability-scoped peer enrollment, per-peer stage-pack delivery, `poll_work:506`, `start_work`, `dispatch:633`, `submit_result`, `cancel_request`, `revoke_peer`, `leave`, and `status`, served over the hardened HTTP surface in `mycelium_interactive/server.py` (TLS, CSP, origin pinning), with `tests/interactive/test_swarm.py` covering it. Tasks 2.1A–2.3A as originally written recreate most of that surface as `mycelium_seed/` without mentioning it. See §6.11 and Task 2.4.
- **Physical preflight already exists.** `mycelium_physical_preflight/` (`generator.py`, `schema.py`, `validator.py`, `__main__.py`) with credential scanning and canonical-JSON validation. Task 3.3 listed the records to collect without referencing it.
- **A request-gateway → Router → native Iroh E2E harness already exists.** `tests/e2e_request_iroh/harness.py` ("Local-only request gateway -> Router -> native Iroh qualification harness") plus `conftest.py` that builds the sidecar. Task 2.3B should extend this, not start from an empty file.
- **A user-facing request surface already exists.** `mycelium_request_gateway/` (`service.py:118 RequestGatewayService`, `asgi.py`, `auth.py`, `cli.py:13 stream_prompt`, `client.py`) and `mycelium_demo/cli.py` with `doctor` / `device-lab` / `serve` subcommands. The plan never routes the physical demo through any of it, so "genuinely useful test product" is currently unserved by every task in the document.

**Consequence:** the plan as written ends with a working planner lane and a physical lane that ignores it, permanently disjoint, with no task that joins them. Phase 3B (new) closes that. §6.9 makes the frozen split explicitly scaffolding with a scheduled removal date rather than the architecture.

### 1.3 Documents the synthesis handover under-represents **[2026-07-22 product pass]**

The handover compresses whole design documents into single bullets. Three are load-bearing for what we are building and must be read directly before Phase 3B — do not work from the handover's summary of them:

| Document | Handover's entire treatment | What it actually contains |
|---|---|---|
| `LAYER_PLANNER_PRODUCT_V1.md` | one bullet: *"cyclic autoregressive loopback intent; multi-loop replication and cross edges"* | §4.3 autoregressive decode cycles and legal cycle openings; §5.1 prefill acyclic path view vs §5.2 decode cyclic path view with distinct payload sizes; **§7 the entire shortest-directed-cycle search design** — directed Hamiltonian cycle / asymmetric TSP framing, Held–Karp `O(n²·2ⁿ)` exact baseline, the §7.2 nested objective (cycle candidate → cycle opening → contiguous layer-allocation DP → separate prefill/decode scores), §7.3 5%-threshold node pruning, §7 fleet-size strategy table, and the column-generation upgrade path |
| `FAULT_TOLERANT_LAYER_REPLANNER.md` | one bullet: *"deterministic typed replan assessments and candidate plans"* | the failover candidate model that Phase 3B.2 needs when a peer leaves a live swarm |
| `REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md` | folded into the Router inventory | how the Router consumes an **opened** cycle so forward stages stay a DAG while the decode loopback is an explicit generation-cycle relation |

See §6.10 for the cyclic-graph state of play, which is the specific thing Astra asked to have verified.

### 1.4 As-built reconciliation (2026-07-22, 14:00 CEST) **[2026-07-22 as-built pass]**

**Read this before touching any file.** Between the 13:00 product pass and now, another agent implemented most of Phase 2 on a branch this plan did not name, choosing different module paths than the plan specified. The plan's `Files:` lists were therefore **wrong** — an agent following them literally would have created `mycelium_node_agent/` beside the existing `mycelium_node/`, `mycelium_seed/http_api.py` beside `http.py`, and so on. Corrected below.

Landed on `integration/mycelium-multi-device-readiness` (9 commits, ~8,500 insertions, `133 passed in 7.37s` across the new lanes):

| Plan task | Plan said | **As built** | State |
|---|---|---|---|
| 0.1 | `test_router_docs.py` | same | ✅ landed (`debb4f4`) |
| 0.2 | `scripts/claim_boundary_audit.py` | same | ✅ landed (`debb4f4`) |
| 2.1A | `mycelium_invite/bundle.py` | same | ✅ landed (`19c2d93`) |
| 2.1B | `mycelium_invite/sqlite_registry.py` | same | ✅ landed (`19c2d93`) |
| 2.2A | `mycelium_membership/contracts.py`, `tests/membership/test_contracts.py` | `mycelium_membership/contracts.py`, **`tests/membership/test_membership_contracts.py`** | ✅ landed (`aa14402`), gaps in Phase 2R |
| 2.2B | `mycelium_node_agent/{agent,__main__}.py`, `tests/node_agent/` | **`mycelium_node/{identity,membership,process}.py`**, `tests/node_agent/` | ✅ landed (`299278c`), **no `__main__`** |
| 2.3A | `mycelium_seed/{coordinator,http_api,placement,__main__}.py`, `tests/seed/` | **`mycelium_seed/{coordinator,http,state}.py`**, **`tests/seed_coordinator/`** | ✅ landed (`ac18360`,`d76fe6d`), **no `placement.py`, no `__main__`** |
| 2.3B | `tests/integration/test_seed_node_agent_e2e.py` | **`tests/seed_coordinator/test_local_two_node.py`** (real subprocesses + real sidecar via the `e2e_request_iroh` fixture — the reuse §6.1 asked for) | ⚠️ partial, 130 lines |
| 3.1 | `mycelium_qualification/stage_pack.py`, `tests/qualification/test_stage_pack.py` | **`stage_pack.py`** (top level, 1,030 lines), **`test_stage_pack.py`** (top level, 591 lines) | ✅ mostly landed (`0f468b4`,`ad370ea`), gaps in Phase 2R |
| 2.4 | decision task | — | ❌ not done — **but now decided, see §6.11** |

**Use the as-built paths from here on.** The module names are fine; do not rename them to match the old plan text. Three genuine structural wins in the as-built code that the plan did not ask for and should keep: `mycelium_seed/state.py` (SQLite persistence factored out of the coordinator), `mycelium_node/identity.py` (node identity separated from membership), and idempotent heartbeat-renewal recovery keyed by `(node_id, generation, heartbeat_message_id)`.

Verified good: **no member-count branching anywhere in `mycelium_seed/`** — grep for `!= 2`, `len(members)`, `node_a`/`node_b` returns nothing. §6.8's N-peer-clean requirement is satisfied. That was the expensive thing to get right and it was got right.

The seven things the as-built code does **not** yet do are Phase 2R below. None are rework; all are additions.

---

## 2. Current verified baseline **[rewritten by the as-built pass]**

Active branch is **no longer** `integration/mycelium-live-demo`. Five worktrees now exist:

```bash
export PATH=/opt/homebrew/bin:$HOME/bin:$HOME/.nvm/versions/node/v22.21.1/bin:$PATH

# The live lane — build here
export REPO=/Users/evinova/Projects/mycelium-multi-device-readiness
cd "$REPO" && git status --short --branch
```

```text
## integration/mycelium-multi-device-readiness...origin/integration/mycelium-multi-device-readiness
```

| Worktree | Branch | HEAD | Role |
|---|---|---|---|
| `mycelium-multi-device-readiness` | `integration/mycelium-multi-device-readiness` | `22ae8a9` | **live lane, build here** |
| `mycelium-canonical-audit-20260722` | `integration/mycelium-live-demo` | `88688f5` | the old reconstruction point; historical |
| `mycelium-membership-contracts` | `integration/mycelium-membership-contracts-mac` | `debb4f4` | Phase 0 only; absorbed into readiness |
| `mycelium-durable-heartbeat` | `fix/durable-heartbeat-renewal` | `d76fe6d` | absorbed into readiness |
| `mycelium-stage-pack` | `build/model-stage-pack` | `d76fe6d` | absorbed into readiness |
| `mycelium-phase-2r-contracts-mac` | detached | `5dfef7b`+ | **ACTIVE agent worktree — do not touch** |
| `mycelium-phase-2r0` | detached | `ad370ea` | recent scratch, clean |
| `mycelium-phase-2r0-mac` | detached | `ad370ea` | recent scratch, holds the 2R.0 rename |

All four named branches (`fix/durable-heartbeat-renewal`, `build/model-stage-pack`, `integration/mycelium-membership-contracts-mac`, `integration/mycelium-live-demo`) are **ancestors of the live lane** — their work is merged. **Do not keep building in those worktrees.** Confirm before starting:

```bash
LIVE=$(git rev-parse origin/integration/mycelium-multi-device-readiness)
git merge-base --is-ancestor <branch> "$LIVE" && echo absorbed
```

**Worktree hygiene (2026-07-22 ~19:15).** Eight worktrees now exist and several hold *superseded* uncommitted work — `mycelium-stage-pack`'s untracked `stage_pack.py`/`test_stage_pack.py` are byte-identical to what landed, and `mycelium-membership-contracts`'s `mycelium_membership/contracts.py` is 464 lines **behind** the live version. Do not treat uncommitted files in absorbed worktrees as work to rescue; diff them against the live lane first.

Cargo build caches were reclaimed from the four absorbed worktrees (~8 GB; `native/iroh_transport/target` only — **no source or uncommitted file was touched**). They rebuild on demand via the `tests/e2e_request_iroh/conftest.py` fixture. The caches in the live lane, the active agent worktree, and the two recent scratch worktrees were deliberately **left intact**.

**Before creating another worktree, check whether an idle one already suits** — each carries a ~1.5 GB build cache once its sidecar is built.

Focused gate on the live lane, actually observed 2026-07-22 14:00:

```bash
python3.14 -m pytest -q tests/invite tests/membership tests/node_agent \
  tests/seed_coordinator test_stage_pack.py
# 133 passed in 7.37s
```

Full-suite gate after Task 2R.0, actually observed on the integration Mac at
`5dfef7b` (the tested worktree was `ad370ea` plus the byte-identical 2R.0 diff):

```bash
python3.14 -m pytest -q
# 1954 passed, 11 skipped, 121 subtests passed in 126.67s
```

Recovered-feature gate (unchanged in meaning, still hardware-skipped):

```bash
python3.14 -m pytest -q \
  tests/invite tests/tokenizer tests/model \
  tests/physical_qualification tests/qualification/test_physical_deployment.py
```

Observed at reconstruction: `18 passed, 9 skipped` from 27 collected. Hardware skips do not prove a physical route.

**Current authoritative full-suite figure — `22ae8a9`, integration Mac, 2026-07-22 ~18:30:**

```text
1973 passed, 11 skipped, 121 subtests passed in 135.77s
```

Re-record this number after every merge (§7.1 merge discipline). Superseded figures are kept below only as history — never quote them as current.

**Current authoritative full-suite figure — `b317566`, integration Mac, 2026-07-22 ~22:15:**

```text
2031 passed, 3 skipped, 121 subtests passed in 192.68s
```

This run followed a targeted replay of the one transient five-worker cold/contended load timeout. Ten orphaned sidecars belonging only to prior `/tmp/mycelium-*validation*` worktrees were terminated; the exact 5-way DialoGPT test then passed in 10.71 s, and the complete clean-state suite above passed. This confirms the previously documented timeout hazard; it does not justify weakening or raising the 60 s gate.

Integrated execution state:

- Phase 2R liveness, placement seam, seed/node entrypoints, 3/5-way stage-pack gates, and frozen FP16 policy: landed through `132e7c3`.
- Canonical real two-node local E2E: landed in `tests/seed_coordinator/test_local_two_node.py`; Main's stale untracked `tests/integration/test_seed_node_agent_e2e.py` was not copied into Git or deleted.
- Task 3B.5 steps 1–3: NumPy selected from measured Main imports/footprints; same-seed monolithic MLX parity measured at max absolute logit drift `1.4901161193847656e-08`, exact argmax; landed as `4fbe48f`. Stage parity and mixed physical Mac+Linux execution remain open steps 4–5.
- Task 3.2 skeleton: seed-signed current-epoch offer verification, transfer allowlist/digest confinement, arbitrary peer-count dry-run, physical host/boot distinctness, and argv-only runner gates; landed as `b317566`. Physical `prepare/run/recover/seal` deliberately returns `physical_execution_not_implemented`; no readiness record was produced.
- Parallel subagents: every dispatched implementation/review worker failed at provider entry with HTTP 402 before touching files. Do not infer independent review from those envelopes.
- Pixel 8 Pro: tailnet identity `100.126.233.4`, offline since 2026-07-20; no ADB/mDNS service, no Main-side bridge token/config, and bridge/worker ports 9020/9018 closed on tailnet and LAN candidates. Device-side Tailscale or wireless-debug/bridge setup is required before live execution.

**Pinned model snapshot [2026-07-22 product pass]:** every task in this plan that says "DialoGPT" or "GPT-2" means exactly:

```text
microsoft/DialoGPT-small
revision 49c537161a457d5256512f9d2d38a87d81ae0f0e
~/.cache/huggingface/hub/models--microsoft--DialoGPT-small/snapshots/49c537161a457d5256512f9d2d38a87d81ae0f0e
```

as already hardcoded in `tests/model/test_dialogpt_local_decode.py:14`.

**✅ FETCHED 2026-07-22 ~19:40 (operator-authorised, Wi-Fi).** `hf download microsoft/DialoGPT-small --revision 49c537…` landed 12 files at the exact pinned path, including `model.safetensors` (351,256,598 B), `vocab.json`, `merges.txt`, and `config.json`. The recovered-feature gate moved from **18 passed / 9 skipped** to **61 passed / 1 skipped**. This is no longer a Task 3.3 preflight blocker on the integration Mac — but **it is still absent on every physical peer**; Task 3.3 preflight must verify the checkpoint digest per host, and the same explicit fetch is required on the M4 Pro and any other route member. Downloads remain disabled *during* qualification (§7).

> **⚠️ Latent flakiness found while verifying the fetch — read before Task 3.4.** The first post-download gate run **failed** on `test_sharded_local_processes_match_monolithic_greedy_decode[5]` with `stage-worker load timed out` (`distributed_inference_qualification.py:392`). Re-running that test in isolation passed in 13.05 s, and a full gate re-run passed 61/1 in 56.11 s. The failing run took **107.09 s — roughly double** — because the 350 MB checkpoint had just been written and five stage workers cold-loaded it concurrently against a cold page cache.
>
> It is a **wall-clock deadline** (`timeout_seconds: float = 60.0`, `distributed_inference_qualification.py:511`), not a hang. On slower, contended, or thermally-throttled hardware — or on the M4 Pro's first cold run — this will trip again and will look exactly like a failed physical run. Before Task 3.4: warm the checkpoint (one monolithic load) as an explicit preflight step, and treat any `stage-worker load timed out` as **inconclusive, not a failure**, re-running before recording anything. Do not silently raise the timeout to make a physical run pass — that is a §5 "never edit tolerance to make a run pass" violation in a different costume. If it needs raising, raise it deliberately, in its own commit, with the measured cold-load time as justification.

**History, superseded — do not quote as current.** The pre-Phase-0 baseline on the original checkout was 1,828 passed / 2 failed / 11 skipped / 121 subtests. Both failures are **closed**: the `test_router_docs.py` private-plan dependency (Task 0.1) and the Device Lab claim-audit classification (Task 0.2) both landed in `debb4f4`. The suite then broke at *collection* until Task 2R.0 (`5dfef7b`) renamed `tests/seed_coordinator/test_core.py` → `test_coordinator_core.py`.

M4 Pro is reachable and carried every Main-native gate above. The physical multi-host gate is still blocked because no second distinct activation-capable peer is currently reachable with proven identity/credentials: Astra's MacBook (`100.117.33.124`) remained unreachable through a 120-attempt SSH watcher, and Pixel 8 Pro is offline/unconfigured as recorded above. Do not replace either with loopback or same-host evidence.

---

## 3. Hard target

The headline demo is complete only when all of these hold in one bound run:

1. Two physically distinct Macs participate: M4 Pro and Evis MacBook Pro.
2. Each host runs a distinct node-agent process, physical-node process, and native Iroh sidecar with recorded host/boot/process/endpoint identities.
3. A seed-generated invitation is accepted once, rejected on replay, and bound to the seed trust key and URL.
4. The seed records real capacity profiles and assigns disjoint GPT-2 layer ranges.
5. Each node receives and loads only its assignment-bound tensor pack; no route process loads the monolithic reference or another node's layer pack.
6. Prompt tokenization, prefill, and at least eight greedy decode steps use the pinned local pretrained DialoGPT snapshot (§2).
7. Every cross-stage activation frame travels through production `Router` + native `IrohTransport` between physical hosts.
8. SSH/Tailscale/HTTP may bootstrap or carry signed control messages, but may not forward activations or logits.
9. Generated token IDs exactly match an independent monolithic reference for the fixed public demo prompt; decoded output is recorded as coherent text.
10. Hidden-state/logit tolerance is frozen before the physical run and checked without changing it after seeing results.
11. Cancellation releases route, KV, capacity, transport, and worker state.
12. Remote node death and restart produce a new PID, endpoint, generation, and topology version; stale traffic is rejected and `RECOVERY_PREFILL` continues without token loss or duplication.
13. One immutable evidence directory binds source commit, model digest, assignments, node identities, transport observations, token parity, lifecycle probes, signatures, and cleanup under one `run_id` and `deployment_id`.
14. `mycelium_qualification.qualifier:RouteQualificationV1` is the only component allowed to emit `route_ready=true`.
15. `release_ready` remains false.

### 3.1 Product target — what makes it a swarm rather than a pipe **[2026-07-22 product pass]**

The headline target above is a *transport and correctness* proof. It is deliberately allowed to use a hand-frozen placement. The **product** target is Phase 3B and is complete only when all of these hold in one further bound run:

16. Placement for the physical run is produced by `mycelium_layer_planner.planner:plan_snapshot` from a real `GossipService` `EvidenceBundle` carrying each node's measured `mycelium.device_profile.v2` / `device_status.v1`, not by a frozen constant. Changing a node's advertised capacity changes the assignment it receives, and this is demonstrated by an A/B run.
17. The execution graph for that run is compiled by `mycelium_router.layer_builder:build_execution_graph`, not by `mycelium_qualification.physical_deployment:build_execution_graph`.
18. A third device joins the same live swarm through the same invitation path and receives a *different* assignment as a consequence of its measured profile, with no code change and no operator-typed layer range.
19. The route order and the decode loopback edge come from the directed-cycle search (`cycle_search.search_cycle` via `primary_plan.plan_primary`), and the chosen order is shown to differ from — and cost less than — naive node-id ordering on at least one real measured link-cost matrix. See §6.10.
20. A prompt submitted through `mycelium_request_gateway` (not a test harness) streams tokens back from the physical swarm.
21. Removing one non-entry peer mid-swarm produces a scoped replan candidate rather than a whole-swarm failure.

Items 16–21 are gates G4b and G8 in §5.

---

## 4. Non-goals until the physical FP16 gate passes

- Quantization, pruning, speculative decoding, throughput optimization, continuous batching tuning, or larger-model selection.
- UI polish, marketing screenshots, or demo copy.
- Treating local process sharding, loopback TCP/Iroh, fake hosts, or Pixel HTTP as physical route success.
- Full-model fallback on either route process.
- New transport, planner, runtime, signer, or qualifier implementations when existing production modules can be adapted. **This now explicitly includes the planner, cycle search, LayerBuilder, `SwarmCoordinator`, `mycelium_physical_preflight`, and `mycelium_request_gateway` — see §1.2. Writing a second one of any of these is a plan violation, not an optimization.**
- WAN/NAT/relay-scale claims.
- Arbitrary model-family support beyond the recovered GPT-2/DialoGPT path.
- Release readiness.

Non-goals that survive **past** the FP16 gate, i.e. still out of scope in Phase 3B:

- WAN/NAT/relay-scale claims (unchanged).
- Byzantine or untrusted-peer threat models. Phase 3B peers are seed-invited and signed; a malicious-peer story is separate work.
- Replica groups / multi-loop replication and cross-cycle edges (`LAYER_PLANNER_PRODUCT_V1.md` §6). Phase 3B proves one primary cycle only.
- Column-generation planner upgrade (`LAYER_PLANNER_PRODUCT_V1.md` §7 close).

---

## 5. Proof ladder and stop conditions

| Gate | Required proof | Current status | Stop condition |
|---|---|---:|---|
| G0 | Clean repository baseline and claim-boundary gates | **Passed at `5dfef7b`: 1954 passed, 11 skipped, 121 subtests** | No new feature commit while red |
| G1 | Local pretrained model and 2/3/5-process token parity | Passed | Regression blocks all later work |
| G2 | Durable invitation, node-agent, seed-coordinator contracts | Missing | No physical orchestration before localhost E2E |
| G3 | Stage-local tensor packs; no unassigned tensors | Missing | No model transfer to physical node |
| G4 | One real two-Mac Router/Iroh decode with exact token parity | Missing; M4 Pro offline | Headline target; no substitute evidence |
| G5 | Physical cancellation and restart/recovery | Missing | No qualification attempt |
| G6 | Immutable evidence accepted by sole qualifier | Missing | `route_ready` stays false |
| **G4b** | **Same physical route, placement produced by gossip→planner→LayerBuilder instead of the frozen split** | **Missing** | **Product claim blocked; "capacity-aware" may not be said aloud** |
| **G8** | **Third device joins live and receives a capacity-derived assignment; peer departure produces a scoped replan candidate** | **Missing** | **"Swarm" may not be said aloud** |
| **G9** | **A non-MLX device executes a real assigned layer through production Router transport (§6.13)** | **Missing; no non-MLX runtime exists at all** | **"Heterogeneous" may describe membership only, never execution** |
| G7 | Full repository/Rust/UI/claim gates green | Baseline known, not final | No push of final physical-proof tranche |
| Q1 | Quantized monolithic quality + memory gate | Deferred behind G6 | No distributed quantized work |
| Q2 | Quantized physical route accepted | Deferred | No limited-hardware claim |

Ordering: `G0 → G1 → G2 → G3 → [Phase 2R] → G4 → G5 → G6 → G7(interim) → G4b → G8 → G9 → G7(final)`, with G9's runtime-contract extraction runnable in parallel from the start.

Priority: Phase 3B (G4b, G8) and G9 both outrank Phase 4 (Q1, Q2). Quantization extends reach on devices that already run; G9 creates a device class that currently runs nothing, and the swarm is the product. **Phase 2R gates all of Phase 3** — five of its seven tasks change signed contract shapes, and a contract change after a run is sealed against it invalidates that run.

Fail closed at every gate. Preserve failed evidence; never edit evidence or tolerance to make a run pass.

### 5.1 Cross-reference to the synthesis handover's claim ladder (Gate I)

The synthesis handover (`~/Projects/.audit/MYCELIUM_MVP_SYNTHESIS_HANDOVER.md` §8, Gate I) defines the authoritative claim-language ladder. Never describe this project's state using a stronger label than the highest rung actually proved, and never skip a rung when describing progress:

1. analytical simulation
2. deterministic in-process component test
3. local multi-process test
4. two logical peers on one host
5. two physical peers carrying verified synthetic activation envelopes
6. two physical peers provisioning artifacts
7. two physical peers loading assigned stages
8. two physical peers executing full-route inference
9. fault-tolerant physical distributed inference

Approximate mapping from this plan's gates to that ladder and to handover §8 Gates A–I, so status reports stay consistent across both documents:

| This plan's gate | Ladder rung reached when green | Handover gate(s) it satisfies |
|---|---|---|
| G1 (local token parity) | 3–4 | Gate C (current-source quality) |
| G2 (membership/orchestration contracts) | still 3–4 (control-plane only, no physical data-plane yet) | Gate A/B partial |
| G3 (stage-local packs) | still 3–4 | Gate D (artifact provisioning) |
| G4 (real two-Mac decode) | **5→8** — the activation-spike already sits at rung 5 (synthetic envelopes); G4 must additionally prove rungs 6 (provisioning), 7 (stage loading), and 8 (full-route execution) in the same bound run | Gates D, E, F, G |
| G5 (cancellation + restart/recovery) | 8→9 | Gate H |
| G6 (sealed qualifier acceptance) | confirms whichever rung the sealed evidence actually supports — the qualifier does not itself raise the rung | Gate G/H sign-off |
| **G4b (planner-driven placement)** | **stays at 8–9; the ladder does not measure placement provenance** | **Gate F, properly — the handover's Gate F is about the Layer Builder using only valid current evidence, which the frozen split never exercises** |
| **G8 (join-and-replan swarm)** | **9, and only here** | **Gate F + Gate H together** |
| G7 (full repo gates green) | not a rung, a hygiene gate | Gate A/B/C maintenance |

Do not report "G4 passed" as rung 9 — recovery (G5) and negative-run rejection (Task 3.7) are still required to reach rung 9's "fault-tolerant" bar.

**Claim-language warning added by the product pass:** Gate I's ladder measures *physical execution*, not *placement intelligence*. It is possible to sit at rung 8 with a hardcoded split. When reporting status, always pair the rung with a placement qualifier, e.g. `rung 8, frozen placement` versus `rung 8, planner placement`. Saying "capacity-aware distributed inference" while G4b is red is a claim-boundary violation even though the rung number is honest.

---

## 6. Locked architecture decisions

### 6.1 Reuse production seams

- Reuse `physical_inference_node.py:PhysicalNodeService`; node agent wraps its command protocol rather than cloning Router/runtime logic.
- Reuse `probe.py:profile` and `mycelium_capacity_profiles.compile_capacity_profile` for node capability input.
- Reuse `mycelium_qualification.physical_deployment.prepare_physical_deployment` and `compile_layer_assignments` for placement inputs.
- Reuse `mycelium_qualification.signing.Ed25519EvidenceSigner` for seed/node signatures.
- Reuse `mycelium_qualification.sealer`; do not create a second promotion path.
- Reuse native `IrohTransport`; application HTTP is control plane only.

**Additional seams identified by the product pass [2026-07-22 product pass]** — all verified present, all previously missing from this list:

- Reuse `mycelium_interactive.swarm:SwarmCoordinator` for invitation/enrollment/work-dispatch semantics; see §6.11 and Task 2.4 for the extend-vs-supersede decision.
- Reuse `mycelium_physical_preflight` (`generator`/`schema`/`validator`, `python3.14 -m mycelium_physical_preflight`) for Task 3.3; do not hand-roll preflight collection.
- Reuse `tests/e2e_request_iroh/harness.py` and its sidecar-building `conftest.py` as the base for Task 2.3B.
- Reuse `mycelium_layer_planner.planner:plan_snapshot`, `gossip_adapter:planner_snapshot_from_evidence_bundle`, `primary_plan:plan_primary`, and `cycle_search:search_cycle` for Phase 3B placement. Do not write a placement heuristic.
- Reuse `mycelium_router.layer_builder:build_execution_graph` for Phase 3B graph compilation.
- Reuse `mycelium_request_gateway` (`RequestGatewayService`, `cli:stream_prompt`) for the Task 3B.4 user-facing prompt path.

### 6.2 Bootstrap trust

The current token alone is insufficient for a fresh node to authenticate future seed responses because the public-key trust anchor is out of band. Add a join bundle containing:

```json
{
  "protocol": "mycelium.join_bundle.v1",
  "invite_token": "<redacted from logs>",
  "seed_url": "http://<tailscale-host>:<port>",
  "seed_key_records": [{"algorithm": "ed25519", "key_id": "...", "public_key": "..."}],
  "seed_key_digest": "sha256:..."
}
```

The bundle itself is the operator-delivered trust object. Pass it through an owner-only file or stdin, never a process-list argument. The node verifies all subsequent seed records against that pin.

### 6.3 Membership durability

`InviteRegistry` is currently memory-only. Physical membership must use SQLite with an atomic unique nonce insert, expiry pruning, and restart persistence. A seed restart must not make an invitation reusable.

### 6.4 Stage-local transfer

The seed may hold the complete local checkpoint. Route nodes receive assignment packs containing only:

- entry node: token/position embeddings plus its assigned transformer blocks;
- intermediate node: only its assigned transformer blocks;
- final node: its assigned blocks plus final normalization and output-head material;
- exact assignment, parent-manifest, tensor-name, dtype, size, and content-digest bindings.

A monolithic Safetensors source is not copied wholesale to every node.

The three roles above are already written for an N-node route (entry / intermediate / final). Keep the intermediate case implemented and tested even though the first physical proof uses only two nodes — Task 3B.2 adds the third device and must not require a stage-pack rewrite.

### 6.5 Same-run evidence

Do not combine local model parity from one run with physical transport from another. Every positive claim must bind the same run/deployment/request IDs, source/model/assignment digests, node signatures, and transport observations.

### 6.6 Transport peer authentication must be signed, not a static allowlist

`mycelium_router/transports/iroh.py` currently authenticates peers by exact string match on `expected_endpoint_id` alone — no signature, no epoch, no revocation. The synthesis handover explicitly names this exact condition `production_ready=false` (§4.5) and requires, for Gate G: *"Signed deployment membership authorizes exact iroh EndpointIDs and rejects stale/revoked epochs."*

For this demo, the seed coordinator (Task 2.3A) is the natural issuer of that membership snapshot: it already signs `AssignmentOfferV1` and knows each node's public key from `JoinRequestV1`. The node agent (Task 2.2B) must configure `IrohTransport` with an `expected_endpoint_id` that came from a **signed, deployment/epoch-scoped record issued by the seed** — not a value typed into a config file or CLI flag by the operator. A stale or wrong-epoch membership record must be rejected before the transport accepts the connection. Add this as an explicit RED case to Task 2.2B and Task 3.2 (see amendments below); do not let the physical demo quietly ship with the same static-allowlist posture the activation spike already flagged as insufficient.

Note the sites that will need to consume the signed record rather than a constant: `iroh.py:148` (constructor parameter), `:297` (handshake comparison), `:531` and `:818` (replacement-endpoint comparisons), `:1142` (fallback). Any of these left reading an operator-supplied constant reopens the gap.

### 6.7 Heartbeat and receipt liveness policy

No heartbeat or keepalive logic exists anywhere in `mycelium_router/` or `mycelium_gossip/` today. The synthesis handover locks this policy as cross-session decisions #18–19 (§5) and as part of Gate H (§8):

- A valid activation or application receipt is passive evidence of recent peer/edge liveness and must suppress redundant active-period heartbeats.
- Low-rate heartbeat/keepalive remains necessary only while idle, and for failure detection when no inference traffic exists.
- One missing receipt is a **scoped hop/edge timeout signal**, not immediate proof of whole-node death. Peer death requires corroboration through connection state, idle heartbeat/keepalive failure, and evidence-plane freshness/quarantine policy.

`HeartbeatV1` (Task 2.2A) must carry this behavior, not just the wire shape. Task 3.6 (remote death/restart/recovery) must distinguish "in-flight decode stage died mid-request" (detected via transport/Router failure, already covered) from "an otherwise-idle peer went dark" (must be caught by keepalive + evidence-freshness, not by a single missing receipt).

### 6.8 Gossip evidence-plane scoping for the first two-node proof **[amended by the product pass]**

Gossip (`mycelium_gossip`) is already wired into production planning (`mycelium_router/layer_builder.py`, `mycelium_layer_planner/gossip_adapter.py`) and is fully green (108 passed, 2 skipped, verified 2026-07-22). The seed coordinator's bespoke HTTP `JoinRequestV1`/`HeartbeatV1` membership plane (Tasks 2.2A–2.3A) is **not** a replacement for gossip's evidence-plane role in the general N-peer architecture — it is a deliberately scoped substitute for the first 2-known-peer proof, where a central seed can track both nodes' capacity directly without needing gossip's decentralized dissemination.

**The original text of this section forbade generalizing past two peers. That ceiling is lifted, with a narrower one in its place.** The revised rule:

- **The seed/node-agent control plane must be N-peer-clean from the first line of code.** No `if len(members) != 2`, no two-element tuples, no `node_a`/`node_b` naming, no assumption that the member set is known before the first join. This costs nothing at implementation time and is the difference between Task 3B.2 being an afternoon and being a rewrite.
- **Only the first physical proof (Task 3.4) freezes placement**, and it does so by pinning the *output* of placement, not by hardcoding the *mechanism*. Write the frozen split as a checked-in fixture that the seed loads, so that swapping in planner output at Task 3B.1 is a source substitution and not a surgery.
- **`mycelium_qualification.physical_deployment:build_execution_graph` (2-assignment-locked, `physical_deployment.py:156`) is scaffolding.** Task 3B.1 replaces its use in the physical path with `mycelium_router.layer_builder:build_execution_graph`. Do not add features to the 2-node builder; do not delete it either, since existing qualification tests bind it.

Peers still may not join without seed introduction — that restriction stands for this plan, since invitation-gated membership is the trust model (§6.2). Open/permissionless join is separate work.

### 6.9 The frozen split is scaffolding with a scheduled removal, not the architecture **[2026-07-22 product pass]**

Every artifact produced under the frozen split must be labelled `placement_provenance: "frozen_fixture"` in its evidence record, and Task 3B.1's artifacts must be labelled `placement_provenance: "planner_v2"`. The qualifier must reject an evidence bundle whose `placement_provenance` is absent. This makes "did we ship the demo or the product" a mechanically checkable property rather than a memory of who ran what.

Rationale: the single most likely failure mode of this plan is that G4 goes green, the two-Mac video gets recorded, and the frozen split silently becomes the permanent architecture because nothing in the repository objects to it. A required evidence field objects to it.

### 6.10 Cyclic decode topology and the directed-cycle search **[2026-07-22 product pass]**

*This section exists because Astra asked whether the cyclic graph algorithm works. Short answer: it exists, it is exact for small fleets, it is green, and it has never been run against real measured link costs or a real physical route.*

**Why the graph is cyclic at all.** Prefill traverses the model once — an acyclic source-to-final path. Autoregressive decode does not: after the final stage samples a token, that token must return to the entry stage for the next step. So the decode topology is a **directed cycle**, and the closing edge is asymmetric — it carries a sampled-token envelope (`token_envelope_bytes`, 9 bytes in `physical_deployment.py`) rather than a hidden-state activation (`hidden_size × activation_bytes`, ~1.5 KB for DialoGPT-small FP16). Choosing the route order is therefore an **asymmetric TSP / directed Hamiltonian cycle** problem, not a shortest-path problem, and the cheap closing edge means the optimal order is often *not* the one a symmetric-distance intuition predicts. `LAYER_PLANNER_PRODUCT_V1.md` §4.3, §5.1–5.2 and §7 are the authority; read them, not the handover's one-line summary of them.

**What is implemented, in `mycelium_layer_planner/cycle_search.py`:**

| Function | Strategy | Selected when |
|---|---|---|
| `exact_directed_cycle:65` | full permutation enumeration, canonical rotation, stable ties | `n ≤ policy.exact_cycle_max_nodes` (default 7) |
| `held_karp_cycle:86` | Held–Karp subset DP, `O(n²·2ⁿ)` time / `O(n·2ⁿ)` memory | `n ≤ policy.held_karp_max_nodes` (default 12) |
| `heuristic_cycle:180` via `_nearest_neighbor:134` + `_local_swap:151` | multi-start nearest-neighbour with directed 2-opt-style swap improvement | `n ≤ 32` → `multi_start_insertion`; `≤ 128` → `clustered_refinement`; else `hierarchical_refinement` |
| `search_cycle:202` | the tiering dispatcher over the above | always the entry point |
| `open_cycle:218` | rotates a chosen cycle to a start index and returns the explicit `(last, first)` loopback edge | when handing the cycle to the Router |
| `cycle_cost:43` | directed cost including explicit closure | scoring |

`CycleResult` carries `globally_exact: bool` and `explored_candidates: int`, so the planner reports honestly whether the returned cycle is optimal or merely good — that honesty is already tested.

**How it is wired.** `mycelium_layer_planner/primary_plan.py:118` calls `search_cycle(nodes, network_cost, policy)` to seed the §7.2 nested search (cycle candidate → each feasible opening → contiguous layer-allocation DP → separate prefill/decode phase scores → memory-feasibility rejection → best under policy). `open_cycle`'s loopback then becomes the Router's `loopback_edges`, which is why forward stages can stay a validated DAG while decode still closes the loop — see `ExecutionGraph.loopback_edges` and `mycelium_router/validation.py`.

**Test status, verified 2026-07-22 13:00:**

```bash
python3.14 -m pytest -q test_layer_planner_v1_cycle_exact.py test_layer_planner_v1_cycle_scaled.py
# 7 passed in 1.91s
```

Covering: closure is counted and the reverse direction may differ (asymmetry is real); exact matches brute force with stable ties; a missing edge rejects the cycle; `open_cycle` rotates and preserves the loopback; Held–Karp matches exact on the overlap; strategy boundaries report optimality honestly; results are deterministic under input reordering.

**The honest gap — and Task 3B.3's job.** All seven tests use synthetic cost matrices. Nothing has yet:

1. fed the cycle search a cost matrix derived from **measured** `link_state.v1` RTT/goodput between real hosts (`network_cost.transfer_time_ms` + `phase_edge_costs`);
2. demonstrated a case where the directed search picks an order that **differs from and beats** naive node-id ordering — with only two nodes there is exactly one cycle, so two-Mac G4 cannot possibly exercise this, which is why Task 3B.2's third device is a prerequisite for Task 3B.3;
3. run an opened cycle end-to-end through the physical Router so the loopback edge carries a real sampled token between real hosts;
4. checked the asymmetry claim empirically — that the cheap token-envelope closing edge shifts the optimal order versus a symmetric cost model.

Item 2 is the specific thing worth telling Astra: **the algorithm is proven correct against brute force, but has never been shown to matter.** Three real devices with genuinely asymmetric links is the smallest experiment that can show it does. Item 4 is the interesting result if it holds.

### 6.11 Reconciling `mycelium_seed` with the existing `SwarmCoordinator` **[decision recorded, as-built pass]**

> **DECIDED — EXTEND. One swarm.** Operator decision, 2026-07-22. `mycelium_seed` becomes the signed/durable transport-agnostic core; `mycelium_interactive.swarm:SwarmCoordinator` is refactored to sit on top of it as the **browser peer-class adapter** — not retired, not left parallel. Browser peers inherit Ed25519-signed records and SQLite durability.
>
> There is one membership plane, one trust model, one lease/generation concept, and one place where a peer's capacity enters placement. This is now load-bearing for the product target (§6.13): browsers, phones, Linux laptops and Macs are peer *classes* of one swarm, differing in **runtime** and **transport**, never in **membership**.
>
> Task 2.4 is therefore no longer a decision task — it is the refactor. The as-built `mycelium_seed/{coordinator,state}.py` already has the right shape (no member-count branching, identity keyed by `node_id`, generation/incarnation fencing); what it lacks is any notion of peer class → Task 2R.7.

The analysis that produced the decision follows.


`mycelium_interactive/swarm.py:287 SwarmCoordinator` already implements roughly 80% of Task 2.3A's endpoint list. Overlap:

| Task 2.3A endpoint | Existing equivalent |
|---|---|
| `POST /v1/join` | `create_invite:418` + `exchange_invite:443` → `JoinGrant` |
| `GET /v1/commands` | `poll_work:506` |
| `POST /v1/command-results` | `submit_result:728` |
| `GET /v1/status` | `status:881` |
| (assignment offer) | `dispatch:633` + per-peer stage-pack scoping |
| (revocation / lease end) | `revoke_peer:846`, `leave:857` |
| (cancellation) | `cancel_request:797` |

Genuine differences that mean it is **not** a drop-in: it is memory-only (no SQLite durability, §6.3); it authenticates with bearer session tokens rather than Ed25519-signed records (§6.6); it is built for browser peers exchanging JSON hidden-state matrices, not Mac peers exchanging Iroh activation frames; and it holds one `stage_pack` for the whole coordinator rather than per-assignment packs (§6.4).

**Decision required in Task 2.4, before any `mycelium_seed/` file is written.** The two acceptable outcomes:

- **Extend** — `mycelium_seed` becomes the signed/durable transport-agnostic core, and `SwarmCoordinator` is refactored to sit on top of it as the browser-peer adapter. Higher up-front cost; one swarm.
- **Supersede with a dated convergence plan** — `mycelium_seed` is built fresh for signed Mac peers, `SwarmCoordinator` keeps serving the browser swarm, and a written note states which one wins and when the other retires.

The unacceptable outcome is building `mycelium_seed` without deciding, leaving two divergent swarm control planes with different trust models and no owner. Whichever is chosen, record it in `QUESTIONS.md`.

### 6.13 Target shape: genuinely heterogeneous devices — and the MLX blocker that creates **[2026-07-22 as-built pass]**

> **DECIDED.** Operator decision, 2026-07-22: the swarm is for **genuinely heterogeneous devices** — phones, browsers, Linux laptops and Macs in one swarm — not an Apple-Silicon fleet.

Known device roster for MVP testing:

| Device | Runtime today | Transport today | Peer class |
|---|---|---|---|
| Evis MacBook Pro | MLX ✅ | native Iroh ✅ | full route member |
| M4 Pro (`m4pro`) | MLX ✅ | native Iroh ✅ | full route member — **offline, blocks G4** |
| Astra's MacBook | MLX ✅ | native Iroh ✅ | full route member |
| Pixel | `physical_pixel_host_stage.py`, pure-Python | **stdin/http, not Router** ❌ | non-production stage |
| Linux laptop ×2 | **none — no MLX** ❌ | Iroh available (Rust sidecar builds) | **blocked, see below** |
| External testers' devices | unknown | unknown | unknown — see onboarding note |

**This decision converts a Phase 4 afterthought into a critical-path blocker.** Every runtime path in the tree is MLX: `runtime_loader.py`, `mycelium_router/mlx_runtime.py`, `two_process_runtime_qualification.py`, `distributed_inference_qualification.py`. `install_serving_backend.py` only *recommends* a backend. **A Linux laptop cannot hold a layer today** — there is no code path by which it executes a transformer block. Neither can the Pixel, on the production Router path.

So the stated product target is unreachable through Phase 3B alone. Phase 3B proves *heterogeneous membership and placement* (any device can join, be measured, and be assigned) but only Macs can actually *execute*. That is a real and useful milestone — say it precisely, do not round it up to "heterogeneous inference".

**Task 4.5 is therefore promoted out of Phase 4** and becomes **Task 3B.5**, running in parallel with 3B.1–3B.4 rather than after quantization. See that task for the runtime-selection gate. Quantization (Phase 4) stays deferred: it extends reach on devices that *can* already run, which is a smaller win than a second device class that currently runs nothing.

**Claim-language rule this adds.** Until a non-MLX device executes a real assigned layer through production Router transport, the highest honest statement is:

```text
Heterogeneous membership and capacity-driven placement across N device classes;
execution proven on Apple Silicon only.
```

Never "heterogeneous distributed inference". §5.1's rung-plus-provenance format gains a third component: rung, placement provenance, **and executing device classes**.

**External testers — onboarding note.** "Other people with multiple other devices are interested in helping test" changes the trust posture. Everything in this plan assumes operator-controlled hardware: invitation bundles delivered by owner-only file, `0700`/`0600` state roots, SSH-driven preflight, prompts that are fixed and public. A stranger's device joining means: their device sees layer weights and activation tensors for its assigned stage; the operator cannot verify their host identity claims; and evidence signed by their node is only as trustworthy as their key custody. None of that is a blocker for *testing*, but it must not silently become the production trust model. Before any non-operator device joins, write `docs/contracts/external-tester-boundary.md` stating what a tester's node can see, what it may attest to, and what the swarm refuses to believe from it. Byzantine peers remain out of scope (§4) — this note is about being explicit that the current model is *trusted-invited-peer*, not *untrusted-peer*.

### 6.12 The product surface is the request gateway, not a test harness **[2026-07-22 product pass]**

`mycelium_request_gateway/` already provides `RequestGatewayService`, ASGI app, auth, observability, a client, and `cli.py:13 stream_prompt`. `mycelium_demo/cli.py` already exposes `doctor`, `device-lab`, and `serve`. `tests/e2e_request_iroh/harness.py` already proves gateway → Router → native Iroh locally.

Every physical run in Phase 3 may be driven by the qualification controller, because those runs exist to produce evidence. But Task 3B.4 must demonstrate the swarm through the ordinary user path — `stream_prompt` against a running gateway backed by the physical route — because "genuinely useful test product" means someone who is not holding this plan can type a prompt and get tokens from the swarm.

---

## 7. Safety and privacy invariants

- Keep this plan local and ignored; do not force-add it.
- Use explicit file staging; never `git add -A` or `git add .`.
- Private signing keys never leave their process or enter evidence.
- Invitation files, SQLite state, stage packs, and evidence roots use mode `0700` directories and `0600` files.
- Logs redact invite tokens, bearer/control tokens, raw key material, shell environment, and model-cache absolute paths.
- Fixed public demo prompt only; no personal prompts in evidence.
- Node-agent commands are exact-schema, signed, run/deployment bound, replay protected, deadline bounded, and idempotent where retryable.
- Model/artifact downloads are disabled during qualification. A missing snapshot is a preflight blocker. (The pinned DialoGPT snapshot in §2 is currently missing on the integration Mac — fetch it as an explicit pre-qualification step, never from inside a qualification run.)
- Controller cleanup is mandatory on success, failure, timeout, and interruption.
- Do not wake, reconfigure, or install software on physical peers implicitly. Preflight may inspect; installation/download requires an explicit execution decision.
- M4 Pro being offline means G4 is blocked, not passed by another route.

---

## 7.1 Swarm tactics — parallelize aggressively, but only across disjoint files **[2026-07-22 swarm pass]**

**Default to fan-out.** Most remaining tasks in this plan are file-disjoint and should be run as parallel subagents, not sequentially. The 2R.1/2R.3/2R.7 tranche proved the opposite case is also real — those three *had* to be serialized because they touched one signed contract — so the rule is not "always parallel", it is **"parallel unless they share a write surface."**

### The parallelism test

Before fanning out, for each candidate pair ask:

1. **Do they write the same file?** If yes → serialize, or give one agent both tasks.
2. **Do they change the same *contract*, even in different files?** If yes → serialize. A signed wire shape edited by two agents produces two incompatible schema versions that both pass their own tests. This is the expensive failure mode; the 2R.1/2R.3/2R.7 grouping exists because of it.
3. **Does one need the other's output to write its RED test?** If yes → serialize.
4. Otherwise → **run them in parallel.**

### Currently parallel-safe (as of `22ae8a9`, with 2R.4 in flight)

| Lane | Task | Write surface | Notes |
|---|---|---|---|
| A | **2R.4 liveness** | `mycelium_membership/contracts.py`, `mycelium_seed/{coordinator,state}.py`, `mycelium_node/membership.py` | **IN FLIGHT — do not touch these files from any other lane** |
| B | 2R.2 `PlacementSource` | `mycelium_seed/placement.py` (new) + a constructor arg | Touches `coordinator.py` — **coordinate with lane A or wait** |
| C | 2R.5 `__main__` entrypoints | `mycelium_seed/__main__.py`, `mycelium_node/__main__.py` (both new) | Fully disjoint → **safe now** |
| D | 2R.6 tolerances + N-way packs | `tolerances/`, `stage_pack.py`, `test_stage_pack.py` | Fully disjoint → **safe now** |
| E | 3B.5 steps 1–3 (runtime contract + backend selection) | `runtime_contracts.py`, `runtime_loader.py`, new `mycelium_runtime_*/` | Fully disjoint → **safe now**, and unblocks the two Linux laptops |
| F | 3.2 controller skeleton | `physical_inference_qualification.py` (new) | Disjoint from all of 2R → **safe now**; its RED cases can be written before the physical hosts exist |
| G | 2.4 `SwarmCoordinator` refactor | `mycelium_interactive/{swarm,server}.py` | Unblocked now that 2R.7 landed; touches `coordinator.py` seams — **coordinate with A/B** |

Lanes C, D, E, F are safe to launch **simultaneously right now**. That is four parallel subagents while lane A finishes.

### What each subagent must be given

A subagent starting cold will re-derive context badly. Hand it, explicitly:

1. The **task section verbatim** from this plan, including every RED case.
2. **§1.4's as-built path map** — otherwise it creates `mycelium_node_agent/` beside `mycelium_node/`. This has already happened once.
3. The **branch and base commit**, and instructions to work on `feat/<task-slug>` off that base.
4. The **write surface it owns**, and an explicit list of files it must not touch.
5. The **claim-boundary rules** (§5.1, §6.9, §6.13) if its work produces any evidence or prose.

### What subagents must NOT do

- **No physical hardware operations.** SSH to peers, node launches, sealing, and qualifier invocation stay in the primary integration driver. Parallel agents touching one M4 Pro is a race with no upside.
- **No pushes to `integration/*`.** Subagents push to their own `feat/*` branch; the driver merges in a chosen order.
- **No contract edits without owning the whole contract.** See the parallelism test, rule 2.
- **No evidence writes.** One run, one writer, one seal (§6.5).
- **No plan edits.** The plan is the shared authority; concurrent edits to it clobber. Subagents report findings back; the driver amends the plan and records the new SHA-256.

### Merge discipline

Merge order is the driver's decision, not first-to-finish. After each merge run the **full** suite, not the focused lane — the whole point of Task 2R.0 was that focused lanes hid a broken suite for two hours of work. Record the number each time.

### Where fan-out does *not* help

- **Phase 3 physical runs (3.3–3.8)** are inherently sequential: one hardware set, one run identity, one evidence root. Parallelism here corrupts evidence.
- **Task 3B.1's A/B proof** needs two runs of the *same* route with a changed capacity input. Sequential by construction.
- **Anything downstream of a blocked gate.** Fanning out past a red gate produces work that must be re-verified once the gate clears.

---

# Phase 0 — Restore an honest green baseline

### Task 0.1: Replace the private-plan presence test with a public contract test

**Before starting:** run `git log --oneline -5 integration/mycelium-membership-contracts-mac` and diff it against the current live-demo HEAD. Commit `debb4f4` on that branch already rewrites `test_router_docs.py` to point at `docs/request-streaming-session-lifecycle.md`. If that branch has not diverged further in a conflicting way, cherry-pick or merge that commit instead of re-authoring the fix, then verify it still satisfies the RED/GREEN criteria below. (As of 2026-07-22 13:00 that branch is still exactly one commit ahead — 5 files, +86/−3 — so a clean cherry-pick is expected.)

**Objective:** Preserve the Router request-lifecycle contract without requiring an ignored internal planning document.

**Files:**
- Modify: `test_router_docs.py`
- Modify or create only if needed: a tracked public contract under `docs/contracts/`

**RED:** Keep the current failure as evidence that tracked tests depend on deleted private prose.

```bash
python3.14 -m pytest -q \
  test_router_docs.py::RouterDocumentationTests::test_request_usability_plan_covers_streaming_and_kv_lifecycle
```

Expected before repair: `FileNotFoundError` for `docs/plans/2026-07-16-request-streaming-session-lifecycle-mvp.md`.

**GREEN:** Point the test at a stable public contract or replace prose assertions with executable/public-contract assertions. Do not restore the private plan to Git. Preserve coverage of session lifecycle, `RuntimePort.release_kv`, SSE streaming, slow-consumer handling, complete-chat behavior, and the fault-tolerance boundary.

**Verify:** `python3.14 -m pytest -q test_router_docs.py` → 4 passed.

**Commit:** `test: remove private plan dependency from router docs gate`

### Task 0.2: Classify Device Lab as an exact product-action client

**Before starting:** commit `debb4f4` on `integration/mycelium-membership-contracts-mac` already adds `ui/web/src/features/deviceLab/deviceLabClient.ts` to `_ALLOWED_PRODUCT_ACTION_CLIENTS` and extends the audit test with a negative-control `arbitraryClient.ts` case. Reconcile from that commit rather than re-deriving the allowlist entry independently — but still verify no directory/pattern broadening slipped in, per the GREEN criteria below.

**Objective:** Keep Observatory read-only while permitting only the known Device Lab infer/cancel product-action client.

**Files:**
- Modify: `scripts/claim_boundary_audit.py`
- Modify: `tests/claims/test_claim_boundary_audit.py`

**RED:** Add a fixture proving `ui/web/src/features/deviceLab/deviceLabClient.ts` is accepted while a neighboring unlisted file with POST remains rejected.

**GREEN:** Add only the exact Device Lab path to `_ALLOWED_PRODUCT_ACTION_CLIENTS`; do not broaden by directory, filename pattern, endpoint, or HTTP method.

**Verify:**

```bash
python3.14 -m pytest -q tests/claims/test_claim_boundary_audit.py
python3.14 scripts/claim_boundary_audit.py --repo-root . --json
```

Expected: tests pass; checker exits 0 with `ok=true`, `route_ready=false`, `release_ready=false`.

**Commit:** `test: classify device lab product actions in claim audit`

### Task 0.3: Freeze baseline before feature work

```bash
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
python3.14 scripts/claim_boundary_audit.py --repo-root . --json
git diff --check
git status --short
```

Expected: zero test failures; hardware skips remain explicit; clean tree after commit.

---

# Phase 1 — already landed, do not re-execute

There is no Phase 1 task list in this document by design. Phase 1 (local pretrained Safetensors sources, arbitrary layer-count/node splits, GPT-2 byte-level BPE, coherent monolithic decode, exact greedy-token parity across 2/3/5 local process shards) landed before this plan was written and is gate G1. Its regression suite is `tests/model/`, `tests/tokenizer/`, and `distributed_inference_qualification.py`. If any of it goes red, stop all later work until it is green again — G1 regression blocks everything.

*(This heading exists solely so an agent working the §8 tranche list does not go looking for a missing phase.)*

---

# Phase 2 — Membership and orchestration bridge

> **STATUS: LANDED 2026-07-22 on `integration/mycelium-multi-device-readiness`.** Tasks 2.1A, 2.1B, 2.2A, 2.2B, 2.3A built and green (133 tests); 2.3B partial. **Do not re-execute these tasks.** They are retained below as the contract record — what was required, and against which the as-built code was audited. The as-built module paths differ from the `Files:` lists below; §1.4 has the corrected map and is authoritative. Carry-forward gaps are **Phase 2R**, not rework here.

### Task 2.1A: Add a seed-pinned invitation bundle — ✅ LANDED (`19c2d93`)

**Objective:** Turn the landed token into a complete fresh-node trust bootstrap without changing its signer boundary.

**Files:**
- Create: `mycelium_invite/bundle.py`
- Modify: `mycelium_invite/__init__.py`
- Create: `tests/invite/test_bundle.py`

**RED cases:** canonical round trip; seed URL mismatch; token payload mismatch; key-digest mismatch; unknown fields; malformed key record; token absent from rendered logs/errors.

**Minimal interface:**

```python
@dataclass(frozen=True)
class JoinBundle:
    invite_token: str
    seed_url: str
    seed_key_records: tuple[dict[str, object], ...]
    seed_key_digest: str


def encode_join_bundle(bundle: JoinBundle) -> bytes: ...
def parse_join_bundle(raw: bytes) -> JoinBundle: ...
```

**Verify:** `python3.14 -m pytest -q tests/invite/test_token.py tests/invite/test_bundle.py`.

**Commit:** `feat: bind swarm invites to seed trust records`

### Task 2.1B: Make single-use invitation consumption durable — ✅ LANDED (`19c2d93`)

**Objective:** Preserve nonce consumption across seed restart and concurrent join attempts.

**Files:**
- Create: `mycelium_invite/sqlite_registry.py`
- Modify: `mycelium_invite/__init__.py`
- Create: `tests/invite/test_sqlite_registry.py`

**RED cases:** first consume succeeds; replay fails; close/reopen still rejects; concurrent consumers produce one success; expired records prune without re-enabling a still-valid replay; non-owner-readable DB fails closed.

**Minimal interface:**

```python
class SQLiteInviteRegistry:
    def consume(self, *, nonce: str, expires_at: float, now: float) -> None: ...
    def prune(self, *, now: float) -> int: ...
```

Use `BEGIN IMMEDIATE`, a unique nonce key, WAL only if permissions and cleanup are tested, and no token storage.

**Verify:** `python3.14 -m pytest -q tests/invite`.

**Commit:** `feat: persist single-use invite consumption`

### Task 2.2A: Freeze node/seed wire contracts — ✅ LANDED (`aa14402`); gaps → Tasks 2R.1, 2R.3, 2R.4, 2R.7

**Objective:** Define strict signed control records before implementing either side.

**Files:**
- Create: `mycelium_membership/__init__.py`
- Create: `mycelium_membership/contracts.py`
- Create: `tests/membership/test_contracts.py`

**Contracts:**

- `JoinRequestV1`: invite token, node public-key record, capacity observation, node/boot/process identity, nonce, timestamp.
- `JoinAcceptanceV1`: member ID, swarm ID, seed identity, lease expiry, command-poll URL, artifact base URL.
- `HeartbeatV1`: member/lease, measured capacity delta, physical-node state, endpoint/generation, monotonic sequence.
- `AssignmentOfferV1`: deployment/assignment IDs, stage pack manifest, graph/topology version, peer bindings, **`placement_provenance` (§6.9)**.
- `NodeObservationV1`: exact command/result, process/endpoint/load proof, cleanup counters.

**RED cases:** exact-field rejection; signature/wrong-key/tamper rejection; run/deployment mismatch; stale sequence; expired lease; replay; noncanonical JSON; oversized documents; secret-shaped field names; **valid activation/receipt evidence for a member suppresses that member's next scheduled heartbeat** (per §6.7); **idle member with no traffic and no heartbeat past its interval is flagged liveness-stale, distinct from an active-decode transport failure**; **one missed heartbeat/receipt alone never flips a member to dead — only corroborated idle keepalive failure plus evidence-freshness expiry does**; **[product pass] every contract that carries a member set accepts 1, 2, 3, and 8 members without special-casing (§6.8) — a contract that only round-trips two members fails this task**; **[product pass] `AssignmentOfferV1` missing `placement_provenance` is rejected.**

**Verify:** `python3.14 -m pytest -q tests/membership/test_contracts.py`.

**Commit:** `feat: define signed membership control contracts`

### Task 2.2B: Build the native node agent around `PhysicalNodeService` — ✅ LANDED (`299278c`) as `mycelium_node/`; gaps → Tasks 2R.1, 2R.5

**Objective:** Join a seed, advertise measured capability, receive one assignment, launch the existing physical node service, and expose signed observations.

**Files:** *(HISTORICAL — as-built paths differ; see §1.4. Built as `mycelium_node/{identity,membership,process}.py`. Do not create `mycelium_node_agent/`.)*
- Create: `mycelium_node_agent/__init__.py`
- Create: `mycelium_node_agent/agent.py`
- Create: `mycelium_node_agent/__main__.py`
- Create: `tests/node_agent/test_agent.py`
- Create: `tests/node_agent/test_cli.py`

**CLI:**

```bash
python3.14 -m mycelium_node_agent \
  --join-bundle-file /owner-only/join.json \
  --state-root /owner-only/node-state \
  --sidecar-binary native/iroh_transport/target/release/iroh-sidecar
```

**RED cases:** invitation never appears in argv/logs; seed pin verified; local signer private; join retries do not duplicate membership; wrong assignment rejected; digest mismatch rejected before write/load; only assigned tensor pack materialized; `PhysicalNodeService` command mapping is exact; heartbeat sequence monotonic; SIGTERM/timeout reaps service and sidecar; restart keeps member identity but rotates process/endpoint generation; **`IrohTransport` is configured only with an `expected_endpoint_id` sourced from a signed, epoch-scoped membership record from the seed (per §6.6) — a peer presenting a valid-looking but unsigned or stale-epoch endpoint ID is rejected before any activation frame is accepted**; **[product pass] the agent accepts an assignment naming it as entry, intermediate, *or* final stage, and rejects an assignment that names a role its stage pack does not support — the intermediate case must be tested even though the first physical run has no intermediate node (§6.4).**

**Implementation rule:** agent composes or supervises `PhysicalNodeService`; it does not reimplement Router, MLX runtime, or Iroh transport. It also does not accept operator-typed `expected_endpoint_id` values for the physical run — that value must trace back to a seed-signed record (§6.6).

**Verify:**

```bash
python3.14 -m pytest -q tests/node_agent
python3.14 -m pytest -q tests/physical_qualification/test_node_service.py
```

**Commit:** `feat: add assignment-bound node agent`

### Task 2.3A: Build the seed coordinator — ✅ LANDED (`ac18360`, `d76fe6d`); gaps → Tasks 2R.1–2R.5

**Prerequisite:** Task 2.4's extend-vs-supersede decision must be recorded before the first `mycelium_seed/` file is created (§6.11).

**Objective:** Mint invitation bundles, consume joins, maintain leases, compile capacity-backed placement, serve stage packs, and issue signed commands.

**Files:** *(HISTORICAL — as-built paths differ; see §1.4. Built as `mycelium_seed/{coordinator,http,state}.py`, tests in `tests/seed_coordinator/`. `placement.py` and `__main__.py` were NOT built → Tasks 2R.2 and 2R.5.)*
- Create: `mycelium_seed/__init__.py`
- Create: `mycelium_seed/coordinator.py`
- Create: `mycelium_seed/http_api.py`
- Create: `mycelium_seed/placement.py` *(product pass — the placement seam, see below)*
- Create: `mycelium_seed/__main__.py`
- Create: `tests/seed/test_coordinator.py`
- Create: `tests/seed/test_http_api.py`
- Create: `tests/seed/test_placement.py` *(product pass)*

**MVP endpoints:**

```text
POST /v1/join
POST /v1/heartbeat
GET  /v1/commands?member_id=...
POST /v1/command-results
GET  /v1/artifacts/<sha256>
GET  /v1/status
```

**RED cases:** atomic invite use; restart persistence; unsigned/tampered request rejection; member-key binding; lease expiry; stale heartbeat/command result; two-node readiness only after both load proofs and Iroh endpoints verify; artifact path traversal; range requests either implemented exactly or rejected; no cache of secret responses; status never reports route-ready; **`AssignmentOfferV1` carries a seed-signed, epoch-scoped EndpointID membership record per member (§6.6), and a record for a since-rotated or revoked epoch is rejected by node agents**; **[product pass] joins from 1, 2, 3, and 8 members are all accepted and tracked without special-casing; nothing in `coordinator.py` or `http_api.py` may branch on member count (§6.8).**

**Planner rule [rewritten by the product pass]:** placement is reached through one seam only:

```python
# mycelium_seed/placement.py
class PlacementSource(Protocol):
    def compile(self, members: Sequence[MemberRecord]) -> PlacementDecision: ...

class FrozenPlacementSource:      # Task 2.3A — first physical proof
    """Loads a checked-in fixture. placement_provenance='frozen_fixture'."""

class PlannerPlacementSource:     # Task 3B.1 — the product
    """gossip EvidenceBundle -> planner_snapshot_from_evidence_bundle
       -> plan_snapshot -> RoutePlanV2. placement_provenance='planner_v2'."""
```

The seed selects a source at construction. For the first physical proof it uses `FrozenPlacementSource` reading a deterministic two-node DialoGPT split from a fixture file. `PlannerPlacementSource` is Task 3B.1 and must require **zero changes** to `coordinator.py`, `http_api.py`, or the node agent. If implementing 3B.1 requires touching those files, this task was done wrong.

Dynamic optimization is not the first target — but the *seam* for it is, and it is cheap now and expensive later.

**Verify:** `python3.14 -m pytest -q tests/seed tests/invite tests/membership`.

**Commit:** `feat: add signed seed coordinator`

### Task 2.3B: Prove localhost control-plane E2E with real dependencies — ⚠️ PARTIAL as `tests/seed_coordinator/test_local_two_node.py`

**Objective:** Exercise seed + two node agents + two physical-node subprocesses + SQLite + native Iroh on one host while preserving `route_ready=false`.

**As built (⚠️ finish this, do not restart it):** `tests/seed_coordinator/test_local_two_node.py` — 130 lines, `test_seed_and_two_real_node_processes_join_over_tcp`. It does use real node processes, a real TCP control plane, and the real native sidecar (imported from the `e2e_request_iroh` fixture — exactly the reuse §6.1 asked for). What it proves is **join over TCP**. What it does not yet prove is the rest of the required-assertion list below.

**Files:** extend `tests/seed_coordinator/test_local_two_node.py` (do **not** create `tests/integration/test_seed_node_agent_e2e.py` — that path is superseded).

**Use real:** localhost HTTP server, SQLite file, subprocesses, native sidecar, local deterministic deployment. Mock only clocks/failures that cannot be triggered safely.

**Required assertions — remaining:** one-use invitation actually rejected on replay in this harness; two distinct agent/service/sidecar PIDs recorded; stage load proofs; Iroh endpoint exchange; **one local distributed request end to end**; **exact token parity**; restart persistence (seed restart, membership survives); full cleanup with zero leaked processes; `route_ready=false` because hosts are not physical-distinct — assert this comes from the qualification authority's host-distinctness check and not from a hardcoded literal.

Blocked on Task 2R.5 (`__main__` entrypoints) for the "launched as real processes by the controller" form, though the current in-process-spawn form can carry most assertions sooner.

**Verify:**

```bash
python3.14 -m pytest -q tests/seed_coordinator/test_local_two_node.py
python3.14 -m pytest -q tests/node_agent tests/seed_coordinator tests/membership tests/invite
```

**Commit:** `test: prove seed and node agents end to end locally`

---

# Phase 2R — Carry-forward gaps in the as-built Phase 2 **[2026-07-22 as-built pass]**

Phase 2 landed well (§1.4): 133 tests green, N-peer-clean, good factoring. These seven gaps are the difference between what landed and what Phase 3/3B need. **None is rework — all are additions to working code.** Do them before Task 3.2, because five of them change contract shapes, and changing a signed contract after a physical run has been sealed against it invalidates the run.

Ordered by what blocks the most downstream work.

### Task 2R.0: Repair full-suite test collection — ✅ LANDED (`5dfef7b`)

**The gap.** `python3.14 -m pytest -q` on the live lane **does not run**. It aborts during collection:

```text
import file mismatch:
  tests/request_gateway/test_core.py
  tests/seed_coordinator/test_core.py
HINT: remove __pycache__ / .pyc files and/or use a unique basename
Interrupted: 1 error during collection
2 skipped, 1 error in 0.86s
```

Two test files share the basename `test_core.py` in directories that are not packages. Most `tests/*` subdirectories (`gossip`, `e2e_request_iroh`, `bootstrap_preflight`, …) **do** have `__init__.py`; the five newest — `seed_coordinator`, `node_agent`, `membership`, `invite`, `request_gateway` — do not.

This is why it went unnoticed: every focused lane passes (`133 passed`), and the focused lanes are what the new work was verified against. **The full suite has not run since the new modules landed.** G0 and G7 are therefore red, not "baseline known" — and no full-suite regression from ~8,500 new lines has ever been observed.

**Do not "fix" this by adding empty `__init__.py` to those five directories** — I probed that and it converts one collection error into four different ones in `tests/request_gateway/`, because those modules resolve imports relative to rootdir today. The fix needs to be chosen deliberately: either unique basenames (rename `tests/seed_coordinator/test_core.py` → `test_coordinator_core.py`, cheapest and most local), or a consistent packaging/`importmode` policy applied across all of `tests/`.

**Files:** rename within `tests/seed_coordinator/`, or `pyproject.toml`/`pytest.ini` if the packaging route is chosen.

**Verify:** `python3.14 -m pytest -q` completes collection and reports a real number. **Record that number in §2 as the new baseline.**

**Commit:** `test: restore full-suite collection`

> **⚠️ LIVE-AGENT HAZARD, 2026-07-22 ~16:20.** That worktree has **uncommitted work in flight** — `mycelium_seed/{coordinator,state}.py`, `mycelium_node/membership.py` and four test files modified (+759 lines), plus an untracked `tests/integration/test_seed_node_agent_e2e.py` written at 16:18. Another agent is working *right now*.
>
> Two things follow. **First: coordinate before editing those files** — `git stash`/`checkout` in that worktree will destroy someone's work. **Second: that agent is creating `tests/integration/test_seed_node_agent_e2e.py`, which is the path the pre-16:00 version of this plan specified for Task 2.3B — while the as-built work already put that proof in `tests/seed_coordinator/test_local_two_node.py`.** This is the stale-path duplication §1.4 warns about, happening live. Reconcile the two files rather than keeping both; §1.4's map is authoritative on where things go.
>
> The in-flight diff contains **no** `placement_provenance` and **no** peer-endpoint records, so it is not Task 2R.1 or 2R.3 — those remain unclaimed.

### Task 2R.1: Bind signed peer EndpointID records into `AssignmentOfferV1` (unblocks §6.6) — ✅ LANDED (`6d9680a`)

**The gap.** The as-built `SeedCoordinator.assignment_offer` (`mycelium_seed/coordinator.py:708`) signs deployment/assignment/stage-pack/graph digests and `load_generation` — but carries **no peer bindings**. A node receiving this offer learns what to load and nothing about *who it may talk to*. §6.6 requires `IrohTransport`'s `expected_endpoint_id` to trace to a seed-signed, epoch-scoped record; with the current offer shape that is impossible, and Task 2.2B's own RED case cannot be satisfied. The seed already holds every member's `endpoint_id` (`_Member.endpoint_id`, `coordinator.py:86`) — the data is there, it just is not issued.

**Files:** modify `mycelium_seed/coordinator.py`, `mycelium_membership/contracts.py`, `mycelium_node/membership.py`; extend `tests/seed_coordinator/test_core.py`, `tests/node_agent/test_membership.py`.

**RED cases:** offer carries a per-peer record `{node_id, endpoint_id, deployment_epoch, membership_generation, valid_from, valid_until}` for every peer this node may exchange activations with, signed under the same envelope; a record for a rotated generation is rejected by the node; a record past `valid_until` is rejected; a peer absent from the offer is rejected at transport-connect time, not at first frame; the node **refuses an operator-supplied `expected_endpoint_id`** when a signed record exists for that peer; revocation (member removed, generation bumped) invalidates previously issued records.

**Commit:** `feat: issue signed epoch-scoped peer endpoint records in assignment offers`

### Task 2R.2: Add the `PlacementSource` seam (unblocks Task 3B.1 / G4b)

**The gap.** `assignment_offer(...)` takes `assignment_id`/`assignment_digest` as **parameters** — placement is decided entirely outside the seed, by whoever calls it. That is not wrong, but it means there is no named seam, so Task 3B.1 has nowhere to insert planner placement without editing call sites across the controller and tests. §6.8 and Task 2.3A required this explicitly and it was not built.

**Files:** create `mycelium_seed/placement.py`, `tests/seed_coordinator/test_placement.py`; modify `mycelium_seed/coordinator.py` (accept a `PlacementSource` at construction).

Implement exactly the protocol in Task 2.3A: `PlacementSource.compile(members) -> PlacementDecision`, with `FrozenPlacementSource` (loads a checked-in fixture, `placement_provenance="frozen_fixture"`) now and `PlannerPlacementSource` in Task 3B.1. **Acceptance test for this task:** a stub `PlannerPlacementSource` returning a different split must be swappable with zero edits to `coordinator.py`, `http.py`, or `mycelium_node/`.

**Commit:** `feat: add the seed placement source seam`

### Task 2R.3: Thread `placement_provenance` through contracts and evidence (§6.9) — ✅ LANDED (`6d9680a`, enum correction `22ae8a9`)

**The gap.** The field appears nowhere in the tree. Without it, §6.9's mechanical check — "did we ship the demo or the product" — does not exist, and the frozen split can silently become permanent.

**Files:** modify `mycelium_membership/contracts.py` (`AssignmentOfferV1`), `mycelium_seed/coordinator.py`, `mycelium_qualification/qualifier.py`; extend `tests/membership/test_membership_contracts.py`, `tests/qualification/`.

**RED cases:** an offer without `placement_provenance` is rejected; only `frozen_fixture` and `planner_v2` are accepted; the sealed evidence bundle carries it verbatim; **the qualifier rejects an evidence bundle in which it is absent**; it cannot be edited post-seal without breaking the manifest hash.

**Commit:** `feat: bind placement provenance through membership and qualification`

### Task 2R.4: Implement the §6.7 liveness semantics

**The gap.** The as-built heartbeat is a **lease renewal** — monotonic sequence, replay-protected, durably recovered (`seed_heartbeat_renewals`, keyed `(node_id, generation, heartbeat_message_id)`). That is good and keeps its tests. But grep for `idle`, `liveness`, `suppress`, `receipt` in `mycelium_membership/contracts.py` returns **nothing**. None of §6.7's three locked policies exist: activation/receipt traffic does not suppress heartbeats; there is no idle-vs-in-flight distinction; nothing prevents one missed beat from reading as death. Task 3.6's second scenario and handover Gate H are unsatisfiable as built.

**Files:** modify `mycelium_membership/contracts.py`, `mycelium_seed/coordinator.py`, `mycelium_node/membership.py`; extend `tests/membership/test_membership_contracts.py`, `tests/seed_coordinator/test_core.py`.

**RED cases:** exactly the three from Task 2.2A, still unimplemented — valid activation/receipt evidence suppresses that member's next scheduled heartbeat; an idle member past its keepalive interval is flagged `liveness_stale`, a **distinct** state from an active-decode transport failure; one missed beat alone never flips a member to dead — only corroborated idle keepalive failure **plus** evidence-freshness expiry does.

**Commit:** `feat: scope idle peer liveness separately from lease renewal`

### Task 2R.5: Add runnable `__main__` entrypoints for seed and node

**The gap.** Neither `mycelium_seed/__main__.py` nor `mycelium_node/__main__.py` exists. Both are libraries. The CLI in Task 2.2B (`python3.14 -m mycelium_node_agent --join-bundle-file ...`) does not run. Tasks 3.3–3.4 launch these as real processes on real hosts over SSH — impossible today, and this is the single hardest blocker to *starting* Phase 3.

**Files:** create `mycelium_seed/__main__.py`, `mycelium_node/__main__.py`, `tests/seed_coordinator/test_cli.py`, `tests/node_agent/test_cli.py`.

**RED cases:** join bundle read from an owner-only **file or stdin, never argv** (§6.2); invite token absent from `argv`, logs, and error text; non-owner-readable bundle or state root fails closed; SIGTERM reaps the physical-node subprocess and the sidecar within a bounded deadline; `--dry-run` performs no network I/O and emits `route_ready=false`; exit codes distinguish preflight failure, join rejection, and runtime failure.

**Commit:** `feat: add seed and node process entrypoints`

### Task 2R.6: Freeze the numeric tolerance and prove N-way stage packs

**The gap.** `stage_pack.py` (1,030 lines) and `test_stage_pack.py` (591 lines) are substantial and cover ownership, tamper, traversal, symlink, corruption, tied-alias, and concurrent-mutation cases well. Two plan requirements are missing: no `tolerances/` file exists (§3 item 10 requires the tolerance frozen *before* the physical run; Task 3.1 assigned it here), and I found no test proving a **3-way or 5-way** split's packs union to exactly the source tensor set — the check that keeps the packer from being quietly 2-node-shaped before Task 3B.2 adds a third member.

**Files:** create `tolerances/dialogpt-small-fp16.json`; extend `test_stage_pack.py`.

**RED cases:** 3-way and 5-way splits each produce packs whose union is exactly the source tensor set, with no overlap and no omission; the intermediate role (neither entry nor final) round-trips; the tolerance file binds source commit + model digest and any post-run modification is detectable.

**Commit:** `test: prove n-way stage pack coverage and freeze fp16 tolerance`

### Task 2R.7: Add peer class to membership (enables §6.11 extend, §6.13 heterogeneity) — ✅ LANDED (`6d9680a`)

**The gap.** Membership has no notion of *what kind of device* a member is. With the extend decision (§6.11) and the heterogeneous target (§6.13), the seed must be able to hold a Mac (MLX + Iroh), a browser (WASM/JS + HTTP), a Pixel (pure-Python + HTTP), and a Linux laptop (runtime TBD) in one membership plane while routing only production-transport peers into the activation path.

**Files:** modify `mycelium_membership/contracts.py`, `mycelium_seed/coordinator.py`; create `docs/contracts/swarm-control-plane.md`; extend `tests/membership/test_membership_contracts.py`.

**RED cases:** a member declares `peer_class` (`mac_mlx_iroh`, `browser_http`, `pixel_http`, `linux_tbd`) and a `runtime_capability` record at join; the seed accepts all classes into membership; **only peer classes whose transport is production Router/Iroh are eligible for activation-carrying placements**, and a placement naming an ineligible class is rejected at offer time, not at run time; capacity from an ineligible class still enters the evidence plane (it can be measured and displayed) without becoming route-eligible.

**Commit:** `feat: classify peers by runtime and transport capability`

---

### Task 2.4: Refactor `SwarmCoordinator` onto the seed membership plane **[decision: EXTEND, §6.11]**

**Prerequisite:** Task 2R.7 (peer class must exist in membership before the browser class can be expressed on it).

**Objective:** One membership plane. Refactor `mycelium_interactive.swarm:SwarmCoordinator` to obtain identity, invitations, leases, generations, and durability from `mycelium_seed`, keeping its browser-specific surface (WASM stage delivery, JSON hidden-state exchange, origin pinning) as the peer-class adapter on top.

**Files:**
- Modify: `mycelium_interactive/swarm.py`, `mycelium_interactive/server.py`
- Modify: `mycelium_seed/coordinator.py` only if a genuine adapter seam is missing
- Create: `docs/contracts/swarm-control-plane.md` (tracked, public)
- Modify: `QUESTIONS.md` (record the extend decision, dated)
- Extend: `tests/interactive/test_swarm.py`

**Steps:**

1. Read `mycelium_interactive/swarm.py` in full (~930 lines) and `tests/interactive/test_swarm.py`.
2. Map each overlapping method (§6.11 table) onto its `mycelium_seed` equivalent: `create_invite`/`exchange_invite` → seed invite mint/consume; `poll_work`/`submit_result` → seed command/result plane; `revoke_peer`/`leave` → seed generation bump; `status` → seed member projection.
3. Replace bearer session tokens with Ed25519-signed member records. Browser peers hold a keypair in the page; the token becomes a transport credential, not the membership credential.
4. Move peer state to `mycelium_seed/state.py` so browser membership survives restart — today it is memory-only.
5. Keep browser-specific logic (stage-pack matrices, `matrix_digest`, origin normalization) in `swarm.py`. It is the adapter, not the plane.
6. Write `docs/contracts/swarm-control-plane.md`: the one plane, its peer classes, what each class must present at join, and which classes are activation-eligible (§6.13 / Task 2R.7).

**RED cases:** a browser peer's membership survives a coordinator restart; a browser peer and a Mac peer cannot collide on `node_id`; a revoked browser peer's in-flight work is rejected on generation, not just on token; a browser peer is accepted into membership but **rejected for an activation-carrying placement** (§6.13); the existing browser-swarm tests still pass unchanged in behavior.

**Verify:** `python3.14 -m pytest -q tests/interactive tests/seed_coordinator tests/membership`.

**Commit:** `refactor: move browser swarm onto the seed membership plane`

---

# Phase 3 — Physical multi-machine inference

### Task 3.1: Compile assignment-local Safetensors stage packs — ✅ MOSTLY LANDED (`0f468b4`, `ad370ea`) as top-level `stage_pack.py`; gaps → Task 2R.6

**Objective:** Transfer only tensors owned by each assignment instead of copying a monolithic checkpoint to every node.

**Files:**
- Create: `mycelium_qualification/stage_pack.py`
- Modify: `mycelium_qualification/physical_deployment.py`
- Modify: `runtime_loader.py`
- Create: `tests/qualification/test_stage_pack.py`
- Create: `tolerances/dialogpt-small-fp16.json` *(product pass — see below)*

**RED cases:** exact tensor ownership; no unassigned transformer block; entry/final special tensors correct; tied `lm_head` alias preserved; parent manifest and assignment digests bound; output files owner-only and no symlink/hardlink reuse; tamper/path escape/duplicate tensor rejected; source checkpoint unchanged; pack-based stages match current assignment execution; **[product pass] a 3-way and a 5-way split each produce packs whose union is exactly the source tensor set with no overlap — proving the packer is not 2-node-shaped even though the first physical run is.**

**Positive parity:** run the same prompt through pack-based 2/3/5-stage local workers and require exact greedy-token parity with the independent reference.

**Tolerance freeze [product pass]:** §3 item 10 requires the numeric tolerance to be frozen *before* the physical run, but no task previously produced it. This task does: derive hidden-state and logit tolerances from local pack-based vs monolithic FP16 runs, write them to `tolerances/dialogpt-small-fp16.json` with the source commit and model digest bound in, and commit that file. Task 3.4 reads it and may not modify it. A tolerance file modified after a physical run invalidates the run.

**Verify:**

```bash
python3.14 -m pytest -q tests/qualification/test_stage_pack.py
python3.14 -m pytest -q tests/model/test_distributed_dialogpt_decode.py
```

**Commit:** `feat: compile assignment-local model stage packs`

### Task 3.2: Build the physical qualification controller

**Objective:** Orchestrate two physical Macs without letting SSH become the inference transport or evidence authority.

**Files:**
- Create: `physical_inference_qualification.py`
- Create: `tests/physical_qualification/test_controller.py`

**Commands:**

```text
preflight   — inspect toolchain, source/model/sidecar digests, peer reachability
prepare     — create run root, seed state, bundles, assignments, stage packs
run         — launch seed/agents, wait for signed readiness, issue prompt
cancel      — execute cancellation probe
recover     — execute remote death/restart/recovery probe
seal        — stop writers, seal evidence, invoke qualifier once
cleanup     — reap bounded processes and remove ephemeral remote roots
```

**RED cases:** explicit transfer allowlist; digest acknowledgment; no environment/credential/model-cache traversal; short socket roots; bounded SSH/process commands; stdout/stderr separation; cleanup on every injected failure; route stays false in dry-run/fake/local modes; physical mode requires distinct host and boot identities; **a node presenting a valid endpoint ID but no seed-signed membership record for the current epoch is rejected by the controller before `run` proceeds (per §6.6) — the controller must not silently fall back to operator-supplied `expected_endpoint_id` values**; **[product pass] `--peers` accepts an arbitrary host list; nothing in the controller branches on exactly two hosts.**

**Control boundary:** SSH starts processes and can transfer tracked source/bootstrap files. Stage packs must flow through the signed seed artifact service. Activations must flow through Iroh.

**Verify:**

```bash
python3.14 -m pytest -q tests/physical_qualification/test_controller.py
python3.14 physical_inference_qualification.py preflight --dry-run
```

Dry-run must perform no SSH and emit `route_ready=false`.

**Commit:** `feat: add physical inference qualification controller`

### Task 3.3: Physical preflight on M4 Pro and Evis MacBook Pro

**Objective:** Prove both real hosts are available and byte-compatible before launching inference.

**Files [product pass]:**
- Use, do not recreate: `mycelium_physical_preflight/` (`generator.py`, `schema.py`, `validator.py`, `python3.14 -m mycelium_physical_preflight`)
- Modify only if a required record below is genuinely missing from its schema: `mycelium_physical_preflight/schema.py` + matching tests in `tests/physical_preflight/`
- No other source changes unless preflight exposes a tested defect.

Required records:

- hostname, stable host ID, boot ID, architecture, OS version;
- git HEAD and clean/dirty state;
- Python 3.14, MLX import/version, local checkpoint digest;
- native sidecar digest and `--version`/health result;
- Tailscale address and peer reachability;
- free disk/RAM and no conflicting demo process/port;
- exact source/transfer manifest digest.

Expected route members:

```text
node-a: M4 Pro (`m4pro`, user `evinova-self`)
node-b: Evis MacBook Pro (`Evis-MacBook-Pro`, user `evinova`)
```

**Known blocker [product pass]:** the pinned DialoGPT snapshot (§2) is absent on the integration Mac. Preflight will and should fail on the checkpoint-digest record until it is fetched. Fetch it as an explicit, logged operator step outside any qualification run, then re-run preflight — do not add a download path to the controller.

If M4 Pro remains offline, record `G4 BLOCKED: physical peer unavailable` and stop. Do not run a second node on Evis Mac as replacement evidence.

**Commit:** `chore: record physical preflight for two-mac route` (records only; no source change if the schema already covered everything)

### Task 3.4: Run the first real two-Mac pretrained decode

**Objective:** Close the headline target before adding recovery or quantization work.

**Files:** evidence only — no source change. If a source change proves necessary mid-run, stop, discard the run, make the change under RED→GREEN, and start a fresh run with a new `run_id`. Patching source during a qualification run invalidates it.

Execution sequence:

1. Seed starts on one Mac with fresh run signer and SQLite state, using `FrozenPlacementSource` (§6.9 — evidence must record `placement_provenance: "frozen_fixture"`).
2. Create one owner-only join bundle for each node.
3. Start node agent on each physical host.
4. Wait for two signed capacity profiles, leases, stage-pack digests, stage load proofs, and Iroh endpoint bindings.
5. Publish the production execution graph and device-state snapshot. *(For this task that graph comes from `mycelium_qualification.physical_deployment:build_execution_graph`, the 2-node scaffold. Task 3B.1 replaces it with `mycelium_router.layer_builder:build_execution_graph`.)*
6. Tokenize the fixed public prompt with `GPT2BPETokenizer`.
7. Run prefill and at least eight greedy decode steps through production Router/Iroh.
8. Run the independent monolithic reference in a separate process not participating in the route.
9. Compare exact token IDs and the tolerances frozen in `tolerances/dialogpt-small-fp16.json` (Task 3.1). Do not read a tolerance from anywhere else.
10. Decode and record output text.
11. Complete and prove KV/capacity/transport cleanup.

**Required physical observations:** distinct host/boot/PID/endpoint IDs; non-loopback peer address; native Iroh frame/receipt sequence; path lock; two committed reservations; stage-local tensor inventories; no alternate/fallback path; receiver monotonic timing; exact tokens; cleanup counters zero; **relay-vs-direct connection class recorded for the Iroh path (per Gate G/handover §4.5's transport-recommendation table — direct UDP over Tailscale/LAN is expected, but the class must be observed, not assumed)**; **per-stage/per-hop trace correlates request ID, path ID, and hop index without exposing prompt content or tensor bytes in the trace record**; **[product pass] measured per-direction link cost between the two hosts, recorded as a `link_state.v1` record — this is the first real input to Task 3B.3's cost matrix and costs nothing to capture here.**

**Pass artifact:** `.myc-phys/<run-id>/happy-path/` under an ignored owner-only root. Keep `route_ready=false` until lifecycle and qualifier gates finish.

### Task 3.5: Prove distributed cancellation

**Objective:** Cancel while the remote decode stage is in flight and prove cleanup on both physical hosts.

**Files:** evidence only unless a defect is found; defects are fixed under RED→GREEN in `mycelium_router/` with a test in `tests/path_cancellation_adversarial/`.

Use a bounded harness interlock only to make timing reproducible. The interlock may pause real runtime execution; it may not synthesize transport success or latency.

Required evidence: `PathCancellation` crosses Iroh; both relays remove path state; both runtimes release KV; reservations release; pending deliveries become zero; no post-cancel token; both agents remain healthy for a subsequent request.

**Commit:** `test: prove physical path cancellation` (only if a source fix was needed)

### Task 3.6: Prove remote death, restart, stale rejection, and Router recovery

**Objective:** Continue one request after real remote process loss with no local full-model fallback.

**Files:** evidence only unless production recovery ordering proves insufficient; then `mycelium_router/` + `mycelium_router/transports/` under RED→GREEN with tests in `tests/qualification/`.

Sequence:

1. Decode at least one token.
2. Terminate the remote physical-node process, not merely an HTTP handler.
3. Observe old PID exit and production transport failure.
4. Agent starts a different process/sidecar with new endpoint and generation using the same assignment-local pack digest.
5. Publish higher topology version and replacement placement ID.
6. Inject one stale-generation frame and require rejection.
7. Submit production failure report and require Router `RECOVERY_PREFILL`.
8. Continue generation; require contiguous unique token indexes and exact final reference parity.
9. Prove old placement exclusion and final cleanup.

If production recovery ordering cannot satisfy this, add a minimal RED test to Router/transport contracts and repair production code. Never bypass Router recovery in the controller.

**Additional required proof (per §6.7 / handover Gate H):** run a second, separate scenario where the remote node stays alive but the request has no in-flight decode (idle interval between demo runs) and its heartbeat is withheld. Confirm this produces a liveness-stale signal only after the idle keepalive interval elapses — not immediately on one missed beat — and that it is recorded as a distinct evidence class from the active-decode transport-failure recovery proven above. Conflating the two is a claim-boundary violation: an active in-flight death is detected through Router/transport failure percolating up; an idle peer going dark is detected through keepalive/evidence-freshness expiry. Both must be independently demonstrated.

**Commit:** `feat: scope idle peer liveness separately from in-flight transport failure` (if source work was needed)

### Task 3.7: Bind real negative runs

**Objective:** Make qualification fail closed for real forbidden mutations under the same source/model contract.

**Files:**
- Create: `tests/qualification_adversarial/test_physical_negative_runs.py` (or extend the existing directory)
- Evidence under `.myc-phys/<run-id>/negative/<case>/`

At minimum execute and record: stale proof, wrong revision, wrong endpoint, missing tensor, expired reservation, sequence replay, dropped peer, full-model fallback, simulator participation, synthetic timing.

Each negative run must show the real validator/rejection seam, stable code, digest, and `route_ready=false`. Hand-authored reason strings are insufficient.

**Commit:** `test: bind real physical negative runs to rejection seams`

### Task 3.8: Seal and invoke the sole qualification authority

**Objective:** Attempt promotion once from immutable bytes after all writers stop.

**Files:** evidence only; `mycelium_qualification/sealer.py` and `qualifier.py` are used unmodified. If the qualifier must change to accept `placement_provenance` (§6.9), that is a separate RED→GREEN change in `mycelium_qualification/` landed *before* this task runs, with a test that an evidence bundle lacking the field is rejected.

Sequence:

1. Stop node/controller evidence writers.
2. Verify node/seed signatures and exact run/deployment bindings.
3. Write canonical documents create-new and fsync.
4. Build manifest; reopen and rehash every pin.
5. Invoke `qualify_sealed_evidence(...)` with real verifiers and current time.
6. Serialize the returned qualification record separately.

Pass condition: `evidence_class=physical_qualification`, `route_ready=true`, no reason codes, authority `mycelium_qualification.qualifier:RouteQualificationV1`, and `placement_provenance=frozen_fixture` recorded (not hidden).

Fail condition: preserve rejected evidence, report stable reasons, keep route false, and do not weaken gates.

**Commit:** `feat: bind placement provenance into sealed qualification evidence`

### Task 3.9: Interim verification, review, commit, and push of the physical-proof tranche

Run on the integration machine:

```bash
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
python3.14 scripts/claim_boundary_audit.py --repo-root . --json
python3.14 -m compileall -q .
git diff --check
(cd native/iroh_transport && cargo fmt --check)
(cd native/iroh_transport && cargo clippy --all-targets --all-features -- -D warnings)
(cd native/iroh_transport && cargo test)
(cd ui/web && npm run check)
```

Then request two independent read-only reviews:

1. Router/runtime/recovery semantics and no-fallback proof.
2. Membership/signature/evidence/qualifier claim boundary.

Repair confirmed issues with RED tests. Stage explicit source/test files only. Verify the private Desktop plan and runtime evidence are ignored/untracked. Commit meaningful tranches and push immediately after all required gates pass, following repository ownership policy.

Claim boundary at this point:

- `route_ready=true` applies only to the pinned model, source commit, assignments, physical hosts, evidence validity window, and qualifier version.
- **Placement was frozen by hand.** Report as `rung 8–9, frozen placement` (§5.1). Do not say "capacity-aware", "swarm", "chooses the best path", or "assigns layers by capability" — none of those are yet true of the physical route.
- `release_ready=false`.
- No WAN, relay-scale, arbitrary-model, Pixel-production-runtime, or throughput claim.

**Then continue to Phase 3B.** Phase 3 is the proof. Phase 3B is the product.

---

# Phase 3B — From frozen split to a real swarm **[2026-07-22 product pass]**

Prerequisite: G6 green (Task 3.8 accepted) and Task 3.9 pushed. Phase 3B takes priority over Phase 4.

### Task 3B.1: Drive physical placement from gossip evidence through the planner (G4b)

**Objective:** Re-run the accepted physical route with placement computed from measured capacity instead of a fixture, changing no coordinator, agent, or transport code.

**Files:**
- Create: `mycelium_seed/planner_placement.py` (`PlannerPlacementSource` per Task 2.3A's seam)
- Create: `tests/seed/test_planner_placement.py`
- Modify: `mycelium_qualification/physical_deployment.py` — add a path that delegates graph construction to `mycelium_router.layer_builder:build_execution_graph`. Do **not** modify or delete the existing 2-assignment function (`physical_deployment.py:146`); existing qualification tests bind it.
- Create: `tests/qualification/test_physical_layer_builder_graph.py`

**Pipeline to wire (all components already exist — §6.1):**

```text
node agent capacity profile  (probe.py:profile + compile_capacity_profile)
  -> mycelium.device_profile.v2 / device_status.v1 gossip records
  -> GossipService EvidenceBundle
  -> gossip_adapter.planner_snapshot_from_evidence_bundle:164
  -> planner.plan_snapshot:76 -> RoutePlanV2
  -> planner_assignment.compile_bound_layer_assignments:183
  -> layer_builder.build_execution_graph:347 -> ExecutionGraphV1
  -> unchanged Router / Iroh / sealer / qualifier
```

**RED cases:** planner snapshot rejects a stale/mixed-generation evidence bundle; a node whose `device_status.v1` is past its TTL is excluded from placement rather than silently defaulted; `RoutePlanV2` placement intent is not treated as readiness (handover convention 5); assignments derived from the plan cover every layer exactly once with half-open ranges; `layer_builder` rejects a missing load proof, a stale epoch, and a runtime-endpoint set that does not match the assignment set; `placement_provenance` is `planner_v2`.

**The A/B proof (this is the actual deliverable, not the plumbing):** run the physical route twice against the same two Macs, with one node's advertised capacity materially different between runs (constrain memory or report a lower measured throughput — a real constraint, not a doctored profile). Require:

- the two runs produce **different** layer assignments;
- both produce exact token parity with the monolithic reference;
- the difference traces to the measured capacity input, shown by diffing the two planner snapshots.

If both runs produce identical assignments, the planner is not actually consuming the capacity signal and this task is not done — regardless of whether the tests pass.

**Verify:**

```bash
python3.14 -m pytest -q tests/seed/test_planner_placement.py tests/qualification/test_physical_layer_builder_graph.py
python3.14 -m pytest -q test_layer_planner_v1_*.py
python3.14 physical_inference_qualification.py run --placement planner --peers m4pro,localhost
```

**Gate:** G4b. Seal and qualify this run separately (Task 3.8 sequence, new `run_id`, new qualification ID). The FP16 frozen-placement acceptance does not authorize the planner-placement claim.

**Commit:** `feat: drive physical placement from measured capacity evidence`

### Task 3B.2: Third device joins the live swarm (G8)

**Objective:** Prove membership is a swarm property, not a two-host constant — a third device joins a running deployment and receives a capacity-derived assignment with no code change and no operator-typed layer range.

**Files:**
- Modify: `mycelium_seed/coordinator.py` only if a genuine N-peer defect surfaces (per Task 2.3A this should be zero changes; any change needed here is a Task 2.3A bug, log it as such)
- Create: `tests/integration/test_three_member_swarm.py`
- Create: `tests/seed/test_member_departure_replan.py`

**Device options, in preference order** (roster confirmed by operator 2026-07-22, §6.13):

1. **Astra's MacBook** — best option. MLX + native Iroh, so it is a full production-transport route member and needs no new runtime. Use this for the G8 join proof and as the third node for Task 3B.3's cycle search. Coordination cost: it is not the operator's machine, so preflight and process launch need Astra present or an agreed access path.
2. **A Linux laptop** — joins membership today, executes nothing until Task 3B.5 lands (§6.13). Valuable *now* as a membership/placement peer even while ineligible for activation-carrying placements (Task 2R.7 makes that distinction enforceable), and it is the device that makes G9 real.
3. **The Pixel** via `physical_pixel_host_stage.py` — non-production-Router path, so it proves *membership and placement* but not *production transport*; evidence must say so and G8 stays partial if the Pixel is the only third member.
4. **A second process on the M4 Pro** with distinct boot-scoped identity — weakest; may not be reported as a third physical peer.

For Task 3B.3 specifically, prefer option 1: the cycle-search experiment needs three peers that all actually carry activations, and options 2–4 cannot until G9.

**Required proof:**

- the third member joins through the same invitation bundle path, mid-deployment;
- the planner reassigns layers across three stages, producing an intermediate-role assignment (§6.4) that has never run physically before;
- the new assignment for each existing member is a consequence of the new capacity set, not a hardcoded 3-way split;
- exact token parity survives the three-stage route;
- **departure:** removing one non-entry member produces a scoped replan candidate via the fault-tolerant replanner (`FAULT_TOLERANT_LAYER_REPLANNER.md`, `mycelium_layer_planner/replanning.py`), not a whole-swarm failure, and the replan is *candidate intent* — activation still requires provisioning, load proof, and epoch fencing (handover convention 12).

**Verify:**

```bash
python3.14 -m pytest -q tests/integration/test_three_member_swarm.py tests/seed/test_member_departure_replan.py
python3.14 -m pytest -q simulate_layer_replanner.py test_layer_planner_v1_*.py
```

**Gate:** G8 (join half).

**Commit:** `feat: admit a third member to a live swarm deployment`

### Task 3B.3: Exercise the directed-cycle search on real measured link costs

**Objective:** Answer Astra's question — does the cyclic graph algorithm work, and does it *matter*? See §6.10 for the full state of play; read `LAYER_PLANNER_PRODUCT_V1.md` §4.3, §5.1–5.2, and §7 before starting.

**Prerequisite:** Task 3B.2. With two nodes there is exactly one directed cycle, so nothing about cycle *search* can be exercised until a third member exists.

**Files:**
- Create: `tests/integration/test_physical_cycle_search.py`
- Create: `docs/qualification/directed-cycle-search-physical.md` (tracked, public — the result, whichever way it goes)
- Modify only if a defect is found: `mycelium_layer_planner/cycle_search.py`, `network_cost.py`, `primary_plan.py`

**Required experiments:**

1. **Real cost matrix.** Build the directed per-pair cost matrix from measured `link_state.v1` records captured across the three physical members (Task 3.4 already captures the first pair) through `network_cost.transfer_time_ms` and `phase_edge_costs`. Record the matrix in evidence. Confirm it is genuinely asymmetric — if measured `A→B` and `B→A` are identical to measurement precision, say so, because that weakens the ATSP framing and is itself a finding.
2. **Does the order matter?** Compare `search_cycle`'s chosen order against naive node-id ordering on that real matrix. Record the cost delta. **A null result is a valid and publishable outcome** — if the delta is under a few percent on three local Macs, that is evidence the cycle search earns its keep only at larger fleet sizes or over heterogeneous links, and it should be written down rather than buried.
3. **Asymmetry of the closing edge.** The decode loopback carries a ~9-byte token envelope; forward edges carry ~1.5 KB hidden states (DialoGPT-small FP16). Confirm `cycle_cost:43` prices the closing edge with the token-envelope size and not the activation size, and show whether a symmetric-cost model would have picked a different order. This is the sharpest test of whether the cyclic framing buys anything.
4. **Opened cycle through the real Router.** Take `open_cycle:218`'s output — the rotated order plus its explicit `(last, first)` loopback — through `layer_builder` into a physical three-member decode, and confirm the forward stages validate as a DAG while the loopback edge carries a real sampled token between real hosts. This is the first time the cyclic topology has existed outside a unit test.
5. **Strategy tiering honesty.** With 3 members the search takes the `exact_directed_cycle` branch (`n ≤ exact_cycle_max_nodes=7`), so `globally_exact` must be `true`. Assert the reported `mode` and `globally_exact` match the branch actually taken, and record `explored_candidates`. If a fleet large enough to cross into `held_karp_cycle` (`n ≤ 12`) or the heuristic tiers is ever available, repeat — but do not fabricate one.

**Explicitly out of scope:** replica groups, multi-loop replication, cross-cycle edges, and the column-generation upgrade (`LAYER_PLANNER_PRODUCT_V1.md` §6 and §7 close). One primary cycle only.

**Verify:**

```bash
python3.14 -m pytest -q test_layer_planner_v1_cycle_exact.py test_layer_planner_v1_cycle_scaled.py
python3.14 -m pytest -q tests/integration/test_physical_cycle_search.py
```

**Commit:** `test: qualify directed-cycle search against measured physical links`

### Task 3B.4: Serve the swarm through the ordinary user path, then final verification

**Objective:** Someone who has never read this plan types a prompt and gets tokens back from the physical swarm (§6.12).

**Files:**
- Modify: `mycelium_request_gateway/backend.py` — back the gateway with the physical route rather than a local fixture
- Create: `tests/request_gateway/test_physical_backend.py`
- Modify: `mycelium_demo/cli.py` if a `serve --swarm` affordance is missing
- Modify: `README.md` — move the now-true items out of "Not working yet"

**Required proof:**

- `python3.14 -m mycelium_request_gateway.cli stream_prompt ...` against a running gateway returns streamed tokens generated across the physical swarm;
- cancellation from the client releases route/KV/capacity on every member (reuses Task 3.5's seams);
- the gateway refuses to serve when the qualifier has not accepted the current deployment — readiness is not a gateway-local opinion;
- no prompt content appears in any trace, evidence record, or log.

Then run the full gate set from Task 3.9 again, plus:

```bash
python3.14 -m pytest -q tests/request_gateway tests/e2e_request_iroh
python3.14 scripts/claim_boundary_audit.py --repo-root . --json
```

Request the same two independent reviews, plus a third: **placement provenance and swarm claim boundary** — does every "capacity-aware" or "swarm" word in README, UI, and docs trace to a sealed `placement_provenance=planner_v2` run?

**Final claim boundary after Phase 3B:**

- `route_ready=true` applies only to the pinned model, source commit, physical hosts, evidence validity window, and qualifier version — now with `placement_provenance=planner_v2`.
- Report as `rung 9, planner placement, 3 members, LAN`.
- `release_ready=false`.
- No WAN, NAT-traversal, relay-scale, arbitrary-model, untrusted-peer, replica-group, or throughput claim.

**Commit:** `feat: serve the physical swarm through the request gateway`

### Task 3B.5: Select and integrate a second runtime so a non-Apple device can execute **[promoted from Task 4.5 by §6.13]**

**Objective:** Make at least one non-MLX device class execute a real assigned layer through production Router transport, so "heterogeneous" describes execution and not only membership.

**Why it is here and not in Phase 4.** §6.13: every runtime path in the tree is MLX. The roster includes two Linux laptops and a Pixel that today cannot hold a layer at all. Quantization extends reach on devices that already run; this task creates a device class that runs at all. It may proceed in parallel with 3B.1–3B.4 — it shares no files with them.

**Files:**
- Create: `mycelium_runtime_<backend>/` (name after the chosen backend, decided in step 2)
- Modify: `runtime_contracts.py`, `runtime_loader.py` — extract the backend-neutral contract MLX currently satisfies implicitly
- Create: `tests/runtime_<backend>/`
- Create: `docs/qualification/second-runtime-selection.md` (tracked, public — the evaluation and the decision)

**Steps:**

1. **Extract the runtime contract.** `RuntimePort` exists in the Router, but `runtime_loader.py` and `mycelium_router/mlx_runtime.py` are MLX-specific. Make the seam explicit and prove it with the existing MLX backend before adding a second — a second backend behind an implicit contract will silently diverge.
2. **Select the backend on measured evidence, not prose** (§4 forbids prose-chosen backends). Evaluate against the pinned DialoGPT-small requirements: GPT-2 block operators, FP16, KV cache control, per-layer loading of a stage pack rather than a whole model, and a Python-callable surface. Candidates worth measuring for the Linux laptops: PyTorch CPU, ONNX Runtime, GGML/llama.cpp bindings, plain NumPy. Record what each fails on. NumPy is the honest baseline — slow but dependency-light, and the Pixel stage already proves a pure-Python stage can produce correct hidden states.
3. **Prove monolithic parity first** on the new backend: exact greedy token parity against the MLX reference on the fixed public prompt, before any distributed work.
4. **Prove stage parity:** a single assigned stage on the new backend produces hidden states matching MLX within the frozen tolerance (Task 2R.6).
5. **Join the swarm:** the Linux laptop joins as `peer_class=linux_<backend>` (Task 2R.7), becomes activation-eligible, and carries a real stage in a mixed Mac+Linux physical route with exact token parity.
6. **Record the cost.** Measure its per-layer latency and feed it back as real `device_profile.v2` capacity, so the planner (Task 3B.1) actually places fewer layers on the slow device. **A heterogeneous swarm where the planner ignores the speed difference is not the product** — this is the experiment that closes the loop between §6.13 and G4b.

**Explicitly out of scope:** the Pixel's production runtime. Mobile is a third device class with its own transport problem; solve Linux first, since Linux has a working Iroh sidecar and the Pixel does not.

**Verify:**

```bash
python3.14 -m pytest -q tests/runtime_<backend>
python3.14 -m pytest -q test_runtime_loader.py test_router_mlx_runtime.py
```

**Gate:** G9 — heterogeneous execution. Until green, the §6.13 claim ceiling holds.

**Commit:** `feat: add a second layer runtime for non-apple peers`

---

# Phase 4 — Quantization for constrained hardware (strictly after G6)

Do not begin this phase merely because local sharding works. Begin only after a sealed FP16 physical run is accepted. **Phase 3B takes priority over this phase** — quantization extends hardware reach, but the swarm is the product, and a quantized frozen-split pipe is still a pipe.

### Task 4.1: Freeze quantization requirements from measured hardware

**Objective:** Select one first quantized runtime from real constraints instead of adding a generic abstraction.

Collect on Evis MacBook and M4 Pro:

- assignment-pack bytes;
- peak RSS during prefill/decode;
- tokens/second and inter-stage activation bytes;
- supported MLX quantized operations;
- model quality/parity on a frozen public prompt corpus.

First recommendation for the two-Mac path: MLX weight-only 4-bit, group size 64, with FP16 activations and KV. If actual MLX/operator evidence rejects this, record the failure and choose 8-bit; do not add an unverified portable/mobile claim.

Freeze acceptance before distributed testing:

- exact greedy token parity on the fixed demo prompt;
- exact token parity on the frozen short corpus, or fail the selected bit width;
- numeric error threshold derived and recorded from quantized-monolithic vs FP16 reference;
- at least 40% lower assignment-pack bytes and measured peak model RSS on constrained host;
- no increase in cross-stage activation precision/bytes unless separately justified.

### Task 4.2: Add an explicit quantization contract

**Files:**
- Create: `mycelium_quantization/__init__.py`
- Create: `mycelium_quantization/contracts.py`
- Create: `tests/quantization/test_contracts.py`

Bind algorithm, bits, group size, axis, scale/zero representation, source tensor digest, quantized tensor digest, dequant dtype, runtime backend/version, assignment ID, and parent manifest. Reject unknown schemes and mixed unbound tensors.

**Verify:** `python3.14 -m pytest -q tests/quantization/test_contracts.py`.

### Task 4.3: Compile quantized assignment-local stage packs

**Files:**
- Create: `mycelium_quantization/mlx_pack.py`
- Modify after Task 3.1 creates it: `mycelium_qualification/stage_pack.py`
- Modify: `runtime_loader.py`
- Create: `tests/quantization/test_mlx_pack.py`

TDD ladder:

1. one linear tensor quantize/dequantize;
2. one GPT-2 block;
3. monolithic quantized model;
4. 2/3/5 local quantized stage packs;
5. frozen prompt-corpus token parity;
6. bytes/RSS evidence.

Quantized distributed output compares first against an independently loaded quantized monolithic reference, then reports its bounded difference from FP16. Do not conflate quantization error with network/sharding error.

### Task 4.4: Re-run the physical proof with quantized stage packs

Use the same seed, node-agent, stage-pack, Router/Iroh, lifecycle, sealer, and qualifier path. Bind quantization metadata into deployment/assignment/load-proof/evidence digests. Require a new qualification ID; the earlier FP16 acceptance does not authorize quantized route readiness. Quantized pack bytes also change the planner's cost inputs — re-run Task 3B.1's A/B if quantization is expected to shift placement.

### Task 4.5 — **MOVED.** See Task 3B.5.

The non-MLX runtime gate was promoted out of Phase 4 by the §6.13 heterogeneity decision. It is now on the critical path, not behind quantization.

---

## 8. Immediate executable tranche **[rewritten by the as-built pass]**

Tasks 0.1–0.3, 2.1A, 2.1B, 2.2A, 2.2B, 2.3A and most of 3.1 are **done** (§1.4). The next tranche is Phase 2R, in this order:

1. **Confirm the branch, and check for a live agent.** Work in `/Users/evinova/Projects/mycelium-multi-device-readiness` on `integration/mycelium-multi-device-readiness` (`22ae8a9`). Verify `fix/durable-heartbeat-renewal` and `build/model-stage-pack` are ancestors, then leave those worktrees alone. **Run `git status` first** — as of 16:20 there was uncommitted in-flight work from another agent (Task 2R.0 note). Never stash or checkout over it.
2. **Task 2R.0 — ✅ landed at `5dfef7b`.** Full suite: 1954 passed, 11 skipped, 121 subtests.
3. **Task 2R.1 — ✅ landed at `6d9680a`.** Signed peer EndpointID records in `AssignmentOfferV1`.
4. **Task 2R.3 — ✅ landed at `6d9680a`, corrected at `22ae8a9`.** `placement_provenance` accepts only `frozen_fixture` / `planner_v2`; qualifier rejects absence and evidence remains manifest-bound.
5. **Task 2R.7 — ✅ landed at `6d9680a`.** Peer class + runtime capability; only production Router/Iroh class is activation-eligible. Full Main baseline at `22ae8a9`: **1973 passed, 11 skipped, 121 subtests**.
6. Task 2R.4 — §6.7 liveness semantics (idle-stale, receipt suppression, no-death-on-one-beat).
7. Task 2R.2 — `PlacementSource` seam.
8. Task 2R.5 — `__main__` entrypoints for seed and node. **Hardest blocker to starting Phase 3** — nothing can be launched over SSH until this exists.
9. Task 2R.6 — N-way stage-pack coverage + frozen tolerance file.
10. Task 2.3B — finish the localhost E2E to full assertion coverage.
11. Task 2.4 — refactor `SwarmCoordinator` onto the seed plane (needs 2R.7).

Then stop and run a focused review before touching the physical controller (Task 3.2).

Steps 3–5 are deliberately grouped: all three add fields to the same signed membership contracts, and **a contract change after a physical run is sealed against it invalidates that run.** Get the wire shape right once, before Phase 3 seals anything.

This tranche is entirely executable while the M4 Pro is offline.

**Parallel track, no file overlap:** Task 3B.5 step 1–3 (extract the runtime contract, select a second backend, prove monolithic parity) can run alongside, and unblocks the two Linux laptops that are otherwise inert (§6.13).

---

## 9. Final acceptance checklist

**Phase 0–2 — landed 2026-07-22 (§1.4), verified not re-derived:**

- [x] Private plan remains local/untracked.
- [x] G0 baseline green; sibling branch `integration/mycelium-membership-contracts-mac` reconciled, not duplicated.
- [x] Invitations seed-pinned, durable, and single-use across restart/concurrency.
- [x] Node agent wraps existing physical service; no runtime duplication.
- [x] Seed coordinator contains **no member-count branch** (§6.8) — verified by grep, the expensive thing to get right.
- [x] Node receives only assignment-local stage pack.

**Phase 2R — carry-forward gaps (all blocking Phase 3):**

- [x] Full suite collects and runs (2R.0) — **1954 passed, 11 skipped, 121 subtests at `5dfef7b`; G0 green.**
- [x] `AssignmentOfferV1` carries signed epoch-scoped peer EndpointID records (2R.1) — landed at `6d9680a`.
- [ ] `PlacementSource` seam exists; a stub planner source swaps in with zero coordinator edits (2R.2).
- [x] `placement_provenance` threaded through contracts and **rejected-if-absent by the qualifier** (2R.3); enum fixed to `frozen_fixture` / `planner_v2` at `22ae8a9`.
- [ ] §6.7 liveness implemented: receipt suppression, idle-stale distinct from in-flight failure, no death on one missed beat (2R.4).
- [ ] `mycelium_seed/__main__.py` and `mycelium_node/__main__.py` run as real processes (2R.5).
- [ ] N-way (3- and 5-split) stage-pack union coverage; intermediate role tested before it runs physically (2R.6).
- [x] Peer class + runtime capability at join; only production-transport classes are activation-eligible (2R.7) — landed at `6d9680a`.
- [ ] Swarm control plane unified — `SwarmCoordinator` refactored onto the seed plane per the **extend** decision (§6.11, Task 2.4).
- [ ] Localhost E2E carries full assertion coverage, not just join-over-TCP (2.3B).
- [ ] Numeric tolerance frozen and committed before the first physical run; unmodified after.

**Phase 3 — the honest physical proof:**

- [ ] Local seed + two-agent real-dependency E2E passes but stays route-false.
- [ ] Both physical Macs pass preflight with distinct identities, using `mycelium_physical_preflight`.
- [ ] Pinned DialoGPT snapshot present on both hosts, fetched outside any qualification run.
- [ ] Native Iroh carries all cross-host activations.
- [ ] DialoGPT generates coherent text across both Macs.
- [ ] Exact token parity and frozen numeric tolerance pass.
- [ ] No route process loads full model or remote assignment.
- [ ] Cancellation cleanup passes.
- [ ] Remote death/restart/recovery passes with stale-generation rejection.
- [ ] Idle-peer heartbeat loss is proven as a distinct evidence class from active-decode transport failure (§6.7).
- [ ] `IrohTransport` peer authentication traces to a seed-signed, epoch-scoped membership record — no operator-typed `expected_endpoint_id` in the physical run (§6.6).
- [ ] Relay-vs-direct Iroh connection class recorded; per-hop trace correlation present without leaking prompt content.
- [ ] Per-direction measured link cost captured as `link_state.v1` during the physical run.
- [ ] Real negative runs fail closed.
- [ ] Evidence sealed, signature-verified, and immutable.
- [ ] Sole qualifier emits physical `route_ready=true` with `placement_provenance=frozen_fixture` visible, not hidden (§6.9).
- [ ] Reported claim language matches the highest handover Gate-I rung actually proved **and names the placement provenance** (§5.1).
- [ ] Full Python/Rust/UI/contract/claim gates pass; physical-proof tranche pushed.

**Phase 3B — the product:**

- [ ] Physical placement produced by `plan_snapshot` from a real gossip `EvidenceBundle`; `placement_provenance=planner_v2` (G4b).
- [ ] Execution graph built by `mycelium_router.layer_builder`, not the 2-assignment scaffold.
- [ ] A/B run proves a real capacity change produces a different assignment — both runs token-exact.
- [ ] Third member joins a live deployment and receives a capacity-derived assignment with no code change (G8).
- [ ] An intermediate-role stage has actually executed on physical hardware.
- [ ] Member departure produces a scoped replan candidate, not a swarm failure.
- [ ] Directed-cycle search exercised on a real measured asymmetric cost matrix; order-vs-naive delta recorded, **including a null result if that is what the data says** (§6.10, Task 3B.3).
- [ ] Opened cycle's loopback edge carried a real sampled token between physical hosts.
- [ ] `globally_exact` / `mode` / `explored_candidates` reported honestly for the branch actually taken.
- [ ] A prompt submitted through `mycelium_request_gateway` streams tokens from the physical swarm (§6.12).
- [ ] README "Not working yet" list updated to match sealed evidence only.
- [ ] Every "capacity-aware" / "swarm" claim in repo prose traces to a `planner_v2` sealed run.
- [ ] `release_ready=false` remains explicit.

**Heterogeneous execution (G9, §6.13) — promoted out of Phase 4:**

- [ ] Backend-neutral runtime contract extracted and proven against the existing MLX backend first.
- [ ] Second backend selected on **measured** evidence with the evaluation written down, not chosen by prose.
- [ ] Monolithic then single-stage parity on the new backend against the MLX reference within the frozen tolerance.
- [ ] A Linux laptop joins as an activation-eligible peer class and carries a real stage in a mixed Mac+Linux route with exact token parity.
- [ ] The planner demonstrably places **fewer layers** on the slower device from its measured profile — the loop between §6.13 and G4b actually closed.
- [ ] `docs/contracts/external-tester-boundary.md` written before any non-operator device joins (§6.13).
- [ ] No "heterogeneous distributed inference" claim until the above is sealed; until then the §6.13 ceiling sentence is the only permitted phrasing.

**Phase 4 — deferred:**

- [ ] Quantization still untouched until G6, deprioritized below **both** Phase 3B and G9, then follows its own proof ladder.
