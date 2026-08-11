import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { GovernanceReadinessSource } from './GovernanceReadinessSource';

const digest = `sha256:${'a'.repeat(64)}`;
const governanceFixture = {
  protocol: 'mycelium.governance_readiness.v1', observed_at_unix_ms: 1, source_kind: 'source_control', source_commit: 'b'.repeat(40), source_worktree_clean: true,
  ledger_protocol: 'mycelium.governance_ledger.v1', ledger_digest: digest,
  contract_manifest_protocol: 'mycelium.contract_manifest.v1', contract_manifest_digest: digest,
  governance_gate_protocol: 'mycelium.governance_gate.v1', governance_gate_ok: true,
  authorized_product_action_count: 8, capability_count: 15, milestone_count: 7,
  release_exclusions: ['runtime recovery remains open'], release_ready: false,
} as const;

describe('GovernanceReadinessSource', () => {
  it('shows versions and honest release exclusions', async () => {
    render(<GovernanceReadinessSource client={{ load: async () => governanceFixture }} />);
    expect(await screen.findByText('mycelium.governance_ledger.v1 · aaaaaaaaaaaa')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Build governance' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Not release-ready');
    expect(screen.getByText('1 current release exclusions')).toBeInTheDocument();
  });
});
