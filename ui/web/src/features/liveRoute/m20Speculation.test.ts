import { describe, expect, it } from 'vitest';
import { decodeM20SpeculativePlan, decodeM20SpeculativeRuntime } from './m20Speculation';
import { m20PlanFixture, m20RuntimeFixture } from './m20SpeculationFixtures';

describe('M20 speculative contracts', () => {
  it('decodes the closed disabled physical decision', () => {
    const plan = decodeM20SpeculativePlan(structuredClone(m20PlanFixture));
    const runtime = decodeM20SpeculativeRuntime(structuredClone(m20RuntimeFixture));
    expect(plan.decision.reason).toBe('batched_target_verification_unavailable');
    expect(plan.compatibility.tokenizer).toBe(true);
    expect(runtime.mode).toBe('disabled');
  });

  it('rejects private and unknown fields', () => {
    expect(() => decodeM20SpeculativePlan({ ...structuredClone(m20PlanFixture), prompt: 'private' })).toThrow(/unknown or missing fields/i);
    expect(() => decodeM20SpeculativeRuntime({ ...structuredClone(m20RuntimeFixture), token_ids: [1] })).toThrow(/unknown or missing fields/i);
  });
});
