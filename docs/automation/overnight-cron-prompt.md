# Mycelium overnight continuation agent

Continue the Mycelium/DDAI MVP from all interactive sessions that have ended or become safely quiescent. Work in the dedicated integration worktree only:

`/Users/evinova-self/Projects/mycelium-wt-overnight`

Expected branch: `automation/mycelium-overnight`.

First load and follow `mlops/distributed-inference-mvp` and `test-driven-development`. Then read `docs/automation/overnight-build.md` completely. That file defines the current lane map, collision gate, Phase 6 MVP decision, priorities, acceptance gates, verification commands, and report format. Treat it as mandatory.

Critical boundaries:

1. Write only in the overnight worktree and `/tmp`. Canonical main and all A1/A2/B/C worktrees are read-only inputs. Never reset, clean, switch, commit, or edit them.
2. No network, remote host, phone, credentials, package installation, fetch/pull/push, PR/comment, external source import, or recursive cron changes.
3. At run start, inspect canonical main plus all source lanes listed in the queue. Apply the 12-minute/process-based collision gate. Skip any active or ambiguous lane; retry next tick.
4. Integrate committed or safely quiescent work into the overnight branch only. A1/A2 are competing Phase 6 lanes: compare and select/reconcile through evidence, never blindly combine.
5. Before new implementation, harvest all safely available session output. Then complete at most one smallest coherent TDD tranche.
6. Phase 6 MVP must use `prefill_chunk_size_tokens=0`, whole-prompt PREFILL, and whole-context replay for decode. Do not build or claim progressive prefill or persistent KV continuity in this tranche.
7. Never set or imply `route_ready: true`. Local multiprocess work is not physical two-host evidence.
8. Aim for 22 minutes of active work, reserve remaining time for focused and broad verification, and never overlap another run.
9. Stage explicit paths only; never `git add -A` or `git add .`. Commit only verified work. Never push.
10. Append a compact row to the queue journal. Final response under 1,800 characters: inspected lanes, integrated source identity, RED→GREEN evidence, broad gates, commit, honest claim boundary, next target. If no safe progress exists, make no speculative edit and report exact reason.

Do the work now. Do not merely propose steps.