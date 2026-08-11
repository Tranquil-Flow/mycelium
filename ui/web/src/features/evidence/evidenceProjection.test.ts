import { describe, expect, it, vi } from 'vitest';
import { decodeEvidenceHistory, decodeEvidenceProjection, evidenceIsCurrentLive, HISTORICAL_EVIDENCE_PATH, HttpEvidenceProjectionClient, RUNTIME_EVIDENCE_PATH } from './evidenceProjection';

const runtime = () => ({
  protocol: 'mycelium.evidence_projection.v1', record_id: 'runtime-a', capability: 'route_execution', source_kind: 'live_runtime', authority: 'route', generation: 2,
  captured_at_unix_ms: 2_000, observed_at_unix_ms: 1_900, valid_until_unix_ms: 5_000, freshness: 'current', payload_protocol: 'mycelium.live_route_status.v1', payload: { protocol: 'mycelium.live_route_status.v1', route_alive: true },
});
describe('evidence projections', () => {
  it('decodes live and immutable historical sources', () => {
    expect(decodeEvidenceProjection(runtime()).freshness).toBe('current');
    const historical = { ...runtime(), record_id: 'history-a', source_kind: 'sealed_historical', freshness: 'historical', valid_until_unix_ms: null };
    expect(decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records: [historical] }).records[0].source_kind).toBe('sealed_historical');
  });
  it('rejects historical evidence presented as current and unknown fields', () => {
    expect(() => decodeEvidenceProjection({ ...runtime(), source_kind: 'sealed_historical', valid_until_unix_ms: null })).toThrow(/source\/freshness mismatch/i);
    expect(() => decodeEvidenceProjection({ ...runtime(), private_path: '/secret' })).toThrow(/unknown or missing/i);
  });
  it('never treats expired or historical evidence as current live state', () => {
    const live = decodeEvidenceProjection(runtime());
    expect(evidenceIsCurrentLive(live, 4_000)).toBe(true);
    expect(evidenceIsCurrentLive(live, 5_001)).toBe(false);
    const historical = decodeEvidenceProjection({ ...runtime(), source_kind: 'sealed_historical', freshness: 'historical', valid_until_unix_ms: null });
    expect(evidenceIsCurrentLive(historical, 2_000)).toBe(false);
  });
  it('uses product-named evidence endpoints', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(runtime()), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ protocol: 'mycelium.evidence_history.v1', records: [] }), { status: 200 }));
    const client = new HttpEvidenceProjectionClient(fetcher);
    await client.loadRuntime(); await client.loadHistory();
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([RUNTIME_EVIDENCE_PATH, HISTORICAL_EVIDENCE_PATH]);
  });
});
