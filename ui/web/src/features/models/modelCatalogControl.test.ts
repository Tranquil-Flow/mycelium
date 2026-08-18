import { describe, expect, it } from 'vitest';
import type { DeploymentActivationStatus, PreparedDeploymentCandidate } from '../liveRoute/deploymentActivation';
import type {
  M17CatalogEntry,
  M17FeasibilityReport,
  M17LifecycleModel,
  M17ModelOperation,
} from '../liveRoute/m17ModelOperation';
import { humanReason, projectModelCatalogControls } from './modelCatalogControl';

const revision = 'a'.repeat(40);
const digest = `sha256:${'b'.repeat(64)}`;

function entry(modelId: string, state: M17CatalogEntry['state'] = 'compatible'): M17CatalogEntry {
  return Object.freeze({
    model_id: modelId, revision, state, architecture: 'Qwen2ForCausalLM', adapter_id: state === 'discovered' ? null : 'qwen2_dense',
    checkpoint_format: 'safetensors', quantization: 'bfloat16', num_layers: 24, weight_bytes: 1_000_000,
    exact_tensor_accounting: true, required_file_count: 4, present_file_count: 4,
    reasons: state === 'discovered' ? ['runtime_adapter_unavailable:qwen2'] : [], artifact_digest: digest,
  });
}

function lifecycle(modelId: string, state: M17LifecycleModel['state'] = 'compatible'): M17LifecycleModel {
  return Object.freeze({
    model_id: modelId, revision, artifact_digest: digest, state,
    authority: state === 'active' ? 'qualified_deployment_registry' : 'architecture_runtime_adapter',
    reason: state === 'active' ? 'selected_for_future_requests' : 'catalog_compatible', evidence_ref: digest,
    deployment_ids: state === 'active' ? ['deployment-active'] : [],
    active_deployment_id: state === 'active' ? 'deployment-active' : null,
    selectable: state === 'active' || state === 'qualified',
  });
}

function feasibility(modelId: string, state: M17FeasibilityReport['state'], validUntil = 2_000): M17FeasibilityReport {
  return Object.freeze({
    model_id: modelId, revision, state, planner: 'capability_aware_contiguous_exact_weight_dp', stages: [],
    reasons: state === 'feasible' ? [] : ['insufficient_memory:node-c'], evidence_generation: 1,
    evidence_valid_until_unix_ms: validUntil, evaluated_at_unix_ms: 500,
    provisioning_authorized: state === 'feasible', maximum_qualified_context_tokens: state === 'feasible' ? 4096 : 0,
    maximum_qualified_concurrency: state === 'feasible' ? 1 : 0, cached_artifact_bytes: 1_000_000,
    missing_artifact_bytes: 0, modeled_transfer_ms: 0, modeled_execution_ms: null,
    resource_bottleneck: { kind: state === 'feasible' ? 'execution' : 'memory', node_id: state === 'feasible' ? null : 'node-c', headroom_bytes: null, reason: null },
    required_directed_edges: [], feasibility_digest: digest,
    representation_authority: { kind: 'locally_derived_candidate' as const, owner_decision_digest: null, prior_feasibility_digest: null, source_artifact_digest: null, quantizer: null },
  });
}

function candidate(modelId: string, state: PreparedDeploymentCandidate['state']): PreparedDeploymentCandidate {
  return Object.freeze({
    candidate_id: `deployment-${modelId}`, deployment_id: `deployment-${modelId}`, model_id: modelId, model_revision: revision,
    quantization: 'int8-weight-only', topology_size: 3, plan_digest: digest, state,
    phase: state === 'activating' ? 'opening_route' : null, completed_steps: state === 'activating' ? 2 : state === 'active' || state === 'qualified' ? 4 : 0,
    total_steps: 4, reason_code: state === 'failed' ? 'startup_challenge_failed' : state === 'unavailable' ? 'route_unavailable' : null,
  });
}

function operation(entries: readonly M17CatalogEntry[], reports: readonly M17FeasibilityReport[], models: readonly M17LifecycleModel[]): M17ModelOperation {
  const modelLifecycle: M17ModelOperation['lifecycle'] = { protocol: 'mycelium.model_lifecycle.v1', catalog_digest: digest, models, lifecycle_digest: digest, route_ready: false };
  return Object.freeze({ protocol: 'mycelium.model_operation.v1', catalog_generation: 4, catalog_digest: digest, entries, feasibility_reports: reports,
    selection_authority: 'qualified_deployment_registry', download_policy: 'operator_approval_required',
    lifecycle: modelLifecycle,
    route_ready: false, operation_digest: digest });
}

function activation(candidates: readonly PreparedDeploymentCandidate[]): DeploymentActivationStatus {
  return Object.freeze({ protocol: 'mycelium.deployment_activation.v1', generation: 2, busy_candidate_id: null, invalid_candidate_count: 0, candidates });
}

