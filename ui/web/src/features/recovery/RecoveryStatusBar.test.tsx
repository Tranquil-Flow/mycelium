// SPDX-License-Identifier: AGPL-3.0-only
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PRODUCT_ROUTES, type ProductRouteId } from '../../app/navigation';
import { ProductEvidenceProvider } from '../productEvidence/ProductEvidenceContext';
import type { ProductEvidenceState } from '../productEvidence/source';
import { decodeProductSnapshot, type ProductSnapshot } from '../productEvidence/contracts';
import productSnapshotFixture from '../../../../../contracts/compatibility-fixtures/product-snapshot-v1.json';
import { RecoveryStatusBar } from './RecoveryStatusBar';

function snapshot(cursor: number, recovering = 1): ProductSnapshot {
  return {
    internet_native: decodeProductSnapshot(productSnapshotFixture).internet_native,
    protocol: 'mycelium.product_snapshot.v1',
    publication: { snapshot_id: `snapshot-${cursor}`, generation: cursor, cursor, published_at_unix_ms: 1_500_000, source_mode: 'live' },
    supported_entity_kinds: ['artifact', 'assignment', 'device', 'directed_link', 'incident', 'load_proof', 'qualification', 'recovery', 'request', 'route', 'runtime_kv_ownership', 'source_provenance', 'stage'],
    source_states: [{ source_id: 'recovery-source', authority: 'request_gateway_recovery', status: 'current', observed_at_unix_ms: 1_500_000, valid_until_unix_ms: 1_500_000, generation: null, reason_code: null }],
    entities: [{
      entity_id: 'recovery-runtime', kind: 'recovery', label: 'Full-context recovery', source_id: 'recovery-source',
      binding: { deployment_id: 'deployment-a6', deployment_epoch: 8, route_id: null, route_generation: null, topology_version: null },
      freshness: { status: 'current', observed_at_unix_ms: 1_500_000, valid_until_unix_ms: 1_500_000 },
      attributes: { request_count: 1, recovering_count: recovering, terminal_count: 0, terminal_blocked_count: 0, cleanup_complete_count: 0, reconciled_count: 0, max_recovery_attempt: 1, breaker_state: 'closed' },
    }],
    relations: [], readiness: [], notices: [],
    provenance: { projector: 'mycelium_product_spine', projector_version: 'm12-v1', source_mode: 'live' },
  };
}

function state(cursor: number, recovering = 1): ProductEvidenceState {
  return { status: 'connected', source_mode: 'live', freshness: 'current', generation: cursor, cursor, snapshot: snapshot(cursor, recovering), reason_code: null };
}

function mutableSource(initial: ProductEvidenceState) {
  let current = initial;
  let listener: ((next: ProductEvidenceState) => void) | null = null;
  return {
    getState: () => current,
    loadInitial: vi.fn(async () => current),
    subscribe: vi.fn((next: (value: ProductEvidenceState) => void) => { listener = next; return () => { listener = null; }; }),
    emit(next: ProductEvidenceState) { current = next; listener?.(next); },
  };
}

const routeVocabulary: Readonly<Record<ProductRouteId, readonly string[]>> = {
  inference: ['Request count', 'Recovering count', 'Terminal count'],
  lab: ['Maximum recovery attempt', 'Cleanup complete count', 'Breaker state'],
  network: ['Recovery source status', 'Snapshot cursor', 'Recovering count'],
  nodes: ['Request count', 'Cleanup complete count', 'Reconciled count'],
  plans: ['Maximum recovery attempt', 'Recovering count', 'Terminal blocked count'],
  readiness: ['Recovery source status', 'Cleanup complete count', 'Reconciled count'],
  incidents: ['Terminal count', 'Terminal blocked count', 'Breaker state'],
  settings: ['Maximum recovery attempt', 'Recovery source status', 'Retained snapshot count'],
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('shared A6 recovery product evidence', () => {
  it('renders one provider snapshot across the exact eight routes without route refetches', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const source = mutableSource(state(12));
    const { rerender } = render(<ProductEvidenceProvider source={source}><RecoveryStatusBar route="inference" /></ProductEvidenceProvider>);
    for (const descriptor of PRODUCT_ROUTES) {
      rerender(<ProductEvidenceProvider source={source}><RecoveryStatusBar route={descriptor.id} /></ProductEvidenceProvider>);
      const status = screen.getByLabelText('Full-context replay recovery status');
      expect(status).toHaveTextContent('Full-context replay recovery');
      expect(status).toHaveAttribute('data-route', descriptor.id);
      expect(status).toHaveTextContent('Shared cursor 12');
      for (const label of routeVocabulary[descriptor.id]) expect(status).toHaveTextContent(label);
    }
    expect(PRODUCT_ROUTES.map(({ id }) => id)).toEqual(['inference', 'lab', 'network', 'nodes', 'plans', 'readiness', 'incidents', 'settings']);
    expect(source.loadInitial).toHaveBeenCalledTimes(1);
    expect(source.subscribe).toHaveBeenCalledTimes(1);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('keeps reconnect history shared while navigation changes only vocabulary', async () => {
    const source = mutableSource(state(20));
    const { rerender } = render(<ProductEvidenceProvider source={source}><RecoveryStatusBar route="inference" /></ProductEvidenceProvider>);
    source.emit(state(21, 0));
    await waitFor(() => expect(screen.getByLabelText('Full-context replay recovery status')).toHaveTextContent('Shared history 2'));
    rerender(<ProductEvidenceProvider source={source}><RecoveryStatusBar route="settings" /></ProductEvidenceProvider>);
    expect(screen.getByLabelText('Full-context replay recovery status')).toHaveTextContent('Shared cursor 21');
    expect(screen.getByLabelText('Full-context replay recovery status')).toHaveTextContent('Shared history 2');
  });

  it('reloads the latest shared snapshot without a private recovery endpoint', async () => {
    const source = mutableSource(state(30));
    const first = render(<ProductEvidenceProvider source={source}><RecoveryStatusBar route="inference" /></ProductEvidenceProvider>);
    first.unmount();
    source.emit(state(31, 0));
    render(<ProductEvidenceProvider source={source}><RecoveryStatusBar route="inference" /></ProductEvidenceProvider>);
    expect(screen.getByLabelText('Full-context replay recovery status')).toHaveTextContent('Shared cursor 31');
    expect(source.loadInitial).toHaveBeenCalledTimes(2);
  });
});
