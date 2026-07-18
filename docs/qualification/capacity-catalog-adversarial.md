# Capacity Catalog Adversarial Qualification Note

## Scope

This tests-only lane exercises `CapacityProfileCatalog` as a bounded local catalog. It adds an independent state-machine reference model, a deterministic generated operation corpus, and direct adversarial boundary tests. No production, router, runtime, gossip, bootstrap-preflight, package API, or existing capacity-profile test file is modified.

## Deterministic corpus

- Seeds: `0, 1, 2, 7, 11, 19, 23, 29, 31, 37, 42, 101, 31337, 24301, 42362, 12648430`
- Trace count: 16
- Trace length: exactly 20 operations (minimum 20, maximum 20)
- Total generated operations: 320
- Operation distribution: 162 inserts, 158 resolves
- Serialization: stable sorted compact JSON; canonical profile bytes and source-evidence digests are omitted
- Failure behavior: generated conformance tests deletion-minimize any model/production disagreement and report the seed, first differing observation, and minimized serialized trace

The generated traces compare the production catalog with a reference state machine that imports no production module. The corpus covers add, replay, replace, explicit lookup, deprecation, stale and missing states, compare-and-swap rejection, replacement authorization rejection, capacity exhaustion, exact expiry transitions, backward time, invalid time and TTL values, and source-evidence reuse rejection.

## Direct adversarial coverage

Direct tests additionally cover:

- exact maximum-TTL acceptance, next-representable-float rejection, and exact stale boundary
- boolean, NaN, positive infinity, negative infinity, and oversized-integer time rejection
- documented fail-closed monotonic-time floor: valid caller time remains observed after later TTL, replacement-authorization, CAS, or capacity rejection; complete lookup snapshots and entry counts remain unchanged
- replay at full capacity without TTL extension
- replacement failure at full capacity without partial deprecation
- simulated profile-digest collision without catalog mutation
- immutable direct lineage across three revisions
- stale compare-and-swap lineage rejection
- isolation of all eight slot-identity dimensions
- source-evidence digest exclusion from slot identity and isolation between revisions
- canonical-document parser enforcement, including whitespace, reordered keys, duplicate keys, non-finite JSON numbers, unknown fields, bytes subclasses, wrong types, and oversized documents
- absence of activation methods or authority grants

Every observed result and parsed document remains:

- `route_ready=false`
- `release_ready=false`
- `qualification_evaluated=false`

## Local verification

Run on the isolated branch from base `62a0127` with Python 3.14:

```text
python3.14 -m pytest -q tests/capacity_catalog_adversarial
60 passed in 0.06s

python3.14 -m compileall -q tests/capacity_catalog_adversarial
(exit 0)

git diff --check
(exit 0)
```

Compatibility check:

```text
python3.14 -m pytest -q tests/capacity_profiles
92 passed in 0.04s
```

No production discrepancy was observed, so no minimized RED corpus is present.

## Claim boundary

This note records local software evidence only. It grants no activation or routing authority and does not establish accepted physical qualification. `route_ready=false`, `release_ready=false`, and `qualification_evaluated=false` remain unchanged.
