// SPDX-License-Identifier: AGPL-3.0-only
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './App';
import type { NodeApi, NodeStatus, ServiceStatus } from './api';

const base: NodeStatus = {
  phase: 'unpaired',
  endpoint_id: 'abcdef0123456789',
  relay_online: true,
  swarm_id: null,
  member_generation: null,
  lease_expires_at: null,
  lease_fresh: false,
  heartbeats_sent: 0,
  last_heartbeat_at: null,
  last_error: null,
  stage_loaded: null,
  frames_received: 0,
  frames_executed: 0,
  last_execute_ms: null,
  peer_class: 'android_native_iroh',
  runtime_backend: 'mobile-rust-reference',
  route_ready: false,
  route_assignment: null,
  route_assignment_error: null,
  serving_intent: false,
};

function fakeApi(overrides: Partial<NodeApi> = {}) {
  let push: (s: NodeStatus) => void = () => undefined;
  let pushService: (s: ServiceStatus) => void = () => undefined;
  let link: (u: string) => void = () => undefined;
  const api: NodeApi = {
    status: vi.fn(async () => base),
    pair: vi.fn(async () => ({ ...base, phase: 'paired' })),
    join: vi.fn(async () => ({ ...base, phase: 'joined', swarm_id: 'swarm-a', member_generation: 1 })),
    drain: vi.fn(async () => ({ ...base, phase: 'drained', member_generation: 1 })),
    servingStart: vi.fn(async () => ({ running: true, uptime_ms: 0, mode: 'android_foreground_service' })),
    servingStop: vi.fn(async () => ({ running: false, uptime_ms: 0, mode: 'android_foreground_service' })),
    servingStatus: vi.fn(async () => ({ running: false, uptime_ms: 0, mode: 'android_foreground_service' })),
    onStatus: vi.fn(async (h) => {
      push = h;
      return () => undefined;
    }),
    onService: vi.fn(async (h) => {
      pushService = h;
      return () => undefined;
    }),
    exportEvidence: vi.fn(async () => ({ shared: true, observations: 3 })),
    scanQr: vi.fn(async () => 'mycelium:QRCODE'),
    onDeepLink: vi.fn(async (h) => {
      link = h;
      return () => undefined;
    }),
    ...overrides,
  };
  return {
    api,
    push: (s: NodeStatus) => push(s),
    pushService: (s: ServiceStatus) => pushService(s),
    link: (u: string) => link(u),
  };
}

