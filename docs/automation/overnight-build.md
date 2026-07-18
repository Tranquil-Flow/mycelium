# Mycelium overnight continuation queue

Status: ARMED for 02:00 CEST on isolated branch `automation/mycelium-overnight`.
Base at queue creation: `dc9ca4c5e8f5bc73c2e3e9ae762afad45db25350`.
Authoritative architecture plan: canonical worktree `.hermes/plans/2026-07-17_142630-mycelium-ddai-mvp-synthesis-plan.md`.

## Latest honest status

Observed handover at 2026-07-18 01:22 CEST:

- Component completion: 94%.
- Verified working MVP: 35%.
- Phase 6: in progress, not yet verified.
- Phase 7 wire codec: merged and verified.
- Phase 7 native transport: pending.
- Phase 8 two-Mac proof: pending.
- Phase 9 read-only gateway: merged and verified.
- Phase 9 semantic live UI/request lane: pending.
- Phases 10 recovery/mobile and 11 demo/release: pending.

These percentages are planning estimates, not readiness claims. Every scheduled run must re-probe live Git state and tests before changing them.

## Concurrent-session pickup map

The 02:00 run inherits work from four interactive lanes. All source lanes are read-only to automation:

| Lane | Path | Branch | Expected role |
|---|---|---|---|
| Main | `/Users/evinova-self/Projects/mycelium` | `main` | serial integration and canonical plan updates |
| A1 | `/Users/evinova-self/Projects/mycelium-wt-p6-exec` | `phase6/two-process-router-exec` | Phase 6 qualification continuation |
| A2 | `/Users/evinova-self/Projects/mycelium-wt-p6-local-exec` | `phase6/local-exec` | competing/parallel Phase 6 qualification lane |
| B | `/Users/evinova-self/Projects/mycelium-wt-p7-sidecar` | `phase7/iroh-sidecar` | authenticated native sidecar |
| C | `/Users/evinova-self/Projects/mycelium-wt-p9-semantic-ui` | `phase9/semantic-observatory` | semantic Observatory gateway/UI |

Automation writes only to `/Users/evinova-self/Projects/mycelium-wt-overnight`. It may inspect source lanes and integrate committed or safely quiescent work into the overnight branch, but must never modify, reset, clean, commit, or switch a source lane.

## Hard safety boundary

- Write only in `/Users/evinova-self/Projects/mycelium-wt-overnight` and temporary files under `/tmp`.
- Never edit canonical `main`, A1, A2, B, C, any other worktree, or another repository.
- No network, fetch, pull, push, PR, issue/comment, package install, remote host, phone, credential use, or external source import.
- No recursive cron creation or schedule modification.
- Do not modify immutable evidence runs.
- Never set or imply `route_ready: true`; only an authorized physical qualification may do that.
- Keep Mycelium independent: no code or protected edits from other distributed-inference repositories.
- Use TDD for new behavior: focused RED, minimal implementation, focused GREEN, then relevant broad gates.
- Preserve truthful claim boundaries: local process evidence is not two-host distributed-inference evidence.
- Stage explicit files only. Never use `git add -A` or `git add .`.

## Active-lane collision gate

Before harvesting any source lane, inspect its branch, status, recent commits, diff, untracked files, newest relevant file mtime, and running commands.

Treat a source lane as active and skip it for this tick if any of these hold:

1. a process/test command references that worktree;
2. a source or test file changed in the previous 12 minutes;
3. Git/index lock state exists;
4. output is changing during a two-probe check;
5. state is ambiguous.

Do not wait on or kill an active process. A later 30-minute tick will retry.

## Reconciliation rules

1. Merge only committed local `main` history into the overnight branch. Never consume canonical uncommitted changes.
2. For a clean, inactive lane with commits not yet integrated, review its complete diff and evidence before merging or cherry-picking into the overnight branch.
3. For an inactive lane with uncommitted work quiescent for at least 12 minutes, snapshot its diff and explicitly reviewed untracked files through `/tmp`, apply only to the overnight branch, then independently test. Never commit in the source lane.
4. A1 and A2 are competing Phase 6 implementations. Compare architecture, tests, and evidence; select or reconcile deliberately. Do not blindly concatenate both.
5. Never treat an agent summary or a commit message as verification. Re-run the focused gates and all affected broad gates in the overnight worktree.
6. Resolve ordinary conflicts only in the overnight branch. If a conflict changes canonical protocol authority or evidence semantics, abort that integration and report the architecture conflict.

