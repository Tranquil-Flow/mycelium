export const MODEL_PREPARATION_PATH = '/__mycelium/model-preparation';
export const MODEL_PREPARATION_START_PATH = '/__mycelium/model-preparation/start';

export type ModelPreparationPhase = 'validating_capacity' | 'compiling_assignments' | 'verifying_local_artifacts' | 'staging_peers' | 'publishing_candidate';
export type ModelPreparationState = 'idle' | 'preparing' | 'succeeded' | 'failed';

export interface ModelPreparationStatus {
  readonly protocol: 'mycelium.model_preparation.v1';
  readonly generation: number;
  readonly state: ModelPreparationState;
  readonly phase: ModelPreparationPhase | null;
  readonly model_id: string | null;
  readonly revision: string | null;
  readonly candidate_id: string | null;
  readonly topology_size: number | null;
  readonly transfer_bytes: number;
  readonly verified_bytes: number;
  readonly reason_code: string | null;
  readonly started_at_unix_ms: number | null;
  readonly completed_at_unix_ms: number | null;
  readonly download_authorized: false;
  readonly activation_started: false;
}

export interface ModelPreparationClient {
  status(signal?: AbortSignal): Promise<ModelPreparationStatus>;
  start(modelId: string, revision: string): Promise<ModelPreparationStatus>;
}

async function decode(response: Response): Promise<ModelPreparationStatus> {
  const value = await response.json() as ModelPreparationStatus | { error?: unknown };
  if (!response.ok) throw new Error(typeof (value as { error?: unknown }).error === 'string' ? String((value as { error: string }).error) : `model_preparation_http_${response.status}`);
  return value as ModelPreparationStatus;
}

export class HttpModelPreparationClient implements ModelPreparationClient {
  async status(signal?: AbortSignal): Promise<ModelPreparationStatus> {
    return decode(await fetch(MODEL_PREPARATION_PATH, { signal, cache: 'no-store', credentials: 'same-origin' }));
  }

  async start(modelId: string, revision: string): Promise<ModelPreparationStatus> {
    return decode(await fetch(MODEL_PREPARATION_START_PATH, {
      method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model_id: modelId, revision }),
    }));
  }
}
