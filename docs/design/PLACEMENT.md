# Speed-aware placement (PV21)

Plan selection only. Nothing here changes `mycelium_live` route internals or A5/A6/A7 semantics; the native
builder, staging and qualification still enforce whatever split is chosen. Code: `mycelium_a13_macos/placement.py`.

## 1. Join-time calibration

Each Mac times **one real layer** of the announced model on its own MLX runtime, using the weights it already
holds (never a download), in a bounded run (default 1.5 s budget, 4-48 decode samples, median reported):

- decode: one-token matmul against every 2-D weight of the layer (memory-bandwidth bound) -> `decode_ms_per_layer`
- prefill: 128-token matmul against the same weights (compute bound) -> `prefill_tokens_per_second_per_layer`

No usable weights or MLX (slice mode before the slices exist, or an error) -> a documented chip-identity
estimate, labelled `basis: estimated`: decode = layer bytes / (published bandwidth x 0.6), prefill =
layer bytes per token / (published GPU FLOPs x 0.4). Unknown chip -> no record, and that Mac gets the memory-only split.

The record is the optional, closed `step_calibration` key of the member's **signed** `mycelium.a13_demo_member_report.v1`
(run exchange); `run_exchange.validate_report` bounds it. If `lane/rel2-member` later folds calibration into the member
state report, only `demo_target.DemoTargetAgent._report` and `demo_chain.member_capability_records` change.

Limit: the proxy streams the **source** (bf16) layer, not the int8 runtime tensors. It is consistent across members, so
the *ratio* between Macs is what the split uses; absolute tok/s are predictions, not measurements.

## 2. Placement

`choose_placement(members, profile, policy, force_split, pair_rtt_ms)` returns a `layer_split` document (same protocol,
digest-checked by `split_ranges`) plus a `placement` block.

- Objective: minimize the slowest stage's decode time, where stage time = layers x `decode_ms_per_layer` (+ the
  output head / tied embedding as layer-equivalents on the last stage) + the one-way link latency to the next stage
  (the sampled token returns to stage 0, so it is a ring). Tie-break: slowest stage prefill, then total decode.
  Exact DP over contiguous partitions; deterministic integer nanoseconds.
- Memory: per member `min(policy fraction of RAM (1/2), standing cap, 3/4 RAM)`; a stage must satisfy
  `ceil(stage_bytes x resident_factor) + 1 GiB <= budget` (same rule as `memory_proportional_split`). No feasible
  partition -> `member_cannot_hold_share` (by name via the memory split).
- Link RTT: median RTT each Mac measured to the owner (capability report `link.rtt_ms_median`; the owner is 0). A hop
  between two members is `rtt_a + rtt_b` unless a direct pair RTT is supplied.
- Fallback: any member without a calibration (and no known chip) -> unchanged `memory_proportional_split`, no `placement` key.

Single-stream token latency = sum of stage computes + sum of one-way hops. A pipeline only overlaps stages across
**concurrent** requests, which the model deliberately does not credit; `bottleneck_ms` (slowest stage) is the
concurrent-throughput bound and is reported alongside.

## 3. Single-Mac fast path

If the whole model fits one Mac's budget and its predicted single-node token latency is lower than the best split's
(hop RTT included), the decision is `mode: single` ("Fast path chosen: fits on <Mac> (a tok/s vs b split); the other Mac
stays available.") with `host` and `replicas`. Owner setting `always_split` (`/api/placement`, UI checkbox, or
`MYCELIUM_A13_PLACEMENT`) forces the split for demos.

**Not executed yet:** the route builder needs >= 2 hosts (`layer_split.MIN_HOSTS`, `chain_compile._compile_hosts`), and a
one-host route is a route-internals change outside this lane. So `applied: false`, `note: single_host_route_not_supported_yet_running_best_split`,
and the compile still builds the best split. Flip `placement.SINGLE_HOST_ROUTE_SUPPORTED` when a one-host route exists.

## 4. Explanation

`/api/status` -> `placement.decision.explanation` (also `owner_run.placement`) and the UI line under the run state, e.g.
`Split 18/10 because the MacBook is 1.7x slower per layer.`, `Split 22/6 because memory limits what each Mac can hold.`,
`Fast path chosen: fits on the Mac mini (3.5 tok/s vs 2.7 split); the other Mac stays available.` Estimated inputs add `(estimated)`.

## 5. Per-token latency decomposition (hook + design note)

`placement.decompose_token_latency(records, wall_ns)` is the sink: records `{stage, kind in compute|serialize|send|receive|other,
start_ns, end_ns}` stack into per-stage per-kind totals and expose `residual_ns` against wall time (target: within 5%).

`mycelium_live`/`mycelium_router` do **not** emit these per-hop records today (the A10 overlap trace validates a given trace
but the live route does not produce one), and adding emission is a route-internals change. Design for the follow-up lane:
stamp `time.monotonic_ns()` at stage compute start/end, after serialize, before send, after receive in the hop wrapper only;
carry stamps in a side-channel record per token (not in the A5/A6/A7 wire payload); correlate the two Macs with the existing
`clock_offset` estimate; feed the records here and compare stage compute against `placement.members[].decode_ms_per_layer x layers`
to re-calibrate the split.

## Wave-3 device measurements (not run here)

1. A/B 0.5B, two Macs: single-Mac (`auto`, once a one-host route exists; until then compare the split with the second Mac's layers = 1) vs `always_split`; tok/s p50 and TTFT p50/p95, ABBA, >= 10 runs/arm, same Wi-Fi.
2. A/B 7B on Wi-Fi day, same protocol; record ping RTT at start, both Macs' thermal state.
3. Split sweep for the demo pair: 18/10, 16/12, 14/14, 12/16 with a fixed prompt set; plot tok/s vs split; check the predicted optimum (`placement.prediction`) against the measured best.
4. Calibration truth: the logged `step_calibration` (measured and estimated) per Mac vs the observed per-stage time from (5) once the hook is fed.