## Priority queue

### P0 — Recover and integrate interactive-session output

- Inspect Main, A1, A2, B, and C.
- Integrate only complete, reviewable work without touching source worktrees.
- Record exact source branch/commit or dirty-snapshot identity in the run journal.

### P1 — Finish Phase 6 genuine two-process Router qualification

MVP decision is locked for this tranche:

- Set `RouterConfig(prefill_chunk_size_tokens=0)`.
- Send the complete prompt through ordinary PREFILL.
- Decode by replaying complete context.
- Do not add or claim progressive prefill or persistent KV-backed decode yet.

Acceptance requires:

- two persistent spawned MLX workers with one exact disjoint assignment each;
- parent-controlled bounded non-pickle IPC and clean shutdown;
- two production `Router` instances using `LoopbackSocketMesh` TCP and `mycelium.router_wire.v1` framing;
- real stage-0 activation bytes crossing the captured TCP frame into stage 1;
- prefill plus at least two decode outputs;
- parity with an independent concatenated MLX reference;
- distinct child PIDs, assignment/path-lock/token evidence, activation digest, and no hidden full-model fallback;
- timeout, crash, malformed-RPC, cancellation, and cleanup tests;
- `route_ready: false` unless a later separately authorized physical gate proves otherwise.

### P2 — Integrate and harden Phase 7 native sidecar

Only after reviewing Lane B output. Keep the 16 MiB operational cap. Require authenticated UDS, inherited-pipe bootstrap secret, peer credentials, challenge-response, directional keys, replay/sequence protection, endpoint-key pinning, bounded queues, cancellation, reconnect, acknowledgements, strict canonical Router ingress, and redacted observability.

No network dependency fetch is allowed overnight. If the pinned Rust dependency is unavailable locally, preserve source and report the exact dependency gate rather than substituting a mock transport.

### P3 — Integrate and harden Phase 9 semantic Observatory lane

Only after reviewing Lane C output. Preserve strict privacy projection, supported-version checks, exact deployment/model/provenance binding, explicit claim scopes, freshness/conflicts, same-origin defaults, one publication/SSE owner, and a live-label gate requiring both a current route challenge and a real request lifecycle.

Never expose prompts, token IDs/content, activations, logits, KV state, tensors, weights, credentials, raw endpoints/IPs/paths, or raw Router frames. Fixtures remain labeled static/demo.

### P4 — Serial integration and next local tranche

After P1–P3 are green, choose exactly one smallest locally verifiable tranche from the authoritative plan. Prefer recovery/cancellation/state-machine hardening or release reproducibility that does not require remote hosts. Do not begin physical Phase 8 without explicit current authorization containing both hosts and exact staging/cleanup scope.

## Per-tick execution budget

- Aim for 22 minutes of implementation/reconciliation and reserve the rest for verification and a compact report.
- Complete at most one coherent TDD tranche per tick.
- If another scheduled slot is missed because the prior run is still active, never overlap or spawn a duplicate worker.
- Prefer one clean verified commit over broad unfinished edits.
- If no source lane is safe to harvest, work only on a disjoint existing overnight-branch task or make no changes.

## Verification gates

Always run the smallest focused tests first. Then run gates based on touched scope:

- Python: focused tests, relevant Router/gateway suites, full `python3 -m pytest -q`, `python3 -m compileall -q`, and `git diff --check`.
- Rust touched: `cargo fmt --check`, `cargo clippy --all-targets --all-features -- -D warnings`, and `cargo test` with local caches only.
- UI touched: existing package checks/tests with the existing lockfile; no package install.
- Contract touched: contract manifest verification, audit, pins, and golden-frame checks.

Some worktrees omit local ignored planning authorities expected by the full suite. If needed, copy only these exact files from canonical into the overnight worktree for the test run, then remove them before staging:

- `ROUTER_HANDOVER.md`
- `docs/plans/2026-07-16-request-streaming-session-lifecycle-mvp.md`

Do not commit those temporary copies. Report pre-existing or environment-specific failures separately; never describe a failing full suite as green.

