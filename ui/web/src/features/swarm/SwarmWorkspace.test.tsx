import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PRODUCT_SWARM_PROTOCOL, type ProductSwarmStatus } from '../../app/contracts';
import type { SwarmClient } from './SwarmClient';
import SwarmWorkspace from './SwarmWorkspace';

const NOW = 1_900_000_000_000;
const status: ProductSwarmStatus = {
  protocol: PRODUCT_SWARM_PROTOCOL,
  native_nodes: [
    {
      member_id: 'native-private',
      capability: 'native_inference_node',
      membership_state: 'reachable',
      connectivity: 'direct',
      endpoint_id: '192.168.1.22',
    },
    {
      member_id: 'native-relay',
      capability: 'native_inference_node',
      membership_state: 'assigned',
      connectivity: 'relayed',
      endpoint_id: 'endpoint-relay',
    },
  ],
  browser_workers: [
    {
      peer_id: 'browser-1',
      capability: 'synthetic_browser_probe',
      state: 'ready',
      expires_at_unix_ms: NOW + 45_000,
    },
  ],
};

function fakeClient(overrides: Partial<SwarmClient> = {}): SwarmClient {
  return {
    status: vi.fn(async () => status),
    createInvite: vi.fn(async (capability) => ({
      protocol: PRODUCT_SWARM_PROTOCOL,
      invite_id: 'invite-1',
      invite_code: 'one-time-enrollment-code-1234',
      capability,
      expires_at_unix_ms: NOW + 30_000,
    })),
    join: vi.fn(async () => ({
      protocol: PRODUCT_SWARM_PROTOCOL,
      joined: true as const,
      member_id: 'native-joined',
      capability: 'native_inference_node' as const,
    })),
    leave: vi.fn(async (memberId) => ({
      protocol: PRODUCT_SWARM_PROTOCOL,
      member_id: memberId,
      left: true,
    })),
    ...overrides,
  };
}

