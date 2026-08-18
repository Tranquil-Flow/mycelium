import { afterEach, describe, expect, it, vi } from 'vitest';
import { HttpModelPreparationClient, type ModelPreparationStatus, type ModelRepresentationDecision } from './modelPreparation';

const digest = `sha256:${'a'.repeat(64)}`;
const decision: ModelRepresentationDecision = {
  protocol: 'mycelium.model_representation_decision.v2',
  model_id: 'Qwen/Qwen2.5-7B-Instruct',
  revision: 'b'.repeat(40),
  source_quantization: 'bfloat16',
  serving_dtype: 'float32',
  serving_quantization: 'int8-weight-only',
  representation_digest: digest,
  conversion_authorized: true,
  source_artifact_digest: digest,
  quantizer: 'mycelium.rowwise_symmetric_int8.v1',
  download_authorized: false,
};
const status: ModelPreparationStatus = {
  protocol: 'mycelium.model_preparation.v1', operation: 'warm_reacquire', generation: 4,
  state: 'succeeded', phase: null, model_id: decision.model_id, revision: decision.revision,
  representation_digest: digest, owner_decision_digest: digest, candidate_id: 'candidate-1', topology_size: 2,
  transfer_bytes: 0, verified_bytes: 140, cache_receipt_count: 2, cached_verified_bytes: 140,
  transferred_verified_bytes: 0, origin_bytes: 0, reason_code: null, started_at_unix_ms: 1,
  completed_at_unix_ms: 2, download_authorized: false, activation_started: false,
};

afterEach(() => vi.restoreAllMocks());

describe('model preparation product client', () => {
  it('starts exact warm reacquisition through the dedicated same-origin path', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(status), {
      status: 202,
      headers: { 'content-type': 'application/json' },
    }));

    expect(await new HttpModelPreparationClient().reacquire('candidate-1', decision)).toEqual(status);
    expect(fetcher).toHaveBeenCalledWith('/__mycelium/model-preparation/reacquire', expect.objectContaining({
      method: 'POST', credentials: 'same-origin',
      body: JSON.stringify({ candidate_id: 'candidate-1', decision }),
    }));
  });
});