describe('node app', () => {
  it('one scan pairs and joins, with a pseudonymous device ref', async () => {
    const { api } = fakeApi();
    render(<App api={api} />);
    const scan = await screen.findByRole('button', { name: 'Scan invite QR' });
    await waitFor(() => expect(scan).toBeEnabled());
    fireEvent.click(scan);
    await waitFor(() => expect(api.join).toHaveBeenCalledTimes(1));
    expect(api.pair).toHaveBeenCalledWith('mycelium:QRCODE', 'device-abcdef012345');
    expect(await screen.findByTestId('phase')).toHaveTextContent('joined');
    expect(screen.getByText('swarm-a')).toBeInTheDocument();
  });

  it('a mycelium: deep link pairs without any tap; other links are ignored', async () => {
    const { api, link } = fakeApi();
    render(<App api={api} />);
    await waitFor(() => expect(api.onDeepLink).toHaveBeenCalled());
    await act(async () => link('https://example.com'));
    expect(api.pair).not.toHaveBeenCalled();
    await act(async () => link('mycelium:INVITE'));
    await waitFor(() => expect(api.join).toHaveBeenCalled());
    expect(api.pair).toHaveBeenCalledWith('mycelium:INVITE', expect.stringMatching(/^device-/));
  });

  it('shows the core refusal code instead of pretending success', async () => {
    const { api } = fakeApi({
      join: vi.fn(async () => {
        throw 'seed_join_refused:membership_peer_class_invalid';
      }),
    });
    render(<App api={api} />);
    const scan = await screen.findByRole('button', { name: 'Scan invite QR' });
    await waitFor(() => expect(scan).toBeEnabled());
    fireEvent.click(scan);
    expect(await screen.findByRole('alert')).toHaveTextContent('membership_peer_class_invalid');
    expect(screen.getByTestId('phase')).toHaveTextContent('paired');
  });

  it('streams status events and gates serving on membership; stop drains first', async () => {
    const { api, push } = fakeApi({ status: vi.fn(async () => ({ ...base, phase: 'paired' })) });
    render(<App api={api} />);
    await screen.findByRole('button', { name: 'serving' });
    fireEvent.click(screen.getByRole('button', { name: 'serving' }));
    expect(screen.getByRole('button', { name: 'Start serving' })).toBeDisabled();
    await act(async () => push({ ...base, phase: 'joined', member_generation: 2, heartbeats_sent: 3, lease_fresh: true }));
    fireEvent.click(screen.getByRole('button', { name: 'Start serving' }));
    await screen.findByRole('button', { name: 'Stop serving' });
    fireEvent.click(screen.getByRole('button', { name: 'Stop serving' }));
    await waitFor(() => expect(api.servingStop).toHaveBeenCalled());
    const drainOrder = (api.drain as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    const stopOrder = (api.servingStop as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    expect(drainOrder).toBeLessThan(stopOrder);
  });

  it('never claims route readiness at enrollment', async () => {
    const { api, push } = fakeApi({ status: vi.fn(async () => ({ ...base, phase: 'joined', member_generation: 1 })) });
    render(<App api={api} />);
    await act(async () => push({ ...base, phase: 'joined', member_generation: 1 }));
    expect(await screen.findByText(/no \(placement/)).toBeInTheDocument();
  });
  it('shows the owner-issued route assignment and its stage, never route readiness', async () => {
    const { api, push } = fakeApi({ status: vi.fn(async () => ({ ...base, phase: 'joined', member_generation: 1 })) });
    render(<App api={api} />);
    expect(await screen.findByText('none')).toBeInTheDocument();
    await act(async () =>
      push({
        ...base,
        phase: 'joined',
        member_generation: 2,
        route_assignment: 'route-1a2b',
        stage_loaded: 'sha256:ed97b63c01f619ce350bc726419e9cb605de0d54893e00a37b8898db4b87ab9c',
      }),
    );
    expect(screen.getByText(/route-1a2b \(issued by the swarm owner\)/)).toBeInTheDocument();
    expect(screen.getByText('sha256:ed97b63c01f6')).toBeInTheDocument();
    expect(screen.getByText(/no \(placement/)).toBeInTheDocument();
    await act(async () => push({ ...base, phase: 'joined', member_generation: 2, route_assignment_error: 'route_assignment_refused:seed_member_lease_expired' }));
    expect(screen.getByText(/route_assignment_refused:seed_member_lease_expired/)).toBeInTheDocument();
  });

  it('exports the sealed evidence through the share sheet from settings', async () => {
    const { api } = fakeApi({ status: vi.fn(async () => ({ ...base, phase: 'joined', member_generation: 1 })) });
    render(<App api={api} />);
    await screen.findByRole('button', { name: 'settings' });
    fireEvent.click(screen.getByRole('button', { name: 'settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Export evidence' }));
    await waitFor(() => expect(api.exportEvidence).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/3 observations shared/)).toBeInTheDocument();
  });

  it('names the copy saved to Downloads next to the share sheet', async () => {
    const { api } = fakeApi({
      status: vi.fn(async () => ({ ...base, phase: 'joined', member_generation: 1 })),
      exportEvidence: vi.fn(async () => ({
        shared: true,
        observations: 2,
        downloads: 'mycelium-evidence-1759320000000.json',
      })),
    });
    render(<App api={api} />);
    await screen.findByRole('button', { name: 'settings' });
    fireEvent.click(screen.getByRole('button', { name: 'settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Export evidence' }));
    expect(await screen.findByText(/2 observations shared/)).toBeInTheDocument();
    expect(
      await screen.findByText(/Saved to Downloads\/mycelium-evidence-1759320000000\.json/),
    ).toBeInTheDocument();
  });

  it('shows the export refusal code when there is nothing sealed yet', async () => {
    const { api } = fakeApi({
      status: vi.fn(async () => ({ ...base, phase: 'joined', member_generation: 1 })),
      exportEvidence: vi.fn(async () => {
        throw 'mobile_evidence_export_empty';
      }),
    });
    render(<App api={api} />);
    await screen.findByRole('button', { name: 'settings' });
    fireEvent.click(screen.getByRole('button', { name: 'settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Export evidence' }));
    expect(await screen.findByText(/mobile_evidence_export_empty/)).toBeInTheDocument();
  });

  it('says it is not serving while the lease is lost, and serving again once it is held', async () => {
    const held = { ...base, phase: 'joined', swarm_id: 'swarm-a', member_generation: 1, lease_fresh: true };
    const { api, push } = fakeApi({
      status: vi.fn(async () => held),
      servingStatus: vi.fn(async () => ({ running: true, uptime_ms: 5, mode: 'android_foreground_service' })),
    });
    render(<App api={api} />);
    await screen.findByText(/lease held/);
    fireEvent.click(screen.getByRole('button', { name: 'serving' }));
    expect(await screen.findByText(/Serving in the background/)).toBeInTheDocument();

    await act(async () => push({ ...held, phase: 'reconnecting', lease_fresh: false, last_error: 'seed_unreachable:timeout' }));
    expect(screen.getByText(/Not serving right now: reconnecting/)).toBeInTheDocument();
    expect(screen.queryByText(/Serving in the background/)).not.toBeInTheDocument();
    expect(screen.getByTestId('phase')).toHaveTextContent('reconnecting');
    fireEvent.click(screen.getByRole('button', { name: 'status' }));
    expect(screen.getByText(/lease lost, reconnecting/)).toBeInTheDocument();

    await act(async () => push({ ...held, member_generation: 2 }));
    fireEvent.click(screen.getByRole('button', { name: 'serving' }));
    expect(await screen.findByText(/Serving in the background/)).toBeInTheDocument();
  });

  it('a restarted app that restored serving says it is serving once the resumed lease is held', async () => {
    // Process death: the service snapshot at mount is "not running"; the shell
    // restores it from the persisted intent and streams the change.
    const resumed = { ...base, phase: 'reconnecting', swarm_id: 'swarm-a', member_generation: 3, serving_intent: true };
    const { api, push, pushService } = fakeApi({ status: vi.fn(async () => resumed) });
    render(<App api={api} />);
    await screen.findByRole('button', { name: 'serving' });
    fireEvent.click(screen.getByRole('button', { name: 'serving' }));
    expect(await screen.findByText('Not serving.')).toBeInTheDocument();
    await act(async () => pushService({ running: true, uptime_ms: 0, mode: 'android_foreground_service' }));
    expect(screen.getByText(/Not serving right now: reconnecting/)).toBeInTheDocument();
    await act(async () => push({ ...resumed, phase: 'joined', member_generation: 4, lease_fresh: true }));
    expect(screen.getByText(/Serving in the background/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop serving' })).toBeInTheDocument();
    // Stopped from the notification: the label follows.
    await act(async () => pushService({ running: false, uptime_ms: 0, mode: 'android_foreground_service' }));
    expect(screen.getByText('Not serving.')).toBeInTheDocument();
  });

  it('a revoked node says it is no longer a member', async () => {
    const { api } = fakeApi({
      status: vi.fn(async () => ({ ...base, phase: 'revoked', swarm_id: 'swarm-a', member_generation: 1, last_error: 'seed_resume_refused:seed_resume_member_revoked' })),
      servingStatus: vi.fn(async () => ({ running: true, uptime_ms: 5, mode: 'android_foreground_service' })),
    });
    render(<App api={api} />);
    expect(await screen.findByText(/revoked by the swarm owner/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'serving' }));
    expect(screen.getByText(/no longer a swarm member/)).toBeInTheDocument();
  });

  it('keeps settings and evidence export reachable while pairing failed or is pending', async () => {
    const { api } = fakeApi({
      status: vi.fn(async () => ({ ...base, phase: 'pairing' })),
      pair: vi.fn(async () => {
        throw 'pairing_channel_unreachable:HTTP status server error (502 Bad Gateway)';
      }),
    });
    render(<App api={api} />);
    const scan = await screen.findByRole('button', { name: 'Scan invite QR' });
    await waitFor(() => expect(scan).toBeEnabled());
    fireEvent.click(scan);
    expect(await screen.findByRole('alert')).toHaveTextContent('502 Bad Gateway');
    fireEvent.click(screen.getByRole('button', { name: 'settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Export evidence' }));
    await waitFor(() => expect(api.exportEvidence).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'status' }));
    expect(screen.getByRole('button', { name: 'Scan invite QR' })).toBeInTheDocument();
  });

  it('shows a plugin rejection object as readable text', async () => {
    const { api } = fakeApi({
      scanQr: vi.fn(async () => {
        throw { message: 'Camera permission denied' };
      }),
    });
    render(<App api={api} />);
    const scan = await screen.findByRole('button', { name: 'Scan invite QR' });
    await waitFor(() => expect(scan).toBeEnabled());
    fireEvent.click(scan);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Camera permission denied');
    expect(alert).not.toHaveTextContent('[object Object]');
  });
});
