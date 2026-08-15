import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M17ModelOperationPanel } from './M17ModelOperationPanel';
import type { M17ModelOperation } from './m17ModelOperation';

const rejection = "architecture_adapter:unsupported model_type: 'qwen3_moe'";
const readableRejection = "Architecture adapter unavailable: unsupported model_type: 'qwen3_moe'";
const revision = 'a'.repeat(40);
const digest = (character: string) => `sha256:${character.repeat(64)}`;
const richStage = {
  node_id: 'node-0', start_layer: 0, end_layer_exclusive: 36,
  required_memory_bytes: 20_000_000_000, available_memory_bytes: 40_000_000_000,
  headroom_bytes: 16_000_000_000, activation_bytes: 500_000_000,
  kv_bytes: 1_000_000_000, workspace_bytes: 500_000_000,
  runtime_reserve_bytes: 2_000_000_000, rss_bytes: 1_000_000_000,
  swap_used_bytes: 0, disk_free_bytes: 50_000_000_000,
  required_disk_bytes: 8_000_000_000, cached_artifact_bytes: 4_000_000_000,
  missing_artifact_bytes: 4_000_000_000, backend: 'mlx', dtype: 'bfloat16',
  quantization: 'bfloat16', decode_mode: 'stage_local_kv',
  maximum_context_tokens: 32_768, maximum_concurrency: 2,
  modeled_transfer_ms: 4_000, modeled_service_work_ms: 250,
  thermal_state: null, power_state: 'external',
} as const;
const feasibleEnvelope = {
  evidence_generation: 15, evidence_valid_until_unix_ms: 2_000_000_000_000,
  evaluated_at_unix_ms: 1_900_000_000_000, provisioning_authorized: true,
  maximum_qualified_context_tokens: 32_768, maximum_qualified_concurrency: 2,
  cached_artifact_bytes: 4_000_000_000, missing_artifact_bytes: 4_000_000_000,
  modeled_transfer_ms: 4_000, modeled_execution_ms: 250,
  resource_bottleneck: { kind: 'memory', node_id: 'node-0', headroom_bytes: 16_000_000_000, reason: null },
  required_directed_edges: [],
} as const;
const rejectedEnvelope = {
  evidence_generation: 15, evidence_valid_until_unix_ms: 2_000_000_000_000,
  evaluated_at_unix_ms: 1_900_000_000_000, provisioning_authorized: false,
  maximum_qualified_context_tokens: 0, maximum_qualified_concurrency: 0,
  cached_artifact_bytes: 0, missing_artifact_bytes: 0,
  modeled_transfer_ms: 0, modeled_execution_ms: null,
  resource_bottleneck: { kind: 'rejection', node_id: null, headroom_bytes: null, reason: rejection },
  required_directed_edges: [],
} as const;
const operation: M17ModelOperation = {
  protocol: 'mycelium.model_operation.v1', catalog_generation: 2,
  catalog_digest: digest('a'), selection_authority: 'qualified_deployment_registry',
  download_policy: 'operator_approval_required', route_ready: false,
  operation_digest: digest('f'),
  entries: [
    { model_id: 'Qwen/Qwen3-8B', revision, state: 'compatible', architecture: 'Qwen3ForCausalLM', adapter_id: 'qwen3', checkpoint_format: 'safetensors_sharded', quantization: 'bfloat16', num_layers: 36, weight_bytes: 16_000_000_000, exact_tensor_accounting: true, required_file_count: 5, present_file_count: 5, reasons: [], artifact_digest: digest('b') },
    { model_id: 'Qwen/Qwen3-30B-A3B-Instruct-2507', revision: 'b'.repeat(40), state: 'incomplete', architecture: 'Qwen3MoeForCausalLM', adapter_id: null, checkpoint_format: 'safetensors_sharded', quantization: 'bfloat16', num_layers: 48, weight_bytes: 0, exact_tensor_accounting: false, required_file_count: 16, present_file_count: 0, reasons: [rejection], artifact_digest: digest('c') },
  ],
  feasibility_reports: [
    { model_id: 'Qwen/Qwen3-8B', revision, state: 'feasible', planner: 'capability_aware_contiguous_exact_weight_dp', stages: [richStage], reasons: [], ...feasibleEnvelope, feasibility_digest: digest('d') },
    { model_id: 'Qwen/Qwen3-30B-A3B-Instruct-2507', revision: 'b'.repeat(40), state: 'infeasible', planner: 'capability_aware_contiguous_exact_weight_dp', stages: [], reasons: [rejection], ...rejectedEnvelope, feasibility_digest: digest('e') },
  ],
  lifecycle: {
    protocol: 'mycelium.model_lifecycle.v1', catalog_digest: digest('a'), lifecycle_digest: digest('9'), route_ready: false,
    models: [
      { model_id: 'Qwen/Qwen3-8B', revision, artifact_digest: digest('b'), state: 'feasible', authority: 'capability_aware_planner', reason: 'swarm_capacity_feasible', evidence_ref: digest('d'), deployment_ids: [], active_deployment_id: null, selectable: false },
      { model_id: 'Qwen/Qwen3-30B-A3B-Instruct-2507', revision: 'b'.repeat(40), artifact_digest: digest('c'), state: 'incomplete', authority: 'local_model_catalog', reason: rejection, evidence_ref: digest('c'), deployment_ids: [], active_deployment_id: null, selectable: false },
    ],
  },
};

describe('M17 model operation UI convergence', () => {
  for (const view of ['plans', 'readiness', 'inference', 'incidents'] as const) {
    it(`shows the same fail-closed reason in ${view}`, () => {
      render(<M17ModelOperationPanel operation={operation} view={view} />);
      expect(screen.getByText(readableRejection)).toBeInTheDocument();
    });
  }

  it('shows assignment-local ranges and capacity without private cache paths on Nodes', () => {
    render(<M17ModelOperationPanel operation={operation} view="nodes" />);
    expect(screen.getByRole('heading', { name: /model catalogue and proposed swarm fit/i })).toBeInTheDocument();
    expect(screen.getByText(/planner intent · not active runtime/i)).toBeInTheDocument();
    expect(screen.getByRole('table', { name: /proposed placement and capacity estimates/i })).toBeInTheDocument();
    expect(screen.getByText('[0, 36)')).toBeInTheDocument();
    expect(screen.getByText(/node-0/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\/Users\/|\.cache\/huggingface/);
  });
});
