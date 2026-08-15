import { describe, expect, it, vi } from 'vitest';
import { HttpArtifactAcquisitionClient, decodeArtifactAcquisitionLedger, decodeArtifactAcquisitionStatus } from './artifactAcquisition';

const digest = `sha256:${'a'.repeat(64)}`;
const status = {
  protocol: 'mycelium.swarm_artifact_acquisition.v1', generation: 2, acquisition_id: 'acquisition-1', state: 'ready', phase: null,
  model_id: 'Qwen/Qwen3-8B', model_revision: 'b'.repeat(40), representation: 'bfloat16 · float32', assignment_id: 'assignment-1', placement_id: 'placement-1', stage_id: 'stage-1', layer_start: 0, layer_end_exclusive: 18,
  total_bytes: 100, cached_verified_bytes: 40, transferred_verified_bytes: 60, missing_bytes: 0, quarantined_bytes: 0, duplicate_bytes_prevented: 40,
  eligible_source_count: 2, active_source_count: 0, sources: [{ source_ref: 'source-000000000001', state: 'rotated', verified_bytes: 30 }, { source_ref: 'source-000000000002', state: 'rotated', verified_bytes: 30 }], origin_bytes: 0, aggregate_bytes_per_second: 50, eta_seconds: 0,
  chunk_count: 3, verified_chunk_count: 3, resumed_chunk_count: 1, source_rotation_count: 1, manifest_digest: digest, assignment_digest: digest, representation_digest: digest, feasibility_digest: digest, evidence_generation: 8, promotion_digest: digest, reason_code: null, retryable: false, started_at_unix_ms: 1_000, updated_at_unix_ms: 1_100, terminal_at_unix_ms: 1_100,
} as const;

describe('artifact acquisition product contract', () => {
  it('decodes a strict accounted terminal ledger', () => {
    const ledger = decodeArtifactAcquisitionLedger({ protocol: 'mycelium.swarm_artifact_acquisition_ledger.v1', generation: 2, current: null, history: [status] });
    expect(ledger.history[0].model_id).toBe('Qwen/Qwen3-8B');
    expect(ledger.history[0].sources).toHaveLength(2);
  });

  it.each([
    { ...status, private_path: '/tmp/model' },
    { ...status, missing_bytes: 1 },
    { ...status, sources: [{ ...status.sources[0], endpoint: '10.0.0.1' }, status.sources[1]] },
    { ...status, promotion_digest: null },
    { ...status, layer_end_exclusive: 0 },
  ])('rejects unknown, private, inconsistent, and widened status', (candidate) => {
    expect(() => decodeArtifactAcquisitionStatus(candidate)).toThrow();
  });

  it('rejects stale or duplicated ledger generations and identities', () => {
    expect(() => decodeArtifactAcquisitionLedger({ protocol: 'mycelium.swarm_artifact_acquisition_ledger.v1', generation: 2, current: null, history: [status, status] })).toThrow();
  });

  it('loads only the same-origin read-only endpoint', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ protocol: 'mycelium.swarm_artifact_acquisition_ledger.v1', generation: 2, current: null, history: [status] }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const result = await new HttpArtifactAcquisitionClient().load();
    expect(result.generation).toBe(2);
    expect(fetcher).toHaveBeenCalledWith('/__mycelium/artifacts/acquisitions', expect.objectContaining({ method: 'GET', credentials: 'same-origin' }));
    fetcher.mockRestore();
  });
});
