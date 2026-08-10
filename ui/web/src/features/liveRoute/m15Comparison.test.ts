import { describe, expect, it, vi } from 'vitest';
import fixture from '../../../../../contracts/compatibility-fixtures/m15-plan-comparison-v1.json';
import { decodeM15PlanComparison, HttpM15ComparisonClient } from './m15Comparison';

describe('M15 plan comparison contract', () => {
  it('decodes the frozen content-free policy matrix', () => {
    const decoded = decodeM15PlanComparison(structuredClone(fixture));
    expect(decoded.comparisons).toHaveLength(2);
    expect(decoded.profiles.every((profile) => profile.content_removed)).toBe(true);
    expect(decoded.comparisons.every((item) => item.pareto_candidate_ids.includes(item.selected_candidate_id))).toBe(true);
    expect(decoded.calibration_state).toBe('observed');
    expect(decoded.observations.every((item) => item.overall_state === 'met')).toBe(true);
  });

  it('rejects unknown fields and non-Pareto selection', () => {
    expect(() => decodeM15PlanComparison({ ...structuredClone(fixture), prompt: 'private' })).toThrow(/unknown|missing/i);
    const invalid = structuredClone(fixture);
    invalid.comparisons[0].pareto_candidate_ids = ['not-selected'];
    expect(() => decodeM15PlanComparison(invalid)).toThrow(/selection/i);
  });

  it('uses the fixed same-origin endpoint', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(fixture), { headers: { 'content-type': 'application/json' } }));
    await new HttpM15ComparisonClient(fetcher as typeof fetch).load();
    expect(fetcher).toHaveBeenCalledWith('/__mycelium/m15-plan-comparison', expect.objectContaining({ credentials: 'same-origin', cache: 'no-store' }));
  });
});
