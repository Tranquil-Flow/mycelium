# Mycelium A10 implementation contract

**Status:** `design_only` (scaffold + strong-RED acceptance suite; no behaviour, no
closure requirement, no state promotion)

**Gate:** A10

**Acceptance base:** `a7b412a841bd92138671429966478918bfce83f0`
("fix(a13-macos): a saturated app answers 503 instead of closing silently (R1)")

**Refines:** `2026-08-18-mycelium-a10-runtime-batching-overlap-target-verification.md`
(digest `sha256:63882c82049f62cff6d2a1e4c5478934963e035a91c36045c4c809b28feb368e`)

**Frozen acceptance inventory:** `tests/a10_acceptance/scenarios.v1.json`
(digest `sha256:0a9f5ebd1f22de5b87f29de057c99c3219d40664fd42c6ad26d2779220c6d67a`)

**Machine source:** `mycelium_router/a10_runtime_batch.py`,
`mycelium_router/a10_target_verification.py`

**Boundary suite:** `tests/a10_scaffold/test_a10_scaffold_boundary.py`
**Strong-RED suite:** `tests/a10_scaffold/test_a10_strong_red_absent_behaviour.py`

This document does not change the A10 specification, the acceptance inventory, any
contract or manifest, or the completion checklist. It resolves the implementation
seams the specification left open, names the surfaces an implementation lane must
provide, and states exactly what the scaffold refuses to do. Nothing here earns a
closure requirement: A10 remains `design_only` until the ordinary browser product
path passes every physical positive and negative gate of specification section 10.

## 1. Claim boundary of this step

- The scaffold implements **no** runtime batching, **no** continuous-arrival
  scheduling, **no** pipeline-overlap verdict, **no** target-verification operation,
  **no** sequential parity oracle and **no** qualification.
- Every declared entry point validates its closed input shape and then raises
  `A10NotImplementedError` with the fixed message `not_implemented:<code>`. It never
  returns a value and never records success.
- The incumbent path is untouched: the target-only route remains authoritative, the
  relay still proposes batches, and the target runtime still serves them with its
  existing sequential loop. The scaffold adds no production wiring.
- No closure requirement of `scripts/mycelium_completion_audit.py` is claimed. The
  scaffold's `EARNED_CLOSURE_REQUIREMENTS` is empty and the boundary suite asserts
  that it stays empty against the audit script's canonical thirteen names.

## 2. Resolved seams (normative)

Each resolution binds a future implementation lane. The named record, accessor or
error code is the contract; renaming it is a contract change, not an implementation
detail.

1. **Proposal versus observation.** The relay's drained `RuntimeBatch` is a
   *proposal*; `BatchDecision` stays proposal-only. Actual membership exists only in
   `mycelium.runtime_batch_observation.v1`, produced by the stage runtime, carrying
   `membership_authority` (`"stage_runtime"`), `backend_invocations`,
   `proposed_member_count`, `actual_member_count` and one outcome per actual member.
   The relay may never synthesize membership from the size it popped.
2. **What "one real backend invocation" means.** `backend_invocations == 1` counts
   real compute at the port boundary for the members it accepted — not calls to the
   public per-item `execute()`. A port that rejects or splits a proposed group must
   report the split (`backend_invocations > 1` or
   `actual_member_count < proposed_member_count`); a split group is not a runtime
   batch. `RuntimeBatchObservation.claims_runtime_batch()` is the single predicate
   for the inventory's `one_real_backend_invocation_has_multiple_members` invariant.
3. **Dispatch-mode honesty.** The test-only sequential double keeps reporting
   `dispatch_mode == "sequential_dispatch"` forever; it may never report a batch.
   Any surface that reports `runtime_batch` must also report the observation fields
   of resolution 1, so a mode label can never travel without its evidence.
