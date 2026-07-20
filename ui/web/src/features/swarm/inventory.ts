import type { ProductSwarmStatus } from '../../app/contracts';
import { displayEndpointIdentity } from './privacy';

export type InventorySort = 'identity' | 'capability' | 'state' | 'connectivity' | 'expiry';

export interface InventoryRow {
  readonly id: string;
  readonly kind: 'native_node' | 'browser_probe';
  readonly capabilityLabel: 'Native model-stage node' | 'Browser worker · developer probe';
  readonly state: string;
  readonly connectivity: string;
  readonly endpointLabel: string;
  readonly expiresAtUnixMs: number | null;
  readonly expiryLabel: string;
  readonly routeReady: false;
}

function compareString(left: string, right: string): number {
  return left.localeCompare(right, 'en', { sensitivity: 'base', numeric: true });
}

export function inventoryRows(
  status: ProductSwarmStatus,
  query: string,
  sort: InventorySort,
  nowUnixMs: number,
  concealNetworkIdentity = false,
): readonly InventoryRow[] {
  const nativeRows: InventoryRow[] = status.native_nodes.map((node) => ({
    id: node.member_id,
    kind: 'native_node',
    capabilityLabel: 'Native model-stage node',
    state: node.membership_state,
    connectivity: node.connectivity,
    endpointLabel: concealNetworkIdentity
      ? 'Network identity concealed'
      : displayEndpointIdentity(node.endpoint_id),
    expiresAtUnixMs: null,
    expiryLabel: 'Session managed',
    routeReady: false,
  }));
  const browserRows: InventoryRow[] = status.browser_workers.map((worker) => {
    const remainingSeconds = Math.max(0, Math.ceil((worker.expires_at_unix_ms - nowUnixMs) / 1_000));
    return {
      id: worker.peer_id,
      kind: 'browser_probe',
      capabilityLabel: 'Browser worker · developer probe',
      state: worker.state,
      connectivity: 'browser session',
      endpointLabel: 'Browser endpoint undisclosed',
      expiresAtUnixMs: worker.expires_at_unix_ms,
      expiryLabel: remainingSeconds === 0 ? 'Expired' : `Expires in ${remainingSeconds}s`,
      routeReady: false,
    };
  });
  const needle = query.trim().toLocaleLowerCase('en');
  const rows = [...browserRows, ...nativeRows].filter((row) => {
    if (needle.length === 0) return true;
    return [row.id, row.capabilityLabel, row.state, row.connectivity, row.endpointLabel]
      .some((field) => field.toLocaleLowerCase('en').includes(needle));
  });
  rows.sort((left, right) => {
    let comparison = 0;
    switch (sort) {
      case 'identity':
        comparison = compareString(left.id, right.id);
        break;
      case 'capability':
        comparison = compareString(left.capabilityLabel, right.capabilityLabel);
        break;
      case 'state':
        comparison = compareString(left.state, right.state);
        break;
      case 'connectivity':
        comparison = compareString(left.connectivity, right.connectivity);
        break;
      case 'expiry':
        comparison = (left.expiresAtUnixMs ?? Number.MAX_SAFE_INTEGER) -
          (right.expiresAtUnixMs ?? Number.MAX_SAFE_INTEGER);
        break;
    }
    return comparison || compareString(left.id, right.id);
  });
  return Object.freeze(rows);
}
