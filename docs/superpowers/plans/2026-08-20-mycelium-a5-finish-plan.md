# A5 Finish Plan — 2026-08-20 (recommendations locked 2026-08-20)

**Goal:** take A5 from `design_only` to a single atomic A5 feature commit.

**Spec:** `docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md` (308 lines)
**Status quo:** spec approved; benchmark protocol frozen + 19 materiality tests green; A4 base shipped (commit `487a1b4`); A3 qualifier authoritative; A2 artifact source live; M18 prior-art for reference only (§1 supersedes).
**Third node:** M4 16GB laptop at `evis-macbook-pro-1` (Tailscale `100.126.111.123`, remote account `evinova`) — see `docs/superpowers/notes/2026-08-19-a5-preflight-third-node-decision.md`. **SSH auth currently broken** (no key in `~/.ssh/` authenticates to `tranquil.flowstate@` on Tailscale despite the host being online). Phase 4 is BLOCKED on resolving this with Evi before any provisioning work begins.

## Locked-in recommendations (Evi, 2026-08-20)

These decisions were approved and are the binding inputs for this plan. Any deviation must be raised before work begins, not discovered mid-phase.

| Question | Decision | Rationale (one line) |
|---|---|---|
| Replica carries which stage? | **stage0 (layers 0–22)** | Dominant compute; gives real materiality vs stage1 which is essentially free |
| Which branch? | **`codex/flexible-swarm-catalog`** (same as A4 commit `487a1b4`) | Linear A4 → A5 history, lowest blast radius, lane topology already aligned |
| Inconclusive material-gain outcome? | **Stay `design_only` with all artifacts shipped** | Spec §1 is permissive; honest outcome; unblocks A6/A7/A8 |
| Per-tab UI single-active-request gate? | **Fix standalone before A5 starts** (Phase 5.0) | Real product bug; cancel currently strands the UI in `streaming/interrupted` and blocks the next submit |

These four answers are the only ones needed to drive this plan. Everything else is a question the agent can answer itself (or surface if blocked).

## What A5 must prove (spec §1, §10, §11)

1. A real multi-stage model graph with two physical pipeline stages (already exists: 0.5B Qwen with stage0 on node-0 M4 Pro + stage1 on node-2 Surface Book).
2. **Provision and qualify at least one replica of a contiguous stage on another eligible physical placement** — yielding ≥2 complete legal qualified tracks.
3. Through the **ordinary browser gateway** (the same A4 gateway that runs `/api/v1/inference`), overlap ≥2 requests and prove **distinct requests use distinct complete tracks**, with exact per-placement work, Router-frame movement, parity, stage-local KV, and zero cleanup delta.
4. Run the **frozen benchmark protocol** (`tests/a5_acceptance/benchmark_protocol.v1.json`): ABBA×3 = 12 measured windows ≥60 terminal requests each; primary metric = completed generated tokens / wall-second; material threshold = 10% point estimate AND paired 95% bootstrap lower bound (10,000 resamples, seed `0xA5`).
5. **Replica-loss negative gate**: removing one replica blocks new admission on affected tracks; surviving track remains usable at reduced capacity; an admitted request on the lost placement terminates explicitly without migration; an unaffected admitted request completes.
6. **All-eight-workspace verification** through the browser: Inference, Device Lab, Network, Nodes, Plans, Readiness, Incidents, Settings — concurrent submit, cancel, replica degradation, navigation, refresh, B/F, reconnect, terminal history, second session.
7. **One atomic A5 feature commit** binding source/spec digests, base + replica identities, artifacts, load + qualification proofs, graph/groups/tracks, browser request/track bindings, per-placement work, benchmark samples, replica-loss negative results, UI checks, regression/audit outputs.

## Hard non-goals (spec §1, §6)

- Not whole-model data parallelism, tensor parallelism, microbatch overlap, speculative decoding, in-flight migration, replay, KV handoff, automatic successor claim.
- No historical M18 replica evidence (§1 supersedes for this gate).
- No live harness result can promote a replica plan (§1, §11).
- No replica-plan failure poisons the base deployment (§6).

## Lane topology

