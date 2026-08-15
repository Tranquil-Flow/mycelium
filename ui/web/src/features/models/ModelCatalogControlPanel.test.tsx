import { fireEvent, render, screen } from '@testing-library/react';
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
  feasibility_reports: [{ model_id: 'Qwen/Ready', revision, state: 'feasible', planner: 'capability_aware_contiguous_exact_weight_dp', stages: [], reasons: [], evidence_generation: 1, evidence_valid_until_unix_ms: 2_000, evaluated_at_unix_ms: 500, provisioning_authorized: true, maximum_qualified_context_tokens: 4096, maximum_qualified_concurrency: 1, cached_artifact_bytes: 1_073_741_824, missing_artifact_bytes: 0, modeled_transfer_ms: 0, modeled_execution_ms: null, resource_bottleneck: { kind: 'execution', node_id: null, headroom_bytes: null, reason: null }, required_directed_edges: [], feasibility_digest: digest, source_quantization: 'bfloat16', serving_quantization: 'int8-weight-only', serving_dtype: 'float32', representation_digest: digest }],
  selection_authority: 'qualified_deployment_registry', download_policy: 'operator_approval_required',
  lifecycle: { protocol: 'mycelium.model_lifecycle.v1', catalog_digest: digest, models: [
    { model_id: 'Qwen/Ready', revision, artifact_digest: digest, state: 'feasible', authority: 'capability_aware_planner', reason: 'swarm_capacity_feasible', evidence_ref: digest, deployment_ids: [], active_deployment_id: null, selectable: false },
    { model_id: 'Qwen/Other', revision, artifact_digest: digest, state: 'discovered', authority: 'local_model_catalog', reason: 'runtime_adapter_unavailable:qwen2', evidence_ref: digest, deployment_ids: [], active_deployment_id: null, selectable: false },
  ], lifecycle_digest: digest, route_ready: false }, route_ready: false, operation_digest: digest,
};
const activation: DeploymentActivationStatus = { protocol: 'mycelium.deployment_activation.v1', generation: 2, busy_candidate_id: null, invalid_candidate_count: 0, candidates: [{ candidate_id: 'candidate-ready', deployment_id: 'candidate-ready', model_id: 'Qwen/Ready', model_revision: revision, quantization: 'int8-weight-only', topology_size: 3, plan_digest: digest, state: 'prepared', phase: null, completed_steps: 0, total_steps: 4, reason_code: null }] };