4. **Verifier commit arithmetic.** One target backend call evaluates a declared
   bounded width `W`. It may accept `k <= W` verified positions. The optional
   correction position is the next one, drawn from the same call:
   `accepted_prefix_count + correction_count <= proposed_position_count`, and
   `correction_count in {0, 1}`. Commit is impossible without an accepted prefix
   count or a single correction; `rollback_to_prefix` commits nothing and restores
   the exact supplied prefix position.
5. **Qualified limits are qualification-owned.** The capability's `maximum_positions`
   and `maximum_bytes` are the exact qualified runtime's bounds, and a capability
   record cannot exist without a `qualification_binding` digest. Unqualified or
   unsafe Settings values are disabled with a reason; they are never defaulted.
6. **Parity compares decisions, not tensors.** The sequential oracle compares
   per-position target decisions and the final committed prefix position/digest.
   Logits, tensors, activations, KV bytes and candidate token IDs stay on the
   authenticated private path and never enter public evidence.
7. **Late arrivals are measured, not assumed.** A late arrival is a request whose
   collection begins after the first decode iteration of an already-active member on
   the same placement; it joins only at an iteration boundary and never mutates
   another request's committed prefix or event order.
8. **Cancellation points are named.** `pre_collection`, `pre_launch`, `post_return`,
   `pre_forward` and `command_cancellation`. A member cancelled before launch is
   removed; a member that cannot be removed after launch may finish bounded compute
   whose result is discarded, with only that member's KV rolled back.
9. **Overlap is a subtraction, not an intersection.** A pair of stage-compute
   intervals from different microbatches (no causal path between them) is overlapping
   only when `min(finish_a, finish_b) - max(start_a, start_b) - (uncertainty_a +
   uncertainty_b) > 0`. Queueing, serialization, transport and UI overlap never
   qualify.
10. **Missing evidence is absent.** A missing observation is `unknown`/absent — never
    zero, never success. The scaffold never fabricates an observation to make a
    surface look complete.
11. **Where records live.** The two A10 modules own the records while the gate is a
    scaffold. Moving a record into the shared `mycelium_router/contracts.py`, and
    registering the six protocols in a wire contract or the contract manifest, are
    owner-reviewed steps of the implementation lane — this lane deliberately changes
    no shared surface.
12. **No hidden changes.** The paired benchmark may change only the declared
    batching/overlap policy; backend, precision, materialization, placement and
    transport stay frozen. The scaffold adds no policy switch, so no measurement can
    bind to it.

## 3. Scaffold boundary (what is implemented, and what is refused)

Declared entry points and their fixed refusal codes:

| Entry point | Refusal code | Real behaviour the lane must supply |
| --- | --- | --- |
| `execute_observed_batch` | `runtime_batch_execution_absent` | one real backend invocation over accepted members; runtime-owned observation; one isolated result per member |
| `observe_pipeline_overlap` | `pipeline_overlap_observation_absent` | uncertainty-aware overlap verdict with causal rejection (specification section 6) |
| `qualify_runtime_batch` | `runtime_batch_qualification_absent` | owner-private executed qualification bundle |
| `verify_candidate_positions` | `target_verification_operation_absent` | one target backend call over bounded positions; commit/rollback transaction |
| `sequential_parity_reference` | `sequential_target_parity_reference_absent` | sequential target authority over the same prefix |
| `qualify_target_verifier` | `target_verification_qualification_absent` | verifier correctness matrix over one exact target runtime |

Implemented as real, testable behaviour (and claimed as nothing more):

- coded shape validation for a proposal, a verification request, an outcome, a
  capability, an interval, a trace and a qualification request;
- `unsupported_generation_policy` rejection at admission: anything other than the
  qualified `deterministic_greedy` policy is refused and is never coerced;
- `unqualified_target_verification_capability`: a capability without a qualification
  binding cannot be constructed;
- the boundary table itself: `scaffold_status().boundaries` enumerates every entry
  point with its code, and the suite asserts both directions (every declared code is
  raised; nothing is raised that is not declared).

Explicitly **not** implemented, and not implied by any passing test:

- causal, clock, stage-application, activation/attempt and loopback rejection in a
  trace (the scaffold validates interval shape and edge references only);
- per-member result count/order, cross-member KV isolation, padding isolation,
  stale-incarnation rejection, timeout/overflow/slow-consumer/cleanup budgets;
- every performance, overlap, benchmark, browser, product and physical claim.

## 4. RED evidence at `a7b412a`

Command (worktree at the acceptance base; see the suite docstring for the archive
reproduction):

```
python3.14 -m pytest tests/a10_scaffold/test_a10_strong_red_absent_behaviour.py -q
```

Result, before and after the scaffold was added — identical, because no test in that
file imports the scaffold:

```
9 failed, 1 passed
```

The nine strong-RED failures are caused by absent baseline behaviour, not by a
scaffold exception. Two of them are plain assertions over real production code:

- `mycelium_router/numpy_runtime.py` `execute_batch()` (a sequential loop) serves a
  two-member batch with **2** single-item invocations where the inventory requires 1.
- the product drain path proposes one batch of two members (`decision reason
  'decode_ready_batch'`) and still executes **2** single-item invocations.

The remaining seven name measured absences: no runtime-owned observation (the NumPy
port reports `mode='complete_context_replay'`, `active_state_count=0`), no target-KV
transaction, no verification operation, no verification admission, no stage-compute
intervals (`RuntimeResult` fields are only `failure_reason`, `failure_scope`,
`payload`, `success`, `token_id`), no product-path observation surface (`batch_execution_profiles()`
is a relay-side EWMA keyed by `(phase, batch_size)`), and no dispatch-mode report on
the sequential double.

The one passing test is a guard, not a claim: an unqualified runtime must not
advertise the verifier. It passes today and must keep passing.

## 5. What this step does NOT prove

- No runtime batch, overlap, verifier, benchmark, product, browser, physical or
  qualification claim. Every RED test above is still red at this commit.
- No A10 closure requirement is earned; the gate stays `design_only` and the
  completion checklist is untouched.
- The scaffold's validation is shape-level only: it does not execute, schedule,
  isolate KV, bound a real queue, or measure anything.
- The boundary suite passes only because the refusals exist; it is not evidence that
  any A10 behaviour works.
- The strong-RED suite uses the real NumPy target port for the runtime-level facts,
  and a counted test double for the product drain path (the relay test recipe cannot
  load a model cheaply); the product-path counts are therefore double-backed, while
  the proposal-versus-invocation shape they observe is real baseline behaviour.
- Nothing here was verified on physical peers, on MLX, or through a browser.

## 6. Implementation slices (future lanes, each ending in its own commit)

Ordering follows the specification's own dependency shape. Each slice must flip its
named RED tests and re-run the frozen gates; no slice may widen the others' claims.

1. **S1 runtime batch truth** — observation record, single backend invocation,
   per-member scatter, `dispatch_mode`, split reporting. Flips the three runtime-level
   and two product-path RED tests. Gate: deterministic negative cases for result
   count/order, stale incarnation and KV isolation.
2. **S2 continuous arrivals and overlap** — iteration-boundary admission, causal
   trace validation, uncertainty-aware verdict. Flips the interval RED test. Gate:
   trace-rejection matrix and the product-path observation test.
3. **S3 target verification** — KV transaction, one-call evaluation, sequential
   parity oracle, sampling rejection, capability binding. Flips the four verifier RED
   tests. Gate: the correctness matrix (≥100 operations, widths `one`,
   `qualified_default`, `qualified_maximum`, ≥2 prefix buckets).
4. **S4 qualification, product and browser** — paired same-route benchmark under the
   frozen three-pair order, all-eight workspace reconstruction, privacy projection,
   regression/audit output. Only S4 can move the gate out of `design_only`, and only
   through the existing authorities.

A lane that needs a path outside its slice stops for owner review rather than widening
itself, exactly as the A4 implementation packet requires.
