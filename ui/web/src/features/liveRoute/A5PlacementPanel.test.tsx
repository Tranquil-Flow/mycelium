import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ConcurrencyLivenessProjection } from './ConcurrencyLivenessPanel';
import { liveRouteStatusFixture } from './routeStatusTestFixture';

function statusWithPlacement(observed: boolean) {
  const status = liveRouteStatusFixture();
  const peer = status.peers[0];
  const stage = { ...status.stages[0], node_id: peer.node_id };
  return { ...status, peers: [{ ...peer, placements: [stage],
    prefill_operation_count: 900, decode_operation_count: 900,
    placement_counters: observed ? { [stage.placement_id]: {
      prefill_operation_count: 3, decode_operation_count: 7,
      active_state_count: 2, active_kv_bytes: 128,
    } } : {},
  }] };
}

describe('placement observations in the shipped Nodes workspace', () => {
  it('shows exact placement work and never substitutes node totals', () => {
    const status = statusWithPlacement(true);
    render(<ConcurrencyLivenessProjection status={status} view="nodes" nowUnixMs={20_000} />);
    const panel = screen.getByRole('region', { name: 'Placement work and qualification' });
    expect(panel).toHaveAttribute('data-deployment-id', status.deployment_id);
    expect(panel).toHaveAttribute('data-topology-version', String(status.topology_version));
    const row = within(panel).getByRole('row', { name: new RegExp(status.peers[0].placements[0].placement_id) });
    expect(within(row).getByText('3')).toBeVisible();
    expect(within(row).getByText('7')).toBeVisible();
    expect(within(row).getByText('128')).toBeVisible();
    expect(within(panel).queryByText('900')).not.toBeInTheDocument();
    expect(within(panel).getByText(/not per-request frame evidence/i)).toBeVisible();
  });

  it.each([
    ['future-issued', 0, false, 'Not yet valid'],
    ['expired', 2_000_000_000_001, false, 'Expired'],
    ['downstream-loss', 20_000, true, 'Lost'],
  ] as const)('invalidates the track qualification on %s', (_case, nowUnixMs, loss, label) => {
    const status = statusWithPlacement(true);
    const qualification = status.replica_track_qualification[0];
    const peer = status.peers[0];
    const revised = { ...status,
      replica_loss_placement_ids: loss ? [qualification.placement_ids.find((id) => id !== qualification.placement_id)!] : [],
      peers: [{ ...peer, placements: [{ ...peer.placements[0], placement_id: qualification.placement_id }] }],
    };
    render(<ConcurrencyLivenessProjection status={revised} view="nodes" nowUnixMs={nowUnixMs} />);
    const panel = screen.getByRole('region', { name: 'Placement work and qualification' });
    expect(within(panel).getByText(new RegExp(`· ${label}$`))).toBeVisible();
  });

  it('keeps absent placement telemetry unknown rather than zero', () => {
    render(<ConcurrencyLivenessProjection status={statusWithPlacement(false)} view="nodes" />);
    const panel = screen.getByRole('region', { name: 'Placement work and qualification' });
    expect(within(panel).getAllByText('Unknown').length).toBeGreaterThanOrEqual(4);
    expect(within(panel).queryByText('900')).not.toBeInTheDocument();
  });
});
