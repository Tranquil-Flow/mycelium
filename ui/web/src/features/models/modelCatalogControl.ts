import type {
  DeploymentActivationPhase,
  DeploymentActivationStatus,
  PreparedDeploymentCandidate,
} from '../liveRoute/deploymentActivation';
import type {
  M17CatalogEntry,
  M17FeasibilityReport,
  M17LifecycleModel,
  M17ModelOperation,
} from '../liveRoute/m17ModelOperation';

export type ModelCatalogAvailability =
  | 'active'
  | 'qualified'
  | 'activating'
  | 'ready_to_activate'
  | 'activation_failed'
  | 'fits_swarm'
  | 'needs_capacity_check'
  | 'does_not_fit'
  | 'compatible'
  | 'unsupported'
  | 'incomplete';

export interface ModelCatalogRow {
  readonly identity: string;
  readonly entry: M17CatalogEntry;
  readonly lifecycle: M17LifecycleModel;
  readonly feasibility: M17FeasibilityReport | null;
  readonly candidate: PreparedDeploymentCandidate | null;
  readonly availability: ModelCatalogAvailability;
  readonly status_label: string;
  readonly detail: string;
  readonly action: 'activate' | 'retry' | null;
  readonly prominent: boolean;
}

const candidatePriority: Readonly<Record<PreparedDeploymentCandidate['state'], number>> = Object.freeze({
  active: 6,
  qualified: 5,
  activating: 4,
  prepared: 3,
  failed: 2,
  unavailable: 1,
});

function identity(modelId: string, revision: string): string {
  return `${modelId}@${revision}`;
}

function humanReason(value: string): string {
  const [code, suffix] = value.split(':', 2);
  const subject = suffix?.trim();
  const reasons: Readonly<Record<string, string>> = Object.freeze({
    insufficient_disk: `Not enough disk space${subject ? ` on ${subject}` : ''}`,
    insufficient_memory: `Not enough safe memory${subject ? ` on ${subject}` : ''}`,
    missing_weight_artifact: 'Model weights are incomplete in the local cache',
    missing_tokenizer: 'Tokenizer files are incomplete in the local cache',
    runtime_adapter_unavailable: `No compatible inference runtime${subject ? ` for ${subject}` : ''}`,
    architecture_adapter: `Architecture adapter unavailable${subject ? `: ${subject}` : ''}`,
    unsupported_or_unknown_dtype: 'Checkpoint precision is not supported',
    evidence_stale: 'Swarm capacity evidence is stale',
    no_feasible_contiguous_exact_weight_allocation: 'No safe contiguous layer allocation fits this swarm',
    startup_challenge_failed: 'Distributed startup challenge failed',
    route_unavailable: 'Prepared route is no longer reachable',
  });
  return reasons[code] ?? value.replaceAll('_', ' ');
}

function phaseLabel(value: DeploymentActivationPhase | null): string {
  if (value === 'validating_plan') return 'Validating its immutable plan';
  if (value === 'opening_route') return 'Loading its physical stages';
  if (value === 'qualifying_route') return 'Running its distributed qualification challenge';
  if (value === 'registering') return 'Adding it to the qualified model selector';
  return 'Activation is running';
}

function candidateByIdentity(status: DeploymentActivationStatus): ReadonlyMap<string, PreparedDeploymentCandidate> {
  const candidates = new Map<string, PreparedDeploymentCandidate>();
  for (const candidate of status.candidates) {
    const key = identity(candidate.model_id, candidate.model_revision);
    const previous = candidates.get(key);
    if (previous === undefined || candidatePriority[candidate.state] > candidatePriority[previous.state]) {
      candidates.set(key, candidate);
    }
  }
  return candidates;
}

export function projectModelCatalogControls(
  operation: M17ModelOperation,
  activation: DeploymentActivationStatus,
  nowUnixMs: number,
): readonly ModelCatalogRow[] {
  const lifecycle = new Map(operation.lifecycle.models.map((model) => [identity(model.model_id, model.revision), model]));
  const feasibility = new Map(operation.feasibility_reports.map((report) => [identity(report.model_id, report.revision), report]));
  const candidates = candidateByIdentity(activation);
  return Object.freeze(operation.entries.map((entry): ModelCatalogRow => {
    const key = identity(entry.model_id, entry.revision);
    const model = lifecycle.get(key);
    if (model === undefined) throw new TypeError(`catalog lifecycle is missing ${key}`);
    const report = feasibility.get(key) ?? null;
    const candidate = candidates.get(key) ?? null;
    let availability: ModelCatalogAvailability;
    let statusLabel: string;
    let detail: string;
    let action: ModelCatalogRow['action'] = null;

    if (model.state === 'active' || candidate?.state === 'active') {
      availability = 'active'; statusLabel = 'Active'; detail = 'Serving new inference requests now.';
    } else if (model.state === 'qualified' || candidate?.state === 'qualified') {
      availability = 'qualified'; statusLabel = 'Qualified'; detail = 'Ready to select from the Model menu.';
    } else if (candidate?.state === 'activating') {
      availability = 'activating'; statusLabel = 'Qualifying'; detail = phaseLabel(candidate.phase);
    } else if (candidate?.state === 'prepared') {
      availability = 'ready_to_activate'; statusLabel = 'Ready to activate'; detail = `Prepared across ${candidate.topology_size} physical ${candidate.topology_size === 1 ? 'stage' : 'stages'}; activation will not download anything.`; action = 'activate';
    } else if (candidate?.state === 'failed') {
      availability = 'activation_failed'; statusLabel = 'Activation failed'; detail = humanReason(candidate.reason_code ?? 'activation_failed'); action = 'retry';
    } else if (report !== null && report.evidence_valid_until_unix_ms < nowUnixMs) {
      availability = 'needs_capacity_check'; statusLabel = 'Recheck required'; detail = report.state === 'feasible'
        ? 'The last capacity result has expired; no provisioning or transfer is authorized.'
        : 'The last capacity rejection has expired; recheck after devices or their resources change.';
    } else if (report?.state === 'feasible') {
      availability = 'fits_swarm'; statusLabel = 'Fits this swarm'; detail = 'A safe contiguous layer allocation exists, but no prepared deployment is available to qualify yet.';
    } else if (report?.state === 'infeasible') {
      availability = 'does_not_fit'; statusLabel = 'Does not fit'; detail = humanReason(report.reasons[0] ?? 'No feasible allocation');
    } else if (entry.state === 'compatible') {
      availability = 'compatible'; statusLabel = 'Compatible'; detail = 'Runtime support exists; this model has not been evaluated against current swarm capacity.';
    } else if (entry.state === 'discovered') {
      availability = 'unsupported'; statusLabel = 'Found, unsupported'; detail = humanReason(entry.reasons[0] ?? 'Runtime support is unavailable');
    } else {
      availability = 'incomplete'; statusLabel = 'Incomplete'; detail = humanReason(entry.reasons[0] ?? 'Required local files are missing');
    }

    return Object.freeze({
      identity: key,
      entry,
      lifecycle: model,
      feasibility: report,
      candidate,
      availability,
      status_label: statusLabel,
      detail,
      action,
      prominent: candidate !== null || report !== null || model.selectable,
    });
  }));
}