describe('ModelCatalogControlPanel', () => {
  it('shows and filters every discovered identity, and activates only the bound candidate', () => {
    const activate = vi.fn(); const refresh = vi.fn();
    render(<ModelCatalogControlPanel operation={operation} activation={activation} capacityRefresh={null} nowUnixMs={1_000} error={null} onActivate={activate} onRefresh={refresh} onRecheckCapacity={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Available models' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: '2 of 2 discovered model identities' })).toBeInTheDocument();
    expect(screen.getAllByText('Ready to activate')).toHaveLength(2);
    expect(screen.getByText(/No action here downloads a model/)).toBeInTheDocument();
    expect(screen.getByText(/bfloat16 → int8-weight-only/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Activate and qualify' }));
    expect(activate).toHaveBeenCalledWith('candidate-ready');
    expect(screen.getByRole('link', { name: 'Add or inspect swarm devices' })).toHaveAttribute('href', '#nodes');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh deployment status' }));
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Qwen/Other')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find a model' }), { target: { value: 'Other' } });
    expect(screen.queryByText('Qwen/Ready')).not.toBeInTheDocument();
    expect(screen.getByRole('table', { name: '1 of 2 discovered model identities' })).toBeInTheDocument();
  });

  it('disables activation while another model is qualifying', () => {
    render(<ModelCatalogControlPanel operation={operation} activation={{ ...activation, busy_candidate_id: 'someone-else' }} capacityRefresh={null} nowUnixMs={1_000} error={null} onActivate={vi.fn()} onRefresh={vi.fn()} onRecheckCapacity={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Activate and qualify' })).toBeDisabled();
  });

  it('keeps the catalogue visible when deployment mutation controls are unavailable', () => {
    render(<ModelCatalogControlPanel operation={operation} activation={{ ...activation, candidates: [] }} capacityRefresh={null} actionsAvailable={false} nowUnixMs={1_000} error="deployment_activation_unavailable" onActivate={vi.fn()} onRefresh={vi.fn()} onRecheckCapacity={vi.fn()} />);
    expect(screen.getByRole('table', { name: '2 of 2 discovered model identities' })).toBeInTheDocument();
    expect(screen.getByText('Qwen/Ready')).toBeInTheDocument();
    expect(screen.getByText(/Preparation and activation controls are unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/Existing qualified inference remains usable/);
  });

  it('can release a qualified prepared route without changing the selected model', () => {
    const unload = vi.fn();
    const qualifiedActivation: DeploymentActivationStatus = { ...activation, candidates: [{ ...activation.candidates[0], state: 'qualified', completed_steps: 4 }] };
    const qualifiedOperation: M17ModelOperation = { ...operation, lifecycle: { ...operation.lifecycle, models: [{ ...operation.lifecycle.models[0], state: 'qualified', selectable: true }, operation.lifecycle.models[1]] } };
    render(<ModelCatalogControlPanel operation={qualifiedOperation} activation={qualifiedActivation} capacityRefresh={null} nowUnixMs={1_000} error={null} onActivate={vi.fn()} onUnload={unload} onRefresh={vi.fn()} onRecheckCapacity={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Unload from memory' }));
    expect(unload).toHaveBeenCalledWith('candidate-ready');
  });

  it('runs an explicit local-only capacity recheck and explains progress', () => {
    const recheck = vi.fn();
    render(<ModelCatalogControlPanel operation={operation} activation={activation} capacityRefresh={{ protocol: 'mycelium.model_capacity_refresh.v1', generation: 2, state: 'refreshing', phase: 'evaluating_models', started_at_unix_ms: 1_000, completed_at_unix_ms: null, operation_digest: null, catalog_generation: null, evaluated_model_count: 0, reason_code: null, download_authorized: false, provisioning_started: false }} nowUnixMs={1_000} error={null} onActivate={vi.fn()} onRefresh={vi.fn()} onRecheckCapacity={recheck} />);
    expect(screen.getByRole('status')).toHaveTextContent(/does not download or provision/);
    expect(screen.getByRole('button', { name: 'Rechecking capacity…' })).toBeDisabled();
  });

  it('renders failed capacity evidence without internal milestone labels', () => {
    render(<ModelCatalogControlPanel operation={operation} activation={activation} capacityRefresh={{ protocol: 'mycelium.model_capacity_refresh.v1', generation: 3, state: 'failed', phase: null, started_at_unix_ms: 1_000, completed_at_unix_ms: 1_100, operation_digest: null, catalog_generation: null, evaluated_model_count: 0, reason_code: 'm17_swarm_evidence_unavailable', download_authorized: false, provisioning_started: false }} nowUnixMs={1_100} error={null} onActivate={vi.fn()} onRefresh={vi.fn()} onRecheckCapacity={vi.fn()} />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Capacity recheck failed: swarm evidence unavailable.');
    expect(alert).not.toHaveTextContent(/m\d+/i);
  });

  it('requires an exact affirmative representation decision before conversion and preparation', () => {
    const prepare = vi.fn();
    render(<ModelCatalogControlPanel operation={operation} activation={{ ...activation, candidates: [] }} capacityRefresh={null} preparation={null} nowUnixMs={1_000} error={null} onActivate={vi.fn()} onPrepare={prepare} onRefresh={vi.fn()} onRecheckCapacity={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Review representation' }));
    const authorize = screen.getByRole('button', { name: 'Authorize representation and prepare' });
    expect(authorize).toBeDisabled();
    expect(screen.getByText(/bfloat16 → int8-weight-only \(float32\)/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: /I authorize creating this exact derived representation/ }));
    fireEvent.click(authorize);
    expect(prepare).toHaveBeenCalledWith({
      protocol: 'mycelium.model_representation_decision.v1',
      model_id: 'Qwen/Ready',
      revision,
      source_quantization: 'bfloat16',
      serving_dtype: 'float32',
      serving_quantization: 'int8-weight-only',
      representation_digest: digest,
      conversion_authorized: true,
    });
    expect(screen.getByText(/no action here downloads a model/i)).toBeInTheDocument();
  });
});
