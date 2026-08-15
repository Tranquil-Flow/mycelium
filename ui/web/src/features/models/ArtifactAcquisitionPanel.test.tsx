import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ArtifactAcquisitionPanel } from './ArtifactAcquisitionPanel';
import { decodeArtifactAcquisitionLedger } from './artifactAcquisition';

const digest = `sha256:${'a'.repeat(64)}`;
const ready = {
  protocol: 'mycelium.swarm_artifact_acquisition.v1', generation: 2, acquisition_id: 'acquisition-1', state: 'ready', phase: null,
  model_id: 'Qwen/Qwen3-8B', model_revision: 'b'.repeat(40), representation: 'bfloat16 · float32', assignment_id: 'assignment-1', placement_id: 'placement-1', stage_id: 'stage-1', layer_start: 0, layer_end_exclusive: 18,
  total_bytes: 100, cached_verified_bytes: 40, transferred_verified_bytes: 60, missing_bytes: 0, quarantined_bytes: 0, duplicate_bytes_prevented: 40,
  eligible_source_count: 2, active_source_count: 0, sources: [{ source_ref: 'source-000000000001', state: 'rotated', verified_bytes: 30 }, { source_ref: 'source-000000000002', state: 'rotated', verified_bytes: 30 }], origin_bytes: 0, aggregate_bytes_per_second: 50, eta_seconds: 0,
  chunk_count: 3, verified_chunk_count: 3, resumed_chunk_count: 1, source_rotation_count: 1, manifest_digest: digest, assignment_digest: digest, representation_digest: digest, feasibility_digest: digest, evidence_generation: 8, promotion_digest: digest, reason_code: null, retryable: false, started_at_unix_ms: 1_000, updated_at_unix_ms: 1_100, terminal_at_unix_ms: 1_100,
};
const ledger = decodeArtifactAcquisitionLedger({ protocol: 'mycelium.swarm_artifact_acquisition_ledger.v1', generation: 2, current: null, history: [ready] });

describe('ArtifactAcquisitionPanel', () => {
  it('renders privacy-reduced placement contributions on Nodes', () => {
    render(<ArtifactAcquisitionPanel ledger={ledger} view="nodes" />);
    expect(screen.getByRole('heading', { name: 'Swarm artifact acquisition' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: /Privacy-reduced artifact sources/ })).toBeInTheDocument();
    expect(screen.getByText('source-000000000001')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\/tmp\/|10\.0\.0\.1/);
  });

  it('keeps promotion, load, and qualification as separate Readiness gates', () => {
    render(<ArtifactAcquisitionPanel ledger={ledger} view="readiness" />);
    expect(screen.getByText(/Promoted/)).toBeInTheDocument();
    expect(screen.getByText('Separate load proof required')).toBeInTheDocument();
    expect(screen.getByText('Separate physical qualification required')).toBeInTheDocument();
  });

  it('explains terminal transfer evidence without making it route-ready', () => {
    render(<ArtifactAcquisitionPanel ledger={ledger} view="inference" />);
    expect(screen.getByRole('table', { name: 'Recent terminal artifact acquisitions' })).toBeInTheDocument();
    expect(screen.getByText(/load and qualification remain separate/)).toBeInTheDocument();
  });
});
