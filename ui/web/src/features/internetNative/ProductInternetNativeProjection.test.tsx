import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ProductRouteId } from '../../app/navigation';
import { decodeProductSnapshot } from '../productEvidence/contracts';
import { ProductInternetNativeProjection } from './ProductInternetNativeProjection';
import {
  INTERNET_NATIVE_FIXTURE,
  productSnapshotWithInternetNative,
} from './testFixtures';

const snapshot = decodeProductSnapshot(productSnapshotWithInternetNative());

const routeExpectation: Readonly<Record<ProductRouteId, string>> = {
  lab: 'Internet-native bootstrap',
  network: 'Internet activation path (Network)',
  nodes: 'Internet member state (Nodes)',
  inference: 'Inference path',
  readiness: 'Internet-native readiness',
  plans: 'Internet-native plan path costs',
  incidents: 'Internet-native incidents',
  settings: 'Internet-native settings',
};

describe('product snapshot internet-native workspace projection', () => {
  it.each(Object.entries(routeExpectation) as Array<[ProductRouteId, string]>) (
    'renders the context-adapted A8 projection on #%s',
    (view, accessibleName) => {
      render(<ProductInternetNativeProjection view={view} snapshot={snapshot} />);
      expect(screen.getByLabelText(accessibleName)).toBeInTheDocument();
    },
  );

  it('preserves unknown-not-zero from the closed snapshot', () => {
    const internetNative = structuredClone(INTERNET_NATIVE_FIXTURE) as unknown as {
      activation_observation: Record<string, unknown> & { metrics: unknown };
    };
    internetNative.activation_observation.metrics = {
      rtt_ms: null,
      warm_rtt_ms: null,
      jitter_ms: null,
      goodput_bytes_per_second: null,
      loss_ratio: null,
      sample_count: null,
      measured_zero: false,
    };
    const unknownSnapshot = decodeProductSnapshot(productSnapshotWithInternetNative(internetNative));

    render(<ProductInternetNativeProjection view="network" snapshot={unknownSnapshot} />);

    expect(screen.getByLabelText('rtt_ms')).toHaveTextContent('unknown');
    expect(screen.getByLabelText('goodput')).toHaveTextContent('unknown');
    expect(screen.getByLabelText('loss')).toHaveTextContent('unknown');
  });

  it('renders only the privacy-safe relay reference', () => {
    const { container } = render(
      <ProductInternetNativeProjection view="network" snapshot={snapshot} />,
    );

    expect(screen.getByLabelText('relay reference')).toHaveTextContent(/^hmac-sha256:/);
    expect(container.textContent).not.toContain('https://');
    expect(container.textContent).not.toContain('relay.example');
    expect(container.textContent).not.toContain('443');
  });

  it('projects bounded incident vocabulary from activation history', () => {
    const internetNative = structuredClone(INTERNET_NATIVE_FIXTURE) as unknown as {
      activation_observation: Record<string, unknown>;
      activation_history: Array<Record<string, unknown>>;
    };
    internetNative.activation_history = [
      internetNative.activation_observation,
      {
        ...internetNative.activation_observation,
        observation_id: 'fixture-observation-2',
        connection_generation: 4,
        path_class: 'relay',
      },
    ];
    internetNative.activation_observation = internetNative.activation_history[1];
    const transitioned = decodeProductSnapshot(productSnapshotWithInternetNative(internetNative));

    render(<ProductInternetNativeProjection view="incidents" snapshot={transitioned} />);

    expect(screen.getByText('Activation path transition')).toBeInTheDocument();
    expect(screen.getByText('Bounded reconnect')).toBeInTheDocument();
  });
});
