import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ConcurrencyLivenessProjection, type ConcurrencyWorkspace } from './ConcurrencyLivenessPanel';
import { liveRouteStatusFixture } from './routeStatusTestFixture';

describe('ConcurrencyLivenessProjection', () => {
  const workspaces: readonly ConcurrencyWorkspace[] = [
    'inference', 'lab', 'network', 'nodes', 'plans', 'readiness', 'incidents', 'settings',
  ];

  for (const view of workspaces) {
    it(`projects truthful capability state into ${view}`, () => {
      render(<ConcurrencyLivenessProjection status={liveRouteStatusFixture()} view={view} />);

      expect(screen.getByRole('region', { name: 'Concurrent execution and scoped liveness' })).toBeInTheDocument();
      expect(screen.getByText('Qualification pending')).toBeInTheDocument();
      expect(screen.getByText(/share one 2,000 ms limit/i)).toBeInTheDocument();
    });
  }

  it('does not promote an advertising node to qualified', () => {
    render(<ConcurrencyLivenessProjection status={liveRouteStatusFixture()} view="lab" />);
    expect(screen.getByText(/advertise a bounded-work-unit backend candidate/i)).toBeInTheDocument();
    expect(screen.getByText(/Advertising is not qualification/i)).toBeInTheDocument();
  });
});