The completion plan assigns A5 to the Capacity lane parallel to A6/A7/etc. Spec §2 says **only the A5 dispatcher + replica_qualification may mark replica tracks qualified and admit them**. Two integration surfaces are real:

- **A4 dispatcher must learn to choose one of N qualified tracks per request** under bounded authority and reservation locks (spec §5). Currently the dispatcher picks one route; track choice is identical. Reuse `a4_concurrency_track_policy_digest` as the discriminated binding.
- **A3 qualifier must accept a `replica_*` qualification type** alongside the existing primary qualification. Reuse the existing route-ready path with `replica_qualification.v1` as an additional authority layer.

These are the only A4-touching changes A5 needs. Everything else is new code that lives under `mycelium_replica_*` packages.

## Plan — 7 phases, in order

Each phase ends with a green gate before the next starts. Lane topology allows **parallel isolated work** on Phase 1 (workload freeze) and Phase 2 (contracts) without blocking integration.

### Phase 0 — Pre-flight (≤ 1 session)

| # | Item | Why first |
|---|---|---|
| 0.1 | **Resolve third-node SSH** with Evi. `~/.ssh/id_ed25519_m4pro_to_laptop` does not authenticate; tailscale reports `tranquil.flowstate@` online but no available key matches. Evi must either re-add a public key to the laptop's `authorized_keys`, confirm the correct username, or relabel which user we should SSH as. **Phase 4 cannot start until this is resolved.** | Third node is physical placement #3 — without working SSH, no replica can be provisioned |
| 0.2 | Confirm A4 commit `487a1b4` is intact and the A4 browser e2e still passes on `codex/flexible-swarm-catalog` | Single source of truth for the lane topology; ensures Phase 5 starts from a green A4 base |
| 0.3 | Replica carries **stage0 (layers 0–22)** on the M4 16GB laptop (locked decision 2026-08-20) | Memory budget fits; dominant compute for materiality |
| 0.4 | Update `docs/handover/2026-08-18-mycelium-a4-implementation-packet.md` and the A5 §11 section to reflect A4 as `done` and A5 starting | Single source of truth for the lane topology |

### Phase 1 — Workload freeze (1 session, parallel-safe with Phase 2)

| # | Item | Output |
|---|---|---|
| 1.1 | Write `tests/a5_acceptance/workload_manifest.v1.json` with prompt/output buckets, arrival schedule, QoS mix, offered concurrency, and product benchmark session digest | Frozen corpus that the benchmark reads |
| 1.2 | Canonicalize the manifest: sha256 of `protocol`, `model_id`, `representation_digest`, `workload_digest`, `arrival_schedule_digest`, `prompt_output_buckets_digest`, `qos_mix_digest`, `session_digest` | These are the identical_binding_fields required by spec §9 and the benchmark protocol |
| 1.3 | Tests: frozen manifest validates, digests recompute, no field drift after re-canonicalize | `tests/a5_acceptance/test_workload_manifest.py` |
| 1.4 | Update `tests/a5_acceptance/test_materiality_protocol.py` to load `workload_digest` from the new manifest and assert identical binding across all measured windows | One bundle |

**Done when:** `pytest tests/a5_acceptance/` green, `workload_manifest.v1.json` digests stable across two consecutive canonicalizations.

### Phase 2 — Closed contracts (1 session, parallel-safe with Phase 1)

| # | Item | Output |
|---|---|---|
| 2.1 | `mycelium_replica_contracts.py` with four capability-named, closed, bounded, canonically digest-bound records: `mycelium.replica_plan.v1`, `mycelium.replica_qualification.v1`, `mycelium.replica_runtime.v1`, `mycelium.replica_benchmark.v1` (spec §7) | The 4 closed shapes the spec names |
| 2.2 | Spec-side definition for each: required fields, optional fields, privacy redaction rules (no prompts, output text, token IDs, logits, KV, credentials, paths, raw addresses, unbounded traces — spec §7 last paragraph) | Definitions live as a sibling design note `docs/superpowers/notes/2026-08-20-mycelium-a5-replica-contract-shapes.md` |
| 2.3 | Deterministic tests per spec §10: closed shape, count/size, canonical digest, privacy, unknown-field, duplicate identity, mixed generation, stale authority, illegal transition | `tests/a5_acceptance/test_replica_contracts.py` |
| 2.4 | Wire the contracts into `scripts/generate_contract_manifest.py` and `scripts/generate_contract_fixtures.py`, regen, audit | Contracts regenerated, `contract_audit.py` PASS |

