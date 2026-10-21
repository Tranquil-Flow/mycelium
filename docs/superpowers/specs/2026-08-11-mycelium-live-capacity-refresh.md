# Mycelium Live Model-Capacity Refresh Specification

**Status:** implementation slice
**Parent:** `2026-08-10-mycelium-m17-multimodel-catalog-feasibility.md`

## Outcome

The model catalog can replace expired static feasibility with an explicit fresh
capacity check. The operation captures independently signed resource observations
from the richest currently qualified planned route, verifies their signatures and
directed-edge completeness, inventories only already-local model snapshots, reruns
the capability-aware contiguous exact-weight allocation planner, and atomically
publishes one new model-operation generation.

This operation does not download, provision, activate, qualify, or select a model.
Those lifecycle authorities remain separate.

## Product contract

`GET /__mycelium/model-capacity-refresh` returns the closed
`mycelium.model_capacity_refresh.v1` status. `POST
/__mycelium/model-capacity-refresh/start` accepts exactly an empty JSON object,
requires the same browser origin, starts one background refresh, and returns HTTP
202. Concurrent starts fail closed. Progress phases are signed-resource capture,
local inventory, model evaluation, and atomic publication.

Inference and Settings expose this as “Recheck swarm capacity.” The UI states that
the operation performs no download or provisioning, polls while work is active, and
reloads the catalog when publication succeeds. Refresh/navigation reconstruct state
from the backend.

## Current boundary

Enrollment and capacity admission remain distinct. This slice evaluates members in
the richest current qualified planned route; a newly enrolled standby member is not
silently assigned or counted. A later convergence slice must obtain signed capability
and directed-link evidence from unplaced members, choose a new route, prepare
assignment-local artifacts, and qualify it before that member can unlock a model.

## Verification gate

1. Closed-shape and same-origin HTTP tests pass.
2. Single-flight, bounded failure, atomic publication, and no-partial-update tests
   pass.
3. The local-only inventory and existing planner are invoked from one verified
   evidence generation; unsupported entries are not treated as feasible.
4. The UI shows progress, terminal result, and errors in product language without
   internal milestone labels or private paths.
5. A live recheck publishes a newer catalog generation while leaving downloads,
   provisioning, qualified deployments, and active selection unchanged.
