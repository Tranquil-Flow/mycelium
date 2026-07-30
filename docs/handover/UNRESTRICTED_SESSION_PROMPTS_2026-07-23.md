# Mycelium unrestricted parallel session prompts — 2026-07-23

These prompts target unrestricted host macOS, not the Aegis seatbelt and not Linux Docker.

Current authority:
- Base: `b317566a73d8c7aebafc76bcbcca7f8eb651163e`
- Base unrestricted full gate: `2031 passed, 3 skipped, 121 subtests`
- Entry-point closure: `ab668bacd9866e19ca14b7b98a07db8bba756b64` (local, not integrated)
- A8 controller: `f04e4381bd7c10956f5276907b4b5801dad95ba2` plus prerequisite `c3dd159` (local, clean, reviewed by its implementation session, not integrated)
- A8 independent reviews: specification PASS and security/quality APPROVED; no Critical/High/Medium findings, five Low and two informational findings, none blocking. Durable records: `.mycelium-plan/a8-reviews/A8_SPEC_COMPLIANCE_f04e438_2026-07-23.txt` and `A8_SECURITY_QUALITY_f04e438_2026-07-23.txt`.
- Shared full-suite runner: `/Users/evinova/Projects/.mycelium-plan/run_full_suite_locked.py`
- Canonical plan: `/Users/evinova/Projects/.mycelium-plan/MVP_COMPLETION_PLAN.md`
- Specification: `/Users/evinova/Projects/.mycelium-plan/mycelium-demo-plan.md`

Run every full suite through the shared runner so concurrent sessions do not cold-load MLX/Iroh simultaneously:

`python3.14 /Users/evinova/Projects/.mycelium-plan/run_full_suite_locked.py --repo ABSOLUTE_WORKTREE`

Never add ignores, skips, xfails, filters, or timeout/tolerance changes to manufacture green. Never use `git add -A` or `git add .`. Stage exact owned files. Commit locally; do not push. Never add AI attribution to commit messages. No physical hardware operations, evidence writes, plan edits, model downloads, or secret output from source-writing sessions.

## Launch wave 1 — safe in parallel

### Session 1 — A1 read-only branch reconciliation

```text
You are the read-only branch-reconciliation scout for the Mycelium MVP.

Run on unrestricted host macOS. Work from:
/Users/evinova/Projects/mycelium-cron-lanerecon

Read:
- /Users/evinova/Projects/.mycelium-plan/MVP_COMPLETION_PLAN.md
- /Users/evinova/Projects/.mycelium-plan/mycelium-demo-plan.md §1.4 and §7.1

Do not edit, stage, commit, merge, cherry-pick, reset, delete, push, or create worktrees. A previous reconciliation report exists at commit 510a27c; treat it as prior evidence, not authority. Fetching remote refs is allowed if network works, but report if it does not.

Audit every branch currently ahead of integration, including older branches and these current local lanes:
- build/model-stage-pack
- feat/phase-2.3b-local-e2e
- feat/phase-2r-entrypoints
- feat/phase-2r-liveness
- feat/phase-2r-placement-source
- feat/phase-2r-stagepacks
- feat/phase-3-physical-controller-v2
- feat/phase-3b5-second-runtime
- feat/physical-qualification-mvp
- fix/durable-heartbeat-renewal
- integration/mycelium-membership-contracts
- feat/session-2r5-entrypoint-closure @ ab668bacd9866e19ca14b7b98a07db8bba756b64
- feat/session-3.2-qualification-controller @ f04e4381bd7c10956f5276907b4b5801dad95ba2
- test/session-2r6-exact-pack-closure
- feat/session-2.3b-local-e2e-v2
- test/session-3b3-cycle-offline

For each branch, use ancestry plus content comparison (`git cherry`, stable patch-id, path-level diff, and line-level inspection where needed). Classify:
1. fully present on integration by content;
2. genuinely ahead and safe to cherry-pick;
3. partial/superseded, requiring selective extraction;
4. contaminated or conflicting;
5. active/incomplete — do not integrate.

Verify task claims against actual RED cases, not commit subjects. Pay special attention to the now-known correction: integration commit 634b19f was incomplete for Task 2R.5; local ab668bac closes the remaining entrypoint contract. Task 2R.6/dcfc7e2 also lacks exact 3/5 pack tensor-union and pack-based logits/token parity closure.

Return a table: branch, tip SHA, unique commits, exact files, task coverage, classification, recommended action, dependency order. Return read-only findings only. Do not modify any repository.
```

