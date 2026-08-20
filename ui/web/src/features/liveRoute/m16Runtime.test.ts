import { describe, expect, it } from 'vitest';
import { decodeM16RuntimeStatus } from './m16Runtime';

export function m16RuntimeFixture() {
  return {
    protocol: 'mycelium.concurrent_request_runtime.v1', generated_at_monotonic_s: 100, deployment_id: 'deployment-m16', deployment_epoch: 16, topology_version: 7, graph_digest: `sha256:${'1'.repeat(64)}`,
    queue: { depth: 1, maximum_items: 64, queued_bytes: 32, maximum_bytes: 1024, interactive_depth: 1, batch_depth: 0, active_request_ids: [], maximum_active_requests: 4 },
    placements: [{ placement_id: 'placement-a', node_id: 'node-a', memory_capacity_bytes: 1000, reserved_memory_bytes: 100, free_memory_bytes: 900, kv_capacity_bytes: 500, reserved_kv_bytes: 50, free_kv_bytes: 450, workspace_capacity_bytes: 250, reserved_workspace_bytes: 50, free_workspace_bytes: 200, active_reservations: 1, maximum_reservations: 4 }],
    requests: [{ request_id: 'request-a', workload_profile_id: 'interactive_chat_v1', qos_class: 'interactive', phase: 'queued', path_id: 'path-a', path_attempt: 0, path_manifest_digest: `sha256:${'2'.repeat(64)}`, topology_version: 7, path_state: 'locked', candidate_placement_ids: ['placement-a'], placement_ids: ['placement-a'], reservation_count: 1, admitted_at_monotonic_s: 100, queued_at_monotonic_s: 100, dispatch_at_monotonic_s: null, terminal_at_monotonic_s: null, queue_wait_ms: null, terminal_state: null }],
    incidents: [],
    batch_state: { mode: 'sequential_dispatch', maximum_runtime_batch_size: 20, observed_batches: [], continuous_batching: false, pipeline_overlap: false },
    claim_boundary: 'bounded admission and sequential physical dispatch',
    performance_budgets: [],
  };
}

describe('M16 runtime status contract', () => {
  it('decodes the closed privacy-reduced resource projection', () => {
    const decoded = decodeM16RuntimeStatus(m16RuntimeFixture());
    expect(decoded.queue.interactive_depth).toBe(1);
    expect(decoded.placements[0].free_kv_bytes).toBe(450);
  });

  it('rejects unknown private fields and inflated batch claims', () => {
    expect(() => decodeM16RuntimeStatus({ ...m16RuntimeFixture(), prompt: 'private' })).toThrow(/unknown|missing/i);
    const invalid = m16RuntimeFixture();
    invalid.batch_state.continuous_batching = true;
    expect(() => decodeM16RuntimeStatus(invalid)).toThrow(/claim boundary/i);
  });
});
