# Parallel lane topology audit

`mycelium_lane_audit` gives the integration driver a deterministic, read-only view of concurrent Git feature lanes before any cherry-pick or merge attempt.

It exists because clean worktree status alone is insufficient. A lane can be clean while based on the wrong commit, can modify paths outside its declared ownership, or can collide with another lane or with changes already present on the integration target.

## Current invocation

From a checkout containing this package:

```text
python3.14 -m mycelium_lane_audit \
  --repo-root /Users/evinova-self/Projects/mycelium \
  --manifest docs/integration/2026-07-18-active-lanes.json
```

Output is one canonical JSON line using protocol `mycelium.lane_topology_audit.v1`. The report omits repository and worktree paths. Lists and lane records are sorted so repeated audits of unchanged Git state are byte-identical.

The checked-in manifest records declared branch bases and file ownership for the active July 18 feature lanes. Update it only through explicit review when a lane's approved scope or base changes. A manifest entry is coordination metadata, not permission to merge a branch.

## What the audit checks

For every lane:

- local branch existence and exact head;
- exact expected-base object existence;
- whether expected base is an ancestor of lane head;
- commits ahead of expected base;
- committed and uncommitted paths;
- changes outside declared path ownership;
- path overlap with target-tree changes since lane base;
- whether the lane has a registered worktree;
- structural state such as `in_progress_dirty`, `ownership_violation`, `no_feature_commit`, `structurally_reviewable`, or `reviewable_with_target_overlap`.

It also reports exact path intersections between each pair of lanes.

The implementation uses only read-oriented Git commands: `rev-parse`, `cat-file -e`, `merge-base --is-ancestor`, `rev-list`, `diff --name-only`, `worktree list`, and `status --porcelain`. `GIT_OPTIONAL_LOCKS=0` is set for every command. It does not edit an index, branch, ref, worktree file, or product state.

## Claim boundary

This tool does not run or verify tests. It does not inspect semantics, prove conflict resolution, validate contract compatibility, establish cherry-pick order, or make a merge/release decision. A `structurally_reviewable` state means only:

- branch descends from its declared base;
- branch is clean;
- at least one feature commit exists;
- observed changed paths fit declared ownership;
- no target-tree path overlap was detected.

It does not mean tests passed or code is correct.

The report therefore always emits:

```text
route_ready=false
release_ready=false
tests_evaluated=false
```

No local topology result advances physical qualification.

## Integration-driver workflow

1. Run the audit while lane agents are active. Treat dirty states as observation, never as permission to reset or clean another worktree.
2. Let each owner finish and create explicit feature-only commits.
3. Re-run the audit. Investigate every base mismatch, ownership violation, target overlap, and pairwise overlap.
4. Review each lane diff and RED-to-GREEN evidence independently.
5. Recreate or cherry-pick old-base lanes onto a fresh branch from the current integration tip; do not merge old automation history wholesale.
6. Run all required Python, contract, compile, diff, Rust, and UI gates after each accepted slice.
7. Stage explicit reviewed paths only; never `git add -A` or `git add .`.
8. Keep canonical `main` untouched until Evi approves final integration. Do not push from this audit workflow.

The manifest intentionally includes the queued request-token-stream lane even when its branch does not yet exist. `missing_branch` is a truthful state, not an audit failure or an invitation to invent the still-frozen qualification interface.
