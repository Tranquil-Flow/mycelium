# Mycelium A11 Target-Authoritative Speculative Decoding Specification

**Status:** `design_only`; dependency-ready acceptance boundary; production integration
waits for A10 closure

**Gate:** A11

**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`

**Supersedes for this gate:** `2026-08-11-mycelium-m20-speculative-decoding.md` and
the unintegrated `mycelium.m20_*` evidence surfaces

**Depends on:** A3 qualified useful target; A4 concurrent and interruptible request
path; A6/A7 target-path recovery policy when available; A10 physically qualified
multi-position target verifier and runtime batching

**Architecture:** Astra section 4.9

## 1. Outcome and claim boundary

A11 adds an optional speculative-decode overlay to an already-qualified target-only
deployment. A distinct draft runtime may propose bounded token sequences, but the exact
selected target remains the sole token, sampling, committed-position, and output
authority. No proposal becomes browser-visible until the A10 target verifier accepts it
and atomically commits the corresponding target KV.

Speculation is **off by default**. A qualified enabled decision permits an explicit
future-request preference; it never silently changes an admitted request or displaces
the target-only incumbent. If exact compatibility, target-verifier capability, parity,
cleanup, or material end-to-end gain is absent, the product publishes a bounded,
measured disabled decision and continues target-only operation.

A11 does not make a draft a target, transfer KV between models or peers, weaken model
acquisition authorization, infer capability from a script, hide target/path loss as
draft fallback, or claim mobile speculative operation from a desktop result. It does
not claim benefit from draft speed, predicted acceptance, fixture timing, synthetic
tokens, direct callbacks, or qualification-only runs. Only same-route ordinary-product
measurements may support a performance claim.

## 2. Authority and eligibility

- The qualifier remains the sole authority for target readiness, A10 verifier support,
  draft-role qualification, and the final speculative decision. The registry owns
  future-request target selection; Settings owns only the user's bounded preference.
- The target runtime owns target KV, verified positions, correction tokens, committed
  watermark, generation policy, and all emitted output.
- The draft runtime owns only draft KV and uncommitted proposals. It cannot write target
  state, emit output, advance the target watermark, or authorize fallback.
- A4 owns request lifetime, deadline, cancellation, liveness, scoped incidents, and
  exactly-once cleanup. A10 owns target verification transactions, runtime batching,
  queue bounds, slow-consumer protection, and per-member attribution.
- Target/path loss follows A6 replay or A7 compatible-KV recovery only when that exact
  capability is qualified. Otherwise the request terminates explicitly. Draft loss is
  never evidence of target loss.
- The request gateway owns private prompt/output and browser streams. The public
  product projection receives privacy-reduced counters and decisions only.

The selected target binds the same exact model, revision, manifest, representation,
graph, assignments, path generation, load proofs, qualification, and runtime
incarnations as target-only serving. A draft binds its own exact model, revision,
manifest, representation, artifact authorization, runtime, load proof, draft-role
qualification, and resource reservation.

No model download, conversion, quantization, or representation substitution is implied
by this specification. A missing draft remains unavailable until the owner separately
authorizes its exact local identity and representation. Draft discovery or local files
alone grant no execution authority.

The read-only `scripts/inspect_local_speculation_compatibility.py` may inventory static
tokenizer and active-position facts from exact existing local snapshots. Its result is
`design_input_only`, always reports `route_ready=false` and
`qualification_evaluated=false`, and cannot authorize a draft, advertise A10 target
verification, or satisfy an A11 physical/performance gate. The legacy
`scripts/qualify_m20_speculation.py` assumes one `model.safetensors` file and compares
raw model-head vocabulary and disabled-position fields as authority; A11 implementation
must replace that seam rather than promote its output.

## 3. Exact compatibility boundary

Before any speculative admission, closed evidence must prove:

- distinct immutable target and draft identities;
- identical tokenizer implementation/digest, canonical token-ID mapping and count,
  special-token IDs/semantics, normalization, and byte-fallback behavior. A model's
  padded output-head `vocab_size` is not the tokenizer vocabulary: target and draft may
  have different padded head sizes only when both cover every canonical token ID and
  generation masks every non-canonical/reserved ID identically;
- identical position indexing, rotary/position semantics, attention-mask and context
  truncation behavior for the admitted context. Disabled configuration fields such as
  `sliding_window` when `use_sliding_window=false` are recorded but do not create a
  false incompatibility; enabling them or changing their effective behavior does;
- an exact generation-policy identity, including greedy mode or the complete target
  sampling algorithm, parameters, seed, counter state, and numerical semantics;
- bounded proposal width/count/bytes and an A10 verifier qualified for that width and
  exact target runtime binding;
- separate target and draft KV schemas, owners, capacity reservations, commit
  watermarks, rollback operations, and cleanup; and
- authenticated bounded proposal transfer plus current deployment, membership,
  liveness, path, and runtime-incarnation authority.

Initial A11 qualification is deterministic greedy decode. Sampling is disabled unless
the exact target sampling decision can be reproduced by the A10 verifier with the same
seed/counter semantics and passes a separately enumerated parity matrix. Unsupported or
unknown generation policy fails speculative admission and uses target-only mode; it is
never silently changed.

Draft and target KV are never shared or transferred. Matching KV-schema digests are
compatibility evidence only, not permission to alias memory. Proposal transfer contains
only the minimum private token-position envelope needed by the target verifier and is
never projected to Observatory or persisted in public evidence.

## 4. Closed product contracts

A11 replaces the unintegrated milestone-named documents with capability-named, closed,
bounded, canonically digested records:

1. `mycelium.speculative_decode_plan.v1` — immutable target/draft/verifier identities,
   compatibility facts, workload, proposal bounds, deadline/circuit-break policy,
   resource envelope, target-only fallback, benchmark threshold, and decision.
2. `mycelium.speculative_decode_runtime.v1` — request/cycle state and privacy-reduced
   counts for proposed, verified, accepted, rejected, target-corrected, committed,
   rollback, deadline, fallback, cancellation, cleanup, and terminal outcome.
3. `mycelium.speculative_decode_benchmark.v1` — same-route paired target-only and
   candidate windows, samples, acceptance, transfer/verification cost, end-to-end
   timing, confidence, parity, reliability, resource state, and materiality decision.
4. `mycelium.speculative_decode_qualification.v1` — owner-private executed artifact
   binding compatibility, runtime, physical positive/negative gates, browser checks,
   benchmark decision, and regression results, with a privacy-reduced public projection.

All contracts reject unknown fields/protocols, duplicate identities, non-finite or
negative measurements, excess proposal widths/counts/bytes, stale generations,
incompatible target/draft/verifier binding, invalid state transitions, impossible
counts, unbounded histories, and mismatched canonical digests. A runtime invariant
requires:

`accepted + rejected <= verified <= proposed`

and every committed position must be target-verified or target-corrected exactly once.

Public records contain no prompts, output text, raw token IDs, logits, tensors,
activations, KV bytes, sampler state, credentials, keys, raw EndpointIDs, addresses,
usernames, hostnames, private paths, command lines, or exception strings. Missing
measurements remain `unknown`/`null`; they do not become zero acceptance, zero cost, or
success.

## 5. Runtime cycle and KV transaction semantics

Each speculative cycle begins at one target-owned committed watermark shared only as a
bounded identity/position binding. The draft proposes at most the admitted width before
its adaptive deadline. The proposal is generation-, request-, attempt-, cycle-, prefix-,
and idempotency-bound and is sent over the authenticated private runtime path.

The target opens an A10 verification transaction from the exact committed target
prefix, evaluates the proposal in one real bounded multi-position target call, and
accepts only the common target-authoritative prefix. At the first mismatch it rolls
back tentative state after the accepted prefix, commits that prefix plus one target-
authoritative correction position, and rejects the remaining proposals. If every
proposal matches, it commits the verified proposal positions. The next cycle begins
only from the resulting target watermark.

Draft KV advances tentatively while proposing. After verification it commits only the
accepted target-confirmed prefix and rolls back rejected suffix state; after a target
correction it consumes that target-owned correction before proposing again. If the
draft cannot reproduce the committed prefix exactly, speculation ends and target-only
decode resumes. Draft cleanup can never roll target state back.

Output is released only after the target commit succeeds. Duplicate identical cycle
operations are idempotent. Conflicting duplicates, stale prefixes, late results, target
or draft incarnation drift, excess width, verifier parity failure, or partial commit
fail closed to the previous target watermark. No token is duplicated, skipped, or
emitted twice.

Each request reserves target KV, draft KV, verifier workspace, proposal bytes, runtime
batch capacity, transport buffers, and browser output independently. Cancellation is
checked before draft work, before transfer, before target verification, after verifier
return, before commit, and before output. Cancellation or terminal failure rolls back
tentative target state, releases draft state, and records exactly one terminal result
without affecting another batch member.

## 6. Adaptive deadline, fallback, and circuit breaker

The draft may not make the target wait indefinitely. The default proposal deadline is:

`min(100 ms, max(5 ms, 0.75 * warm target-only p95 TPOT))`

measured for the exact route/workload and clamped by the request's remaining deadline.
The admitted value, clamp reason, draft queue time, compute time, transfer time, and
deadline outcome are recorded. A planner prediction cannot extend it during an active
request.

If no valid proposal arrives by the deadline, the target performs its ordinary next
step from its committed watermark. A late proposal is rejected by cycle and prefix
identity. Deadline fallback, draft cancellation, draft runtime/transport loss, invalid
proposal, low rolling acceptance, or draft thermal throttling does not alter target KV
and does not close the target connection.

The qualified default circuit breaker opens for future cycles in the current request
after either three consecutive proposal deadlines, any compatibility/parity violation,
or a 20-cycle rolling window whose observed end-to-end speculative cycle time is not
better than equivalent target-only time. It falls back target-only for the remainder of
that request. Compatibility/parity violations also revoke new speculative admission
until requalification; performance/thermal causes use a bounded five-minute cooldown
and require fresh observations before retry.

Draft loss falls back from the exact target committed watermark. Target or immutable-
path loss invokes the separately qualified A6/A7 outcome or terminates explicitly. It
cannot be relabelled `draft_fallback`. If fallback itself cannot prove the target
watermark, the request terminates rather than replaying or guessing.

## 7. Frozen benchmark and promotion rule

Before candidate execution, an immutable manifest binds exact target/draft/
representations, verifier capability, route/session/generation, stage runtimes,
workload inputs, offered arrival schedule, prompt/output-length buckets, QoS and
concurrency, generation policy, proposal width/deadline, circuit breaker, token limits,
warmup, allowed samples, and software/configuration digests.

Target-only and speculative modes run in one stable product benchmark session without
server restart, route reselection, model substitution, or instrumentation change.
After one unscored warmup per mode, the benchmark uses three paired repetitions with
alternating pair order: `AB`, `BA`, `AB` (six measured windows total). Here `A` is
target-only and `B` is speculative. Each window contains at least 12 completed requests
and 384 target-owned committed output tokens across at least two prompt-length and two
output-length buckets. Error, timeout, cancellation, fallback, and rejected-admission
outcomes remain in reliability and latency accounting.

The primary material threshold is a **10% reduction in end-to-end wall-clock time per
target-owned committed output token**, with the paired 95% bootstrap lower confidence
bound for improvement also at or above 10%. Measurement begins at ordinary gateway
admission and includes draft queue/compute, proposal transfer, target verification,
rollback, fallback, output backpressure, and terminal cleanup. Promotion also requires:

- exact target-only output parity for every completed request;
- no more than 10% regression in interactive p95 TTFT;
- no regression in terminal error, timeout, cancellation, or cleanup success rate;
- zero target-KV corruption, duplicate/missing output, cross-request state, or residue;
- observed non-zero proposal, target-verification, acceptance, and rejection/fallback
  coverage; and
- all resource, queue, liveness, and slow-consumer bounds to remain satisfied.

Confidence is computed over paired windows. Route churn, stale qualification,
membership/runtime incarnation change, thermal or power-limit drift outside the frozen
envelope, missing samples, or software/configuration mismatch invalidates the pair.
Thresholds, workloads, widths, deadlines, and exclusions cannot change after candidate
measurements are inspected.

An enabled decision requires every compatibility/parity/negative gate and the material
threshold. Otherwise A11 publishes `measured_disabled` with a closed reason such as
`no_authorized_local_draft`, `incompatible_token_semantics`,
`target_verifier_unavailable`, `parity_failed`, `insufficient_acceptance`,
`draft_deadline_exceeded`, `material_gain_not_observed`, or
`confidence_bound_not_met`. A capability-unavailable reason reports which measurements
were impossible and never fabricates a performance sample. A disabled decision is a
truthful completed evaluation, not a benefit claim.

## 8. Promotion, selection, rollback, and retention

Speculation remains disabled globally and for existing requests after qualification.
When the decision is `qualified_enabled`, Settings may expose an explicit off-by-
default preference for future requests. Each admission records `target_only`,
`speculative_requested`, `speculative_admitted`, or a closed fallback reason. A browser
cannot force an unavailable, stale, incompatible, or resource-infeasible overlay.

The target-only deployment remains independently selectable and qualified. Promotion
adds an overlay; it does not replace or mutate the target representation. Revoking the
draft, verifier, plan, or benchmark decision immediately blocks new speculative
admission while preserving target-only service.

A runtime parity violation, target-KV invariant breach, cross-request contamination,
or target/draft/verifier identity drift triggers immediate overlay rollback and
qualification revocation. Performance regression, repeated deadline fallback, thermal
throttling, or low acceptance disables the overlay under the bounded circuit breaker
without marking the target deployment failed. Incidents preserve the exact narrow
reason and current target-only availability.

## 9. Desktop-first and platform boundary

The physical positive gate first uses a qualified desktop/native draft runtime with
locally authorized assets. An Android, iOS, iPadOS, browser, or other mobile member does
not gain draft eligibility from membership, generic compute, synthetic probes, or the
desktop result.

Mobile draft operation requires its own A9/A12 platform capability, artifact, runtime,
thermal/power, background/lifecycle, network-loss, parity, deadline, cleanup, and
ordinary-product qualification. Every mobile peer remains ineligible for the optional
draft role until A11 is physically closed and bound to the exact target workload. A11
is not a prerequisite for generic A12 mobile closure or for ordinary qualified mobile
participation. A mobile attempt may be reported as unavailable or ineligible without
affecting desktop target-only operation.

## 10. Eight-workspace product contract

Product copy uses human capability names and contains no internal A/M labels.

- **Inference:** target model, optional off-by-default acceleration preference,
  admitted mode, proposal/verified/accepted/rollback/fallback counts, target-owned
  progress, deadline state, and exact disabled/fallback reason without raw tokens.
- **Device Lab:** draft-role compatibility, backend/verifier requirements, width/
  deadline limits, parity, lifecycle, thermal/power, cleanup, and platform-specific
  qualification; synthetic workers remain non-qualifying.
- **Network:** target and draft logical roles, bounded private proposal movement,
  target-verification spans, selected direct/relay/unknown path, and fallback, distinct
  from ordinary activation and modeled animation.
- **Nodes:** target/draft role, exact model/revision/representation, artifact/load/
  qualification, KV ownership, reservations, proposal work, acceptance, deadline,
  thermal state when observed, and freshness.
- **Plans:** compatibility matrix, workload, proposal width/deadline, predicted versus
  observed acceptance and cost, same-route A/B, confidence, material threshold,
  promotion decision, and target-only fallback.
- **Readiness:** separate target, verifier, draft, compatibility, parity, rollback,
  fallback, cleanup, benchmark, confidence, freshness, and promotion proofs.
- **Incidents:** incompatibility, proposal timeout, low acceptance, draft loss,
  verifier/rollback failure, target loss, cancellation, circuit break, qualification
  revocation, and target-only outcome with narrow scope.
- **Settings:** target-only default, qualified future-request preference, bounded width/
  deadline policy, privacy/retention, and human-readable disabled reason. Unsafe edits
  cannot mutate active requests or bypass qualification.

All views consume one current public generation and distinguish live, stale,
historical, fixture, modeled, unknown, enabled, disabled, fallback, and failed state.
Refresh, direct navigation, workspace switching, Back/Forward, reconnect, bounded
terminal history, and a clean second session reconstruct public state while keeping
tab-private prompt/output isolated. Keyboard, responsive layout, accessible names, and
reduced-motion behavior are required.

## 11. Verification matrix

The closed machine-checked acceptance inventory is
`tests/a11_acceptance/scenarios.v1.json`. It freezes exact target/draft/verifier
identity, tokenizer/position compatibility, separate KV authority, target-owned cycle
transactions, adaptive policy, same-route benchmark thresholds, enabled/disabled
decision rules, privacy, and workspace mappings. Passing
`tests/a11_acceptance/test_a11_scenario_manifest.py` proves only that these design inputs
remain closed and complete. It does not satisfy a runtime, physical, benefit, enabled,
measured-disabled, browser, qualification, or completion claim.

| Acceptance family | Required inventory scenarios |
| --- | --- |
| Exact identities and compatibility | `exact_target_draft_verifier_identity`, `tokenizer_position_compatibility`, `incompatible_admission_target_only` |
| Separate KV ownership | `separate_target_draft_kv` |
| Target-authoritative commit and rollback | `target_authoritative_all_match_commit`, `target_mismatch_correction_rollback`, `cycle_conflict_rollback_to_watermark` |
| Cancellation and cleanup | `cancellation_transaction_cleanup` |
| Adaptive deadline and circuit breaker | `adaptive_deadline_target_fallback`, `circuit_breaker_and_cooldown` |
| Draft versus target loss | `draft_loss_target_only_fallback`, `target_loss_explicit_outcome` |
| Target-only baseline and confidence-bound benefit | `target_only_same_route_baseline`, `confidence_bound_material_benefit`, `invalid_benchmark_pair` |
| Promotion or truthful disabled decision | `measured_disabled_outcome`, `qualified_enabled_off_by_default` |
| Browser reconstruction and privacy | `all_workspace_reconstruction_privacy` |

### Contract and deterministic gates

- Closed-shape, count/byte, digest, privacy, unknown-field, duplicate/conflict, stale
  prefix/generation, illegal transition, impossible count, and bounded-history tests.
- Compatibility tests cover tokenizer/vocabulary/special-token/position/generation
  semantics, padded model-head vocabulary with identical canonical token mapping,
  active versus disabled position fields, exact model/representation/verifier identity,
  missing measurements, unqualified draft, stale authorization, and unsupported
  sampling.
- Runtime tests cover all-match and first-position/interior/final-position mismatch,
  correction, separate KV, commit/rollback, duplicate and late cycles, adaptive
  deadline, circuit breaker, cancellation at every boundary, concurrent requests,
  slow consumer, shutdown, and zero residue.
- Planner tests may predict acceptance and gain, but tests assert those projections
  never enable or qualify the overlay.

### Physical positive gates

1. On a useful larger qualified target and a qualified desktop draft, the ordinary
   product gateway performs real proposal transfer and A10 multi-position target
   verification with observed acceptance, rejection/correction, rollback, and exact
   target-only output parity.
2. Draft loss and proposal deadline during live browser requests fall back from the
   target committed watermark; target output remains exact and resources return to
   baseline.
3. The same-route/session paired benchmark passes the frozen end-to-end gain,
   confidence, TTFT, reliability, and cleanup thresholds, or publishes the exact
   measured-disabled decision without an acceleration claim.
4. A qualified-enabled overlay remains off for a new session until explicitly chosen;
   selection applies only to a future request and is preserved truthfully through
   refresh/reconnect.

### Physical negative gates

- Incompatible tokenizer, vocabulary, special tokens, positions, generation policy,
  target/verifier binding, representation, stale draft, excess width, and insufficient
  resource reservation all reject speculative admission while target-only remains
  available.
- Low acceptance, slow/thermally throttled draft, draft runtime/transport loss, late
  proposal, target-verifier failure, rollback failure, target loss, cancellation,
  partial batch failure, browser disconnect, and shutdown preserve target authority and
  produce their exact scoped outcome.
- Target loss cannot be reported as draft fallback. Missing committed-watermark proof
  terminates rather than guesses. No stale proposal or result may mutate a newer cycle.
- A script-set capability, fixture, synthetic callback, modeled acceptance, predicted
  gain, draft-only timing, sealed M20 document, or UI preference cannot satisfy parity,
  promotion, performance, or browser gates.

### Browser and regression gates

All eight live workspaces pass target-only, requested, admitted, accelerated, fallback,
disabled, stale, and revoked states through direct navigation, refresh, Back/Forward,
workspace switching, reconnect, cancellation, terminal history, and an independent
second session. Accessibility, privacy, contracts, governance, release security,
frontend, full Python, A4 concurrency/liveness, A6/A7 recovery where applicable, A10
batch/verifier, runtime, transport, registry, and cleanup suites pass after executed
evidence stabilizes.

## 12. Completion

The owner-private qualification bundle binds source/specification digests; exact target,
draft, representations, artifacts, runtimes, and authorization; A10 verifier; route,
session, workload, generation policy, proposal/deadline/circuit-break policy; target-
only and speculative samples; acceptance, transfer, verification, rollback, fallback,
parity, confidence, resources, and cleanup; all-eight browser results; negative gates;
the acceptance-inventory digest; and regression/audit output.

A11 is complete only when the ordinary product path publishes either:

1. `qualified_enabled` after measured material end-to-end benefit and every correctness,
   fallback, negative, UI, and physical gate, while remaining off by default; or
2. `measured_disabled` after the applicable physical evaluation with an exact bounded
   reason and no benefit claim.

The accepted outcome lands in one atomic A11 feature commit. Until physical and browser
evidence exists, the capability remains `design_only` or the narrower truthful
intermediate state. Neither outcome broadens mobile eligibility or weakens the always-
available qualified target-only path.
