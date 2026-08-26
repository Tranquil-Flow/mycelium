// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { NetworkPathPanel } from './NetworkPathPanel';
import type {
  InternetActivationObservation,
  RelayProjection,
} from './types';

const RELAY_REFERENCE = 'hmac-sha256:' + 'b'.repeat(64);

function observation(overrides: Partial<InternetActivationObservation>): InternetActivationObservation {
  return {
    protocol: 'mycelium.internet_activation_observation.v1',
    observation_id: 'obs-1',
    connection_generation: 1,
    connection_reuse: 0,
    path_class: 'direct',
    path_source: 'bound_live_connection',
    endpoint_pseudonym: 'sha256:' + 'a'.repeat(64),
    observed_at_unix_ms: 1752500000000,
    freshness: 'current',
    evidence_lifetime_until_unix_ms: 1752500090000,
    metrics: {
      rtt_ms: 12,
      warm_rtt_ms: 9,
      jitter_ms: 2,
      goodput_bytes_per_second: 1500000,
      loss_ratio: 0,
      sample_count: 64,
      measured_zero: true,
    },
    ...overrides,
  };
}

function relay(): RelayProjection {
  return {
    protocol: 'mycelium.relay_projection.v1',
    relay_reference: RELAY_REFERENCE,
    region: 'unknown',
    projection_generation: 1,
    stable: true,
    observed_at_unix_ms: 1752500000000,
  };
}

describe('NetworkPathPanel (Network workspace)', () => {
  it('renders a relay observation with only the HMAC reference', () => {
    const { container } = render(
      <NetworkPathPanel
        observation={observation({ path_class: 'relay' })}
        relay={relay()}
      />,
    );
    expect(screen.getByLabelText('path class').textContent).toBe('relay');
    expect(screen.getByLabelText('relay reference').textContent).toBe(RELAY_REFERENCE);
    expect(container.textContent).not.toContain('https://');
    expect(container.textContent).not.toContain('relay.example');
    expect(container.textContent).not.toContain('443');
  });

  it('renders every nullable metric as unknown, never zero', () => {
    render(
      <NetworkPathPanel
        observation={observation({
          metrics: {
            rtt_ms: null,
            warm_rtt_ms: null,
            jitter_ms: null,
            goodput_bytes_per_second: null,
            loss_ratio: null,
            sample_count: null,
            measured_zero: false,
          },
        })}
        relay={null}
      />,
    );
    for (const label of ['rtt_ms', 'warm_rtt_ms', 'jitter_ms', 'goodput', 'loss']) {
      expect(screen.getByLabelText(label).textContent).toBe('unknown');
    }
    expect(document.body.textContent).not.toContain('> 0<');
  });

  it('renders an unknown-path observation without any path claim', () => {
    render(
      <NetworkPathPanel
        observation={observation({
          path_class: 'unknown',
          path_source: 'unknown',
          metrics: {
            rtt_ms: null,
            warm_rtt_ms: null,
            jitter_ms: null,
            goodput_bytes_per_second: null,
            loss_ratio: null,
            sample_count: null,
            measured_zero: false,
          },
        })}
        relay={null}
      />,
    );
    expect(screen.getByLabelText('path class').textContent).toBe('unknown');
    expect(screen.getByLabelText('rtt_ms').textContent).toBe('unknown');
  });

  it('renders the explicit measured zero loss correctly', () => {
    render(
      <NetworkPathPanel
        observation={observation({
          metrics: {
            rtt_ms: 5,
            warm_rtt_ms: 4,
            jitter_ms: 1,
            goodput_bytes_per_second: 100,
            loss_ratio: 0,
            sample_count: 64,
            measured_zero: true,
          },
        })}
        relay={null}
      />,
    );
    expect(screen.getByLabelText('loss').textContent).toBe('0');
  });

  it('renders unknown everywhere when no observation is supplied', () => {
    render(<NetworkPathPanel observation={null} relay={null} />);
    expect(screen.getByLabelText('path class').textContent).toBe('unknown');
    expect(screen.getByLabelText('rtt_ms').textContent).toBe('unknown');
    expect(screen.getByLabelText('relay reference').textContent).toBe('unknown');
  });
});
