import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import {
  filterAndSortNodes,
  projectNodeInventory,
  redactedNodeDetail,
} from './nodeInventory';

describe('node inventory projection', () => {
  const bundle = loadStaticObservatoryBundle();
  const inventory = projectNodeInventory(bundle.snapshot, bundle.provisioning);

  it('keeps simulator and provisioning identities in separate scopes without guessed mapping', () => {
    expect(inventory).toHaveLength(bundle.snapshot.nodes.length + bundle.provisioning.nodeIds.length);
    expect(inventory.filter((node) => node.scope === 'simulation')).toHaveLength(6);
    expect(inventory.filter((node) => node.scope === 'artifact_provisioning')).toHaveLength(2);
    expect(inventory.every((node) => node.identityMapping === 'not_established')).toBe(true);
  });

  it('projects hardware, memory, assignment, location precision, and honest unknown runtime fields', () => {
    const simulatorNode = inventory.find((node) => node.scope === 'simulation')!;
    const provisionedNode = inventory.find((node) => node.scope === 'artifact_provisioning')!;

    expect(simulatorNode.memory.availableGb).toBeGreaterThanOrEqual(0);
    expect(simulatorNode.locationPrecision).toBeTruthy();
    expect(simulatorNode.runtimeBackend.state).toBe('unknown');
    expect(provisionedNode.assignment?.exactRange).toMatch(/^\[\d+,\d+\)$/);
    expect(provisionedNode.readiness.artifactsVerified).toBe('PROVEN');
    expect(provisionedNode.readiness.runtimeLoaded).toBe('NOT_PROVEN');
  });

  it('searches and sorts while retaining unknown values rather than coercing them', () => {
    const filtered = filterAndSortNodes(inventory, {
      query: 'artifact provisioning',
      key: 'id',
      direction: 'asc',
    });
    expect(filtered).toHaveLength(2);
    expect(filtered.map((node) => node.id)).toEqual([...filtered.map((node) => node.id)].sort());
    expect(filtered.every((node) => node.deviceClass.state === 'unknown')).toBe(true);
  });

  it('exposes only a redacted allowlisted detail projection', () => {
    const detail = redactedNodeDetail(inventory[0]);
    const serialized = JSON.stringify(detail).toLowerCase();

    expect(detail.redaction).toMatch(/allowlisted/i);
    expect(serialized).not.toContain('private_address');
    expect(serialized).not.toContain('credential');
    expect(serialized).not.toContain('/users/');
  });
});
