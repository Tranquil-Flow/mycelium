// SPDX-License-Identifier: AGPL-3.0-only
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LiveRouteWorkspace } from './LiveRouteWorkspace';
import { liveRouteStatusFixture } from './routeStatusTestFixture';
import m15Fixture from '../../../../../contracts/compatibility-fixtures/m15-plan-comparison-v1.json';
import { decodeM15PlanComparison } from './m15Comparison';
import { decodeLiveRouteStatus } from './routeStatus';
import nativeInterruptibility from '../../../../../tests/fixtures/native-phone-interruptibility.json';

const client = { load: async () => liveRouteStatusFixture() };
const workloadClient = { load: async () => decodeM15PlanComparison(structuredClone(m15Fixture)) };

describe('LiveRouteWorkspace', () => {
  it('labels native replay cleanup as unmeasured rather than a desktop bound', async () => {
    const fixture = liveRouteStatusFixture();
    const native = { ...fixture, peers: fixture.peers.map((peer, i) => i === 0
      ? { ...peer, interruptibility: nativeInterruptibility.public_loaded } : peer) };
    render(<LiveRouteWorkspace view="network" qualification={null} freshness="current"
      client={{ load: async () => decodeLiveRouteStatus(native) }} />);
    expect(await screen.findByText(/cleanup bound unmeasured/)).toBeInTheDocument();
  });
  it('keeps independent source governance visible while physical route evidence loads or fails', async () => {
    let rejectRoute!: (reason: Error) => void;
    const pending = new Promise<never>((_, reject) => { rejectRoute = reject; });
    render(<LiveRouteWorkspace view="readiness" qualification={null} freshness="stale" client={{ load: () => pending }} />);
    expect(screen.getByRole('heading', { name: 'Readiness' })).toBeInTheDocument();
    expect(screen.getByText('Loading physical route evidence…')).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { name: 'Build governance' })).toHaveLength(1);
    expect(screen.getByText('Not release-ready.', { exact: false })).toBeInTheDocument();
    rejectRoute(new Error('route_status_unavailable'));
    expect(await screen.findByText('route_status_unavailable')).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { name: 'Build governance' })).toHaveLength(1);
  });

  it('mounts governance exactly once on a successful Readiness route', async () => {
    render(<LiveRouteWorkspace view="readiness" qualification={null} freshness="stale" client={client} />);
    expect(await screen.findByText('Physical qualification')).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { name: 'Build governance' })).toHaveLength(1);
  });

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
    expect(screen.getByText(/stage_local_kv · 0 active · 0 B/)).toBeInTheDocument();
    expect(screen.getByText(/qwen2 · position released · peak 4.0 KiB · released \(normal_completion\)/)).toBeInTheDocument();
    expect(screen.getByText(/decode work 8 input tokens \/ 8 operations/)).toBeInTheDocument();
  });

  it('labels observed timing as physical measurement', async () => {
    render(
      <LiveRouteWorkspace
        view="plans"
        qualification={null}
        freshness="current"
        client={client}
        workloadClient={workloadClient}
      />,
    );

    expect(await screen.findByText('Qualified deployment measurement')).toBeInTheDocument();
    expect(screen.getByText(/observed physical execution/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Workload-aware plan frontier/i })).toBeInTheDocument();
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
