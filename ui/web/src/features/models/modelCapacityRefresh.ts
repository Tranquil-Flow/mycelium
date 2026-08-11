export const MODEL_CAPACITY_REFRESH_PATH = '/__mycelium/model-capacity-refresh';
export const MODEL_CAPACITY_REFRESH_START_PATH = '/__mycelium/model-capacity-refresh/start';

export type ModelCapacityRefreshPhase = 'capturing_resources' | 'scanning_local_models' | 'evaluating_models' | 'publishing';
export type ModelCapacityRefreshState = 'idle' | 'refreshing' | 'succeeded' | 'failed';

export type ModelCapacityRefreshStatus = Readonly<{
  protocol: 'mycelium.model_capacity_refresh.v1';
  generation: number;
  state: ModelCapacityRefreshState;
  phase: ModelCapacityRefreshPhase | null;
  started_at_unix_ms: number | null;
  completed_at_unix_ms: number | null;
  operation_digest: string | null;
  catalog_generation: number | null;
  evaluated_model_count: number;
  reason_code: string | null;
  download_authorized: false;
  provisioning_started: false;
}>;

export interface ModelCapacityRefreshClient {
  status(signal?: AbortSignal): Promise<ModelCapacityRefreshStatus>;
  start(signal?: AbortSignal): Promise<ModelCapacityRefreshStatus>;
}

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const SAFE_CODE = /^[a-z][a-z0-9_]{0,127}$/;
const STATES = new Set<ModelCapacityRefreshState>(['idle', 'refreshing', 'succeeded', 'failed']);
const PHASES = new Set<ModelCapacityRefreshPhase>(['capturing_resources', 'scanning_local_models', 'evaluating_models', 'publishing']);

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError('model capacity refresh is invalid');
  return value as Record<string, unknown>;
}

function integer(value: unknown, nullable = false): number | null {
  if (nullable && value === null) return null;
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new TypeError('model capacity refresh integer is invalid');
  return Number(value);
}

export function decodeModelCapacityRefreshStatus(value: unknown): ModelCapacityRefreshStatus {
  const item = object(value);
  const expected = ['protocol', 'generation', 'state', 'phase', 'started_at_unix_ms', 'completed_at_unix_ms', 'operation_digest', 'catalog_generation', 'evaluated_model_count', 'reason_code', 'download_authorized', 'provisioning_started'];
  if (Object.keys(item).sort().join(',') !== expected.sort().join(',') || item.protocol !== 'mycelium.model_capacity_refresh.v1') throw new TypeError('model capacity refresh shape is invalid');
  const state = item.state as ModelCapacityRefreshState;
  const phase = item.phase as ModelCapacityRefreshPhase | null;
  const reason = item.reason_code as string | null;
  const digest = item.operation_digest as string | null;
  if (!STATES.has(state) || (phase !== null && !PHASES.has(phase)) || (state === 'refreshing') !== (phase !== null) || (reason !== null && (typeof reason !== 'string' || !SAFE_CODE.test(reason))) || (digest !== null && (typeof digest !== 'string' || !SHA256.test(digest))) || item.download_authorized !== false || item.provisioning_started !== false) throw new TypeError('model capacity refresh state is invalid');
  return Object.freeze({
    protocol: 'mycelium.model_capacity_refresh.v1',
    generation: integer(item.generation)!, state, phase,
    started_at_unix_ms: integer(item.started_at_unix_ms, true),
    completed_at_unix_ms: integer(item.completed_at_unix_ms, true),
    operation_digest: digest,
    catalog_generation: integer(item.catalog_generation, true),
    evaluated_model_count: integer(item.evaluated_model_count)!,
    reason_code: reason,
    download_authorized: false,
    provisioning_started: false,
  });
}

async function request(path: string, init: RequestInit): Promise<ModelCapacityRefreshStatus> {
  const response = await fetch(path, { ...init, cache: 'no-store', credentials: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer', headers: { accept: 'application/json', ...init.headers } });
  if (!response.ok) {
    let code = `model_capacity_refresh_${response.status}`;
    try { const body = object(await response.json()); if (typeof body.error === 'string' && SAFE_CODE.test(body.error)) code = body.error; } catch { /* bounded fallback */ }
    throw new Error(code);
  }
  if (!(response.headers.get('content-type') ?? '').toLowerCase().startsWith('application/json')) throw new Error('invalid_capacity_refresh_content_type');
  return decodeModelCapacityRefreshStatus(await response.json());
}

export class HttpModelCapacityRefreshClient implements ModelCapacityRefreshClient {
  status(signal?: AbortSignal) { return request(MODEL_CAPACITY_REFRESH_PATH, { method: 'GET', signal }); }
  start(signal?: AbortSignal) { return request(MODEL_CAPACITY_REFRESH_START_PATH, { method: 'POST', signal, headers: { 'content-type': 'application/json' }, body: '{}' }); }
}