## Commit and report rules

- Commit only after focused gates pass and the affected broad gate result is understood.
- Use explicit `git add <paths...>` and inspect `git diff --cached --name-only`.
- Keep source provenance in the commit message or journal.
- Append one compact journal row per run.
- Discord report must stay below 1,800 characters and state: source lanes inspected, files/commit integrated, RED→GREEN evidence, broad gate result, commit, honest claim boundary, and next target.
- If no safe progress is possible, make no speculative edit; report the exact blocker.

## Run journal

| UTC time | Source identity | Commit | Priority item | RED | Focused/full gates | Result / next |
|---|---|---|---|---|---|---|
| 2026-07-18T00:24Z | Main/A1 `f2bc55b`; A2 dirty snapshot reviewed; B/C active | `f2bc55b` | P0/P1 | Integration-only: source RED was not preserved; A2 was rejected for API drift, `InProcessMesh`, and `route_ready=true` | Focused 13 passed; CLI qualified with 3 TCP activations and `route_ready=false`; full 647 passed, 2 skipped, 95 subtests, 2 missing-doc failures; compile/diff checks passed | Selected A1/Main; local two-process Router parity only. Retry B/C after quiescence. |
| 2026-07-18T00:57Z | C `dc9ca4c` + quiescent 14-file snapshot `11d46f79`; Main/A1/A2/B inspected, B withheld | `this commit` | P3 | Re-subscribe regression failed on cached `LIVE · QUALIFIED`, then passed after replacement-stream connecting transition | Focused 10 Python + 45 UI; UI full 70 + build; Python 658 passed, 2 skipped, 117 subtests with 1 inherited missing-plan failure; compile/diff; 11 contract pins/audit + 17 contract + 3 golden tests green | Read-only semantic evidence/live-label gate only; no physical-host, execution-parity, or `route_ready` claim. Next: retry hardened B after generation/cancellation review. |
| 2026-07-18T01:33Z | B `dc9ca4c` + quiescent 13-file snapshot `9bd62eb5`; Main/A1/A2/C inspected | `this commit` | P2 | RED: explicit cancellation/permit release and active-ID retention failed in Rust; unknown/terminal cancel, malformed hello, and endpoint-generation fence failed in Python; all focused GREEN | Rust fmt/clippy + 21 tests; Python focused 12; contract audit + 28 tests/7 subtests; full Python 670 passed, 2 skipped, 117 subtests with 1 inherited missing-plan failure; compile/diff green | Authenticated local sidecar and local three-process iroh behavior only; process-lifetime delivery, no physical-host, Router-dispatch, crash-durability, or `route_ready` claim. Next: smallest local Phase 10 recovery/cancellation tranche. |
| 2026-07-18T01:54Z | Main/A1 `f2bc55b`; A2/B/C dirty snapshots re-inspected and already selected/rejected by prior journaled evidence | `this commit` | P4 | Stale decode-step failure report recovered unexpectedly; future-step report also advanced recovery; each focused RED then GREEN | Focused 2 + related 23 passed; full Python 672 passed, 2 skipped, 117 subtests with 1 inherited missing-plan failure; compile/diff and `route_ready` scan green; UI gate unavailable (`vitest` absent, no install) | Decode recovery now accepts failure only for the current token index. Local state-machine evidence only; no physical-host, remote cancellation, execution-parity, or `route_ready` claim. Next: bind failure scope identity to the locked manifest. |
| 2026-07-18T02:24Z | Main/A1 `f2bc55b`; quiescent A2 `f534a1b9`, B `d8a58518`, C `279810be` snapshot identities re-inspected | `this commit` | P4 | Off-path placement, edge, and device reports each triggered recovery; 3 focused RED then GREEN | Focused 3 + end-to-end 10; Router 164 passed, 1 inherited plan test deselected, 42 subtests; full Python 675 passed, 2 skipped, 117 subtests with the same 1 missing-plan failure; supplementary 675 green; compile/diff and `route_ready` scan green | Recovery exclusions now accept only identities bound to the locked path. Local state-machine evidence only; no authenticated sender, physical host, execution parity, or `route_ready` claim. Next: bind failure-report origin to the registered Router/transport peer. |
