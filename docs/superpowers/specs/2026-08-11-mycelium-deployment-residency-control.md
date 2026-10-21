# Mycelium qualified deployment residency control specification

**Status:** implemented; unload physically verified
**Parent:** `2026-08-10-mycelium-m17-multimodel-catalog-feasibility.md`

## Outcome

An operator can release the memory and processes of a qualified, non-selected
candidate-backed deployment without deleting its immutable prepared plan. The model
returns to **Ready to activate**, so it can be loaded and independently qualified
again later. This makes a local model catalog useful on a finite swarm: qualified
choices need not all remain resident simultaneously.

## Safety contract

- The active deployment cannot be unloaded. The operator must explicitly select a
  different qualified deployment first.
- A deployment with an admitted request or candidate canary cannot be unloaded.
- Only a candidate discovered from the owner-controlled candidate directory can use
  the product unload action. Initial boot routes remain resident because the service
  does not yet have a prepared reactivation plan for them.
- Unload closes every stage process and transport owned by that route, removes the
  runtime from the serving registry, and atomically persists the reduced registry.
  It does not delete the candidate plan, staged model files, local source cache,
  history, qualification evidence, or another deployment.
- No model download, model selection, preparation, or capacity refresh is implied.
  A subsequent capacity check must use fresh post-unload observations.

## Product contract

`POST /__mycelium/deployment-activation/unload` is a same-origin, closed-shape
operation accepting exactly `{candidate_id}`. Success returns the existing
`mycelium.deployment_activation.v1` projection with the candidate in `prepared`
state. Busy, active, unknown, or failed-close cases return bounded reason codes.

The model catalog and prepared-deployments views show **Unload from memory** only for
a qualified candidate-backed standby. Active models remain labelled **Selected**.
After unload, all model controls refresh and the capacity control explains that a new
capacity check is required before another preparation decision.

## Verification gate

1. Registry tests prove only an idle non-selected runtime can be removed and that its
   physical route is closed.
2. Activation tests prove a qualified candidate returns to prepared and can be
   reactivated and requalified from the same immutable plan.
3. HTTP tests prove same-origin and exact request shape.
4. UI tests prove the action is visible only for a qualified standby and dispatches
   the exact candidate ID.
5. A physical candidate is unloaded, its process memory is released, capacity is
   refreshed, and it can subsequently be activated again.

The three-host Qwen2.5-3B candidate passed the implemented unload portion of gate 5:
all three stage processes closed, the selected deployment remained unchanged, and the
candidate returned to `prepared` with its plan retained. The privacy-reduced record is
`contracts/compatibility-fixtures/deployment-residency-physical-v1.json`. A fresh
post-unload capacity refresh remains blocked because capability evidence is currently
owned by a loaded planned route; independent standby-node resource probing is the next
required capability rather than an inferred success.
