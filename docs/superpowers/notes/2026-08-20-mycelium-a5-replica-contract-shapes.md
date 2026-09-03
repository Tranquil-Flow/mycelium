# A5 Replica Contract Shapes — 2026-08-20

**Spec authority:** `docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md` §7 (Closed product contracts) and §8 (Eight-workspace product behavior).

**Status:** implemented — validators are enforced at qualifier/install and product projection boundaries.

These four closed shapes are the only authoritative wire formats the A5 gate emits on the production path. Each shape is capability-named, closed (no extra fields), bounded, canonically digest-bound, and privacy-reduced (no prompts, output text, token IDs, logits, activations, KV contents, credentials, private paths, raw addresses, unbounded traces).

The shape definitions are deterministic and checkable: validators live in `mycelium_replica_contracts.py`; deterministic tests live in `tests/a5_acceptance/test_replica_contracts.py`; fixture compatibility entries are exposed via `compatibility_fixtures()` and consumed by `scripts/generate_contract_fixtures.py`.

## Shape 1: `mycelium.replica_plan.v1`

A planner output. Reports `route_ready=false` always. Captures the immutable base binding, replica groups, candidate placements, complete legal tracks, directed edges, workload flow, predicted gain, uncertainty, failure-domain facts, zero-flow removals, and every rejection.

### Required fields

| Field | Type | Description |
|---|---|---|
| `protocol` | literal string | `"mycelium.replica_plan.v1"` |
| `plan_id` | bounded text | Stable identifier for this plan revision |
| `plan_digest` | sha256 | Canonical digest of this document |
| `deployment_id` | bounded text | Base deployment this plan is bound to |
| `deployment_epoch` | integer ≥ 0 | Base deployment epoch |
| `base_qualification_digest` | sha256 | Digest of the base qualification this plan extends |
| `issued_at_unix_ms` | integer ≥ 0 | Wall-clock issuance time |
| `model_id` | bounded text | e.g. `"Qwen/Qwen2.5-0.5B-Instruct"` |
| `model_revision` | bounded text | Immutable model revision |
| `representation_digest` | sha256 | Loaded representation digest |
| `route_generation` | integer ≥ 0 | Base route generation |
| `route_ready` | literal `false` | Per spec §7 — plans never report route_ready=true |
| `replica_groups` | list of objects | At least 2 ordered groups covering the model once |
| `legal_tracks` | list of objects | All complete legal tracks over the groups |
| `directed_edges` | list of objects | Every ingress/egress/local/closure edge used |
| `traffic_fractions` | list of objects | Per-track traffic fractions, sum to 1 within tolerance |
| `predicted_marginal_gain_fraction` | float | Planner's predicted throughput improvement vs primary-only |
| `uncertainty` | object | Per-track uncertainty bands |
| `failure_domain_facts` | list of objects | Known facts; missing data stays missing |
| `zero_flow_removals` | list of bounded text | Placement IDs removed for zero final flow |
| `rejections` | list of objects | Every rejection with reason |
| `workload_digest` | sha256 | Binding to `workload_manifest.v1.json` |

### Privacy redaction rule

`predicted_marginal_gain_fraction` is the only numeric performance data; everything else is structural identity. No metric samples, no latency distributions, no token counts in plan documents.

## Shape 2: `mycelium.replica_qualification.v1`

The qualifier's output. Only the qualifier may emit a replica-qualified track. Captures exact placement and track lifecycle authorities, current evidence, parity, resource and physical challenge results, workload envelope, and expiry.

### Required fields

| Field | Type | Description |
|---|---|---|
| `protocol` | literal string | `"mycelium.replica_qualification.v1"` |
| `qualification_id` | sha256 | Stable digest of this qualification document |
| `qualification_digest` | sha256 | Same as qualification_id for digest parity with A3 |
| `deployment_id` | bounded text | Base deployment |
| `deployment_epoch` | integer ≥ 0 |  |
| `replica_group_id` | bounded text | The replica group this qualification certifies |
| `placement_id` | bounded text | The placement within the group |
| `track_id` | bounded text | The complete legal track this qualification certifies |
| `traffic_fraction` | float in (0, 1] | Qualified share of ordinary admission assigned to this track |
| `qualifier_generation` | integer ≥ 0 | Authority generation (epoch fencing) |
| `issued_at_unix_ms` | integer ≥ 0 |  |
| `expires_at_unix_ms` | integer ≥ issued_at_unix_ms | Time-bounded authority |
| `evidence_bundle_digest` | sha256 | Bound evidence bundle |
| `load_proof_digest` | sha256 | Per-placement load proof |
| `artifact_verification_digest` | sha256 | Per-placement artifact verification |
| `parity_verified` | bool | True iff parity check passed |
| `startup_challenge_passed` | bool | True iff startup challenge passed |
| `memory_within_bounds` | bool | True iff all memory budgets respected |
| `cleanup_within_bounds` | bool | True iff cleanup verified within bounds |
| `directed_link_qualified` | bool | True iff every directed link on the track qualifies |
| `workload_envelope_digest` | sha256 | Workload bounds this qualification covers |
| `rejected_reasons` | list of bounded text | Empty iff qualification succeeded |
| `route_ready` | bool | Per spec §1: this shape carries route-ready for the replica track |

### Note on `route_ready`

