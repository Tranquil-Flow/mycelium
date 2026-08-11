import { describe, expect, it } from 'vitest';
import { decodeM22Release } from './m22Release';
import { m22ReleaseFixture } from './m22ReleaseFixtures';

const clone = () => JSON.parse(JSON.stringify(m22ReleaseFixture)) as Record<string, unknown>;

describe('M22 release decoder', () => {
  it('accepts and freezes the closed release projection', () => {
    const decoded = decodeM22Release(clone());
    expect(decoded.model.model_id).toBe('Qwen/Qwen2.5-3B-Instruct');
    expect(decoded.qwen3_8b.adapter_id).toBe('qwen3');
    expect(Object.isFrozen(decoded.services)).toBe(true);
  });

  it('rejects unknown fields and wrong closed types', () => {
    const extra = clone(); extra.private_path = '/secret';
    expect(() => decodeM22Release(extra)).toThrow(/unknown or missing/i);
    const wrong = clone(); (wrong.physical as Record<string, unknown>).simulated = 'false';
    expect(() => decodeM22Release(wrong)).toThrow(/simulated is invalid/i);
  });
});
