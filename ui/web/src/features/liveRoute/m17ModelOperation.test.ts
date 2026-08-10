import { describe, expect, it } from 'vitest';
import { decodeM17ModelOperation } from './m17ModelOperation';

function fixture() {
  const revision = 'a'.repeat(40);
  const catalogDigest = `sha256:${'b'.repeat(64)}`;
  return {
    protocol: 'mycelium.model_operation.v1',
    catalog_generation: 2,
    catalog_digest: catalogDigest,
    catalog: {
      protocol: 'mycelium.model_catalog.v1', generation: 2, catalog_digest: catalogDigest,
      entries: [{
        model_id: 'Qwen/Qwen3-8B', revision, state: 'compatible',
        architecture: 'Qwen3ForCausalLM', adapter_id: 'qwen3',
        checkpoint_format: 'safetensors_sharded', quantization: 'bfloat16',
        num_layers: 36, weight_bytes: 16_381_470_720, exact_tensor_accounting: true,
        required_file_count: 6, present_file_count: 6, reasons: [],
        artifact_digest: `sha256:${'c'.repeat(64)}`,
      }],
    },
    feasibility_reports: [{
      protocol: 'mycelium.model_feasibility.v1', model_id: 'Qwen/Qwen3-8B', revision,
      state: 'feasible', planner: 'capability_aware_contiguous_exact_weight_dp',
      evidence_generation: 15, reasons: [], feasibility_digest: `sha256:${'d'.repeat(64)}`,
      evidence_valid_until_unix_ms: 2_000_000_000_000,
      evaluated_at_unix_ms: 1_900_000_000_000,
      provisioning_authorized: true,
      maximum_qualified_context_tokens: 32_768,
      maximum_qualified_concurrency: 2,
      cached_artifact_bytes: 4_000_000_000,
      missing_artifact_bytes: 4_000_000_000,
      modeled_transfer_ms: 4_000,
      modeled_execution_ms: 250,
      resource_bottleneck: { kind: 'memory', node_id: 'node-0', headroom_bytes: 31_061_189_120 },
      required_directed_edges: [],
      stages: [{ node_id: 'node-0', start_layer: 0, end_layer_exclusive: 15,
        required_memory_bytes: 7_593_516_544, available_memory_bytes: 38_654_705_664,
        headroom_bytes: 31_061_189_120, activation_bytes: 1_000_000,
        kv_bytes: 2_000_000, workspace_bytes: 500_000_000,
        runtime_reserve_bytes: 3_000_000_000, rss_bytes: 1_000_000_000,
        swap_used_bytes: 0, disk_free_bytes: 50_000_000_000,
        required_disk_bytes: 8_000_000_000, cached_artifact_bytes: 4_000_000_000,
        missing_artifact_bytes: 4_000_000_000, backend: 'mlx', dtype: 'bfloat16',
        quantization: 'bfloat16', decode_mode: 'stage_local_kv',
        maximum_context_tokens: 32_768, maximum_concurrency: 2,
        modeled_transfer_ms: 4_000, modeled_service_work_ms: 250,
        thermal_state: null, power_state: 'external' }],
    }],
    selection_authority: 'qualified_deployment_registry',
    download_policy: 'operator_approval_required', route_ready: false,
    lifecycle: {
      protocol: 'mycelium.model_lifecycle.v1', catalog_digest: catalogDigest,
      state_definitions: [], route_ready: false,
      models: [{
        model_id: 'Qwen/Qwen3-8B', revision,
        artifact_digest: `sha256:${'c'.repeat(64)}`,
        state: 'feasible', authority: 'capability_aware_planner',
        reason: 'swarm_capacity_feasible', evidence_ref: `sha256:${'d'.repeat(64)}`,
        deployment_ids: [], active_deployment_id: null, selectable: false,
      }],
      lifecycle_digest: `sha256:${'f'.repeat(64)}`,
    },
    operation_digest: `sha256:${'e'.repeat(64)}`,
  };
}

describe('M17 model operation projection', () => {
  it('keeps compatible and feasible distinct from qualified selection', () => {
    const operation = decodeM17ModelOperation(fixture());
    expect(operation.entries[0].state).toBe('compatible');
    expect(operation.feasibility_reports[0].state).toBe('feasible');
    expect(operation.selection_authority).toBe('qualified_deployment_registry');
    expect(operation.lifecycle.models[0].state).toBe('feasible');
    expect(operation.route_ready).toBe(false);
  });

  it('rejects a catalog that claims route readiness', () => {
    const value = fixture();
    value.route_ready = true;
    expect(() => decodeM17ModelOperation(value)).toThrow(/authority/);
  });

  it('rejects stale or malformed immutable identity', () => {
    const value = fixture();
    value.catalog.entries[0].revision = 'main';
    expect(() => decodeM17ModelOperation(value)).toThrow(/revision/);
  });
});
