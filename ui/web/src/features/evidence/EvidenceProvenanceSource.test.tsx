import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { EvidenceProvenanceSource } from './EvidenceProvenanceSource';
import { decodeEvidenceHistory, decodeEvidenceProjection } from './evidenceProjection';

const runtime = decodeEvidenceProjection({ protocol: 'mycelium.evidence_projection.v1', record_id: 'runtime-a', capability: 'route_execution', source_kind: 'live_runtime', authority: 'route', generation: 2, captured_at_unix_ms: 1_900_000_000_000, observed_at_unix_ms: 1_900_000_000_000, valid_until_unix_ms: 1_900_000_003_000, freshness: 'current', payload_protocol: 'mycelium.live_route_status.v1', payload: { protocol: 'mycelium.live_route_status.v1', counters: { frames_sent: 14, frames_received: 13 }, recent_inferences: [{ request_id: 'safe-id' }, { request_id: 'safe-id-2' }] } });
const historical = decodeEvidenceProjection({ ...runtime, record_id: 'history-a', capability: 'stage_local_kv', source_kind: 'sealed_historical', authority: 'physical gate', captured_at_unix_ms: 1_000, observed_at_unix_ms: 1_000, valid_until_unix_ms: null, freshness: 'historical' });

describe('EvidenceProvenanceSource', () => {
  it('labels current runtime and sealed records without milestone copy', async () => {
    render(<EvidenceProvenanceSource client={{ loadRuntime: async () => runtime, loadHistory: async () => decodeEvidenceHistory({ protocol: 'mycelium.evidence_history.v1', records: [historical] }) }} />);
    expect(await screen.findByRole('heading', { name: 'Live now' })).toBeInTheDocument();
    expect(screen.getByText(/14 frames sent · 13 received · 2 terminal runs retained by server/i)).toBeInTheDocument();
    expect(screen.getByText('Recorded evidence (1)')).toBeInTheDocument();
    expect(screen.getByText(/do not describe current runtime readiness/i)).toBeInTheDocument();
  });
});
