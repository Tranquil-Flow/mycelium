export const DEPLOYMENT_ACTIVATION_PATH = '/__mycelium/deployment-activation';
export const DEPLOYMENT_ACTIVATION_START_PATH = '/__mycelium/deployment-activation/start';
export const DEPLOYMENTS_CHANGED_EVENT = 'mycelium:deployments-changed';

export type DeploymentActivationState =
  | 'prepared'
  | 'activating'
  | 'qualified'
  | 'active'
  | 'unavailable'
  | 'failed';

export type DeploymentActivationPhase =
  | 'validating_plan'
  | 'opening_route'
  | 'qualifying_route'
  | 'registering';

export type PreparedDeploymentCandidate = Readonly<{
  candidate_id: string;
  deployment_id: string;
  model_id: string;
  model_revision: string;
  quantization: string;
  topology_size: number;
  plan_digest: string;
  state: DeploymentActivationState;
  phase: DeploymentActivationPhase | null;
  completed_steps: number;
  total_steps: 4;
  reason_code: string | null;
}>;

export type DeploymentActivationStatus = Readonly<{
  protocol: 'mycelium.deployment_activation.v1';
  generation: number;
  busy_candidate_id: string | null;
  invalid_candidate_count: number;
  candidates: readonly PreparedDeploymentCandidate[];
}>;

export interface DeploymentActivationClient {
  status(signal?: AbortSignal): Promise<DeploymentActivationStatus>;
  activate(candidateId: string, signal?: AbortSignal): Promise<DeploymentActivationStatus>;
}

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const REVISION = /^[0-9a-f]{40}$/;
const SAFE_CODE = /^[a-z][a-z0-9_]{0,63}$/;
const STATES = new Set<DeploymentActivationState>([
  'prepared', 'activating', 'qualified', 'active', 'unavailable', 'failed',
]);
const PHASES = new Set<DeploymentActivationPhase>([
  'validating_plan', 'opening_route', 'qualifying_route', 'registering',
]);

function object(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError(`${path} is invalid`);
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, fields: readonly string[], path: string): void {
  if (Object.keys(value).sort().join(',') !== [...fields].sort().join(',')) throw new TypeError(`${path} shape is invalid`);
}

function integer(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new TypeError(`${path} is invalid`);
  return Number(value);
}

function text(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 512) throw new TypeError(`${path} is invalid`);
  return value;
}

function candidate(value: unknown, index: number): PreparedDeploymentCandidate {
  const path = `deployment_activation.candidates[${index}]`;
  const item = object(value, path);
  exact(item, [
    'candidate_id', 'deployment_id', 'model_id', 'model_revision', 'quantization',
    'topology_size', 'plan_digest', 'state', 'phase', 'completed_steps',
    'total_steps', 'reason_code',
  ], path);
  const state = text(item.state, `${path}.state`) as DeploymentActivationState;
  if (!STATES.has(state)) throw new TypeError(`${path}.state is invalid`);
  const phase = item.phase === null ? null : text(item.phase, `${path}.phase`) as DeploymentActivationPhase;
  if (phase !== null && !PHASES.has(phase)) throw new TypeError(`${path}.phase is invalid`);
  const reason = item.reason_code === null ? null : text(item.reason_code, `${path}.reason_code`);
  const revision = text(item.model_revision, `${path}.model_revision`);
  const digest = text(item.plan_digest, `${path}.plan_digest`);
  const topologySize = integer(item.topology_size, `${path}.topology_size`);
  const completed = integer(item.completed_steps, `${path}.completed_steps`);
  const total = integer(item.total_steps, `${path}.total_steps`);
  if (
    !REVISION.test(revision) || !SHA256.test(digest) || topologySize < 1 || total !== 4 || completed > total ||
    (state === 'activating') !== (phase !== null) ||
    (state === 'failed' || state === 'unavailable') !== (reason !== null) ||
    ((state === 'qualified' || state === 'active') && completed !== total) ||
    (reason !== null && !SAFE_CODE.test(reason))
  ) throw new TypeError(`${path} state is inconsistent`);
  return Object.freeze({
    candidate_id: text(item.candidate_id, `${path}.candidate_id`),
    deployment_id: text(item.deployment_id, `${path}.deployment_id`),
    model_id: text(item.model_id, `${path}.model_id`),
    model_revision: revision,
    quantization: text(item.quantization, `${path}.quantization`),
    topology_size: topologySize,
    plan_digest: digest,
    state,
    phase,
    completed_steps: completed,
    total_steps: 4,
    reason_code: reason,
  });
}

export function decodeDeploymentActivationStatus(value: unknown): DeploymentActivationStatus {
  const item = object(value, 'deployment_activation');
  exact(item, ['protocol', 'generation', 'busy_candidate_id', 'invalid_candidate_count', 'candidates'], 'deployment_activation');
  if (item.protocol !== 'mycelium.deployment_activation.v1' || !Array.isArray(item.candidates)) {
    throw new TypeError('deployment_activation protocol is invalid');
  }
  const busy = item.busy_candidate_id === null ? null : text(item.busy_candidate_id, 'deployment_activation.busy_candidate_id');
  const candidates = Object.freeze(item.candidates.map(candidate));
  if (busy !== null && !candidates.some((item) => item.candidate_id === busy && item.state === 'activating')) {
    throw new TypeError('deployment_activation busy candidate is invalid');
  }
  return Object.freeze({
    protocol: 'mycelium.deployment_activation.v1',
    generation: integer(item.generation, 'deployment_activation.generation'),
    busy_candidate_id: busy,
    invalid_candidate_count: integer(item.invalid_candidate_count, 'deployment_activation.invalid_candidate_count'),
    candidates,
  });
}

async function errorCode(response: Response): Promise<string> {
  try {
    const value = object(await response.json(), 'deployment_activation.error');
    return typeof value.error === 'string' && SAFE_CODE.test(value.error) ? value.error : `deployment_activation_${response.status}`;
  } catch {
    return `deployment_activation_${response.status}`;
  }
}

export class HttpDeploymentActivationClient implements DeploymentActivationClient {
  async status(signal?: AbortSignal): Promise<DeploymentActivationStatus> {
    return this.request(DEPLOYMENT_ACTIVATION_PATH, { method: 'GET', signal });
  }

  async activate(candidateId: string, signal?: AbortSignal): Promise<DeploymentActivationStatus> {
    return this.request(DEPLOYMENT_ACTIVATION_START_PATH, {
      method: 'POST',
      signal,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ candidate_id: candidateId }),
    });
  }

  private async request(path: string, init: RequestInit): Promise<DeploymentActivationStatus> {
    const response = await fetch(path, {
      ...init,
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      headers: { accept: 'application/json', ...init.headers },
    });
    if (!response.ok) throw new Error(await errorCode(response));
    const type = response.headers.get('content-type')?.toLowerCase() ?? '';
    if (!type.startsWith('application/json')) throw new Error('invalid_activation_content_type');
    return decodeDeploymentActivationStatus(await response.json());
  }
}
