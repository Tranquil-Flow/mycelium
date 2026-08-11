# Mycelium local-only model preparation specification

**Status:** implementation target
**Parent:** `2026-08-10-mycelium-m17-multimodel-catalog-feasibility.md`

## Outcome

A compatible model that is already complete in the operator's local cache can move
from a fresh `feasible` result to an immutable prepared physical route from the model
catalog. Preparation uses the exact contiguous allocation in that result, gives each
peer only its assignment-authorized checkpoint files plus shared runtime material,
verifies every transferred byte, and publishes an operator plan into the existing
activation directory. It never downloads, activates, qualifies, or selects a model.

Preparation, activation, qualification, and selection remain separate user-visible
states. The active deployment is never changed by preparation failure.

## Authority and safety

- The browser supplies only exact public `model_id` and immutable `revision` values.
- The service resolves both against the current server-owned catalog and configured
  local cache; it never accepts paths or checkpoint bytes from the browser.
- Work starts only from a current `feasible` report with
  `provisioning_authorized=true`. Model identity, evidence generation, feasibility
  digest, stage order, layer ranges, backend, and decode mode are frozen in a private
  preparation authorization document.
- A changed/expired capacity generation fails before model copying or peer staging.
- The assignment compiler binds the authorization digest into every assignment.
- Preparation rewrites a local dense Qwen checkpoint into assignment-addressable
  stage shards, verifies exact tensor ownership, then transfers only each peer's
  allowed stage files. Shared code/config/tokenizer assets may be present on every
  peer. Full-checkpoint replication is forbidden.
- Existing acquisition/staging uses temporary files, exact byte/digest validation,
  atomic promotion, bounded failure, and warm reuse. The incumbent route and registry
  selection are untouched.
- Candidate publication is the last atomic operation. The existing activation service
  must independently open, load, challenge, qualify, and register it.

## Product contract

`GET /__mycelium/model-preparation` returns
`mycelium.model_preparation.v1`. `POST /__mycelium/model-preparation/start` accepts
exactly `{model_id, revision}`, is same-origin, single-flight, and returns HTTP 202.
Public state is `idle`, `preparing`, `succeeded`, or `failed`; preparing phases are
`validating_capacity`, `compiling_assignments`, `verifying_local_artifacts`,
`staging_peers`, and `publishing_candidate`. Status contains no local paths, host
addresses, credentials, prompts, or model bytes.

The model catalog offers **Prepare on swarm** only for a current feasible identity.
It polls and renders the preparation phase, verified/transfer byte counts when known,
and bounded failure reasons. After success, the same row becomes **Ready to activate**;
qualification and selection remain explicit later actions. All copy says that no
download is authorized.

## Verification gate

1. Contract tests cover closed request/status shapes, path privacy, stale evidence,
   identity mismatch, incompatible/infeasible models, single-flight, and bounded
   public failures.
2. Builder tests prove the exact feasibility stage order/ranges/backends are used and
   the authorization digest is assignment-bound.
3. Acquisition/staging tests prove assignment-local manifests, no download, warm
   reuse, interrupted transfer behavior, digest failure, and no candidate publication
   on partial failure.
4. UI tests cover prepare availability, every progress phase, failure/retry, refresh,
   transition to prepared activation, and absence of internal milestone labels.
5. A complete already-local larger model is prepared across physical peers, activated,
   qualified, explicitly selected, and completes a browser request with exact model,
   revision, deployment, topology, and frame attribution.
