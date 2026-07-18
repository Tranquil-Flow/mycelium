# Capacity-profile compiler handover

## Scope

Worktree: `/Users/evinova-self/Projects/mycelium-wt-capacity-profiles`

Branch: `feature/capacity-service-profiles`

Base: `5bd2dc0ea8a9f2972a71009cd9d2f65be39b433a`

Verified implementation commit:
`a614bc7b8c702ba832805951079993b99c26f56d`.

This is a disjoint local-evidence tooling tranche. It adds only:

- `mycelium_capacity_profiles/__init__.py`
- `mycelium_capacity_profiles/contracts.py`
- `mycelium_capacity_profiles/compiler.py`
- `mycelium_capacity_profiles/status.py`
- `tests/capacity_profiles/test_profiles.py`

No active agent-lane path was edited. No merge to `main`, push, PR, fetch, pull,
package install, remote-host action, credential access, runtime activation, or
physical qualification occurred.

## Delivered behavior

The compiler transforms bounded empirical observations into a canonical capacity
profile with three separate outputs:

1. `max_safe_concurrency`: highest contiguous observed concurrency before the
   first unsafe observation;
2. `interactive_concurrency_limit`: highest safe contiguous point satisfying the
   configured p95 TTFT and TPOT SLOs;
3. `batch_concurrency_limit`: lowest safe concurrency at maximum observed
   aggregate output throughput.

The profile identity binds:

- model digest;
- source-evidence digest;
- quantization;
- backend and exact runtime build;
- hardware class and power mode;
- context bucket;
- KV mode.

Canonical JSON and `sha256:` profile digests are deterministic. Digest material
includes the complete key, policy, evaluated observations, derived limits, and
boundary summaries. Changing runtime, model, quantization, or source evidence
changes the profile digest.

The compiler fails closed on:

- empty, duplicate, non-contiguous, or non-1-based concurrency observations;
- booleans where exact integers are required;
- non-finite, negative, zero-success, or partially missing measurements;
- safe points below the policy's minimum sample count;
- an unsafe concurrency-1 baseline;
- recommendations above the first unsafe point;
- manually forged derived limits, including Python `bool == int` aliases.

OOM, memory-budget overflow, thermal throttling, and incomplete sample evidence
are unsafe. Failed observations may omit all latency/throughput metrics rather
than fabricate measurements. The first unsafe and first interactive-SLO-miss
boundaries are emitted explicitly.

## STATUS adapter boundary

`status_with_capacity_profile()` is non-mutating and emits a schema-valid
`mycelium.device_status.v1` copy with a compact profile reference. It rejects
wrong protocols and conflicting existing references. It updates
`concurrency_limit` only when the caller passes the exact keyword authorization
`allow_concurrency_limit_update=True`; omission, `False`, and integer aliases
fail closed.

The adapter is a publication helper, not a readiness authority. The emitted
reference always contains `route_ready=false`. No scheduler, router, admission,
or qualifier consumer was connected in this tranche.

## TDD and review ledger

Initial RED covered separate safety/interactive/batch limits, canonical digest
stability and sensitivity, monotonic first-unsafe bounding, failed-observation
metrics, minimum samples, direct-construction forgery, and STATUS adaptation.

First adversarial review found four material omissions, all repaired with
regressions:

1. source-evidence digest missing from the profile key;
2. failed observations required fabricated metric values;
3. no explicit first-unsafe boundary;
4. missing-measurement validation was incomplete.

Second review found no high-severity implementation issue. It identified direct
`dataclasses.replace()` forgery as the remaining relevant structural path and
recommended making STATUS concurrency promotion explicit. Final RED cases then
proved the `bool == int` alias and implicit promotion behavior before both were
closed. Focused final result: 13 passed.

## Observed gates on implementation commit

| Command | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q tests/capacity_profiles/test_profiles.py` | 0 | 13 passed. |
| `python3.14 -m pytest -q` | 0 | 1018 passed, 2 skipped, 117 subtests passed. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `git diff --check` and staged variant | 0 | No whitespace errors. |
| `cargo fmt --check` | 0 | Passed. |
| `cargo clippy --all-targets --all-features -- -D warnings` | 0 | Passed offline. |
| `cargo test` | 0 | 21 passed, 0 failed. |
| `npm run check` | 0 | 70 Vitest plus 3 Node tests passed; typecheck and production build passed. |
| `python3.14 scripts/release_security_audit.py` | 0 | 359 tracked files accepted. |
| `python3.14 scripts/claim_boundary_audit.py` | 0 | 142 source files accepted. |

The UI gate used canonical's existing dependencies through a temporary
`node_modules` symlink, performed no install, and removed the symlink after the
run. Vite emitted only its existing large-chunk advisory.

## Claim boundary and next work

`route_ready=false` and `release_ready=false` remain mandatory. Profiles express
bounded local observations only. They do not prove physical capacity, authorize
routing, attest source evidence, replace qualifier-owned evidence, or establish
cross-host behavior.

Before any production consumer may rely on a profile:

1. freeze a versioned profile/publication contract;
2. bind source-evidence digests to immutable accepted evidence bundles;
3. define freshness, deprecation, and replacement authority;
4. obtain qualifier-owned physical measurements for each exact identity key;
5. add admission-side policy that independently caps, expires, and rejects
   unsupported profiles;
6. prove behavior under physical load, OOM, thermal pressure, reconnects, and
   stale gossip.

No accepted physical capacity evidence exists in this tranche.
