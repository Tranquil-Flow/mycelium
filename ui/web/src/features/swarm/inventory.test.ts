import { describe, expect, it } from 'vitest';
import { PRODUCT_SWARM_PROTOCOL, type ProductSwarmStatus } from '../../app/contracts';
import { inventoryRows, type InventorySort } from './inventory';

const fixture: ProductSwarmStatus = {
  protocol: PRODUCT_SWARM_PROTOCOL,
  native_nodes: [
    {
      member_id: 'native-relay',
      capability: 'native_inference_node',
      membership_state: 'assigned',
      connectivity: 'relayed',
      endpoint_id: 'endpoint-relay',
    },
    {
      member_id: 'native-direct',
      capability: 'native_inference_node',
      membership_state: 'qualified',
      connectivity: 'direct',
      endpoint_id: '10.1.2.3',
    },
  ],
  browser_workers: [
    {
      peer_id: 'browser-probe',
      capability: 'synthetic_browser_probe',
      state: 'ready',
      expires_at_unix_ms: 1_900_000_030_000,
    },
  ],
};

describe('device inventory projection', () => {
  it('keeps native node and browser probe capabilities distinct', () => {
    const rows = inventoryRows(fixture, '', 'identity', 1_900_000_000_000);
    expect(rows.map((row) => row.kind)).toEqual([
      'browser_probe',
      'native_node',
      'native_node',
    ]);
    expect(rows.find((row) => row.id === 'browser-probe')).toMatchObject({
      capabilityLabel: 'Browser worker · developer probe',
      routeReady: false,
    });
    expect(rows.find((row) => row.id === 'native-direct')?.endpointLabel).toBe(
      'Private address redacted',
    );
  });

  it('conceals opaque network identities when the local privacy setting is enabled', () => {
    const rows = inventoryRows(fixture, '', 'identity', 1_900_000_000_000, true);
    expect(rows.find((row) => row.id === 'native-relay')?.endpointLabel).toBe(
      'Network identity concealed',
    );
  });

  it.each<InventorySort>(['identity', 'capability', 'state', 'connectivity', 'expiry'])(
    'sorts deterministically by %s',
    (sort) => {
      const rows = inventoryRows(fixture, '', sort, 1_900_000_000_000);
      expect(rows).toHaveLength(3);
      expect(new Set(rows.map((row) => row.id)).size).toBe(3);
    },
  );

  it('searches identity, state, and only the approved connectivity vocabulary', () => {
    expect(inventoryRows(fixture, 'relayed', 'identity', 1_900_000_000_000).map((row) => row.id)).toEqual([
      'native-relay',
    ]);
    expect(inventoryRows(fixture, 'developer probe', 'identity', 1_900_000_000_000).map((row) => row.id)).toEqual([
      'browser-probe',
    ]);
    expect(inventoryRows(fixture, 'tails' + 'cale', 'identity', 1_900_000_000_000)).toEqual([]);
  });
});
