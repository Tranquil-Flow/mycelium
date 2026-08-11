import { describe, expect, it } from 'vitest';
import { decodeGovernanceReadiness } from './governanceReadiness';

const digest = `sha256:${'a'.repeat(64)}`;
export const governanceFixture = {
  protocol: 'mycelium.governance_readiness.v1', observed_at_unix_ms: 1, source_kind: 'source_control', source_commit: 'b'.repeat(40), source_worktree_clean: true,
  ledger_protocol: 'mycelium.governance_ledger.v1', ledger_digest: digest,
  contract_manifest_protocol: 'mycelium.contract_manifest.v1', contract_manifest_digest: digest,
  governance_gate_protocol: 'mycelium.governance_gate.v1', governance_gate_ok: true,
  authorized_product_action_count: 8, capability_count: 15, milestone_count: 7,
  release_exclusions: ['runtime recovery remains open'], release_ready: false,
} as const;

describe('governance readiness contract', () => {
  it('accepts the closed non-release projection', () => {
    expect(decodeGovernanceReadiness(governanceFixture)).toEqual(governanceFixture);
  });

  it('rejects promotion and unknown fields', () => {
    expect(() => decodeGovernanceReadiness({ ...governanceFixture, release_ready: true })).toThrow();
    expect(() => decodeGovernanceReadiness({ ...governanceFixture, extra: true })).toThrow();
  });
});
