// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SettingsInternetPanel } from './SettingsInternetPanel';

describe('SettingsInternetPanel (Settings workspace)', () => {
  it('renders operator-visible origin readiness and certificate freshness', () => {
    render(
      <SettingsInternetPanel
        public_origin_ready={true}
        certificate_freshness="current"
        bootstrap_policy="closed_five_route_allowlist"
        relay_policy="forced_relay_test_control"
        invite_entry="owner-private"
        revocation_entry="owner-private"
        show_private_diagnostics={true}
      />,
    );
    expect(screen.getByLabelText('public origin readiness').textContent).toBe('ready');
    expect(screen.getByLabelText('certificate freshness').textContent).toBe('current');
    expect(screen.getByLabelText('bootstrap policy').textContent).toBe(
      'closed_five_route_allowlist',
    );
    expect(screen.getByLabelText('relay policy').textContent).toBe(
      'forced_relay_test_control',
    );
    expect(screen.getByLabelText('invite entry').textContent).toBe('owner-private');
    expect(screen.getByLabelText('revocation entry').textContent).toBe('owner-private');
    expect(screen.getByLabelText('private diagnostics').textContent).toBe(
      'owner-private only',
    );
  });

  it('renders unknown for unknown operator state', () => {
    render(
      <SettingsInternetPanel
        public_origin_ready={null}
        certificate_freshness="unknown"
        bootstrap_policy={null}
        relay_policy={null}
        invite_entry="owner-private"
        revocation_entry="owner-private"
        show_private_diagnostics={false}
      />,
    );
    expect(screen.getByLabelText('public origin readiness').textContent).toBe('unknown');
    expect(screen.getByLabelText('certificate freshness').textContent).toBe('unknown');
    expect(screen.getByLabelText('bootstrap policy').textContent).toBe('unknown');
  });

  it('never renders raw credentials', () => {
    // Even if a credential-shaped value is smuggled through extra props, the
    // component's render path has no field that can display it.
    const credential = 'hunter2-operator-credential';
    const { container } = render(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <SettingsInternetPanel {...({} as any)}
        public_origin_ready={true}
        certificate_freshness="current"
        bootstrap_policy="closed_five_route_allowlist"
        relay_policy="forced_relay_test_control"
        invite_entry="owner-private"
        revocation_entry="owner-private"
        show_private_diagnostics={false}
      />,
    );
    expect(container.textContent).not.toContain(credential);
    expect(container.textContent).not.toContain('secret');
    expect(container.textContent).not.toContain('token');
  });

  it('never renders the private diagnostics content', () => {
    render(
      <SettingsInternetPanel
        public_origin_ready={true}
        certificate_freshness="current"
        bootstrap_policy="closed_five_route_allowlist"
        relay_policy="forced_relay_test_control"
        invite_entry="owner-private"
        revocation_entry="owner-private"
        show_private_diagnostics={false}
      />,
    );
    expect(screen.getByLabelText('private diagnostics').textContent).toBe(
      'owner-private only',
    );
  });
});
