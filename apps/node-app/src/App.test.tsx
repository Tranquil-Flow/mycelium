// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from './App';
import type { NodeApi, NodeStatus } from './api';

const base: NodeStatus = {
  phase: 'unpaired',
  endpoint_id: 'abcdef0123456789',
  relay_online: true,
  swarm_id: null,
  member_generation: null,
  lease_expires_at: null,
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
};

function fakeApi(overrides: Partial<NodeApi> = {}) {
  let push: (s: NodeStatus) => void = () => undefined;
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
    exportEvidence: vi.fn(async () => ({ shared: true, observations: 3 })),
    scanQr: vi.fn(async () => 'mycelium:QRCODE'),
    onDeepLink: vi.fn(async (h) => {
      link = h;
      return () => undefined;
    }),
    ...overrides,
  };
  return { api, push: (s: NodeStatus) => push(s), link: (u: string) => link(u) };
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
    await act(async () => push({ ...base, phase: 'joined', member_generation: 2, heartbeats_sent: 3 }));
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
  it('exports the sealed evidence through the share sheet from settings', async () => {
    const { api } = fakeApi({ status: vi.fn(async () => ({ ...base, phase: 'joined', member_generation: 1 })) });
    render(<App api={api} />);
    await screen.findByRole('button', { name: 'settings' });
    fireEvent.click(screen.getByRole('button', { name: 'settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Export evidence' }));
    await waitFor(() => expect(api.exportEvidence).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/3 observations shared/)).toBeInTheDocument();
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
});
