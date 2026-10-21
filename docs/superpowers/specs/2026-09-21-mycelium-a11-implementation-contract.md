# Mycelium A11 implementation contract (scaffold stage)

**Status:** `scaffold`; resolves implementation ambiguities for gate A11
**Gate:** A11
**Specification:** `2026-08-18-mycelium-a11-target-authoritative-speculative-decoding.md`
**Frozen acceptance inputs:** `tests/a11_acceptance/scenarios.v1.json` (`mycelium.a11_acceptance_scenarios.v1`)
**Baseline:** `a7b412a` on `integration/core-pilot`
**Lane worktree:** `~/Documents/playground/mycelium-a11-lane` (branch `lane/a11-scaffold`)
**Claim boundary:** this document, `mycelium_speculative_decode.py`, and
`tests/a11_scaffold/` are a scaffold: closed contract validation and policy
arithmetic only. Nothing here executes speculation, transfers a proposal,
verifies or commits a target position, runs a benchmark, publishes a decision,
qualifies a deployment, or claims any of the thirteen A11 closure requirements.
The A11 checklist state remains `design_only`.

## 1. Outcome and scope

The A11 specification names four closed records, one policy formula, one circuit
breaker, one benchmark rule, and one decision rule, but leaves implementation
seams undefined. This contract freezes those seams so implementation, tests, and
any parallel lane cannot drift, and the accompanying scaffold binds each frozen
decision to executable validation that either passes or raises a closed code.

Four artefacts exist at this stage:

| Artefact | Path | What it is |
| --- | --- | --- |
| Contract module | `mycelium_speculative_decode.py` | Closed validators, deadline arithmetic, circuit breaker, compatibility fixtures, explicit not-implemented boundaries |
| Contract tests | `tests/a11_scaffold/test_a11_contract_scaffold.py` | Strong-red-first tests for the implemented slice (33 tests) |
| Boundary tests | `tests/a11_scaffold/test_a11_not_implemented_boundaries.py` | Pins for the honest not-implemented boundary contract (17 tests) |
| RED execution tests | `tests/a11_scaffold/test_a11_unimplemented_execution_red.py` | Weak-red markers for the unimplemented execution seams (7 tests, RED by design) |

## 2. Resolved ambiguities

### 2.1 Ownership and naming

- A11-native records replace the unintegrated `mycelium.m20_speculative_*` plan
  and runtime documents as A11 authority. The legacy module
  `mycelium_m20_speculation.py` is **not modified** by this lane; its output may
  not be promoted to an A11 decision (specification section 2), and
  `scripts/qualify_m20_speculation.py` is superseded for this gate rather than
  reused.
- The implementation lives in one capability-named root module,
  `mycelium_speculative_decode.py`, mirroring the single-module precedent of the
  milestone modules. No package split is defined at scaffold stage.
- Protocol names are frozen as written in the specification:
  `mycelium.speculative_decode_plan.v1`,
  `mycelium.speculative_decode_runtime.v1`,
  `mycelium.speculative_decode_benchmark.v1`,
  `mycelium.speculative_decode_qualification.v1`
  (`mycelium_speculative_decode.py:32-35`).

### 2.2 Canonical form, digests, and validation order

- Canonical bytes are `json.dumps(document, sort_keys=True, separators=(",", ":"))`;
  digests are `sha256:` + 64 lowercase hex (`mycelium_speculative_decode.py:180-184`).
- Every record is self-bound through `seal()` (`:242`): the digest field is
  removed, the canonical digest of the remainder is computed, and the digest
  field is set to it. Validation always re-derives the digest and rejects a
  mismatch with the record's generic invalid code.
- Validation order is **shape → semantics → digest** so that semantic
  violations report their specific closed code instead of being masked by a
  stale digest. Negative tests therefore never need to re-seal a document to
  observe the semantic code they target.

### 2.3 Adaptive deadline (specification section 6)

- Frozen constants: minimum `5 ms`, maximum `100 ms`, warm multiplier `0.75`
  (`:62-64`). `admit_proposal_deadline()` (`:268`) is the only place the formula
  lives.
- Clamp order: floor/ceiling clamp first (`floor_applied`, `ceiling_applied`,
  `within_bounds`), then the request-remaining-deadline clamp
  (`request_deadline_clamp`).