### Session 2 — A2 plan status reconciliation

```text
You are the driver-only plan reconciler for the Mycelium MVP. This session edits planning prose only and must touch no source repository.

Target live plan:
/Users/evinova/Desktop/mycelium-demo-plan.md
If that path does not exist, stop and report; do not silently edit the snapshot in `.mycelium-plan`.

Read the current integration log plus all current local session branches. Verify every task heading against actual diffs and RED cases. Do not trust commit subjects.

Known corrections to verify:
- 2R.2: f5322db integrated.
- 2R.4: 1dca9ba integrated.
- 2R.5: integration 634b19f is PARTIAL; local ab668bacd9866e19ca14b7b98a07db8bba756b64 closes dry-run, stdin/file bundle, argv-secret, exit-code, and bounded SIGTERM gaps but is not yet integrated.
- 2R.6: dcfc7e2 is PARTIAL; tolerance and some N-way scaffolding exist, but exact 3/5 tensor-set union/no-overlap/no-omission and pack-based logits/token parity remain open until the new closure lane lands.
- 3B.5: monolithic NumPy parity exists; optional MLX import and assignment-local NumPy stage execution remain open.
- 2.3B: partial.
- 3.2/A8: device-free implementation is committed locally at f04e438 (full branch gate: 2055 passed, 3 skipped, 121 subtests); mark local-only completed, not integrated and not physically proven.
- All physical gates G4/G4b/G8/G9 remain unproven.

Do this:
1. Verify all statuses in both directions: incorrectly marked complete and completed-but-unmarked.
2. Use `✅ LANDED`, `⚠️ PARTIAL`, `🔄 ACTIVE`, or unmarked consistently.
3. Distinguish integrated from local-only completed work.
4. Add one dated amendment-log row.
5. Change status markers and amendment row only; never alter task substance, RED cases, gates, or claim boundaries.
6. Re-read after every surgical edit because another driver may update the file.
7. Recompute SHA-256 and report it.

Do not commit or publish the private plan.
```

### Session 3 — A3 optional MLX plus assignment-local NumPy stage runtime

```text
You own Mycelium A3 / Task 3B.5 runtime completion.

Work only in:
/Users/evinova/Projects/mycelium-sess-a3-runtime
Branch: feat/session-3b5-runtime-completion
Expected clean base: b317566a73d8c7aebafc76bcbcca7f8eb651163e

Read Task 3B.5 and §6.13 from the canonical demo plan. Inspect current code before writing: backend-neutral contracts and monolithic NumPy parity already exist. Do not redo them.

Observed focused baseline at b317566:
`python3.14 -m pytest -q -p no:cacheprovider test_numpy_runtime.py test_runtime_loader.py test_router_mlx_runtime.py`
=> 72 passed.

Run the shared unrestricted full-suite gate before editing. Require the clean b317566 baseline. If the sole failure is the known intermittent exact-cap-frame test, run that exact test three isolated times; do not edit it from this lane. Final pre-commit full suite must complete with zero failures.

Objective:
1. Make MLX optional and lazily imported. Importing runtime contracts, NumPy runtime, and backend selection must work when MLX is absent.
2. Add a real assignment-local NumPy stage backend. It must consume an already authenticated assignment/stage-pack artifact, load only assignment-owned tensors, execute entry/intermediate/final stage roles, and preserve route_ready=false absent a qualified route.
3. Prove stage output parity against MLX within the already-frozen tolerance. Do not modify the tolerance.
4. Preserve current MLX behavior exactly.

Owned files:
- runtime_contracts.py
- runtime_loader.py
- numpy_runtime.py
- test_numpy_runtime.py
- test_runtime_loader.py
- a new narrowly named NumPy stage test file if needed

Not yours:
- stage_pack.py or test_stage_pack.py
- mycelium_seed/**
- mycelium_interactive/**
- mycelium_request_gateway/**
- physical_inference_qualification.py
- native/iroh_transport/**
- any signed membership or evidence contract

Required RED cases:
- With `monkeypatch.setitem(sys.modules, "mlx", None)`, runtime selection imports successfully and selects a real NumPy backend; never ship a fake mlx module.
- Explicit MLX selection while absent fails with a stable backend-unavailable error, not an unrelated import traceback.
- NumPy stage loads only exact assignment tensor keys.
- Entry, intermediate, and final roles execute; unsupported role/pack combinations fail closed.
- Same stage input produces NumPy hidden states/logits within frozen tolerance of MLX and exact greedy argmax/token result.
- Invalid dtype, tensor shape, nonfinite tensor, assignment mismatch, and unverified pack fail before execution.
- Fallback execution never claims route readiness or physical proof.

TDD: RED first, observe expected failure, minimal implementation, focused tests, then shared locked full suite. Run compile and Ruff on owned files. Stage exact paths and commit locally with a plain message such as `feat: execute assignment-local stages with numpy`. Do not push.

Report commit SHA, exact focused/full counts, MLX-absent proof, parity values, and what remains for physical G9.
```