describe('SwarmWorkspace', () => {
  it('renders searchable product-native inventory with truthful capability boundaries', () => {
    render(<SwarmWorkspace client={fakeClient()} initialStatus={status} now={() => NOW} />);

    expect(screen.getByRole('heading', { name: 'Nodes and swarm' })).toBeInTheDocument();
    expect(screen.getAllByText('Native model-stage node')).toHaveLength(2);
    expect(screen.getByText('Browser worker · developer probe')).toBeInTheDocument();
    expect(screen.getByText('Private address redacted')).toBeInTheDocument();
    expect(screen.queryByText('192.168.1.22')).not.toBeInTheDocument();
    expect(screen.getAllByText(/route readiness is qualifier-owned/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/route_ready=false/i).length).toBeGreaterThan(0);

    fireEvent.change(screen.getByRole('searchbox', { name: /search devices/i }), {
      target: { value: 'relayed' },
    });
    expect(screen.getByText('native-relay')).toBeInTheDocument();
    expect(screen.queryByText('native-private')).not.toBeInTheDocument();
  });

  it('creates a bounded native invite and exposes separate copy and QR payload controls', async () => {
    const writeText = vi.fn(async (_value: string) => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const client = fakeClient();
    render(<SwarmWorkspace client={client} initialStatus={status} now={() => NOW} />);

    fireEvent.click(screen.getByRole('button', { name: /create native-node invite/i }));
    const invite = await screen.findByRole('region', { name: /active native-node invite/i });
    expect(within(invite).getByText(/single use/i)).toBeInTheDocument();
    expect(within(invite).getByText(/expires in 30s/i)).toBeInTheDocument();
    expect(client.createInvite).toHaveBeenCalledWith('native_inference_node', 300);

    fireEvent.click(within(invite).getByRole('button', { name: /copy signed invite bundle/i }));
    fireEvent.click(within(invite).getByRole('button', { name: /copy qr payload/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
    expect(writeText.mock.calls[1][0]).toMatch(/^mycelium:\/\/join\?code=/);
  });

  it('keeps browser matrix work inside an explicit collapsed developer/probe panel', async () => {
    const client = fakeClient();
    render(<SwarmWorkspace client={client} initialStatus={status} now={() => NOW} />);

    const details = screen.getByText(/developer\/probe browser workers/i).closest('details');
    expect(details).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText(/developer\/probe browser workers/i));
    expect(screen.getByText(/synthetic matrix probe only/i)).toBeInTheDocument();
    expect(screen.getByText(/cannot set model, stage, inference, or route readiness/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /create browser-probe invite/i }));
    await waitFor(() =>
      expect(client.createInvite).toHaveBeenCalledWith('synthetic_browser_probe', 300),
    );
  });

  it('joins through the coordinator and refreshes status without persisting the invite code', async () => {
    const client = fakeClient();
    render(<SwarmWorkspace client={client} initialStatus={status} now={() => NOW} />);

    fireEvent.change(screen.getByLabelText(/invite code/i), {
      target: { value: 'one-time-enrollment-code-1234' },
    });
    fireEvent.change(screen.getByLabelText(/device name/i), { target: { value: 'Desk node' } });
    fireEvent.click(screen.getByRole('button', { name: /join swarm/i }));

    await screen.findByText(/joined as native-joined/i);
    expect(client.join).toHaveBeenCalledWith('one-time-enrollment-code-1234', 'Desk node');
    expect(screen.getByLabelText(/invite code/i)).toHaveValue('');
    expect(client.status).toHaveBeenCalledTimes(1);
  });

  it('requires explicit confirmation before leave and states the Router boundary', async () => {
    const client = fakeClient();
    render(<SwarmWorkspace client={client} initialStatus={status} now={() => NOW} />);

    fireEvent.click(screen.getByRole('button', { name: /leave native-relay/i }));
    expect(client.leave).not.toHaveBeenCalled();
    const confirmation = screen.getByRole('group', { name: /confirm leave native-relay/i });
    expect(within(confirmation).getByText(/does not mutate Router state/i)).toBeInTheDocument();
    fireEvent.click(within(confirmation).getByRole('button', { name: /confirm leave/i }));

    await waitFor(() => expect(client.leave).toHaveBeenCalledWith('native-relay'));
    expect(await screen.findByText(/native-relay left its device session/i)).toBeInTheDocument();
  });

  it('disables all mutating actions in offline evidence mode', () => {
    const client = fakeClient();
    render(<SwarmWorkspace client={client} initialStatus={status} now={() => NOW} readOnly />);

    expect(screen.getByRole('button', { name: /create native-node invite/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /create browser-probe invite/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /leave native-relay/i })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: /invite code/i })).toBeDisabled();
    expect(screen.getByText(/changes are unavailable in offline evidence mode/i)).toBeInTheDocument();
    expect(client.createInvite).not.toHaveBeenCalled();
    expect(client.join).not.toHaveBeenCalled();
    expect(client.leave).not.toHaveBeenCalled();
  });

  it('explains an injected operator-only boundary without enabling mutations', () => {
    const client = fakeClient();
    render(
      <SwarmWorkspace
        client={client}
        initialStatus={status}
        now={() => NOW}
        readOnly
        readOnlyReason="Native enrollment uses one owner-only signed bundle per device."
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent(/owner-only signed bundle per device/i);
    expect(screen.getByRole('button', { name: /create native-node invite/i })).toBeDisabled();
    expect(client.createInvite).not.toHaveBeenCalled();
  });

  it('fails closed with an accessible error and does not invent inventory data', async () => {
    const client = fakeClient({ status: vi.fn(async () => { throw new Error('status_unavailable'); }) });
    render(<SwarmWorkspace client={client} now={() => NOW} />);

    expect(await screen.findByRole('alert')).toHaveTextContent('status unavailable');
    expect(screen.getByText(/no unverified device status is introduced/i)).toBeInTheDocument();
    expect(screen.queryByText('native-private')).not.toBeInTheDocument();
  });
});