- **An unknown warm target-only p95 is never replaced by a default.** The result
  is `admissible = false`, `admitted_deadline_ms = null`, and
  `warm_target_only_p95_tpot_unknown` (`:65`, `:279-284`). A missing warm
  baseline cannot admit speculation.
- An unknown remaining request deadline is likewise inadmissible
  (`request_deadline_unknown`); a non-positive remaining deadline reports
  `request_deadline_expired`.
- A `planner_prediction` deadline source is rejected at plan validation with
  `planner_prediction_not_authoritative`; only `measured_warm` is admitted
  (`:589-590`).

### 2.4 Circuit breaker (specification section 6)

- `circuit_breaker_decision()` (`:310`) returns a closed triple
  `{state, reason, cooldown_seconds}` with states
  `closed | open_target_only | revoked_requalification_required` (`:70`).
- Precedence: any compatibility/parity violation → `revoked_requalification_required`
  (`cooldown_seconds = null`, requalification required); else three consecutive
  proposal deadlines → `open_target_only` with the frozen
  `300 s` cooldown; else a 20-cycle rolling window whose observed speculative
  cycle time is **not better** than the equivalent target-only time →
  `open_target_only` with cooldown; else `closed/within_policy`.
- Cycles with unknown timing (`null`) are excluded from the rolling window.
  A window that cannot supply twenty complete observations reports
  `closed/insufficient_performance_observations` — it can never open the breaker
  on absent data (`:359-360`).

### 2.5 Decision vocabulary and missing measurements

- The disabled reason set is exactly the eight reasons frozen in
  `tests/a11_acceptance/scenarios.v1.json` (`:39-50`). This lane does not extend it.
- The eight reasons partition into:
  - capability (`no_authorized_local_draft`, `target_verifier_unavailable`):
    no performance measurement may be attached, `sample_count` must be `0`, and
    each measurement must be `null`;
  - correctness (`incompatible_token_semantics`, `parity_failed`): no
    performance measurement may be attached;
  - performance (`insufficient_acceptance`, `draft_deadline_exceeded`,
    `material_gain_not_observed`, `confidence_bound_not_met`): `sample_count >= 1`
    is required, and `material_gain_not_observed` / `confidence_bound_not_met`
    additionally require the corresponding measurement to be present and below
    the frozen threshold.
- A capability or correctness reason carrying a measurement raises
  `capability_reason_may_not_carry_measurement`; a performance reason without a
  real sample raises `measurement_required_for_performance_reason`. Zero is not
  a substitute for unknown (`:526-541`).
- An enabled decision requires: qualified A10 verifier binding, every
  compatibility fact explicitly `true` (unknown is not compatible),
  `deterministic_greedy` generation policy, a complete measured window, and
  measured reduction and paired lower bound both at or above `0.10` with TTFT
  regression at or below `0.10` (`:512-523`).
- Unsupported or unknown generation policy cannot be enabled; a disabled plan
  may carry it only under `incompatible_token_semantics` (`:497-499`).

### 2.6 Benchmark rule (specification section 7)

- Frozen order of measured window modes is
  `target_only, speculative, speculative, target_only, target_only, speculative`
  (the `AB`, `BA`, `AB` pairing expressed in mode names), six windows, one
  unscored warmup per mode, confidence `0.95`, minimums of 12 completed requests,
  384 target-owned committed tokens, and 2 prompt-length plus 2 output-length
  buckets per window (`:72-107`).
- Declared measurements are recomputed from the windows and must agree within
  `1e-9`, and the paired lower bound must not exceed the point estimate; a
  mismatch raises `benchmark_decision_inconsistent`. Thresholds are inspected
  against the frozen constants only, so a threshold cannot move after
  measurements are seen (`:946-986`).
- Invalid pairs (any closed invalidation reason) carry no measurements: the
  decision is `measured_disabled` with reason `confidence_bound_not_met` and the
  closed blocking condition `benchmark_pair_invalid`. The blocking-condition
  sub-vocabulary (`material_gain_below_threshold`, `paired_lower_bound_below_threshold`,
  `ttft_regression_above_limit`, `benchmark_pair_invalid`) exists so that a
  TTFT-driven failure is never mislabelled as a gain failure inside the frozen
  eight-reason vocabulary (`:100-107`).