### Session 4 — A4 SwarmCoordinator EXTEND

```text
You own Mycelium A4 / Task 2.4: refactor SwarmCoordinator onto the signed durable seed membership plane.

Work only in:
/Users/evinova/Projects/mycelium-cron-lane24
Branch: feat/overnight-2.4-swarm-extend
Expected clean base: b317566a73d8c7aebafc76bcbcca7f8eb651163e

Read Task 2.4 and §6.11 in full. The decision is EXTEND: one membership plane, not two. Existing browser behavior remains an adapter on top.

Observed focused baseline:
`python3.14 -m pytest -q -p no:cacheprovider tests/interactive/test_swarm.py tests/seed_coordinator tests/membership`
=> 96 passed.

Run the shared locked unrestricted full suite before editing and before commit.

Owned files:
- mycelium_interactive/swarm.py
- mycelium_interactive/server.py
- tests/interactive/test_swarm.py
- docs/contracts/swarm-control-plane.md
- QUESTIONS.md for the dated EXTEND decision only
- mycelium_seed/coordinator.py and mycelium_seed/state.py ONLY if a genuine adapter seam is absent; write a RED test first and keep the change minimal

Not yours:
- runtime_* or numpy_runtime.py
- stage_pack.py/test_stage_pack.py
- mycelium_qualification/physical_deployment.py
- mycelium_request_gateway/**
- physical_inference_qualification.py
- tests/seed_coordinator/test_local_two_node.py

Required RED cases:
- Browser membership survives coordinator restart through seed durability.
- Browser and Mac identities cannot collide on node_id.
- Revoked browser in-flight work rejects on membership generation, not bearer token alone.
- Browser peer is admitted to membership/evidence but rejected for activation-carrying placement.
- Existing browser invitation, stage-matrix, origin, dispatch/result, cancellation, leave/revoke, and status behavior remains compatible.
- Browser-specific bearer/session token becomes transport credential only; membership authority is signed member state.

No physical operations, no browser key redesign beyond the task, and no signed-contract schema changes. If the existing seed API cannot support the adapter without changing a signed wire contract, stop and report the exact missing seam rather than editing the contract.

TDD strictly. Run focused baseline, locked full suite, contract audit, claim-boundary audit, compile/Ruff, diff check. Stage exact files and commit locally: `refactor: move browser swarm onto the seed membership plane`. Do not push.
```

### Session 5 — A5 planner-placement plumbing

```text
You own Mycelium A5 / Task 3B.1 plumbing only. Do not perform the physical A/B proof.

Work only in:
/Users/evinova/Projects/mycelium-cron-lane3b1
Branch: feat/overnight-3b1-planner-placement
Expected clean base: b317566a73d8c7aebafc76bcbcca7f8eb651163e

Read Task 3B.1, §6.8, and §6.9. The existing `mycelium_seed/placement.py` seam and FrozenPlacementSource are already implemented. Add PlannerPlacementSource and the production LayerBuilder graph delegation path; do not redo the seam.

Observed focused baseline:
`python3.14 -m pytest -q -p no:cacheprovider tests/seed_coordinator/test_placement.py test_layer_planner_v1_gossip_adapter.py test_layer_planner_v1_planner.py test_layer_planner_v1_primary_plan.py test_layer_planner_v1_physical_graph.py test_planner_assignment.py`
=> 39 passed.

Owned files:
- create mycelium_seed/planner_placement.py
- create tests/seed_coordinator/test_planner_placement.py
- modify mycelium_qualification/physical_deployment.py only to add a separate delegation path
- create tests/qualification/test_physical_layer_builder_graph.py

Absolute prohibition: do not modify, delete, generalize, or rename existing `physical_deployment.build_execution_graph`, which is deliberately two-assignment scaffolding bound by existing tests.

Required pipeline:
Gossip EvidenceBundle -> planner_snapshot_from_evidence_bundle -> plan_snapshot -> RoutePlanV2 -> compile_bound_layer_assignments -> mycelium_router.layer_builder.build_execution_graph.

Required RED cases:
- stale or mixed-generation evidence rejects;
- expired device_status excludes the node rather than defaulting;
- placement intent never becomes readiness;
- assignments cover every layer exactly once with half-open ranges;
- missing load proof, stale epoch, or runtime endpoint/assignment mismatch rejects;
- provenance is planner_v2;
- coordinator, HTTP API, and node-agent require zero edits to swap placement source.

No device access, no A/B proof, no evidence sealing. TDD, focused tests, shared locked full suite, planner suite, audits, explicit staging. Commit locally: `feat: add planner placement and layer-builder graph plumbing`. Do not push.
```

