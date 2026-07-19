import { describe, expect, it } from 'vitest';
import {
  calculateObservatoryChangeSet,
  emptyObservatoryChangeSet,
  type ObservatoryChangeInventory,
} from './changeSet';

const previous: ObservatoryChangeInventory = {
  nodes: [
    { id: 'node-a', revision: 'v1:11111111111111111111111111111111' },
    { id: 'node-b', revision: 'v1:22222222222222222222222222222222' },
  ],
  edges: [{ id: 'edge-old', revision: 'v1:33333333333333333333333333333333' }],
  routes: [{ id: 'route-a', revision: 'v1:44444444444444444444444444444444' }],
  readiness: [{ id: 'route-readiness', revision: 'v1:55555555555555555555555555555555' }],
  evidence: [{ id: 'proof-a', revision: 'v1:66666666666666666666666666666666' }],
};

const next: ObservatoryChangeInventory = {
  nodes: [
    { id: 'node-a', revision: 'v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' },
    { id: 'node-c', revision: 'v1:77777777777777777777777777777777' },
  ],
  edges: [{ id: 'edge-new', revision: 'v1:88888888888888888888888888888888' }],
  routes: [{ id: 'route-a', revision: 'v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' }],
  readiness: [{ id: 'route-readiness', revision: 'v1:cccccccccccccccccccccccccccccccc' }],
  evidence: [
    { id: 'proof-a', revision: 'v1:66666666666666666666666666666666' },
    { id: 'proof-b', revision: 'v1:99999999999999999999999999999999' },
  ],
};

describe('Observatory change sets', () => {
  it('identifies deterministic added, removed, and changed entities in every product category', () => {
    const changes = calculateObservatoryChangeSet(previous, next, 10, 11);

    expect(changes).toEqual({
      from_generation: 10,
      to_generation: 11,
      empty: false,
      nodes: { added: ['node-c'], removed: ['node-b'], changed: ['node-a'] },
      edges: { added: ['edge-new'], removed: ['edge-old'], changed: [] },
      routes: { added: [], removed: [], changed: ['route-a'] },
      readiness: { added: [], removed: [], changed: ['route-readiness'] },
      evidence: { added: ['proof-b'], removed: [], changed: [] },
    });
    expect(Object.isFrozen(changes.nodes.added)).toBe(true);
    expect(Object.isFrozen(changes)).toBe(true);
  });

  it('returns one immutable empty value for bootstrap and rejects ambiguous duplicate ids', () => {
    const empty = emptyObservatoryChangeSet(null, 1);
    expect(empty).toMatchObject({ from_generation: null, to_generation: 1, empty: true });
    expect(empty.nodes).toEqual({ added: [], removed: [], changed: [] });

    expect(() =>
      calculateObservatoryChangeSet(
        previous,
        { ...next, nodes: [...next.nodes, next.nodes[0]] },
        10,
        11,
      ),
    ).toThrow(/duplicate.*node-a/i);
  });
});
