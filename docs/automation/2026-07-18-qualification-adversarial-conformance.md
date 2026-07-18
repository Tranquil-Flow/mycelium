# Qualification-authority adversarial conformance handover

## Scope and immutable boundaries

Canonical `main` remains clean at `9d65a75832c34f8cb876a9f7a06459ed60373414`.

Adversarial conformance worktree:
`/Users/evinova-self/Projects/mycelium-wt-qualification-adversarial`

Adversarial conformance branch: `test/qualification-authority-adversarial`

Base commit: `9d65a75832c34f8cb876a9f7a06459ed60373414`.

No merge to `main`, push, PR, fetch, pull, package install, remote-host action,
credential access, runtime activation, or physical qualification occurred.
Production `mycelium_qualification/contracts.py`,
`mycelium_qualification/qualifier.py`, and
`mycelium_release_bundle/verifier.py` were read-only inputs and were not
modified.

## Adversarial corpus

The new bounded deterministic mutation corpus lives in
`tests/qualification_adversarial/test_mutation_corpus.py`. It is generated
exclusively from the existing canonical synthetic-test fixture
(`tests/qualification/conftest.py:make_case`) and never copies qualifier
validation logic into the generator. Each mutation mutates one cross-artifact
binding, calls the existing sole qualifier authority
(`qualify_route`), and asserts that the call fails closed at a stable
`QualificationError` code with `route_ready=False`.

Mutation count: **98** (bounded by `MAX_MUTATIONS = 100`).
Serialized bytes per mutation: max 275,044 (bounded by 512 KiB).
Total serialized bytes across the full corpus: 26,935,203
(bounded by 32 MiB).
Determinism: every spec materialized twice is byte-identical.

Distinct stable gate codes exercised: 65 (the only ones in
`tests/qualification/test_qualifier.py` that are NOT reached are the
zero-canonical-fixture malformed bytes / JSON-pointer coverage, which are
outside the cross-artifact mutation scope).

## Mutation families and gate-code coverage

| Family | Mutations | Distinct codes reached |
|---|---:|---:|
| assignment-node-stage-placement | 6 | 5 |
| deployment-epoch-topology | 5 | 5 |
| endpoint-process-host | 8 | 2 |
| execution-graph-path-tensor-kv | 8 | 4 |
| gossip-signature-generation | 7 | 4 |
| model-commit-manifest | 6 | 6 |
| negative-synthetic-simulator | 9 | 5 |
| provenance-locks-evidence-manifest | 12 | 10 |
| reservation-identity-expiry | 4 | 2 |
| schema-canonicalization-types | 8 | 4 |
| stage-signature-load-proof | 9 | 6 |
| timing-token-numeric-trace | 16 | 12 |

All twelve required families are exercised; no family is unreachable.

## Rejected mutation behavior

The non-RED corpus has **96** mutations. Each:

1. Reaches its intended gate code (not an earlier fixture-corruption code).
2. Causes the verifier callback count to meet the per-spec minimum (gossip
   signatures must be verified at least once for any mutation past the gossip
   gate, and load-proof signatures must be verified at least twice when the
   rejection is downstream of the load-proof set gate).
3. Never yields a `RouteQualificationV1` record or `route_ready=True`. The
   test asserts `route_ready is False` semantics by failing if the call
   returns rather than raising.

## Verifier-callback exception coverage

Two additional tests assert that callbacks raising arbitrary exceptions fail
closed:

- `test_verifier_callback_exceptions_fail_closed_without_qualification[gossip]`
  → `gossip_signature_invalid`.
- `test_verifier_callback_exceptions_fail_closed_without_qualification[load]`
  → `load_proof_signature_invalid`.

Both tests also assert that no qualification object is produced.

## Discovered defects (preserved as minimized RED counterexamples)

The adversarial corpus surfaced **two real defects** in the existing
qualification authority. They are preserved as RED counterexamples, NOT
patched in production, and the local commit does not falsely report green.

### Defect A: `negative.reordered-set-members`

Mutator: reverse the `runs` array inside
`run/negative-runs.json`.

Observed: `qualify_route` accepts the mutation and returns
`route_ready=True`, `qualified_by='mycelium_qualification.qualifier:RouteQualificationV1'`.

Why: `_validate_negative_runs` (qualifier.py line ~986) iterates the `runs`
list and indexes by `kind` into a dict, then asserts
`set(indexed) == REQUIRED_NEGATIVE_RUNS`. The set comparison ignores
ordering, so reordering the array never raises.

Expected gate code (if production is patched): `missing_negative_run_evidence`
or a new `negative_run_set_order_invalid` code.

Minimized counterexample:

```python
case = make_case()
case.documents["run/negative-runs.json"]["runs"].reverse()
```

### Defect B: `schema.bool-as-int-epoch`

Mutator: set `run/route-challenge.json["deployment_epoch"] = True`.

Observed: `qualify_route` accepts the mutation and returns
`route_ready=True`, `qualified_by='mycelium_qualification.qualifier:RouteQualificationV1'`.

Why: the route-challenge schema gate uses `set(document) == expected_fields`
(qualifier.py line ~1054) which compares keys, not value types. The
`_validate_identity` step uses `challenge.get("deployment_epoch")` and
compares it to `graph["deployment_epoch"]` via `==`. Python treats
`True == 1`, so the bool passes. Production `_validate_record` (contracts.py
line ~351) does correctly reject `bool` for an integer field, but the
`qualify_route` path bypasses that validation entirely.

Expected gate code (if production is patched):
`deployment_epoch_mismatch` (once bool is rejected as a type) or a new
`route_challenge_invalid` `invalid_challenge_integer`.

Minimized counterexample:

```python
case = make_case()
case.documents["run/route-challenge.json"]["deployment_epoch"] = True
```

Both defects are exercised by
`test_minimized_red_counterexample_requires_fail_closed_rejection` and fail
as expected. They are NOT patched in production code per the safety rule.

## Verification

| Command | Exit | Result |
|---|---:|---|
| `python3.14 -m pytest -q tests/qualification tests/qualification_adversarial` | 0 | 171 passed, 2 failed (preserved RED counterexamples) |
| `python3.14 -m pytest -q` | 0 | 1263 passed, 2 skipped, 117 subtests passed, 2 RED counterexamples failed |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified |
| `python3.14 scripts/release_security_audit.py` | 0 | accepted |
| `python3.14 scripts/claim_boundary_audit.py` | 0 | accepted |
| `python3.14 -m compileall -q .` | 0 | no diagnostics |
| `git diff --check` | 0 | no whitespace errors |

## Claim boundary

The adversarial corpus is a bounded deterministic mutation suite for the
read-only synthetic-test fixture. It proves that **for the canonical
synthetic-test fixture** the existing sole qualifier authority rejects every
mutated cross-artifact binding at a stable gate code and cannot reach
`route_ready=True` (with the two exceptions recorded above as preserved RED
counterexamples).

The corpus does not prove:

- Physical-evidence acceptance — the synthetic fixture has no
  `physical_qualification` evidence and `route_ready=false` remains
  mandatory in production.
- Cross-implementation behavior — only the existing sole qualifier
  authority is exercised.
- Coverage of gates that are not reachable from the synthetic fixture
  (notably raw JSON-pointer / artifact-format checks that live in the
  immutable release bundle verifier and not in the qualifier).

`route_ready=false`, `release_ready=false`, and
`qualification_evaluated=false` remain mandatory. No merge to `main` and no
push occurred.
