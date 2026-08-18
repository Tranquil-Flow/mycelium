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
import type { ModelPreparationStatus } from './modelPreparation';

export type ModelCatalogAvailability =
  | 'active'
  | 'qualified'
  | 'activating'
  | 'ready_to_activate'
  | 'activation_failed'
  | 'preparing'
  | 'preparation_failed'
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
  readonly action: 'activate' | 'retry' | 'prepare' | 'retry_prepare' | null;
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

export function humanReason(value: string): string {
  const [rawCode, suffix] = value.split(':', 2);
  const code = rawCode.replace(/^m\d+_/, '');
  const subject = suffix?.trim();
  const reasons: Readonly<Record<string, string>> = Object.freeze({
    insufficient_disk: `Not enough disk space${subject ? ` on ${subject}` : ''}`,
    insufficient_memory: `Not enough safe memory${subject ? ` on ${subject}` : ''}`,
    insufficient_load_memory: `Not enough memory to load this representation${subject ? ` on ${subject}` : ''}`,
    missing_weight_artifact: 'Model weights are incomplete in the local cache',
    missing_tokenizer: 'Tokenizer files are incomplete in the local cache',
    runtime_adapter_unavailable: `No compatible inference runtime${subject ? ` for ${subject}` : ''}`,
    architecture_adapter: `Architecture adapter unavailable${subject ? `: ${subject}` : ''}`,
    unsupported_or_unknown_dtype: 'Checkpoint precision is not supported',
    evidence_stale: 'Swarm capacity evidence is stale',
    no_feasible_contiguous_exact_weight_allocation: 'No safe contiguous layer allocation fits this swarm',
    startup_challenge_failed: 'Distributed startup challenge failed',
    route_unavailable: 'Prepared route is no longer reachable',
    model_preparation_workspace_unavailable: 'Preparation storage was disconnected or changed; reconnect it, restart Mycelium, and retry',
    model_preparation_diagnostic_write_failed: 'Preparation storage could not retain its private diagnostic record',
    owner_metadata_reconciliation_required: 'Waiting for owner metadata reconciliation',
    member_inventory_identity_conflict: 'Member inventories disagree on immutable model identity',
  });
  return reasons[code] ?? `${code}${subject ? `: ${subject}` : ''}`.replaceAll('_', ' ');
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
  preparation: ModelPreparationStatus | null = null,
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
    const isPreparation = preparation?.model_id === entry.model_id && preparation.revision === entry.revision;
    let availability: ModelCatalogAvailability;
    let statusLabel: string;
    let detail: string;
    let action: ModelCatalogRow['action'] = null;
    const selectedCandidateUnavailable = candidate?.state === 'unavailable'
      && model.active_deployment_id === candidate.deployment_id;

    if ((model.state === 'active' || candidate?.state === 'active') && !selectedCandidateUnavailable) {
      availability = 'active'; statusLabel = 'Active'; detail = 'Serving new inference requests now.';
      const freshReplacement = candidate === null
        && report?.state === 'feasible'
        && report.provisioning_authorized
        && report.evidence_valid_until_unix_ms >= nowUnixMs;
      if (freshReplacement && (!isPreparation || preparation?.state === 'idle')) {
        action = 'prepare';
        detail = 'Serving new inference requests now. A replacement deployment can be prepared without interrupting this route.';
      } else if (freshReplacement && isPreparation && preparation?.state === 'failed') {
        action = 'retry_prepare';
      }
    } else if (model.state === 'qualified' || candidate?.state === 'qualified') {
      availability = 'qualified'; statusLabel = 'Qualified'; detail = 'Ready to select from the Model menu.';
    } else if (candidate?.state === 'activating') {
      availability = 'activating'; statusLabel = 'Qualifying'; detail = phaseLabel(candidate.phase);
    } else if (candidate?.state === 'prepared') {
      availability = 'ready_to_activate'; statusLabel = 'Ready to activate'; detail = `Prepared across ${candidate.topology_size} physical ${candidate.topology_size === 1 ? 'stage' : 'stages'}; activation will not download anything.`; action = 'activate';
    } else if (candidate?.state === 'failed' || candidate?.state === 'unavailable') {
      availability = 'activation_failed'; statusLabel = candidate.state === 'unavailable' ? 'Route unavailable' : 'Activation failed'; detail = humanReason(candidate.reason_code ?? (candidate.state === 'unavailable' ? 'route_unavailable' : 'activation_failed')); action = 'retry';
    } else if (isPreparation && preparation?.state === 'preparing') {
      availability = 'preparing'; statusLabel = 'Preparing on swarm'; detail = preparation.phase === 'validating_capacity' ? 'Freezing the current capacity decision.'
        : preparation.phase === 'compiling_assignments' ? 'Splitting the model into assignment-owned stages.'
          : preparation.phase === 'verifying_local_artifacts' ? 'Verifying assigned artifacts and acquiring any missing peer stage data.'
            : preparation.phase === 'staging_peers' ? 'Transferring and verifying only each peer’s assigned model files.'
              : 'Publishing the immutable candidate plan.';
    } else if (isPreparation && preparation?.state === 'failed') {
      availability = 'preparation_failed'; statusLabel = 'Preparation failed'; detail = humanReason(preparation.reason_code ?? 'model_preparation_failed'); action = 'retry_prepare';
    } else if (isPreparation && preparation?.state === 'succeeded') {
      availability = 'preparing'; statusLabel = 'Prepared route published'; detail = 'Refreshing activation status; qualification has not started.';
    } else if (report !== null && report.evidence_valid_until_unix_ms < nowUnixMs) {
      availability = 'needs_capacity_check'; statusLabel = 'Recheck required'; detail = report.state === 'feasible'
        ? 'The last capacity result has expired; no provisioning or transfer is authorized.'
        : 'The last capacity rejection has expired; recheck after devices or their resources change.';
    } else if (report?.state === 'feasible') {
      availability = 'fits_swarm'; action = 'prepare';
      if (report.representation_authority.kind === 'locally_derived_candidate') {
        statusLabel = 'Owner approval required';
        detail = 'Capacity fits, but this exact derived representation is not approved. Review and explicitly authorize it before preparation; downloads remain disabled.';
      } else {
        statusLabel = 'Fits this swarm';
        detail = 'A safe contiguous layer allocation exists for the approved immutable representation and can be prepared from local files without a download.';
      }
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
      prominent: candidate !== null || report !== null || model.selectable || isPreparation,
    });
  }));
}
