# RouteQualificationV1 lifecycle evidence

Status: executable contract for `mycelium.route_qualification.v1`.
Authority: `mycelium_qualification.qualifier:RouteQualificationV1`.

This document describes the mandatory `lifecycle_evidence` member of
`run/route-challenge.json`. The Python validator and compatibility fixture are
normative if prose and code differ.

## Claim boundary

Acceptance proves that one immutable physical-qualification bundle contains
coherent observations for distributed cancellation, remote disconnect,
process/endpoint/generation rotation, recovery prefill, token continuity,
stale-frame rejection, and cleanup. It does not independently prove that the
observations came from physical devices; signatures, provenance, transport,
process, timing, parity, negative-run, and evidence-manifest gates provide the
remaining bindings.

Synthetic fixtures remain `route_ready=false`. Only the qualification
authority may issue `route_ready=true`.

## Top-level shape

```json
{
  "kind": "route_lifecycle_evidence_v1",
  "run_id": "<same run_id as route challenge>",
  "cancellation": { "...": "see below" },
  "recovery": { "...": "see below" }
}
```

Unknown or missing fields fail closed. The accepted document is canonically
hashed into `RouteQualificationV1.lifecycle_evidence_digest` and into the
qualification ID material.

## Cancellation evidence

Exact fields:

- `request_id`: non-empty; distinct from the recovered qualification request.
- `path_id`: non-empty; distinct from the recovered qualification path.
- `path_attempt`: positive integer.
- `path_cancellation_observed`: literal `true`.
- `transport_cancellation_observed`: literal `true`.
- `entry_terminal_state`: exactly `cancelled`.
- `remote_terminal_state`: exactly `cancelled`.
- `post_cancel_token_count`: integer zero.
- `local_kv_released`: literal `true`.
- `remote_kv_released`: literal `true`.
- `reservations_released`: literal `true`.
- `capacity_released`: literal `true`.
- `pending_deliveries`: integer zero.
- `trace_digest`: canonical `sha256:<64 lowercase hex>` reference.

## Disconnect, restart, and recovery evidence

Exact fields:

Identity and path binding:

- `request_id`: equals the qualified path request ID.
- `failed_stage_id`: names an accepted stage binding.
- `old_placement_id`: non-empty and differs from `replacement_placement_id`.
- `replacement_placement_id`: equals the final accepted stage binding.
- `old_process_id`: positive integer and differs from `new_process_id`.
- `new_process_id`: equals the final accepted stage binding.
- `process_host_id`: equals the final accepted stage binding.
- `old_endpoint_id`: non-empty and differs from `new_endpoint_id`.
- `new_endpoint_id`: equals the final accepted stage binding.

Rotation and recovery state:

- `old_peer_generation`, `new_peer_generation`: integers with strict increase.
- `old_topology_version`, `new_topology_version`: integers with strict increase;
  new value equals the accepted execution graph topology version.
- `old_path_attempt`, `new_path_attempt`: positive integers with strict increase;
  new value equals the accepted path attempt.
- `failure_observed`: literal `true`.
- `remote_disconnect_observed`: literal `true`.
- `peer_drop_observed`: literal `true`.
- `old_process_exited`: literal `true`.
- `replacement_process_started`: literal `true`.
- `stale_generation_rejected`: literal `true`.
- `stale_frame_rejected`: literal `true`.
- `recovery_phase`: exactly `RECOVERY_PREFILL`.
- `recovery_prefill_observed`: literal `true`.

Continuity and cleanup:

- `generated_token_ids_before_failure`: non-empty integer-token list.
- `generated_token_ids_after_recovery`: non-empty integer-token list.
- `final_token_ids`: exact concatenation of before/after lists and exact match to
  distributed token parity.
- `reference_token_ids`: exact match to final and reference parity tokens.
- `event_sequences`: strict consecutive sequence equal to accepted decode event
  sequences; duplicates and omissions fail.
- `full_model_fallback`: literal `false`.
- `local_kv_released`: literal `true`.
- `remote_kv_released`: literal `true`.
- `reservations_released`: literal `true`.
- `capacity_released`: literal `true`.
- `pending_deliveries`: integer zero.
- `trace_digest`: canonical `sha256:<64 lowercase hex>` reference.

## Stable failure classes

The authority reports specific fail-closed codes, including:

- `lifecycle_evidence_invalid`
- `cancellation_evidence_invalid`
- `cancellation_not_observed`
- `post_cancel_token_emitted`
- `cancellation_cleanup_incomplete`
- `recovery_evidence_invalid`
- `recovery_stage_mismatch`
- `recovery_identity_not_rotated`
- `recovery_generation_not_rotated`
- `recovery_topology_invalid`
- `recovery_path_attempt_invalid`
- `recovery_not_observed`
- `stale_generation_accepted`
- `recovery_prefill_missing`
- `recovery_token_continuity_invalid`
- `sequence_replay`
- `full_model_fallback`
- `recovery_cleanup_incomplete`