### Session 6 — A6 request-gateway local physical-route seam

```text
You own Mycelium A6 / Task 3B.4 local half only.

Work only in:
/Users/evinova/Projects/mycelium-cron-lanegw
Branch: feat/overnight-3b4-request-gateway
Expected clean base: b317566a73d8c7aebafc76bcbcca7f8eb651163e

Read Task 3B.4 and §6.12. Build every device-free part of the ordinary user path; do not claim or simulate a physical run.

Observed focused baseline:
`python3.14 -m pytest -q -p no:cacheprovider tests/request_gateway`
=> 40 passed.

Owned files:
- mycelium_request_gateway/backend.py
- create tests/request_gateway/test_physical_backend.py
- mycelium_demo/cli.py only if a minimal `serve --swarm` selection seam is genuinely absent
- narrowly related request-gateway tests

Not yours:
- physical_inference_qualification.py
- mycelium_seed/**
- mycelium_interactive/**
- runtime_* or stage_pack.py
- README claim changes before sealed physical evidence

Required RED cases:
- Gateway serves only when the sole qualification authority has accepted the current exact deployment; no gateway-local readiness opinion.
- Accepted qualification must match deployment/run/model/source/placement provenance and validity window.
- Unqualified, expired, wrong-deployment, frozen-vs-planner mismatch, and route_ready=false records reject before Router admission.
- Prompt streams through existing RouterSessionBackend, not a local fixture.
- Client cancellation reaches Router cancellation and exactly-once cleanup.
- Prompt text/tokens never appear in logs, evidence-like records, or errors.
- Local tests remain explicitly route-false and make no physical claim.

Do not modify README "Not working yet" items: that is only legal after physical sealed evidence. TDD, focused 40-test baseline, shared locked full suite, claim audit, compile/Ruff, explicit staging. Commit locally: `feat: gate request gateway on qualified swarm routes`. Do not push.
```

### Session 7 — corrective 2R.6 / 3.1 exact pack closure

```text
You own the corrective Task 2R.6 / Task 3.1 closure discovered after the original dcfc7e2 landing.

Work only in:
/Users/evinova/Projects/mycelium-sess-2r6-stagepack
Branch: test/session-2r6-exact-pack-closure
Expected clean base: b317566a73d8c7aebafc76bcbcca7f8eb651163e

Do not redo the existing tolerance file, five-way range test, stage-pack compiler, or broad tamper suite. Current focused baseline:
`python3.14 -m pytest -q -p no:cacheprovider test_stage_pack.py tests/model/test_distributed_dialogpt_decode.py`
=> 22 passed.

Genuine remaining gaps:
1. 3-way and 5-way logical tensor ownership must union to the exact source tensor set, with no omission and no accidental overlap. Explicit tied aliases/overfetched backing shards must be classified separately from logical ownership; do not write an assertion that mistakes an authenticated alias or shared shard for duplicate ownership.
2. Pack-based 2/3/5-stage execution must prove logits within frozen tolerance and exact greedy-token parity against the independent monolithic reference.

Owned files:
- test_stage_pack.py
- create tests/model/test_stage_pack_parity.py if useful
- stage_pack.py only if the RED test exposes a real implementation defect

Not yours:
- tolerances/dialogpt-small-fp16.json (immutable for this lane)
- runtime_contracts.py/runtime_loader.py/numpy_runtime.py
- physical_deployment.py
- any membership/evidence contract

Required RED sequence:
- Build exact 3-way and 5-way assignments from a known source tensor inventory.
- Compare logical owned-key sets pairwise; require empty intersections except explicitly represented tied aliases, and require exact union equality to source keys.
- Require intermediate-role pack round trip.
- Load/execute pack-based 2/3/5 stages and compare logits to monolithic using existing frozen tolerances, then exact greedy argmax/token IDs.
- Mutate one pack to omit, duplicate, or misassign a tensor and prove rejection at the production verifier.

Run RED first. Keep compiler changes minimal if needed. Run focused 22-test baseline plus new tests, then shared locked full suite, compile/Ruff/diff check. Stage exact paths and commit locally: `test: close exact n-way stage-pack ownership and parity`. Do not push.
```