**Done when:** `tests/a5_acceptance/test_replica_contracts.py` green, `scripts/contract_audit.py` PASS, all 4 shapes stable across two regenerations.

### Phase 3 — Isolated leaf modules (parallel, no integration) (2–3 sessions)

All three are planner-side or qualifier-side; none touch the A4 dispatcher. Lane topology allows concurrent work here.

| # | Item | Output |
|---|---|---|
| 3.1 | **Legal-track enumeration** in `mycelium_layer_planner/replication.py` (extend, do not replace). Tests: ≥2 ordered groups, ≥2 qualified placements in one group, exact multi-stage ranges, missing/reversed edges, stable ties, no per-request splitting (spec §3, §4, §10) | `tests/a5_acceptance/test_replication_legal_tracks.py` (as landed) |
| 3.2 | **Flow solver** in `mycelium_layer_planner/flow.py` (extend). Tests: requested/admitted/unmet demand, complete tracks, traffic fractions bounded finite and sum to one within fixed tolerance, before/after bottlenecks, uncertainty, candidate marginal gain, every rejection, zero-flow removals (spec §4, §10) | `tests/a5_acceptance/test_flow_legal_tracks.py` (as landed) |
| 3.3 | **Replica qualification** in `mycelium_qualification/replica.py` (new; plan drift corrected 2026-08-20 — repository qualifier package is `mycelium_qualification`, not `mycelium_qualifier`). Tests: assignment-local acquisition, warm reuse, independent load + qualification, partial-candidate rollback, incumbent preservation, generation fencing, stale/mismatched placement rejection, expiry (spec §2, §5, §10) | `tests/a5_acceptance/test_replica_qualification.py` |

**Done when:** all three test files green, no A4 dispatcher touched, no request-gateway integration done.

### Phase 4 — Third-node workspace + replica-provisioning integration (1–2 sessions, hardware-coupled)

