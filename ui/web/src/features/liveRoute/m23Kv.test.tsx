import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { M23KvPanel } from './M23KvPanel';
import { decodeM23KvEvidence } from './m23Kv';

const raw = {
  protocol: 'mycelium.m23_heterogeneous_kv_gate.v1', generated_at_unix_ms: 1,
  replay_capture_digest: `sha256:${'a'.repeat(64)}`, kv_capture_digest: `sha256:${'b'.repeat(64)}`,
  gates: {
    same_route_model_stages_hosts: true, same_prompt_and_budget: true,
    exact_output_parity: true, one_token_decode_every_stage: true,
    all_stages_advanced_physical_counters: true, kv_active_then_terminally_released: true,
    no_fatal_or_cleanup_failure: true, measured_tpot_improvement: true,
  },
  implemented: true, performance_qualified: true, promotion_state: 'qualified',
  measurements: {
    replay_tpot_ms: 8609.9, kv_tpot_ms: 978.1, tpot_delta_ms: -7631.8,
    tpot_improvement_ratio: 0.886, replay_activation_output_bytes: 5226720,
    kv_activation_output_bytes: 1327272, activation_byte_delta: -3899448,
    replay_total_ms: 29266.8, kv_total_ms: 6691.2,
  },
  claim_boundary: 'One fixed physical route and workload.', evidence_digest: `sha256:${'c'.repeat(64)}`,
};

describe('M23 heterogeneous KV evidence', () => {
  it('decodes the closed evidence and renders its measured A/B', () => {
    const evidence = decodeM23KvEvidence(raw);
    render(<M23KvPanel evidence={evidence} view="plans" />);

    expect(screen.getByText('Stage-local KV qualified')).toBeVisible();
    expect(screen.getByText('Exact output parity').nextSibling).toHaveTextContent('Verified');
    expect(screen.getByText('88.6%')).toBeVisible();
    expect(screen.getByText(/4.98 MiB → 1.27 MiB/)).toBeVisible();
  });

  it('rejects unknown fields', () => {
    expect(() => decodeM23KvEvidence({ ...raw, prompt: 'secret' })).toThrow(/unknown or missing fields/);
  });
});