### Session 8 — A7 continuation on entrypoint closure

```text
You own Mycelium A7 / Task 2.3B localhost E2E completion.

Use only the prepared continuation worktree:
/Users/evinova/Projects/mycelium-sess-a7-e2e-v2
Branch: feat/session-2.3b-local-e2e-v2
Base: ab668bacd9866e19ca14b7b98a07db8bba756b64

This worktree intentionally begins with one modified file:
tests/seed_coordinator/test_local_two_node.py
It is an exact application of the preserved original A7 diff. The original dirty worktree remains untouched. Do not reset/stash/checkout away this diff. Start by inspecting and running it.

Owned write surface for this parallel wave:
- tests/seed_coordinator/test_local_two_node.py ONLY

Do not edit mycelium_seed source during this wave because A4 may own the seed adapter seam. If the E2E exposes a real production defect, preserve the failing RED test, stop, and report the exact source seam needed. Do not fix outside your surface.

Complete the remaining Task 2.3B assertions with real dependencies:
- one-use invitation rejects replay;
- distinct seed/node/service/sidecar PIDs;
- stage load proofs and endpoint exchange;
- one real local distributed request over native Iroh;
- exact token parity;
- seed restart with durable membership;
- bounded cleanup with zero scoped leaked processes;
- route_ready=false from real physical host-distinctness qualification, not a hardcoded literal.

This is unrestricted host work: native Iroh tests must run, not skip. It remains localhost evidence only and cannot satisfy G4.

First run `git diff --check` and the focused file to establish current continuation state. Follow TDD on the existing diff. Before commit run the shared locked unrestricted full suite. Never add ignores/skips/xfails or alter timeouts. Stage the single exact test file and commit locally: `test: complete localhost seed-node native-iroh e2e`. Do not push.

Report exact focused/full counts, PIDs/process cleanup evidence, commit SHA, and any source defect deferred to a serialized fixer.
```

### Session 9 — A9 offline directed-cycle study

```text
You own Mycelium A9 / Task 3B.3 offline half.

Work only in:
/Users/evinova/Projects/mycelium-sess-a9-cycle
Branch: test/session-3b3-cycle-offline
Expected clean base: b317566a73d8c7aebafc76bcbcca7f8eb651163e

Read §6.10 and Task 3B.3. Everything in this lane is synthetic/offline. Never call it physical evidence or measured-host evidence.

Observed focused baseline:
`python3.14 -m pytest -q -p no:cacheprovider test_layer_planner_v1_cycle_exact.py test_layer_planner_v1_cycle_scaled.py`
=> 7 passed.

Owned files:
- create tests/integration/test_cycle_search_offline.py
- create docs/qualification/directed-cycle-search-offline.md

Do not edit cycle_search.py, network_cost.py, primary_plan.py, or layer_builder.py in this parallel wave. If tests reveal a source defect, preserve the failing test and stop/report; A5 may touch adjacent planner behavior.

Required experiments/tests:
- Prove closing edge uses token-envelope bytes (~9) while forward edges use activation bytes (~1.5 KB for the target model), through production scoring functions rather than duplicate arithmetic.
- Provide at least one asymmetric synthetic matrix where exact search differs from and beats naive node-id ordering; record cost delta.
- Provide a controlled null-result matrix where naive and optimal tie or differ negligibly; document the null honestly.
- Compare directed asymmetric scoring with a symmetric-cost simplification and state whether order changes.
- Assert 3 nodes select exact strategy, globally_exact=true, deterministic order/tie break, and honest explored_candidates.
- Preserve missing-edge rejection and open_cycle loopback semantics.

No real link_state claims, no physical Router run, no evidence root. TDD, focused tests, shared locked full suite, explicit staging. Commit locally: `test: qualify directed-cycle search on synthetic asymmetric costs`. Do not push.
```

