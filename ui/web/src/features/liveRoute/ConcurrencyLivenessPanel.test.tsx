import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConcurrencyLivenessProjection, ConcurrencyLivenessSource, type ConcurrencyWorkspace } from './ConcurrencyLivenessPanel';
import type { LiveRouteStatus, LiveRouteStatusClient } from './routeStatus';
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
      const replicaView = view === 'readiness' || view === 'settings'
        ? 'qualification'
        : view === 'incidents'
          ? 'loss'
          : 'tracks';
      expect(screen.getByRole('region', {
        name: `${replicaView} request-level stage replication`,
      })).toBeInTheDocument();
      expect(screen.getByText('placement-fixture-replica')).toBeInTheDocument();
      expect(screen.getAllByText('data parallel').length).toBeGreaterThan(0);
    });
  }

  it('does not promote an advertising node to qualified', () => {
    render(<ConcurrencyLivenessProjection status={liveRouteStatusFixture()} view="lab" />);
    expect(screen.getByText(/advertise a bounded-work-unit backend candidate/i)).toBeInTheDocument();
    expect(screen.getByText(/Advertising is not qualification/i)).toBeInTheDocument();
  });

  it('degrades a complete track when any member placement is lost', () => {
    const status = liveRouteStatusFixture();
    render(
      <ConcurrencyLivenessProjection
        status={{ ...status, replica_loss_placement_ids: ['placement-fixture-stage-1'] }}
        view="incidents"
        nowUnixMs={20_000}
      />,
    );
    expect(screen.getByText(/0 surviving qualified tracks · 1 degraded/i)).toBeInTheDocument();
    expect(screen.getByText('lost — new admission blocked')).toBeInTheDocument();
  });

  it('does not present an expired qualification as current', () => {
    render(
      <ConcurrencyLivenessProjection
        status={liveRouteStatusFixture()}
        view="inference"
        nowUnixMs={2_000_000_000_001}
      />,
    );
    expect(screen.getByText('expired')).toBeInTheDocument();
    expect(screen.queryByText('qualified')).not.toBeInTheDocument();
  });
});

describe('ConcurrencyLivenessSource', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('clears stale evidence when a later poll fails', async () => {
    vi.useFakeTimers();
    const client: LiveRouteStatusClient = {
      load: vi.fn<() => Promise<LiveRouteStatus>>()
        .mockResolvedValueOnce(liveRouteStatusFixture())
        .mockRejectedValueOnce(new Error('live_status_unavailable')),
    };
    render(<ConcurrencyLivenessSource view="inference" client={client} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('placement-fixture-replica')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(screen.getByRole('alert')).toHaveTextContent('live_status_unavailable');
    expect(screen.queryByText('placement-fixture-replica')).not.toBeInTheDocument();
  });

  it('does not overlap status polls', async () => {
    vi.useFakeTimers();
    let resolveLoad: ((status: LiveRouteStatus) => void) | undefined;
    const client: LiveRouteStatusClient = {
      load: vi.fn(() => new Promise<LiveRouteStatus>((resolve) => {
        resolveLoad = resolve;
      })),
    };
    render(<ConcurrencyLivenessSource view="inference" client={client} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(client.load).toHaveBeenCalledTimes(1);
    await act(async () => { resolveLoad?.(liveRouteStatusFixture()); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(client.load).toHaveBeenCalledTimes(2);
  });
});
