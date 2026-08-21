import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  InternetBootstrapPanel,
  type InternetBootstrapPanelProps,
} from './InternetBootstrapPanel';

function makeProps(overrides: Partial<InternetBootstrapPanelProps> = {}): InternetBootstrapPanelProps {
  return {
    bootstrapStatus: null,
    isMember: false,
    activationEligible: false,
    ...overrides,
  };
}

const currentBootstrap = {
  protocol: 'mycelium.internet_bootstrap_status.v1' as const,
  generation: 1,
  observed_at_unix_ms: 1_752_500_000_000,
  freshness: 'current' as const,
  tls_state: 'publicly_trusted' as const,
  canonical_origin_verified: true,
  seed_pin_state: 'verified' as const,
  route_state: 'available' as const,
  invitation_state: 'accepted' as const,
  counters: { requests: 3, joins_accepted: 1, joins_rejected: 2 },
};

function field(name: string): HTMLElement {
  const el = document.querySelector(`[data-field="${name}"]`);
  if (el === null) throw new Error(`missing data-field=${name}`);
  return el as HTMLElement;
}

describe('InternetBootstrapPanel (Device Lab workspace)', () => {
  it('renders every label as "unknown" when no bootstrap status is supplied', () => {
    render(
      <InternetBootstrapPanel
        bootstrapStatus={null}
        isMember={null}
        activationEligible={null}
      />,
    );

    expect(screen.getByText(/bootstrap reachability/i)).toBeInTheDocument();
    expect(screen.getByText(/seed pin state/i)).toBeInTheDocument();
    expect(screen.getByText(/invite state/i)).toBeInTheDocument();
    expect(screen.getByText(/tls state/i)).toBeInTheDocument();
    expect(field('route_state').textContent).toBe('unknown');
    expect(field('tls_state').textContent).toBe('unknown');
    expect(field('canonical_origin_verified').textContent).toBe('unknown');
    expect(field('seed_pin_state').textContent).toBe('unknown');
    expect(field('invitation_state').textContent).toBe('unknown');
    expect(field('preflight').textContent).toBe('unknown');
    expect(field('is_member').textContent).toBe('unknown');
    expect(field('activation_eligible').textContent).toBe('unknown');
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
  });

  it('renders a current positive bootstrap status with verified labels', () => {
    render(
      <InternetBootstrapPanel
        {...makeProps({
          bootstrapStatus: currentBootstrap,
          isMember: true,
          activationEligible: false,
        })}
      />,
    );

    expect(field('route_state').textContent).toBe('available');
    expect(field('tls_state').textContent).toBe('publicly_trusted');
    expect(field('canonical_origin_verified').textContent).toBe('verified');
    expect(field('seed_pin_state').textContent).toBe('verified');
    expect(field('invitation_state').textContent).toBe('accepted');
    expect(field('preflight').textContent).toBe('ready');
    expect(field('is_member').textContent).toBe('yes');
    expect(field('activation_eligible').textContent).toBe('no');
  });

  it('separates membership from activation eligibility', () => {
    render(
      <InternetBootstrapPanel
        {...makeProps({
          bootstrapStatus: { ...currentBootstrap, route_state: 'unavailable' },
          isMember: true,
          activationEligible: false,
        })}
      />,
    );

    expect(field('is_member').textContent).toBe('yes');
    expect(field('activation_eligible').textContent).toBe('no');
    expect(screen.getByText(/membership is visible/i)).toBeInTheDocument();
  });

  it('shows preflight blocked when canonical_origin_verified is false', () => {
    render(
      <InternetBootstrapPanel
        {...makeProps({
          bootstrapStatus: { ...currentBootstrap, canonical_origin_verified: false },
          isMember: false,
          activationEligible: false,
        })}
      />,
    );

    expect(field('preflight').textContent).toBe('blocked');
  });

  it('shows preflight blocked when TLS state is not publicly_trusted', () => {
    render(
      <InternetBootstrapPanel
        {...makeProps({
          bootstrapStatus: { ...currentBootstrap, tls_state: 'unverified' },
          isMember: false,
          activationEligible: false,
        })}
      />,
    );

    expect(field('preflight').textContent).toBe('blocked');
  });

  it('renders unknown membership and activation when isMember is null', () => {
    render(
      <InternetBootstrapPanel
        {...makeProps({
          bootstrapStatus: currentBootstrap,
          isMember: null,
          activationEligible: null,
        })}
      />,
    );

    expect(field('is_member').textContent).toBe('unknown');
    expect(field('activation_eligible').textContent).toBe('unknown');
  });

  it('renders counters without exposing any raw network identity', () => {
    const PSEUDONYM = 'sha256:' + 'a'.repeat(64);
    const { container } = render(
      <InternetBootstrapPanel
        {...makeProps({ bootstrapStatus: currentBootstrap, isMember: true, activationEligible: true })}
      />,
    );
    const html = container.innerHTML;
    expect(html).not.toContain(PSEUDONYM);
    expect(html).not.toContain('http');
    expect(html).not.toContain('@');
    expect(html).toMatch(/requests/);
    expect(html).toMatch(/joins accepted/);
    expect(html).toMatch(/joins rejected/);
  });
});