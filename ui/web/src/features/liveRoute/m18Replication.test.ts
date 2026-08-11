import { describe, expect, it } from 'vitest';
import { decodeM18ReplicaPlan, decodeM18ReplicaRuntime } from './m18Replication';

const sha = (digit: string) => `sha256:${digit.repeat(64)}`;

export function m18PlanFixture() {
  const placement = (id: string, group: string, node: string, start: number, end: number, primary: boolean) => ({ placement_id: id, replica_group_id: group, node_id: node, layer_range: { start, end }, component_roles: ['decoder_layers'], primary, service_capacity_rps: 1 });
  return {
    protocol: 'mycelium.replica_plan.v1', generated_at_unix_ms: 1000,
    deployment: { deployment_id: 'deployment-m18', deployment_epoch: 18, model_id: 'Qwen/Qwen2.5-0.5B-Instruct', model_revision: 'revision', representation_digest: sha('1'), manifest_digest: sha('2'), qualification_id: 'qualification-primary', qualification_digest: sha('3'), decode_mode: 'stage_local_kv', quantization: 'int8' },
    evidence: { generation: 18, evidence_digest: sha('4'), evaluated_at_unix_ms: 1000, valid_until_unix_ms: 2000 },
    planner_snapshot_digest: sha('5'), workload_name: 'concurrent', parallelism: 'data_parallel_request_routing',
    groups: [
      { replica_group_id: 'group-0', layer_range: { start: 0, end: 2 }, component_roles: ['decoder_layers'], primary_placement_id: 'p0', placement_ids: ['p0'] },
      { replica_group_id: 'group-1', layer_range: { start: 2, end: 4 }, component_roles: ['decoder_layers'], primary_placement_id: 'p1', placement_ids: ['p1', 'r1'] },
    ],
    placements: [placement('p0', 'group-0', 'node-0', 0, 2, true), placement('p1', 'group-1', 'node-1', 2, 4, true), placement('r1', 'group-1', 'node-2', 2, 4, false)],
    edges: [{ src_placement_id: 'p0', dst_placement_id: 'p1', kind: 'forward', capacity_rps: 2, cost_ms: 1 }, { src_placement_id: 'p0', dst_placement_id: 'r1', kind: 'forward', capacity_rps: 2, cost_ms: 1 }],
    tracks: [{ track_id: sha('6'), planner_track_id: 'track-000', placement_ids: ['p0', 'p1'], edge_digests: [sha('7')], traffic_fraction: 0.5, cost_ms: 1 }, { track_id: sha('8'), planner_track_id: 'track-001', placement_ids: ['p0', 'r1'], edge_digests: [sha('9')], traffic_fraction: 0.5, cost_ms: 1 }],
    flow: { primary_capacity_rps: 1, replicated_capacity_rps: 2, predicted_gain_rps: 1, unmet_demand_rps: 0 },
    candidate_decisions: [{ iteration: 0, placement_id: 'r1', node_id: 'node-2', replica_group_id: 'group-1', accepted: true, reason: 'accepted_positive_robust_gain', baseline_admitted_rps: 1, proposed_admitted_rps: 2, raw_gain_rps: 1, robust_gain_rps: 0.9, minimum_required_gain_rps: 0.05, failure_domain: 'site-b', failure_domain_warning: null }],
    zero_flow_removed_placement_ids: [], failure_domain_warnings: [], claim_boundary: 'planner intent only', route_ready: false, plan_digest: sha('a'),
  };
}

export function m18RuntimeFixture() {
  const plan = m18PlanFixture();
  return {
    protocol: 'mycelium.replica_runtime.v1', generated_at_monotonic_s: 10, deployment: plan.deployment, replica_plan_digest: plan.plan_digest, parallelism: 'data_parallel_request_routing',
    qualified_tracks: plan.tracks.map((track, index) => ({ track_id: track.track_id, placement_ids: track.placement_ids, traffic_fraction: track.traffic_fraction, qualification_id: `qualification-${index}`, qualification_digest: sha(String(index + 1)), admission_state: 'qualified', active_request_count: index === 0 ? 1 : 0 })),
    requests: [{ request_id: 'request-a', path_id: 'path-a', track_id: plan.tracks[0].track_id, placement_ids: plan.tracks[0].placement_ids, qualification_id: 'qualification-0', qualification_digest: sha('1'), phase: 'decode', admitted_at_monotonic_s: 9, terminal_at_monotonic_s: null, terminal_state: null, placement_work: { p0: { frames_sent: 2, frames_received: 2, work_items: 1 }, p1: { frames_sent: 2, frames_received: 2, work_items: 1 } }, kv_locality: 'request_track_pinned_no_migration' }],
    incidents: [], throughput: { evidence_digest: sha('b'), mode: 'measured_service_rate_weighted_saturation', baseline_request_count: 1, baseline_throughput_rps: 4, replicated_request_count: 6, replicated_throughput_rps: 8, gain_fraction: 1, minimum_required_fraction: 0.05, passed: true }, claim_boundary: 'no recovery',
  };
}

describe('M18 replication contracts', () => {
  it('decodes complete request-level tracks without a tensor-parallel claim', () => {
    const decoded = decodeM18ReplicaPlan(m18PlanFixture());
    expect(decoded.parallelism).toBe('data_parallel_request_routing');
    expect(decoded.tracks.map((track) => track.placement_ids)).toEqual([['p0', 'p1'], ['p0', 'r1']]);
    expect(decoded.route_ready).toBe(false);
  });

  it('decodes immutable request-track and per-placement work attribution', () => {
    const decoded = decodeM18ReplicaRuntime(m18RuntimeFixture());
    expect(decoded.requests[0].track_id).toBe(sha('6'));
    expect(decoded.requests[0].placement_work.p1.work_items).toBe(1);
    expect(decoded.requests[0].kv_locality).toBe('request_track_pinned_no_migration');
    expect(decoded.throughput?.gain_fraction).toBe(1);
  });

  it('rejects unknown fields and recovery claims', () => {
    expect(() => decodeM18ReplicaPlan({ ...m18PlanFixture(), tensor_parallel: true })).toThrow(/unknown or missing/);
    const runtime = m18RuntimeFixture();
    runtime.incidents = [{ incident_id: 'incident-1', kind: 'removed', track_id: sha('6'), reason: 'lost', observed_at_monotonic_s: 11, recovery_claimed: true }] as never;
    expect(() => decodeM18ReplicaRuntime(runtime)).toThrow(/cannot claim recovery/);
  });
});
