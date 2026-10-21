# Mycelium local-only model preparation specification

**Status:** implementation target (representation-authorization amendment)
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

- The browser supplies an exact, closed
  `mycelium.model_representation_decision.v1` document. It binds `model_id`, immutable
  `revision`, source quantization, serving dtype, serving quantization, and the target
  representation digest shown to the owner. A representation-changing preparation
  additionally requires `conversion_authorized=true`; absence is a hard refusal, not a
  default.
- The service resolves both against the current server-owned catalog and configured
  local cache; it never accepts paths or checkpoint bytes from the browser.
- Work starts only from a current `feasible` report with
  `provisioning_authorized=true`. Model identity, evidence generation, feasibility
  digest, serving-representation digest, source and serving quantization, stage order,
  layer ranges, backend, and decode mode are frozen in a private preparation
  authorization document.
- The preparation service verifies that every representation field in the owner
  decision exactly matches the current feasibility report before it creates its
  private authorization. The canonical owner-decision digest is bound into that
  authorization and therefore into the assignment control-plane binding. A changed
  representation requires a fresh feasibility report and a fresh owner decision.
- Feasibility accounts separately for steady-state resident memory and the modeled
  peak while the loader materializes float32 source tensors and the row-wise int8
  serving representation. The larger value, plus runtime workspace, is the admission
  requirement. A source checkpoint that fits but cannot be converted safely is
  rejected as `insufficient_load_memory` before model copying or peer staging.
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
exactly:

```json
{
  "decision": {
    "protocol": "mycelium.model_representation_decision.v1",
    "model_id": "owner/name",
    "revision": "40-character immutable commit",
    "source_quantization": "bfloat16",
    "serving_dtype": "float32",
    "serving_quantization": "int8-weight-only",
    "representation_digest": "sha256:…",
    "conversion_authorized": true
  }
}
```

The endpoint is same-origin, single-flight, and returns HTTP 202. The decision contains
no path and cannot authorize a download. The server rejects missing, extra, stale, or
mismatched fields and rejects a representation change unless the affirmative boolean is
present. This browser action records authorization only for the exact displayed
representation; it is not permission for another model, revision, representation, or
future feasibility generation.
Public state is `idle`, `preparing`, `succeeded`, or `failed`; preparing phases are
`validating_capacity`, `compiling_assignments`, `verifying_local_artifacts`,
`staging_peers`, and `publishing_candidate`. Status contains no local paths, host
addresses, credentials, prompts, or model bytes.

The model catalog first offers **Review representation** for a current feasible
identity. An inline confirmation displays the exact source-to-serving transformation,
immutable model revision, and shortened representation digest. Its authorization
checkbox is initially clear. Only after the owner checks it does the UI offer
**Authorize representation and prepare**. An unchanged source/serving representation
uses **Confirm representation and prepare**, but still sends the same exact binding.
It polls and renders the preparation phase, verified/transfer byte counts when known,
and bounded failure reasons. After success, the same row becomes **Ready to activate**;
qualification and selection remain explicit later actions. All copy says that no
download is authorized.

## Verification gate

1. Contract tests cover closed request/status shapes, path privacy, missing owner
   authorization, extra fields, stale evidence, identity or representation mismatch,
   conversion refusal, incompatible/infeasible models,
   source-fit/load-peak rejection, single-flight, and bounded public failures.
2. Builder tests prove the exact feasibility stage order/ranges/backends and authorized
   representation are used, the owner-decision digest is assignment-bound, and an int8
   build from a BF16-only or mismatched authorization is rejected before artifact work.
3. Acquisition/staging tests prove assignment-local manifests, no download, warm
   reuse, interrupted transfer behavior, digest failure, and no candidate publication
   on partial failure.
4. UI tests cover closed-by-default representation review, affirmative conversion
   authorization, exact request binding, every progress phase, failure/retry, refresh,
   transition to prepared activation, and absence of internal milestone labels.
5. A complete already-local larger model is prepared across physical peers, activated,
   qualified, explicitly selected, and completes a browser request with exact model,
   revision, deployment, topology, and frame attribution.
