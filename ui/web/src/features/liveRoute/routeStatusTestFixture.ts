import type { LiveRouteStatus, M13PlacementProjection, M14TopologyProjection } from './routeStatus';

export function a5QualificationFixture(
  changes: Record<string, unknown> = {},
): Record<string, unknown> {
  const digest = (character: string) => `sha256:${character.repeat(64)}`;
  return {
    protocol: 'mycelium.replica_qualification.v1',
    qualification_id: digest('a'), qualification_digest: digest('a'),
    deployment_id: 'deployment-fixture', deployment_epoch: 1,
    replica_group_id: 'group-fixture-0',
    placement_id: 'placement-fixture-replica',
    placement_ids: ['placement-fixture-replica', 'placement-fixture-stage-1'],
    track_id: 'track-fixture', traffic_fraction: 0.5,
    qualifier_generation: 1, issued_at_unix_ms: 10_000,
    expires_at_unix_ms: 2_000_000_000_000,
    evidence_bundle_digest: digest('b'), load_proof_digest: digest('c'),
    assignment_digest: digest('a'), artifact_verification_digest: digest('d'),
    parity_verified: true, startup_challenge_passed: true,
    memory_within_bounds: true, cleanup_within_bounds: true,
    directed_link_qualified: true, workload_envelope_digest: digest('e'),
    rejected_reasons: [], route_ready: true,
    ...changes,
  };
}

export function m13PlacementFixture(): M13PlacementProjection {
  const digest = (character: string) => `sha256:${character.repeat(64)}`;
  return {
    protocol: 'mycelium.m13_placement_projection.v1',
    snapshot_digest: digest('1'), evidence_bundle_digest: digest('2'),
    snapshot_generation: 9, authority_generation: 2,
    verification_key_digest: digest('3'), valid_until_unix_ms: 2_000_000_000_000,
    placement_provenance: 'planner_v2', decode_mode: 'stage_local_kv',
    quantization: 'int8-weight-only',
    nodes: ['node-0', 'node-1'].map((node_id, index) => ({
      node_id, backend: 'mlx', decode_mode: 'stage_local_kv', start_layer: index * 12,
      end_layer_exclusive: (index + 1) * 12, fast_allocatable_bytes: 20_000_000_000,
      total_allocatable_bytes: 40_000_000_000, prefill_ms_per_layer_token: 0.1,
      decode_ms_per_layer_token: 0.2, profile_digest: digest(index === 0 ? '4' : '5'),
      source_evidence_digest: digest('6'), assignment_id: `assignment-${index}`,
      assignment_digest: digest(index === 0 ? '7' : '8'), assigned_object_count: 2,
      load_proof_digest: digest(index === 0 ? '9' : 'a'), ready: true,
    })),
    links: [
      { src: 'node-0', dst: 'node-1', rtt_ms: 4, jitter_ms: 0.5, bandwidth_Bps: 25_000_000 },
      { src: 'node-1', dst: 'node-0', rtt_ms: 5, jitter_ms: 0.7, bandwidth_Bps: 20_000_000 },
    ],
    exclusions: [],
    ab_deltas: [{
      kind: 'compute_only', changed_input: 'node-0 decode coefficient x4',
      baseline_snapshot_digest: digest('b'), candidate_snapshot_digest: digest('c'),
      allocation_before: [{ node_id: 'node-0', start: 0, end: 12 }, { node_id: 'node-1', start: 12, end: 24 }],
      allocation_after: [{ node_id: 'node-0', start: 0, end: 8 }, { node_id: 'node-1', start: 8, end: 24 }],
    }],
    promotion: { candidate_deployment_id: 'deployment-1', incumbent_deployment_id: 'deployment-0', decision: 'promote', reasons: [], sample_size: 2 },
    route_ready: false,
  };
}