describe('projectModelCatalogControls', () => {
  it('explains removable preparation storage failures in product language', () => {
    expect(humanReason('model_preparation_workspace_unavailable')).toBe(
      'Preparation storage was disconnected or changed; reconnect it, restart Mycelium, and retry',
    );
  });

  it('joins immutable catalog identity to real activation and selection states', () => {
    const entries = [entry('active'), entry('prepared'), entry('feasible'), entry('large'), entry('unsupported', 'discovered')];
    const rows = projectModelCatalogControls(
      operation(entries, [feasibility('prepared', 'feasible'), feasibility('feasible', 'feasible'), feasibility('large', 'infeasible')], entries.map((item) => lifecycle(item.model_id, item.model_id === 'active' ? 'active' : item.state))),
      activation([candidate('prepared', 'prepared')]),
      1_000,
    );
    expect(rows.map((row) => [row.entry.model_id, row.availability, row.action])).toEqual([
      ['active', 'active', null],
      ['prepared', 'ready_to_activate', 'activate'],
      ['feasible', 'fits_swarm', 'prepare'],
      ['large', 'does_not_fit', null],
      ['unsupported', 'unsupported', null],
    ]);
    expect(rows[3].detail).toBe('Not enough safe memory on node-c');
    expect(rows[2].status_label).toBe('Owner approval required');
    expect(rows[2].detail).toMatch(/not approved.*explicitly authorize.*downloads remain disabled/i);
  });

  it('distinguishes approved immutable capacity from proposed conversion capacity', () => {
    const model = entry('approved');
    const report = feasibility('approved', 'feasible');
    const approved = {
      ...report,
      representation_authority: {
        kind: 'approved_existing_immutable_representation' as const,
        owner_decision_digest: digest,
        prior_feasibility_digest: digest,
        source_artifact_digest: digest,
        quantizer: 'mycelium.rowwise_symmetric_int8.v1',
      },
    };

    const [row] = projectModelCatalogControls(
      operation([model], [approved], [lifecycle('approved')]),
      activation([]),
      1_000,
    );

    expect(row.status_label).toBe('Fits this swarm');
    expect(row.detail).toMatch(/approved immutable representation/i);
  });

  it('fails closed when a formerly feasible capacity decision expires', () => {
    const model = entry('stale');
    const [row] = projectModelCatalogControls(operation([model], [feasibility('stale', 'feasible', 999)], [lifecycle('stale')]), activation([]), 1_000);
    expect(row.availability).toBe('needs_capacity_check');
    expect(row.action).toBeNull();
    expect(row.detail).toMatch(/expired/);
  });

  it('can prepare a fresh replacement while the incumbent identity keeps serving', () => {
    const model = entry('active-replacement');
    const [row] = projectModelCatalogControls(
      operation([model], [feasibility('active-replacement', 'feasible')], [lifecycle('active-replacement', 'active')]),
      activation([]),
      1_000,
    );
    expect(row.availability).toBe('active');
    expect(row.action).toBe('prepare');
    expect(row.detail).toMatch(/without interrupting/);
  });

  it('does not prepare over an equivalent active candidate', () => {
    const model = entry('already-active');
    const [row] = projectModelCatalogControls(
      operation([model], [feasibility('already-active', 'feasible')], [lifecycle('already-active', 'active')]),
      activation([candidate('already-active', 'active')]),
      1_000,
    );
    expect(row.availability).toBe('active');
    expect(row.action).toBeNull();
  });

  it('treats an expired rejection as recheckable after swarm membership changes', () => {
    const model = entry('previously-too-large');
    const [row] = projectModelCatalogControls(operation([model], [feasibility('previously-too-large', 'infeasible', 999)], [lifecycle('previously-too-large')]), activation([]), 1_000);
    expect(row.availability).toBe('needs_capacity_check');
    expect(row.detail).toMatch(/devices or their resources change/);
  });

  it('prefers a qualified candidate over stale duplicate candidate records for one model identity', () => {
    const model = entry('duplicate');
    const [row] = projectModelCatalogControls(operation([model], [], [lifecycle('duplicate')]), activation([candidate('duplicate', 'failed'), candidate('duplicate', 'qualified')]), 1_000);
    expect(row.availability).toBe('qualified');
    expect(row.candidate?.state).toBe('qualified');
  });

  it('offers requalification instead of calling an unavailable selected route active', () => {
    const model = entry('unavailable-route');
    const active = {
      ...lifecycle('unavailable-route', 'active'),
      deployment_ids: ['deployment-unavailable-route'],
      active_deployment_id: 'deployment-unavailable-route',
    };
    const [row] = projectModelCatalogControls(
      operation([model], [feasibility('unavailable-route', 'feasible')], [active]),
      activation([candidate('unavailable-route', 'unavailable')]),
      1_000,
    );
    expect(row.availability).toBe('activation_failed');
    expect(row.status_label).toBe('Route unavailable');
    expect(row.detail).toBe('Prepared route is no longer reachable');
    expect(row.action).toBe('retry');
  });
});
