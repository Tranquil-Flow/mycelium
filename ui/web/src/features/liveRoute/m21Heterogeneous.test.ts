import { describe, expect, it } from 'vitest';
import { decodeM21Heterogeneous } from './m21Heterogeneous';
import { m21HeterogeneousFixture } from './m21HeterogeneousFixtures';

describe('M21 heterogeneous contract', () => {
  it('decodes a privacy-reduced heterogeneous route', () => {
    const evidence = decodeM21Heterogeneous(structuredClone(m21HeterogeneousFixture));
    expect(evidence.gate_state).toBe('qualified');
    expect(evidence.members.map((member) => member.runtime_backend)).toEqual(['mlx', 'numpy', 'browser']);
    expect(evidence.route.tailscale_product_dependency).toBe(false);
  });

  it('rejects raw endpoint and credential fields', () => {
    expect(() => decodeM21Heterogeneous({ ...structuredClone(m21HeterogeneousFixture), endpoint_id: 'raw' })).toThrow(/unknown or missing fields/i);
    const member = { ...structuredClone(m21HeterogeneousFixture.members[0]), credential: 'secret' };
    expect(() => decodeM21Heterogeneous({ ...structuredClone(m21HeterogeneousFixture), members: [member] })).toThrow(/unknown or missing fields/i);
  });
});
