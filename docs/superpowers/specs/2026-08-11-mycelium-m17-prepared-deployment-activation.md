# Mycelium M17 Prepared Deployment Activation Specification

**Status:** implementation target
**Milestone:** M17 lifecycle closure
**Parent:** `2026-08-10-mycelium-m17-multimodel-catalog-feasibility.md`

## Outcome

An operator may place a fully built physical operator plan in one configured private
candidate directory. The live product discovers that plan without a restart, exposes
only privacy-reduced identity and progress, opens and challenges its physical route in
the background, and inserts it into the deployment registry only after the existing
qualifier returns a current `route_ready=true` decision. The incumbent remains active
through every candidate failure. A newly qualified candidate becomes selectable for
future requests; it is never selected implicitly.

This closure does not download, convert, repartition, or repair model artifacts. It
does not accept paths, plans, manifests, credentials, or model bytes from the browser.
It advances an already prepared candidate through the existing load and qualification
gates. Automatic plan construction and assignment-local acquisition remain a later
M17 slice.

## Authorities and safety

- The operator configures one absolute, owner-controlled candidate directory.
- Discovery reads regular, non-symlink JSON files directly below that directory and
  validates each with the physical-runner operator-plan parser.
- Candidate identity is the execution graph's immutable deployment ID. Every node in
  the plan must carry the exact same graph. The public record binds model ID, immutable
  revision, topology size, and a SHA-256 plan digest; it never exposes a local path.
- Activation copies the validated bytes into a private immutable state file before
  work starts. Replacement or removal of the discovery file cannot change an in-flight
  activation.
- One activation runs at a time. Duplicate deployment IDs, changed plan digests,
  stale membership, startup-challenge failure, and qualification rejection fail
  closed. Adding a distinct qualified runtime does not alter existing request bindings.
- Route creation, artifact loading, startup challenge, qualification, and registry
  insertion remain separate phases. Registry insertion accepts only a live runtime
  whose qualification binds its exact deployment and model.
- No failure changes the selected deployment. A partially opened route is closed and
  cleaned by the existing runtime loader.

## Product contract

`GET /__mycelium/deployment-activation` returns
`mycelium.deployment_activation.v1` with a monotonic generation, optional busy
candidate, and bounded candidates. Candidate states are `prepared`, `activating`,
`qualified`, `active`, `unavailable`, or `failed`. Activating records carry one of
`validating_plan`, `opening_route`, `qualifying_route`, or `registering`; failed
records carry one bounded public reason code.

`POST /__mycelium/deployment-activation/start` accepts exactly one public
`candidate_id`, requires same-origin browser access, and returns the updated status
with HTTP 202. Repeating activation for a registered deployment is idempotent.

## UI

Inference and Settings show one product-language model catalog surface, derived from
the catalog, lifecycle, feasibility, and activation authorities rather than a fixed
model list. It identifies model/revision, representation, route size, current swarm
fit, and live activation phase. It permits activation only for prepared or failed
candidates and polls while work is active. When qualification completes it refreshes
the model selector; the user still explicitly chooses the new model. Refresh and
navigation reconstruct state from the backend. Internal milestone names are not
product labels.

## Verification gate

1. Discovery rejects unsafe roots, symlinks, invalid plans, graph disagreement,
   duplicate identities, and private-path projection.
2. Activation is single-flight, digest-bound, asynchronous, idempotent after success,
   and leaves the incumbent selected on every failure.
3. Registry insertion rejects dead, unqualified, mismatched, or duplicate runtimes and
   preserves in-flight request bindings.
4. HTTP tests cover closed request shape, origin enforcement, unavailable service,
   accepted work, and bounded public errors.
5. UI contract tests cover every state, retry, progress, qualification refresh, and
   absence of internal milestone labels.
6. A live browser discovers a prepared route, watches it qualify, then selects it and
   completes a physically distributed request with exact history attribution.