export function liveRouteStatusFixture(): LiveRouteStatus {
  return {
    protocol: 'mycelium.live_route_status.v1',
    route_alive: true,
    simulated: false,
    route_identity_digest: `sha256:${'a'.repeat(64)}`,
    deployment_id: 'deployment-1',
    model_id: 'Qwen/Qwen2.5-0.5B-Instruct',
    topology_version: 4,
    decode_mode: 'stage_local_kv',
    counters: { frames_sent: 40, frames_received: 39, applied_operation_count: 24, fatal: null },
    stages: [
      {
        stage_id: 'stage-0', placement_id: 'placement-0', node_id: 'node-0',
        runtime_backend: 'mlx', start_layer: 0, end_layer_exclusive: 12,
        component_roles: ['input_embedding', 'decoder'],
      },
      {
        stage_id: 'stage-1', placement_id: 'placement-1', node_id: 'node-1',
        runtime_backend: 'mlx', start_layer: 12, end_layer_exclusive: 24,
        component_roles: ['decoder', 'final_norm', 'lm_head'],
      },
    ],
    peers: [
      {
        node_id: 'node-0', placements: [], frames_sent: 20, frames_received: 19,
        applied_operation_count: 12, decode_mode: 'stage_local_kv',
        architecture: 'qwen2', supported_decode_modes: ['complete_context_replay', 'stage_local_kv'],
        data_plane_health_observed: true, sidecar_process_alive: true,
        transport_running: true, transport_fatal: false,
        transport_fatal_code: null,
        active_kv_state_count: 0, retained_result_count: 2,
        active_kv_bytes: 0, peak_kv_bytes: 4096, current_position: null,
        prefill_operation_count: 1, prefill_input_token_count: 9,
        decode_operation_count: 8, decode_input_token_count: 8,
        activation_output_bytes: 8192,
        release_state: 'released', last_release_reason: 'normal_completion',
        release_counts: { request_complete: 1 },
        interruptibility: { runtime_backend: 'mlx', decode_mode: 'stage_local_kv', work_unit: 'transformer_layer', maximum_observed_work_unit_ms: 125, observed_work_unit_count: 24, maximum_total_cleanup_ms: 2_000, physical_proof_required: true, backend_candidate: true, cooperative_bound_candidate: true },
      },
    ],
    recent_inferences: [
      {
        context_tokens: 9, output_tokens: 8, prefill_ms: 120, ttft_ms: 140,
        tpot_ms: 25, total_ms: 315,
        peer_counter_deltas: [
          { node_id: 'node-0', frames_sent: 10, frames_received: 9, applied_operation_count: 6 },
        ],
      },
    ],
    incidents: [],
    placement: null,
    topology: null,
    liveness: { protocol: 'mycelium.traffic_liveness.v1', deployment_id: 'deployment-1', generated_at_monotonic_ms: 100, subjects: [], incidents: [], deployment_fatal_reason: null },
    concurrency_liveness_qualification: { protocol: 'mycelium.product_concurrency_liveness_qualification.v1', deployment_id: 'deployment-1', qualification_digest: `sha256:${'b'.repeat(64)}`, maximum_concurrent_requests: 4, cancellation_and_cleanup_bound_ms: 2_000, cooperative_interruption_proven: false, request_scoped_cleanup_proven: false, shared_process_termination_used: false, publisher_generation_fencing_proven: false, scoped_liveness_proven: false, eligible: false, evidence_digest: `sha256:${'c'.repeat(64)}` },
    replica_track_qualification: [a5QualificationFixture({
      deployment_id: 'deployment-1',
    }) as unknown as LiveRouteStatus['replica_track_qualification'][number]],
    replica_loss_placement_ids: [],
  };
}

export function m14TopologyFixture(): M14TopologyProjection {
  const digest = (character: string) => `sha256:${character.repeat(64)}`;
  const nodes = ['node-0', 'node-1', 'node-2'];
  const edge = (src: string, dst: string, logical_role: 'physical_only' | 'forward' | 'decode_loopback', index: number) => ({
    src, dst, src_endpoint_digest: digest('d'), dst_endpoint_digest: digest('e'),
    path_class: 'direct' as const, relay_identity: null, relay_region: null,
    rtt_ms: 4 + index, jitter_ms: 0.25, loss_ratio: 0, goodput_Bps: 10_000_000,
    sample_count: 8, connections_opened: 1, frames_sent: 8, connection_generation: 1,
    fresh_until_unix_ms: 2_000_000_000_000, observation_digest: digest('f'),
    formula: 'one_way_rtt_plus_jitter_v1' as const, logical_role,
  });
  const roles = new Map<string, 'forward' | 'decode_loopback'>([
    ['node-0-node-1', 'forward'], ['node-1-node-2', 'forward'], ['node-2-node-0', 'decode_loopback'],
  ] as const);
  return {
    protocol: 'mycelium.m14_topology_projection.v1', measurement_source: 'iroh_activation_plane',
    decision: {
      mode: 'exact', globally_exact: true, explored_candidates: 2,
      selected_cycle: nodes, selected_cost_ms: 8.625, opened_order: nodes,
      loopback: { src: 'node-2', dst: 'node-0' }, canonical_node_id_order: nodes,
      differs_from_canonical_order: false,
      candidates: [
        { order: nodes, cost_ms: 8.625, selected: true, rejection_reason: null },
        { order: ['node-0', 'node-2', 'node-1'], cost_ms: 12.625, selected: false, rejection_reason: null },
      ],
      winning_rationale: 'minimum measured directed RTT/2 plus jitter; stable lexicographic tie-break',
    },
    allocation: nodes.map((node_id, index) => ({ node_id, start: index * 8, end: (index + 1) * 8 })),
    edges: nodes.flatMap((src, srcIndex) => nodes.filter((dst) => dst !== src).map((dst, dstIndex) => edge(src, dst, roles.get(`${src}-${dst}`) ?? 'physical_only', srcIndex * 2 + dstIndex))),
    exclusions: [], promotion: null, route_ready: false,
  };
}
