import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import App from '../../App';
import type { ProductRouteId } from '../../app/navigation';
import { ProductEvidenceProvider } from './ProductEvidenceContext';
import { decodeProductSnapshot } from './contracts';
import type { ProductEvidenceState } from './source';
import { productSnapshotWithInternetNative } from '../internetNative/testFixtures';

const observatoryState = {
  source_mode: 'live' as const,
  status: 'connected' as const,
  generation: 1,
  source_cursor: 1,
  projection: { snapshot: { qualification: null }, incidents: [] },
  route_ready: false,
  freshness: 'current' as const,
};

const observatorySource = {
  source_mode: 'live' as const,
  getState: () => observatoryState,
  loadInitial: () => observatoryState,
  subscribe: () => () => undefined,
};

const evidenceState: ProductEvidenceState = {
  status: 'connected',
  source_mode: 'fixture',
  freshness: 'current',
  generation: 1,
  cursor: 1,
  snapshot: decodeProductSnapshot(productSnapshotWithInternetNative()),
  reason_code: null,
};

const evidenceSource = {
  getState: () => evidenceState,
  loadInitial: async () => evidenceState,
  subscribe: (listener: (state: ProductEvidenceState) => void) => {
    listener(evidenceState);
    return () => undefined;
  },
};

afterEach(cleanup);

describe('all-workspace product evidence convergence', () => {
  const internetProjectionName: Readonly<Record<ProductRouteId, string>> = {
    inference: 'Inference path',
    lab: 'Internet-native bootstrap',
    network: 'Internet activation path (Network)',
    nodes: 'Internet member state (Nodes)',
    plans: 'Internet-native plan path costs',
    readiness: 'Internet-native readiness',
    incidents: 'Internet-native incidents',
    settings: 'Internet-native settings',
  };

  it.each([
    'inference',
    'lab',
    'network',
    'nodes',
    'plans',
    'readiness',
    'incidents',
    'settings',
  ] as const)('loads #%s directly from the same immutable snapshot', async (route: ProductRouteId) => {
    window.history.replaceState(null, '', `#${route}`);
    render(
      <ProductEvidenceProvider source={evidenceSource}>
        <App
          source={observatorySource as unknown as NonNullable<Parameters<typeof App>[0]['source']>}
        />
      </ProductEvidenceProvider>,
    );

    expect(await screen.findByText(/unified product evidence · fixture/i)).toBeInTheDocument();
    expect(screen.getByLabelText(internetProjectionName[route])).toBeInTheDocument();
    expect(window.location.hash).toBe(`#${route}`);
  });
});