## Do not duplicate

A8 is complete and clean at `f04e4381bd7c10956f5276907b4b5801dad95ba2` in `/Users/evinova/Projects/mycelium-sess-a8-controller`. Its final unrestricted gate was 2055 passed, 3 skipped, 121 subtests; Rust fmt/test/clippy/build passed. Do not launch another A8 writer. The committed surface owns:
- physical_inference_qualification.py
- tests/physical_qualification/test_controller.py
- tests/physical_qualification/test_process_session.py
- native/iroh_transport/src/sidecar.rs
- native/iroh_transport/src/bin/mycelium-iroh-sidecar.rs

Both independent read-only reviews below are complete and approved. Do not relaunch them. A8 is eligible for the serialized integration driver only after every earlier producer dependency is committed and clean. Physical G4/G5/G6 remain unperformed.

## Completed A8 reviews — do not relaunch

### Session 10 — A8 spec-compliance review — COMPLETED PASS

```text
STATUS: COMPLETED. DO NOT RELAUNCH. Durable result: /Users/evinova/Projects/.mycelium-plan/a8-reviews/A8_SPEC_COMPLIANCE_f04e438_2026-07-23.txt

Read-only review only. Do not edit, stage, commit, or run physical hardware.

Review exact A8 commit `f04e4381bd7c10956f5276907b4b5801dad95ba2` plus prerequisite `c3dd159` against Task 3.2 command-by-command and RED-case-by-RED-case. Compare against b317566. Verify arbitrary N peers, exact signed endpoint membership, transfer allowlist/digest confinement, bounded argv-only SSH/process control, stdout/stderr separation, cleanup on every failure, prepare/run/cancel/recover/seal/cleanup lifecycle, stale generation rejection, immutable manifest, exactly-once sole qualifier invocation, and fail-closed readiness.

Return PASS or a numbered list of exact spec gaps with file/line/test references. Distinguish unit/controller readiness from unperformed physical G4/G5/G6 evidence. Make no changes.
```

### Session 11 — A8 security/quality review — COMPLETED APPROVED

```text
STATUS: COMPLETED. DO NOT RELAUNCH. Durable result: /Users/evinova/Projects/.mycelium-plan/a8-reviews/A8_SECURITY_QUALITY_f04e438_2026-07-23.txt

Read-only quality and security review of exact A8 commit `f04e4381bd7c10956f5276907b4b5801dad95ba2` plus prerequisite `c3dd159`. Do not edit or run devices.

Audit command injection, argv secrets, symlink/path traversal, owner-only files, transfer manifests, host-key behavior, process-group cleanup, PID reuse, timeout/interrupt cleanup, stale/replayed command IDs, evidence writer shutdown, manifest mutation, qualifier duplication, and overclaim boundaries. Review Rust sidecar changes for protocol compatibility and bounded behavior. Run focused tests and static checks if safe, but make no source changes.

Return severity-ranked findings and an APPROVED or REQUEST_CHANGES verdict with exact evidence.
```

### Session 12 — serialized integration driver

```text
Do not begin until every producer branch is committed, clean, and reviewed. You are the sole integration writer.

Start from the latest integration/mycelium-multi-device-readiness in a fresh clean integration worktree. Re-run read-only A1 reconciliation. Cherry-pick only reviewed commits, never dirty worktrees, in dependency-aware order:
1. ab668bac entrypoint closure;
2. corrective 2R.6 pack ownership/parity;
3. A3 optional MLX/NumPy stage runtime;
4. A5 planner placement plumbing;
5. A6 request-gateway local seam;
6. A4 SwarmCoordinator EXTEND;
7. A9 offline cycle tests/docs;
8. A7 localhost E2E;
9. A8 controller only after both read-only reviews approve: cherry-pick `c3dd159` first, then `f04e4381bd7c10956f5276907b4b5801dad95ba2`; never cherry-pick the tip alone because its bare-preflight prerequisite is a separate commit.

After EACH cherry-pick run the shared locked unrestricted full suite and relevant focused gate. Stop on the first regression. Resolve conflicts by understanding semantics; never wholesale take ours/theirs. Run contract audit, claim-boundary audit, compileall, Ruff, Rust fmt/clippy/test if Rust changed, UI check if UI changed, and git diff check. Verify no private plan/evidence/token/model-cache path is staged.

Only after the complete integration tree is green may any physical controller run begin. Push the Evi-owned integration branch immediately after all required gates pass; do not push shared/third-party repositories.
```