Spec §1 says `replica_plan.v1` always reports `route_ready=false`. Spec §2 says only the qualifier may mark a replica track qualified. So the qualifier's output (`replica_qualification.v1`) is where `route_ready=true` lives for replica tracks.

## Shape 3: `mycelium.replica_runtime.v1`

Per-request runtime projection. Captures admitted request-to-track bindings, per-placement work, queues, reservations, frames, health, cleanup, and observed performance.

### Required fields

| Field | Type | Description |
|---|---|---|
| `protocol` | literal string | `"mycelium.replica_runtime.v1"` |
| `runtime_digest` | sha256 | Canonical digest of this document |
| `deployment_id` | bounded text |  |
| `deployment_epoch` | integer ≥ 0 |  |
| `qualification_digest` | sha256 | Bound replica qualification |
| `route_generation` | integer ≥ 0 |  |
| `snapshot_generation` | integer ≥ 0 | Increments per publish |
| `observed_at_unix_ms` | integer ≥ 0 |  |
| `tracks` | list of objects | Per-track state: admitted count, queue, reservation, applied work |
| `placements` | list of objects | Per-placement counters: admitted requests, prefill/decode work, frames, memory/KV ownership, cancellation, cleanup, health |
| `batch_mode` | bounded text | `"none"` \| `"replica_only"` \| `"tracked"` — runtime-reported, never inferred from overlap |
| `admitted_requests` | list of objects | Request-to-track binding (request_id, track_id, placement_sequence, edge_set) |
| `rejected_admissions` | list of objects | Admissions refused with reason (saturation, no-qualified-track, etc.) |
| `replica_loss_actions` | list of objects | Which tracks were blocked after a placement loss |

### Privacy redaction rule

`admitted_requests` must contain only request_id, track_id, placement sequence, edge set. **No prompts, no output text, no token IDs, no logits, no KV contents, no credentials.**

## Shape 4: `mycelium.replica_benchmark.v1`

Benchmark run output. Frozen primary-only and replicated workloads, samples, throughput/latency/fairness/resource results, prediction error, material-gain decision, and provenance.

### Required fields

| Field | Type | Description |
|---|---|---|
| `protocol` | literal string | `"mycelium.replica_benchmark.v1"` |
| `benchmark_run_id` | bounded text |  |
| `benchmark_protocol_digest` | sha256 | Bound `benchmark_protocol.v1.json` digest |
| `workload_manifest_digest` | sha256 | Bound `workload_manifest.v1.json` digest |
| `deployment_id` | bounded text |  |
| `deployment_epoch` | integer ≥ 0 |  |
| `route_generation` | integer ≥ 0 |  |
| `started_at_unix_ms` | integer ≥ 0 |  |
| `finished_at_unix_ms` | integer ≥ 0 |  |
| `primary_only_samples` | list of objects | Per-window throughput/latency for primary-only |
| `replicated_samples` | list of objects | Per-window throughput/latency for replicated |
| `paired_improvements` | list of floats | Six baseline/replicated fractional improvements |
| `point_estimate_fraction` | float | Arithmetic mean of paired_improvements |
| `paired_95_percent_bootstrap_lower_bound_fraction` | float | Deterministic bootstrap lower bound (seed 0xA5, 10,000 resamples) |
| `prediction_error_fraction` | float | `predicted_marginal_gain_fraction − point_estimate_fraction` |
| `decision` | enum | `"material"` \| `"not_material"` \| `"inconclusive"` |
| `reasons` | list of bounded text | Decision rationale (inconclusive reasons, invalidations) |
| `qualification_claim` | literal `false` | Per spec §11 — benchmark does not promote a claim |
| `promotion_authorized` | literal `false` | Per spec §11 — benchmark does not authorize promotion |
| `provenance` | object | Software, configuration, instrumentation digests |

### Privacy redaction rule

`primary_only_samples` and `replicated_samples` carry aggregate throughput and latency; **no individual request prompts, output, or token IDs.** `paired_improvements` is the only individual-window result exposed; all other sample detail stays in the underlying benchmark run fixture (`tests/a5_acceptance/benchmark_protocol.v1.json` run fixture, not emitted to the product path).

## Cross-shape invariants

1. **Same canonical digest method.** All four shapes use the same `sha256_canonical_json` digest method.
2. **No private data.** Per spec §7 last paragraph.
3. **Bounded text.** All string identifiers are bounded at 256 UTF-8 bytes.
4. **Generation monotonicity.** `qualifier_generation` is strictly monotonic per qualifier instance; `route_generation` and `snapshot_generation` increment per publish.
5. **Rejection reasons are sorted and unique** in every list-shaped reason field.

## Wire format

All four shapes serialize as canonical JSON (sorted keys, no whitespace, UTF-8). The wire format is identical to the on-disk format. Round-trip JSON parse-and-serialize must preserve the canonical digest.

## Validator pattern

Validators follow `mycelium_live/a4_contracts.py`:

- `_exact(document, fields, code)` rejects any document whose keys are not exactly the listed fields
- `_bounded_text(value)` checks a string is non-empty and ≤256 UTF-8 bytes
- `_digest(value)` checks a sha256:64hex string
- `_integer(value, minimum=0)` checks bounded integer
- One `validate_<shape>(document) -> dict` per shape, raising `ValueError(<code>)` on any violation
- `compatibility_fixtures()` returns a dict of valid fixture documents for the generator
