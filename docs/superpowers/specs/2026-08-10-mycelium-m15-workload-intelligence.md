# Mycelium M15 Workload Intelligence Specification

**Status:** implementation baseline
**Milestone:** M15
**Parent architecture:** `2026-08-09-mycelium-astra-architecture-product-design.md`

## 1. Outcome and boundary

M15 makes workload and phase assumptions an explicit planner authority. The same
signed M13 evidence snapshot and measured M14 directed topology are evaluated under
prefill-favoring, decode-favoring, and combined policies. The result is a deterministic
scenario matrix with a robust winner and an explicit Pareto frontier. M15 does not
claim request admission, queueing, continuous batching, microbatch overlap, or
concurrent physical execution; those remain M16.

The live browser must show modeled values as predictions and physical values as
observations. A missing physical observation stays missing. No prediction may be
relabeled as measured evidence.

## 2. Authorities and frozen contracts

- Gossip/M13 supplies the atomic eligible-node, calibrated-capability, model, runtime,
  and placement-input snapshot.
- M14 supplies measured directed activation-plane link evidence and the frozen cycle.
- The M15 planner supplies policy alternatives, scenario scores, Pareto membership,
  robust selection, and signed deltas. It supplies intent, never runtime readiness.
- The runtime supplies request timing and resource observations.
- Qualification compares observations with frozen budgets and modeled-error bounds.
- The product gateway only projects privacy-reduced records.

M15 freezes these boundary records:

1. The closed workload-profile subrecord in `mycelium.m15_plan_comparison.v1`: a
   content-free profile with an immutable trace digest, sample count, prompt/output
   token distributions, concurrency assumption, explicit batch shape, QoS class,
   scenario weights, and source/calibration labels.
2. `mycelium.m15_plan_comparison.v1`: one snapshot-bound matrix containing the three
   policies, phase-separated predictions, allocations, robust/Pareto decision,
   calibration observations, budget state, and claim boundary.
3. `mycelium.performance_budget.v2`: the M12 sequential budget plus per-workload
   reconnect, peak-memory, energy/thermal, and modeled-error bounds. Queueing,
   admission-latency, concurrency, and batch-shape fields are declared
   `deferred_to_m16` until bounded runtime queues exist. V1 remains readable and is
   not mutated because it is a closed compatibility contract.

Both versioned schemas and the embedded profile shape are closed, bounded, JSON-safe,
and fail on unknown fields, NaN,
infinity, malformed digests, unrecognized policy/QoS values, or private content.

## 3. Workload profiles

M15 accepts two initial qualified profile families derived from measured token-count
traces with text removed:

- `interactive_chat_v1`: latency-priority, batch size 1, prompt/output token
  distributions, and sequential physical acceptance in M15.
- `sustained_batch_v1`: throughput-priority with an explicit batch envelope and
  arrival/concurrency assumptions. Its concurrency and queueing performance remain
  modeled and `deferred_to_m16`; sequential executions may calibrate phase estimates.

Every scenario has a stable identifier, probability weight when the profile uses an
expected distribution, prompt p50/p95, output p50/p95, modeled concurrency, batch
size, QoS class, and arrival-rate assumption. Network or fleet size changes capacity,
not the recorded workload demand.

## 4. Phase objective and plan comparison

The topology-only M14 order remains frozen. M15 changes only the contiguous layer DP
objective and subsequent scoring:

- `prefill_ttft`: minimize the maximum effective prefill stage time.
- `decode_tpot`: minimize the maximum effective per-token decode stage time.
- `balanced`: minimize the maximum per-request service work (prefill plus output
  decode), preserving the pre-M15 behavior.

For every policy and scenario the planner emits TTFT, prefill compute, prefill
transfer, TPOT, decode compute, decode transfer/loopback, response time, single-request
throughput, saturated goodput, memory requirement, and confidence separately.

A plan dominates another only when it is no worse for every scenario on TTFT and
TPOT and no worse on goodput, with at least one strict improvement. Non-dominated
plans form the Pareto frontier. Robust selection uses deterministic minimax normalized
regret across TTFT, TPOT, and goodput. Ties resolve by policy identifier. The result
names the scenario/metric producing the selected plan's worst regret and includes
signed candidate-versus-selected deltas.

## 5. Calibration and budgets

Predictions bind the planner snapshot digest, workload-profile digest, policy ID,
model identity, route/deployment generation, and topology evidence digest. Physical
observations additionally bind request ID, context/output counts, runtime backend,
placement ranges, and before/after frame counters.

M15 calibrates both profiles by sequential browser requests. It reports signed and
absolute modeled-versus-observed error for TTFT, TPOT, and throughput. Non-queueing
budgets either pass, fail, or carry an approved exclusion. Queueing, admission,
concurrency, and batch-shape budgets must read `deferred_to_m16`; they can never read
`met` in M15.

## 6. Privacy and failure states

Workload and Observatory records may contain token counts, distributions, timings,
policy labels, digests, and bounded public diagnostics. They must not contain prompt
text, response text, token IDs, activations, tensors, KV data, credentials, private
addresses, artifact roots, or trace file paths.

Unknown protocol versions, snapshot/profile digest mismatch, missing scenarios,
duplicate policy IDs, non-deterministic comparison, stale topology evidence, invalid
budgets, or observation-binding mismatch withhold the M15 qualified claim. Existing
M14 route serving may remain available if its own qualification remains current.

## 7. UI contract

- **Plans:** synchronized policy comparison, workload assumptions, exact allocations,
  phase-separated predictions, Pareto reasons, winner/worst-regret explanation,
  bottleneck, memory, calibration error, and honest absent pruning state.
- **Inference:** each request carries workload/QoS attribution and shows observed TTFT,
  TPOT, throughput, context/output counts, and topology. Prompt/output content remains
  confined to Inference history.
- **Settings:** displays only qualified workload defaults and states that changes apply
  to future admissions. M15 does not expose a control that implies queueing exists.

Navigation, refresh, Back/Forward, reconnect, and tab continuity use the same eight
stable workspace route IDs.

## 8. Verification gate

1. Contract RED tests precede implementation and cover closed shapes, privacy,
   missing scenarios, deterministic ties, domination, profile mismatch, and deferred
   M16 fields.
2. Pure planner tests show at least one heterogeneous capability matrix where prefill
   and decode objectives produce different contiguous allocations while topology and
   evidence remain identical.
3. The local/live projection renders the three policies, robust winner, Pareto
   frontier, scenario assumptions, and modeled labels without fixture/live confusion.
4. The browser runs interactive and sustained profiles sequentially against the same
   qualified deployment/policy. Observed TTFT/TPOT/throughput appear beside the bound
   predictions and survive refresh.
5. Frozen observed-service and model-error bounds pass. Unmeasured peak-memory,
   energy/thermal, and reconnect fields carry approved exclusions. Queueing,
   admission, concurrency, and batch-shape remain `deferred_to_m16`.

The M15 handover records the focused spec digest, contract manifest digest, RED and
GREEN commands, coherent run identity, physical hosts/runtimes, profile digests,
predicted and observed values, signed errors, frame counters, browser routes, negative
proofs, and remaining M16 boundary.
