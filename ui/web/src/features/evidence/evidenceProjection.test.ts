// SPDX-License-Identifier: AGPL-3.0-only
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
  it('rejects divergent duplicate history and private payload content', () => {
    const sealed = { ...runtime(), record_id: 'history-a', source_kind: 'sealed_historical', freshness: 'historical', valid_until_unix_ms: null };
    expect(() => decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records: [sealed, { ...sealed, payload: { ...sealed.payload, route_alive: false } }] })).toThrow(/duplicate|conflict/i);
    expect(() => decodeEvidenceProjection({ ...runtime(), payload: { ...runtime().payload, nested: { prompt: 'PRIVATE' } } })).toThrow(/private|privacy/i);
    const earlier = { ...sealed, captured_at_unix_ms: 1_000, observed_at_unix_ms: 1_000, record_id: 'earlier' };
    expect(decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records: [sealed, earlier, { ...earlier }] }).records.map(r => r.record_id)).toEqual(['earlier', 'history-a']);
  });
  it('rejects unknown source kind, capability and observation after capture', () => {
    expect(() => decodeEvidenceProjection({ ...runtime(), source_kind: 'unknown' })).toThrow(/source kind/i);
    expect(() => decodeEvidenceProjection({ ...runtime(), capability: 'unknown' })).toThrow(/capability/i);
    expect(() => decodeEvidenceProjection({ ...runtime(), observed_at_unix_ms: 2_001 })).toThrow(/precedes observation/i);
  });
  it('uses product-named evidence endpoints', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(runtime()), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ protocol: 'mycelium.evidence_history.v1', records: [] }), { status: 200 }));
    const client = new HttpEvidenceProjectionClient(fetcher);
    await client.loadRuntime(); await client.loadHistory();
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([RUNTIME_EVIDENCE_PATH, HISTORICAL_EVIDENCE_PATH]);
  });
  it('refuses private envelope metadata rather than only private payload', () => {
    for (const [field, value] of [['authority', 'https://canary.invalid/private'], ['record_id', '/Users/canary/private'], ['payload_protocol', 'Bearer canary']] as const) {
      expect(() => decodeEvidenceProjection({ ...runtime(), [field]: value, ...(field === 'payload_protocol' ? { payload: { protocol: value } } : {}) })).toThrow(/privacy/i);
    }
    expect(decodeEvidenceProjection(runtime()).authority).toBe('route');
  });
  it('keeps the exact shared history bound after duplicate removal', () => {
    const base = { ...runtime(), source_kind: 'sealed_historical', freshness: 'historical', valid_until_unix_ms: null };
    const records = Array.from({ length: 129 }, (_, i) => ({ ...base, record_id: `record-${String(i).padStart(3, '0')}`, captured_at_unix_ms: 1000+i, observed_at_unix_ms: 1000+i }));
    for (const count of [0, 1, 128]) expect(decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records: records.slice(0, count) }).records.length).toBe(count);
    expect(decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records: [...records.slice(0, 128), { ...records[0] }] }).records.length).toBe(128);
    expect(() => decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records })).toThrow(/history is invalid/i);
    expect(() => decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records: [...records.slice(0, 127), { ...records[0], payload: { protocol: base.payload_protocol, other: true } }] })).toThrow(/duplicate conflict/i);
  });
  it('accepts max safe integer and rejects next for all numeric envelope fields', () => {
    for (const field of ['generation', 'captured_at_unix_ms', 'observed_at_unix_ms', 'valid_until_unix_ms'] as const) {
      const candidate = { ...runtime(), [field]: Number.MAX_SAFE_INTEGER };
      if (field === 'observed_at_unix_ms') candidate.captured_at_unix_ms = Number.MAX_SAFE_INTEGER;
      if (field === 'captured_at_unix_ms') candidate.valid_until_unix_ms = Number.MAX_SAFE_INTEGER;
      if (field === 'observed_at_unix_ms') candidate.valid_until_unix_ms = Number.MAX_SAFE_INTEGER;
      expect(decodeEvidenceProjection(candidate)[field]).toBe(Number.MAX_SAFE_INTEGER);
      expect(() => decodeEvidenceProjection({ ...candidate, [field]: Number.MAX_SAFE_INTEGER + 1 })).toThrow(/integer/i);
    }
  });
});