- An unexecuted benchmark (`executed: false`) must carry no windows, the single
  invalidation reason `missing_samples`, and null measurements: a not-executed
  record claims nothing (`:928-938`).

### 2.7 Runtime invariants (specification sections 4 and 5)

`validate_speculative_decode_runtime()` (`:788`) enforces:

- `accepted_count + rejected_count <= verified_count <= proposed_count`
  (`impossible_runtime_counts`);
- `target_corrected_count <= rejected_count` and
  `rollback_count <= proposed_count - accepted_count` (`impossible_runtime_counts`);
- `committed_count == accepted_count + target_corrected_count` and
  `target_committed_output_tokens == committed_count`
  (`committed_positions_not_target_authoritative`) — every committed position is
  target-verified or target-corrected exactly once and emitted exactly once;
- **Amendment (owner ruling 2026-10-07, target bonus):** a request may carry an
  optional `target_bonus_count`, the target's own decision committed after an
  all-match span (drawn by the target at the position after the last candidate,
  exactly as an ordinary DECODE would draw it). Then
  `committed_count == accepted_count + target_corrected_count + target_bonus_count`,
  `target_bonus_count <= accepted_count` (`impossible_runtime_counts`), and a
  `target_only` request carries none. A document without the field means zero.
  The bonus is never taken when it would complete the request, so the last
  token stays an ordinary terminal DECODE;
- a `target_only` request carries zero proposals and no deadline/draft timing;
  a `target_only` runtime document cannot contain a speculative request
  (`target_only_mode_carries_proposals`);
- `target_loss` is **not** representable as a draft fallback reason
  (`target_loss_is_not_draft_fallback`); target loss is the terminal state
  `target_lost_explicit`;
- `cancelled` and `terminal_state` agree, and `cleanup_complete` is `true`.

### 2.8 Proposal bounds

The specification requires bounded width/count/bytes but fixes no number. This
contract freezes load-time bounds of width `<= 16`, count `<= 512`, and bytes
`<= 8192` with `count >= width` and `bytes >= count` (`:115-117`). These are
scaffold-stage numbers chosen for boundedness, not measured limits; they are
flagged for owner review before any physical admission path exists.

### 2.9 Qualification scaffold

`validate_speculative_decode_qualification()` (`:1039`) can represent exactly one
thing: an unexecuted record (`executed: false`, `result: "not_executed"`, null
browser workspaces, null negative gates, null projection). A record claiming
execution raises `qualification_execution_requires_physical_pipeline`. Execution
cannot be validated into existence.

### 2.10 Privacy boundary

- The plan's privacy statement is a frozen literal (`:109-113`).
- `validate_public_projection()` (`:1025`) refuses any key containing private
  markers (prompt, output text, raw token ids, logits, tensors, activations, KV
  bytes, sampler state, credentials, keys, paths, command lines, hostnames,
  addresses, exception strings) with `public_projection_carries_private_fields`.
- Compatibility fixtures claim nothing: the benchmark fixture is unexecuted with
  null measurements, the qualification fixture is `not_executed` with a null
  projection, and the runtime fixture carries only a zero-count target-only
  request (`:1081-1237`).

### 2.11 Not-implemented boundary contract

- Execution seams raise `SpeculativeDecodeUnavailable` (`:168`) with a closed
  code from `BOUNDARY_CODES` (`:1244`): A10 target verification, private proposal
  transfer, target KV commit, live-route benchmark, physical qualification,
  registry admission, and product projection (`:1265-1304`).
- A boundary call **never returns a document**; a plausible payload changes the
  detail string, never the outcome. The boundary error is a `RuntimeError`, not
  a `ValueError`, so it cannot be confused with a contract violation.
- Each closed boundary code maps to exactly one function, and the test suite
  asserts the mapping is total and unique.

## 3. Implemented vs not implemented