| # | Item | Output |
|---|---|---|
| 4.1 | Workspace on the M4 16GB laptop: `/Users/evinova-self/mycelium-a4-concurrency-node3-v6/` with `physical_inference_node.py` deployed sha256-parity to node-0 and node-2, member-runtime, capacity store, socket dir | Third physical placement live |
| 4.2 | Add `--replica-stage-range` and `--node-id` flags to `physical_inference_node.py` for the chosen stage range (0.1); replica carries the same model representation and representation manifest as the primary placement it replicates | Replica placement loaded; same `manifest_digest` and `path_manifest_digest` as the corresponding primary placement |
| 4.3 | Membership lease for `node-3`: heartbeat renews every ~5 min via seed HTTP :8876. Membership service starts at boot, must be restarted after host reboot (same as node-2). Recovery procedure documented in the A5 preflight note | Replica node is discoverable |
| 4.4 | Planner proposes `replica_plan.v1` with two qualified placements in the replica group; qualifier runs artifact verification, load proof, startup challenge, parity, memory, cleanup, directed-link qualification for the replica; replica track becomes qualified (spec §5, §10) | `replica_qualification.v1` issued, `route_ready=true` (note spec §1 says `route_ready=false` for `replica_plan.v1`; the qualifier's output is `replica_qualification.v1` which DOES carry route-ready for the replica track) |

**Done when:** `curl /__mycelium/live-status` shows `route_alive=true`, `replica_track_qualified=true`, third node listed as `node-3`.

### Phase 5 — Dispatcher integration (1 session, single-touch to A4)

**5.0 — UI per-tab single-active-request gate fix (standalone, ≤ 0.5 session, ships FIRST)**

This is a real product bug that blocks Phase 6 physical gates: when a user clicks Cancel, the server may acknowledge the cancellation but hold the SSE open and never deliver a terminal frame; the request stays in `cancelling`, and the next submit is blocked with "A request is already active". Fix it before adding track-aware concurrency on top.

**Authority rule (Evi, 2026-08-20):** the UI must NEVER author a terminal state. `cancelled` is server-terminal-frame-only. A client timer may not synthesize a terminal result, and no wait may be derived from a hardcoded budget (A4's 2,000 ms absolute cancellation budget is not restartable or extendable by any layer). The client exposes no server deadline field, so no bounded wait may exist at all.

| # | Item | Output |
|---|---|---|
| 5.0.1 | `ui/web/src/features/inference/types.ts`: add NON-terminal phase `cancel_unconfirmed` (not in `TERMINAL_INFERENCE_PHASES`) | Phase vocabulary |
| 5.0.2 | `ui/web/src/features/inference/useInferenceSession.ts`: on cancel ack, abort the client's own stream connection (client-side resource release, not state authorship) and transition `cancelling → cancel_unconfirmed`; reducer guards it to only-from-cancelling and terminal-wins; `cancel_unconfirmed` is excluded from the submit-gate lists; no timer, no `cancelled` action | Gate clears; no terminal state authored client-side |
| 5.0.3 | `ui/web/src/features/inference/InferenceWorkspace.tsx`: `phaseLabel` "Cancellation unconfirmed" + visible `activityCopy` detail ("the request's final state belongs to the server; a new request can be submitted"); Cancel button disabled in `cancel_unconfirmed` | Truthful surfacing |
| 5.0.4 | `ui/web/src/features/inference/useInferenceSession.test.tsx`: ack-without-frame → `cancel_unconfirmed`, no history entry, gate cleared, second submit works; reducer unit tests (only-from-cancelling, terminal-wins, non-terminal); restored `resume_cursor_expired` retryable → defined non-looping outcome (auto-restore stops after first attempt) | Suite-green for the new behavior |
| 5.0.5 | PROVISIONAL restore schedule `[500,1500,3000,6000,12000,30000,60000]ms` — NOT frozen. Coordinate with the A4 lane: the in-flight A4 fix may discard unacknowledged events and advance `discarded_through`, so long tails risk `resume_cursor_expired`; adjust the tail (or cap it) once A4 confirms discard semantics | Schedule finalization with A4 lane |
| 5.0.6 | Standalone atomic commit on `codex/flexible-swarm-catalog`: "fix: cancel does not strand the per-tab inference request" — hold until A4 seals (Evi) | Landed before Phase 5.1 |
| 5.0.7 | Re-run the A4 browser e2e (`npm run test:a4-live-browser`) on a fresh serve to confirm no regression | A4 e2e still green |

**5.1–5.4 — Dispatcher track selection + UI projection**

| # | Item | Output |
|---|---|---|
| 5.1 | `mycelium_live/route.py`: per-request track selection via `a4_concurrency_track_policy_digest` discriminator. Admission chooses one qualified track under bounded authority + reservation locks; releases admission state before execution. Scheduling follows traffic fractions subject to current track capacity, QoS, aging, backpressure. Per-request runtime evidence binds track ID + every placement that applied work (spec §5, §8 Inference workspace) | Dispatcher selects a track atomically |
| 5.2 | Qualifier output flows to the A4 dispatcher via existing route_alive path. New field `replica_track_qualification` in `live-status` | Dispatcher sees replica track as admissible |
| 5.3 | Tests: admission/cancel interleavings with 2 tracks, saturation/release, replica loss (block-new + survive), non-participating loss, shutdown, projection during mutation, exact cleanup with no route-global serialization or cross-request ownership leak (spec §10 dispatcher interleavings) | `tests/live/test_a5_dispatch.py` |
| 5.4 | UI projection: Inference workspace shows "request-level stage replication" copy (spec §8), Network shows primary + candidate + qualified + active + surviving + failed tracks, Nodes shows replica role, Plans shows tracks/fractions/gain, Readiness shows independent artifact + load + graph + edge + parity + resource + cleanup + qualification + benchmark + promotion rungs | `ui/web/src/features/liveRoute/ReplicaPanel.tsx` + extensions to existing workspaces |

**Done when:** deterministic suite green, two concurrent browser requests land on distinct tracks (verified via per-request track binding in the SSE event stream).

### Phase 6 — Physical gates (hardware-coupled, 1–2 sessions)

| # | Item | Output |
|---|---|---|
| 6.1 | **Positive**: launch the ordinary browser gateway, submit ≥2 concurrent requests, prove distinct complete tracks used. Capture per-placement work counters, Router-frame movement, parity, stage-local KV, zero cleanup delta | `evidence/a5-product-positive-v100.json` |
| 6.2 | **Benchmark**: run `tests/a5_acceptance/benchmark_protocol.v1.json` end-to-end. 1 unscored warmup per mode (baseline + candidate). 12 measured windows ABBA×3, ≥60 terminal requests each, identical binding across all windows. Capture point estimate + paired 95% bootstrap lower bound. Required: both ≥ 0.10 | `evidence/a5-benchmark-v100.json`, decision = `material` |
| 6.3 | **Negative (replica loss)**: kill the data-plane sidecar on the replica node. Prove (a) affected tracks reject new admission, (b) surviving track remains usable at reduced capacity, (c) admitted request on lost placement terminates explicitly without migration, (d) unaffected admitted request completes | `evidence/a5-replica-loss-v100.json` |
| 6.4 | **Negative (illegal-state)**: corrupt the replica's `assignment_digest`, prove the replica is rejected, the incumbent selector is preserved, no admission poisoned | `evidence/a5-negative-illegal-v100.json` |

**Done when:** all four physical evidence files sealed with hashes, `run_a5_product_gate.py` returns `material`.

### Phase 7 — Seal + atomic commit (1 session)

| # | Item | Output |
|---|---|---|
| 7.1 | All canonical tests green on frozen final bytes: full Python suite, contracts, governance, claim boundary, release security | `tests/` 100% green |
| 7.2 | UI tests green on frozen final bytes: vitest 508/508, browser e2e (3 engines, 8 workspaces, all-states, second session) | `ui/web/` 100% green |
| 7.3 | `scripts/audit_orchestrator.py` (or equivalent) PASS on frozen bytes | 4 audits + claim boundary |
| 7.4 | One atomic A5 feature commit, sole-author (Evi), no Co-authored-by, no push | Commit hash on `codex/flexible-swarm-catalog` (or new A5 branch, your call) |
| 7.5 | Update `docs/handover/mycelium-completion-checklist.v1.json` + `docs/handover/CURRENT_AND_PLANNED_ARCHITECTURE.md`: A5 done, A6/A7 unblocked but separate | Handover reflects truth |

**Done when:** commit lands on the branch, all evidence under `evidence/` is sealed, the completion plan no longer shows A5 as `design_only`.

## Risk register (what can go wrong)

| Risk | Probability | Mitigation |
|---|---|---|
| M4 16GB laptop unavailable / offline (Tailscale relay can drop) | Medium | Phase 0 confirms before Phase 4. Recovery: SSH back, restart membership service (≤ 60 s) |
| Replica's per-request work attribution miscounted across stages | Medium | Spec §3 KV keying + spec §10 parity tests must pass before physical gates; Phase 5.3 enforces |
| Materiality <10% (replica on weaker host can't beat primary by 10%) | Medium | The M4 16GB runs the same 0.5B incumbent at fp16/MLX; primary uses the M4 Pro's matrix unit which is faster; expect gain on concurrency, not per-token. If `inconclusive`, A5 stays `design_only` per spec — honest outcome, not a blocker for A6/A7/A8 |
| Existing 7B standby placement disappears (memory pressure) | Low | Spec §1 preserves the qualified-only selection; 7B standby is a separate artifact; only the 0.5B incumbent is the model under test |
| A4 dispatcher change regresses A4 concurrency test | Low | A4 browser e2e green is a Phase 5 prerequisite; the A4 canonical suite is the gate before Phase 6 |
| Other agents' parallel branches touch `route.py` and create a merge conflict | Low | My A5 work targets `codex/flexible-swarm-catalog` (same branch as the A4 commit); A5 touches `route.py` minimally (track selector); other agents' branches are test/leaf/docs |
| Spec §5 "scheduling follows traffic fractions" requires fractional dispatch logic that didn't exist in A4 | High | Add a `replica_track_dispatch_digest` binding that names the dispatch mode (round-robin vs congestion-aware). Test both. The protocol already names `qualified_track_policy_digest` as an allowed mode-specific binding (§9), so this is a config question, not a code shape question |

## Critical files to know

- Spec: `docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md`
- Benchmark protocol (frozen): `tests/a5_acceptance/benchmark_protocol.v1.json`
- Materiality harness: `tests/a5_acceptance/materiality_harness.py`
- M18 prior-art (reference only, §1 supersedes): `mycelium_m18_replication.py`, `mycelium_layer_planner/`
- A4 commit: `487a1b4` "A4: bounded concurrent distributed inference with scoped liveness and exact cleanup"
- A4 base (now closed): `mycelium_live/route.py`, `mycelium_live/a4_install.py`, `mycelium_live/liveness.py`, `mycelium_live/command_controller.py`
- A3 qualifier: `mycelium_qualifier/`
- A2 artifact source: `mycelium_a2_artifacts/` on node-2
- Node-2 host (Tailscale-relayed, can go offline): `astra@100.125.181.68`, key path `/Users/evinova-self/.ssh/id_ed25519_mycelium_linux` (path only)
- Node-3 host (third placement, TBD): `evis-macbook-pro-1`, Tailscale `100.126.111.123`, remote account `evinova`
- Seed HTTP: `:8876`
- Evidence dir for A5: `/Users/evinova-self/mycelium-physical-run/a5-multistage-20260820/evidence/` (proposed)

## Open questions — none

All four Phase 2 questions Evi asked on 2026-08-20 are locked (see top-of-file "Locked-in recommendations"). The agent proceeds without re-asking them.

If a measurement in Phase 6 returns `inconclusive` or `not_material`, the locked outcome applies: **stay `design_only` with all A5 artifacts shipped**. Do not re-ask whether to try a different model variant or workload — the answer is already no.

## What to confirm before starting Phase 0

If the agent is being started fresh (no in-session context), it must:

1. Read this plan in full.
2. Confirm Phase 0.1 (third-node SSH) is still blocked — if Evi has fixed it in the meantime, the agent proceeds directly to Phase 1. If still blocked, the agent surfaces the blocker in one sentence and waits.
3. Confirm A4 is still at `487a1b4` on `codex/flexible-swarm-catalog` and the A4 browser e2e still passes.

Then start with Phase 1 + Phase 2 in parallel (no integration, pure leaf work).

## Estimated effort

| Phase | Sessions | Hardware required |
|---|---|---|
| 0 Pre-flight | 1 | none |
| 1 Workload freeze | 1 | none |
| 2 Closed contracts | 1 | none |
| 3 Isolated leaf modules | 2–3 | none |
| 4 Third-node + replica provisioning | 1–2 | M4 16GB laptop on |
| 5 Dispatcher integration | 1 | both nodes live |
| 6 Physical gates | 1–2 | both nodes + browser sessions |
| 7 Seal + commit | 1 | none |
| **Total** | **~8–11 sessions** | |

After A5, A6 (replay) and A7 (KV successor) are unblocked (per the completion plan's lane topology) but each remains a separate atomic commit. A8 (infrastructure decisions) is independent.

## What this plan assumes (locked decisions)

These assumptions are no longer negotiable — they were approved and locked on 2026-08-20:

- Third node = M4 16GB laptop (`evis-macbook-pro-1`, Tailscale `100.126.111.123`)
- Replica carries **stage0 (layers 0–22)** — dominant compute, real materiality
- Branch = `codex/flexible-swarm-catalog` (same as A4 commit `487a1b4`)
- Inconclusive material-gain → stay `design_only` with all artifacts shipped
- Per-tab UI single-active-request gate → fix standalone as Phase 5.0 before Phase 5.1

## What this plan does NOT assume

The agent may answer these itself or surface them only if they block:

- Workload corpus details (PromptBucket fractions, arrival rate, QoS mix, offered concurrency) — see `tests/a5_acceptance/workload_manifest.v1.json` already written
- Exact closed-shape field structure for the 4 replica_* contracts — see spec §7
- Whether to extend `mycelium_layer_planner/replication.py` or fork — choose based on M18 code shape (extend, do not replace, per spec §1)
- Stage-0 layer range format in `physical_inference_node.py` deployment JSON
- UI copy wording for the 8 workspaces — spec §8 names the language, agent writes the JSX