## Physical/device sessions — one controller writer at a time

Do not run multiple physical writers concurrently. Read-only scouts may inventory separate devices, but one driver owns run ID, process launch, evidence root, cleanup, sealing, and qualifier invocation.

### Session 13 — Astra read-only inventory

```text
Run only after Astra explicitly says yes, supplies her exact macOS username, enables Remote Login or Tailscale SSH, and her current Tailscale node is online. Target identity must be `Astra Macbook` / `astra-macbook`, current IP 100.117.33.124; reject stale 100.78.72.79.

Known verified state is transitional: earlier identity-bound pings succeeded both directly and through DERP and TCP/22 returned `Connection refused`; the latest authoritative coordinator probe at 2026-07-23T16:31:19+02:00 reported `Online=false`, three ping timeouts, TCP/22 timeout, no SSH banner, and no explicit Astra SSH `Host`/`User` stanza. Evi has authorized continuing, but the exact macOS username remains absent. Re-query identity immediately before access. Do not treat remembered reachability as current command access and do not guess a username.

Read-only only. No installs, downloads, source/config changes, helper accounts, evidence sealing, or long-running services. Verify SSH host key normally; never disable changed-key protection. Collect minimal host/boot/arch/macOS identity, Python 3.14, MLX, Rust/Cargo, Tailscale direct/relay class, memory/disk headroom, exact Mycelium checkout HEAD/status, pinned model presence/digest, native sidecar type/digest/version/health, and conflicting scoped process/port state. Do not enumerate unrelated apps/files.

If the checkout is dirty, stale, missing, or model/toolchain absent, stop and report exact preflight blocker. Return the strongest evidence rung only; inventory is not physical inference.
```

### Session 14 — Pixel read-only readiness

```text
Target Pixel 8 Pro identity: Tailscale 100.126.233.4, hardware serial 47031FDJG000RD. Use existing ADB/Termux authorization only. Do not pair anew, install APKs/tools, alter Wireless Debugging, change Termux allow-external-apps, create bridge tokens/helpers, or dump private app/process/log inventories.

Known pre-permission state: Pixel is Tailscale-reachable and an existing authorized pairing for hardware serial 47031FDJG000RD is present on the coordinator. The ADB binary is `/Users/evinova/Library/Android/sdk/platform-tools/adb`; an ADB server has listened on localhost:5037, but `adb -P 5037 devices -l` was empty while Wireless Debugging was off. Require Evi to enable Wireless Debugging before proceeding. Do not assume TCP/5555: modern Android may advertise a dynamic connect endpoint through mDNS. Use only the already-authorized endpoint the existing ADB server discovers, or ask Evi for the displayed connect endpoint; never initiate a new pairing without separate approval.

Establish evidence ladder: tailnet presence -> authenticated ADB transport -> shell execution identity -> named runtime eligibility -> existing authenticated worker, if already present. Collect only manufacturer/model/device, Android/API/ABI, build fingerprint, boot ID, real hardware serial, and named Termux/Tailscale package presence. If an existing authenticated argv bridge is present, prove health, unauthenticated 401, and authenticated identity without printing token; otherwise stop before bootstrap.

Return exact rung and blockers. Pixel membership/HTTP stage evidence remains partial G8 and cannot substitute for production Router/Iroh activation transport.
```

### Session 15 — physical preflight and first two-Mac run

```text
Launch only after integrated tree is clean/full-green and A8 is committed/reviewed. One driver owns all hardware, processes, run ID, evidence root, cleanup, seal, and qualifier.

Use two distinct Apple-Silicon Macs with explicit operator consent and read-only preflight first. Verify exact source/model/sidecar digests, Python/MLX, clean HEAD, host/boot/PID identity, Tailscale reachability, free resources, and no conflicts. Do not download inside qualification. Run frozen-placement G4 first, capture per-direction link_state.v1, exact tokens/tolerances, native Iroh direct/relay class, process/endpoint identities, stage-local inventories, and cleanup. Then lifecycle/negative runs, stop writers, seal once, and invoke sole qualifier once. Any source change invalidates the run: stop, fix under TDD elsewhere, restart with new run ID.

Do not involve Pixel in G4. Do not overclaim route_ready unless sole qualifier accepts immutable same-run evidence.
```