| Slice | State | Evidence |
| --- | --- | --- |
| Closed plan/runtime/benchmark/qualification validation | implemented | `mycelium_speculative_decode.py:544,788,877,1039` |
| Canonical self-digest `seal()` | implemented | `:242` |
| Adaptive deadline arithmetic | implemented | `:268` |
| Circuit-breaker policy decision | implemented | `:310` |
| Privacy-reduced projection check | implemented | `:1025` |
| Compatibility fixtures that claim nothing | implemented | `:1081` |
| Proposal transfer, A10 verification, target KV commit | **not implemented** (raises) | `:1265-1281` |
| Live same-route paired benchmark execution | **not implemented** (raises) | `:1283` |
| Physical positive/negative and browser gate execution | **not implemented** (raises) | `:1289` |
| Registry admission / eight-workspace product projection | **not implemented** (raises) | `:1295-1304` |
| Any rendered product surface | **not implemented** | — |

## 4. Verification and RED protocol

Commands (run from the lane worktree, clean interpreter environment):

```
env -u PYTHONPATH -u PYTHONHOME /opt/homebrew/bin/python3.14 -m pytest -q tests/a11_scaffold
```

- At baseline `a7b412a` (tests present, module absent) both strong and boundary
  modules fail to collect:
  `ModuleNotFoundError: No module named 'mycelium_speculative_decode'`.
- With the scaffold present the suite reports `50 passed, 7 failed`:
  - **strong RED → GREEN (50):** every test in
    `test_a11_contract_scaffold.py` (33) and
    `test_a11_not_implemented_boundaries.py` (17) fails at `a7b412a` because the
    behaviour does not exist, and passes once the scaffold lands.
  - **weak RED (7):** every test in
    `test_a11_unimplemented_execution_red.py` fails because this lane's own
    boundary raises `SpeculativeDecodeUnavailable`. These are honest
    incompleteness markers, **not** evidence that the baseline lacks the
    behaviour.
- Baseline behavioural probes (ad-hoc, run at `a7b412a` before any file was
  added) confirm the strong half is absent behaviour, not merely a missing file:
  - no code defines any `mycelium.speculative_decode.*` protocol;
  - the legacy runtime validator accepts `verified_count(5) > proposed_count(1)`
    (the A11 upper invariant is unenforced at baseline);
  - the legacy runtime shape rejects every deadline/circuit-breaker field
    (`deadline_ms`, `deadline_outcome`, `target_corrected_count`,
    `circuit_breaker_state`, ... all `m20_runtime_invalid`);
  - no deadline, breaker, or benchmark function exists in the legacy module.

## 5. Closure requirements

The thirteen closure requirement names a gate must bind
(`scripts/mycelium_completion_audit.py:95-109`): `specification`,
`implementation`, `product_integration`, `deterministic_positive`,
`deterministic_negative`, `physical_positive`, `physical_negative`,
`ui_live_all_eight`, `browser_navigation_reconnect`, `browser_second_session`,
`full_regressions_audits`, `architecture_handover`, `atomic_feature_commit`.

**None is earned by this work.** `specification` is already complete at
`a7b412a`. The scaffold advances no requirement to a completed state; the
checklist is untouched and A11 remains `design_only` with its existing blockers
(`dependency A10 atomic completion`, `eligible local draft/target physical
route`). In particular:

- the scaffold is not `implementation`: the execution seams raise;
- it is not `deterministic_positive`/`deterministic_negative`: no runtime
  scenario executes;
- it is not `physical_positive`/`physical_negative`, `ui_live_all_eight`,
  `browser_*`, `full_regressions_audits`, `architecture_handover`, or
  `atomic_feature_commit`: none of those artefacts exist;
- fixtures and validators claim nothing, so no evidence binding may cite them.

## 6. What this work does not prove

1. That speculative decoding works, is beneficial, or is safe to enable — no
   token was proposed, verified, committed, or emitted by this lane.
2. That any A11 record can be produced from a real target/draft/verifier
   binding; the frozen digests are placeholders.
3. That the adaptive deadline improves anything; only the arithmetic is frozen.
4. That the circuit breaker behaves under a live request; only the pure decision
   over synthetic cycles is implemented.
5. That the benchmark rule can be satisfied on the real route; the paired-window
   validator is exercised on synthetic windows only.
6. That A10 verifier capability exists or is bound; the verifier seam raises
   `a10_target_verifier_not_available`.
7. That any closure requirement, checklist state, or gate blocker changed.
8. That the weak-red execution tests will pass without the physical pipeline;
   they are expected to fail until real execution lands.
