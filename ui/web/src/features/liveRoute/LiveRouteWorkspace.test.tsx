import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LiveRouteWorkspace } from './LiveRouteWorkspace';
import { liveRouteStatusFixture } from './routeStatusTestFixture';

const client = { load: async () => liveRouteStatusFixture() };

describe('LiveRouteWorkspace', () => {
  it('renders the ordered physical graph and per-peer counter delta', async () => {
    render(
      <LiveRouteWorkspace
        view="network"
        qualification={null}
        freshness="current"
        client={client}
      />,
    );

    expect(await screen.findByText('Ordered physical pipeline')).toBeInTheDocument();
    expect(screen.getByText('layers [0, 12) · mlx')).toBeInTheDocument();
    expect(screen.getByText('layers [12, 24) · mlx')).toBeInTheDocument();
    expect(screen.getByText('10 / 9 / 6')).toBeInTheDocument();
  });

  it('labels observed timing as physical measurement', async () => {
    render(
      <LiveRouteWorkspace
        view="plans"
        qualification={null}
        freshness="current"
        client={client}
      />,
    );

    expect(await screen.findByText('Qualified deployment measurement')).toBeInTheDocument();
    expect(screen.getByText(/observed physical execution/i)).toBeInTheDocument();
    expect(screen.getByText('140.0 ms')).toBeInTheDocument();
    expect(screen.getByText('25.0 ms')).toBeInTheDocument();
  });

  it('renders a truthful empty live incident state', async () => {
    render(
      <LiveRouteWorkspace
        view="incidents"
        qualification={null}
        freshness="current"
        client={client}
      />,
    );

    expect(await screen.findByText('Physical route incident log')).toBeInTheDocument();
    expect(screen.getByText(/No active physical route incident/)).toBeInTheDocument();
  });

  it('renders observed fail-closed and qualified failover evidence', async () => {
    const incidentStatus = {
      ...liveRouteStatusFixture(),
      incidents: [{
        protocol: 'mycelium.live_route_incident.v1',
        incident_id: 'registry-incident-1',
        deployment_id: 'deployment-1',
        request_id: 'request-1',
        state: 'qualified_failover_selected',
        reason: 'route_peer_process_lost',
        observed_at_unix_ms: 1_786_307_717_000,
      }],
    } as const;
    render(
      <LiveRouteWorkspace
        view="incidents"
        qualification={null}
        freshness="current"
        client={{ load: async () => incidentStatus }}
      />,
    );

    expect(await screen.findByText('qualified failover selected')).toBeInTheDocument();
    expect(screen.getByText(/route_peer_process_lost/)).toBeInTheDocument();
  });
});
