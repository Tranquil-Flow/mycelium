# Mycelium overnight autonomous build queue

Status: ACTIVE on isolated branch `automation/mycelium-overnight` only.
Base at queue creation: `dc9ca4c5e8f5bc73c2e3e9ae762afad45db25350`.
Authoritative architecture plan: canonical worktree `.hermes/plans/2026-07-17_142630-mycelium-ddai-mvp-synthesis-plan.md` (live SHA-256 observed 2026-07-18: `4fba42b7fb9c61dccdfd53b4a7f7fb56750a0ef01760e2560c7c44c79712eb20`).

## Safety boundary

- Work only in `/Users/evinova-self/Projects/mycelium-wt-overnight`.
- Never edit `/Users/evinova-self/Projects/mycelium` or any other worktree/repository.
- No network, fetch, pull, push, PR, issue/comment, package install, remote host, or external source import.
- Do not modify existing immutable evidence runs.
- Do not emit or set `route_ready: true`; only physical qualification may do that.
- Keep Mycelium independent: no code or protected edits from other distributed-inference repositories.
- Use TDD: observe focused RED before production change, then focused GREEN and full-suite GREEN.
- One smallest coherent tranche per scheduled run. Prefer a verified commit over broad unfinished edits.
- Preserve truthful claim boundaries: local work is not physical distributed-inference evidence.

## First-run synchronization

`BASE_SYNC_PENDING`

Exactly once, before the first production tranche:

1. require a clean worktree;
2. rebase this branch onto the current local `main` ref, without network access;
3. on conflict, abort the rebase and report a blocker without production edits;
4. replace `BASE_SYNC_PENDING` with `BASE_SYNC_COMPLETE` in the same green commit as the first tranche.

After completion, never merge/rebase/cherry-pick automatically. This prevents later autonomous runs from colliding with live manual work.

## Weighted plan status

Method: complete phase = 1.0, partial phase = 0.5, pending phase = 0.0. This is a planning estimate, not an MVP-readiness claim.

`[##########----------] 50%` — 4 complete, 6 partial, 4 pending; 7.0/14 weighted phase-equivalents.

| Phase | State | Live evidence / missing gate |
|---|---|---|
| 0 provenance/baseline | partial | private baseline and manifest exist; original locked-environment gate remains incomplete |
| 1 green baseline/contracts | complete | current Python baseline green; compatibility contracts frozen |
| 2 Gossip → Planner → assignments | complete | atomic adapter and assignment compiler integrated |
| 3 assignment provisioning/evidence | complete | assignment-scoped provisioning and coherent bundle integrated |
| 4 MLX loader/backend | partial | assignment-bound load and stage execution pass; no stage-local KV interface |
| 5 deterministic Layer Builder | complete | graph construction/negative validation integrated |
| 6 real local Router execution | partial | live local state/capacity and direct MLX RuntimePort exist; no runtime-service RPC, KV decode continuity, or real two-process inference |
| 7 native iroh | partial | clean-room codec/provenance work only; no authenticated production TransportPort |
| 8 physical qualification | pending | no two-Mac compute/parity qualification |
| 9 request gateway | pending | no qualified distributed token request stream |
| 10 read-only Observatory | partial | typed data lane and read-only gateway tranches exist; live accepted-run projection remains incomplete |
| 11 recovery | pending | intentionally deferred until stable physical prefill/decode |
| 12 Pixel | partial | external qualification evidence only; no exact-stage runtime |
| 13 demo/release | pending | no reproducible accepted orchestration |

Strongest honest execution claim: two independent local spawned processes load/probe disjoint assignment-bound MLX stages. No cross-process prefill/decode or physical distributed inference is yet proven.

## Overnight critical path

Work in order. Check an item only when its acceptance tests and the full Python suite pass. If an item is too large, add indented unchecked sub-tranches beneath it and finish only the smallest one.

- [ ] O1 — Stage-local KV execution primitive
  - Extend the GPT-2 stage backend with explicit per-layer key/value state and position offset.
  - Keep the existing stateless execution API compatible.
  - Prove cached prefill + one-token incremental decode matches full-sequence recomputation for entry and non-entry stages within declared exact/tolerance rules.
  - Reject wrong layer count, shape, dtype, position, overflow, and cross-assignment state.
  - Do not add global mutable caches here.

- [ ] O2 — Assignment/path-bound KV lifecycle in `MLXRuntimePort`
  - Bind cache ownership to deployment epoch, assignment, placement, request/path, path attempt, and stage.
  - PREFILL creates state; DECODE consumes only the next token/activation and strictly advances position/token index.
  - Reject decode-before-prefill, replay, gap, wrong path attempt, wrong placement, overflow, and stale identity.
  - `cancel(path_id)` must free all local state idempotently; snapshots/evidence must not expose prompt/token content.
  - Preserve item-level failure isolation.

- [ ] O3 — Runtime-owned service protocol and client `RuntimePort`
  - Build a bounded, versioned, canonical local RPC seam around an assignment-loaded runtime.
  - Never use pickle or unbounded frames.
  - Runtime process owns loaded tensors, KV state, cancellation, and queue accounting.
  - Client validates request/response identity, timeout, frame size, and process death fail-closed.
  - Keep transport local-only; do not imply authenticated physical networking.

- [ ] O4 — Runtime-owned capacity/lease integration
  - Connect reservations to the serving runtime rather than a fixture or unrelated coordinator.
  - Prove reserve/commit/release/expiry, overcommit rejection, deployment/placement binding, cancellation cleanup, and queue-depth reporting.
  - Preserve existing process-local capacity implementation unless replacement behavior is fully covered.

- [ ] O5 — Real two-process prefill and multi-token decode harness
  - Spawn two persistent runtime processes with disjoint assignments and actual client RuntimePorts.
  - Execute entry → final stage prefill, greedy token loopback, and at least two incremental decode steps with stage-local KV reuse.
  - Compare activation/logit tolerances and exact token IDs against monolithic full-sequence reference.
  - Record both PIDs, disjoint loaded tensor sets, per-stage cache positions, and content-free event evidence.
  - No fixture RuntimePort/CapacityPort may participate.

- [ ] O6 — Local qualification negative gates and evidence
  - Kill either runtime and fail closed.
  - Cover stale proof/revision/epoch, duplicate/replayed envelope, sequence gap, expired lease, dropped process, cancellation, and no hidden full-model fallback.
  - Emit a new local qualification artifact with a SHA-256 manifest and explicit `route_ready: false`.
  - Never mutate old evidence.

## Stop boundary

After O1–O6, or if progress requires native iroh source, physical peers, credentials, architecture approval, package installation, or network access: stop implementation, preserve a clean green branch, and report the exact blocker. Do not invent substitute mocks or weaken acceptance gates.

## Run journal

Append one compact row per scheduled run.

| UTC time | Commit | Queue item | Focused RED | Focused/full GREEN | Result / next |
|---|---|---|---|---|---|
