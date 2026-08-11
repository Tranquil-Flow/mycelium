import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { DeploymentActivationStatus } from '../liveRoute/deploymentActivation';
import type { M17ModelOperation } from '../liveRoute/m17ModelOperation';
import { ModelCatalogControlPanel } from './ModelCatalogControlPanel';

const revision = 'a'.repeat(40);
const digest = `sha256:${'b'.repeat(64)}`;
const entry = (model_id: string, state: 'compatible' | 'discovered' = 'compatible') => ({ model_id, revision, state, architecture: 'Qwen2ForCausalLM', adapter_id: state === 'compatible' ? 'qwen2_dense' : null, checkpoint_format: 'safetensors', quantization: 'bfloat16', num_layers: 24, weight_bytes: 1_073_741_824, exact_tensor_accounting: true, required_file_count: 4, present_file_count: 4, reasons: state === 'discovered' ? ['runtime_adapter_unavailable:qwen2'] : [], artifact_digest: digest } as const);
const operation: M17ModelOperation = {
  protocol: 'mycelium.model_operation.v1', catalog_generation: 7, catalog_digest: digest,
  entries: [entry('Qwen/Ready'), entry('Qwen/Other', 'discovered')],
  feasibility_reports: [{ model_id: 'Qwen/Ready', revision, state: 'feasible', planner: 'capability_aware_contiguous_exact_weight_dp', stages: [], reasons: [], evidence_generation: 1, evidence_valid_until_unix_ms: 2_000, evaluated_at_unix_ms: 500, provisioning_authorized: true, maximum_qualified_context_tokens: 4096, maximum_qualified_concurrency: 1, cached_artifact_bytes: 1_073_741_824, missing_artifact_bytes: 0, modeled_transfer_ms: 0, modeled_execution_ms: null, resource_bottleneck: { kind: 'execution', node_id: null, headroom_bytes: null, reason: null }, required_directed_edges: [], feasibility_digest: digest }],
  selection_authority: 'qualified_deployment_registry', download_policy: 'operator_approval_required',
  lifecycle: { protocol: 'mycelium.model_lifecycle.v1', catalog_digest: digest, models: [
    { model_id: 'Qwen/Ready', revision, artifact_digest: digest, state: 'feasible', authority: 'capability_aware_planner', reason: 'swarm_capacity_feasible', evidence_ref: digest, deployment_ids: [], active_deployment_id: null, selectable: false },
    { model_id: 'Qwen/Other', revision, artifact_digest: digest, state: 'discovered', authority: 'local_model_catalog', reason: 'runtime_adapter_unavailable:qwen2', evidence_ref: digest, deployment_ids: [], active_deployment_id: null, selectable: false },
  ], lifecycle_digest: digest, route_ready: false }, route_ready: false, operation_digest: digest,
};
const activation: DeploymentActivationStatus = { protocol: 'mycelium.deployment_activation.v1', generation: 2, busy_candidate_id: null, invalid_candidate_count: 0, candidates: [{ candidate_id: 'candidate-ready', deployment_id: 'candidate-ready', model_id: 'Qwen/Ready', model_revision: revision, quantization: 'int8-weight-only', topology_size: 3, plan_digest: digest, state: 'prepared', phase: null, completed_steps: 0, total_steps: 4, reason_code: null }] };

describe('ModelCatalogControlPanel', () => {
  it('shows the actionable catalog first, retains all local identities, and activates only the bound candidate', () => {
    const activate = vi.fn(); const refresh = vi.fn();
    render(<ModelCatalogControlPanel operation={operation} activation={activation} capacityRefresh={null} nowUnixMs={1_000} error={null} onActivate={activate} onRefresh={refresh} onRecheckCapacity={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Model catalog' })).toBeInTheDocument();
    expect(screen.getAllByText('Ready to activate')).toHaveLength(2);
    expect(screen.getByText(/No action here downloads a model/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Activate and qualify' }));
    expect(activate).toHaveBeenCalledWith('candidate-ready');
    expect(screen.getByRole('link', { name: 'Add or inspect swarm devices' })).toHaveAttribute('href', '#nodes');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh deployment status' }));
    expect(refresh).toHaveBeenCalledTimes(1);
    const details = screen.getByText(/Show 1 other local model identity/).closest('details');
    expect(details).not.toBeNull();
    expect(within(details!).getByText('Qwen/Other')).toBeInTheDocument();
  });

  it('disables activation while another model is qualifying', () => {
    render(<ModelCatalogControlPanel operation={operation} activation={{ ...activation, busy_candidate_id: 'someone-else' }} capacityRefresh={null} nowUnixMs={1_000} error={null} onActivate={vi.fn()} onRefresh={vi.fn()} onRecheckCapacity={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Activate and qualify' })).toBeDisabled();
  });

  it('runs an explicit local-only capacity recheck and explains progress', () => {
    const recheck = vi.fn();
    render(<ModelCatalogControlPanel operation={operation} activation={activation} capacityRefresh={{ protocol: 'mycelium.model_capacity_refresh.v1', generation: 2, state: 'refreshing', phase: 'evaluating_models', started_at_unix_ms: 1_000, completed_at_unix_ms: null, operation_digest: null, catalog_generation: null, evaluated_model_count: 0, reason_code: null, download_authorized: false, provisioning_started: false }} nowUnixMs={1_000} error={null} onActivate={vi.fn()} onRefresh={vi.fn()} onRecheckCapacity={recheck} />);
    expect(screen.getByRole('status')).toHaveTextContent(/does not download or provision/);
    expect(screen.getByRole('button', { name: 'Rechecking capacity…' })).toBeDisabled();
  });
});
