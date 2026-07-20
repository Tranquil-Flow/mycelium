import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  DeviceLabClient,
  DeviceLabInferenceRecord,
  DeviceLabStatus,
} from './deviceLabClient';
import { DeviceLabWorkspace } from './DeviceLabWorkspace';
import workspaceCss from './DeviceLabWorkspace.module.css?raw';

const digest = (letter: string) => `sha256:${letter.repeat(64)}`;

const evidence: DeviceLabInferenceRecord = {
  protocol: 'mycelium.interactive_inference_record.v1',
  request_id: 'request-existing',
  prompt_digest: digest('a'),
  prompt_bytes: 11,
  initial_tokens: [1, 2, 3],
  generated_tokens: [4, 5],
  generated_labels: ['moon', 'swarm'],
  max_intermediate_error: 1e-7,
  max_logit_error: 2e-7,
  peer_ids: ['peer-a', 'peer-b'],
  required_distinct_peers: 2,
  observed_distinct_peers: 2,
  stage_pack_digest: digest('b'),
  token_records: [
    {
      token_index: 0,
      stage_request_id: 'request-existing:token-0',
      browser_peer_id: 'peer-a',
      browser_job_id: 'job-a',
      browser_output_digest: digest('c'),
      selected_token: 4,
      selected_label: 'moon',
      context_length: 4,
      intermediate_error: 1e-7,
      logit_error: 2e-7,
      route_ready: false,
    },
    {
      token_index: 1,
      stage_request_id: 'request-existing:token-1',
      browser_peer_id: 'peer-b',
      browser_job_id: 'job-b',
      browser_output_digest: digest('d'),
      selected_token: 5,
      selected_label: 'swarm',
      context_length: 5,
      intermediate_error: 5e-8,
      logit_error: 1e-7,
      route_ready: false,
    },
  ],
  created_at: 1_800_000_000,
  completed_at: 1_800_000_001,
  route_ready: false,
  local_evidence_only: true,
};

const status: DeviceLabStatus = {
  protocol: 'mycelium.browser_swarm_status.v1',
  interactive_protocol: 'mycelium.interactive_runtime.v1',
  run_id: 'run-test',
  local_evidence_only: true,
  route_ready: false,
  peer_count: 2,
  ready_peer_count: 2,
  pending_job_count: 0,
  active_request_count: 0,
  completed_request_count: 1,
  stage_pack_digest: digest('b'),
  vocabulary: ['<pad>', 'moon', 'swarm'],
  peers: [
    {
      peer_id: 'peer-a',
      state: 'connected',
      completed_jobs: 1,
      assigned_layer: { start_layer: 1, end_layer_exclusive: 2 },
      pack_digest: digest('b'),
    },
    {
      peer_id: 'peer-b',
      state: 'connected',
      completed_jobs: 1,
      assigned_layer: { start_layer: 1, end_layer_exclusive: 2 },
      pack_digest: digest('b'),
    },
  ],
  recent_requests: [evidence],
};

function fakeClient(overrides: Partial<DeviceLabClient> = {}): DeviceLabClient {
  return {
    status: vi.fn(async () => status),
    createInvite: vi.fn(async () => ({
      url: 'https://lab.test/#join/one-use-token',
      expires_at: 1_800_000_300,
      route_ready: false,
    })),
    infer: vi.fn(async (submission) => ({ ...evidence, request_id: submission.request_id })),
    cancel: vi.fn(async () => true),
    request: vi.fn(async () => evidence),
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('DeviceLabWorkspace', () => {
  it('fails closed without an operator capability and states every evidence boundary', () => {
    const client = fakeClient();
    render(<DeviceLabWorkspace operatorToken={null} client={client} />);

    expect(screen.getByRole('heading', { name: 'Device Lab' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Operator capability missing');
    expect(screen.getAllByText(/route_ready=false/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/local evidence only/i)).toBeInTheDocument();
    expect(screen.getByText(/synthetic browser stage.*never model inference/i)).toBeInTheDocument();
    expect(screen.getByText(/physical-device identity remains unproven/i)).toBeInTheDocument();
    expect(screen.getByText(/does not mutate Router/i)).toBeInTheDocument();
    expect(client.status).not.toHaveBeenCalled();
  });

  it('shows live joined/ready/minimum-distinct-peer status, evidence metrics, token journey, and history', async () => {
    const client = fakeClient();
    render(<DeviceLabWorkspace operatorToken="memory-token" client={client} />);

    expect(await screen.findByText('2 joined')).toBeInTheDocument();
    expect(screen.getByText('2 ready')).toBeInTheDocument();
    expect(screen.getByText(/minimum 2 distinct peer sessions met/i)).toBeInTheDocument();
    expect(screen.queryByText(/exact-2 target/i)).not.toBeInTheDocument();
    expect(screen.getByText('moon swarm')).toBeInTheDocument();
    expect(screen.getByText(/2 \/ 2 exact peer sessions/i)).toBeInTheDocument();
    expect(screen.getByRole('table', { name: /per-fixture-token browser-stage evidence/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /fixture-token journey/i })).toBeInTheDocument();
    expect(screen.getByText(/local deterministic input transform/i)).toBeInTheDocument();
    expect(screen.getByText(/bounded browser matrix exercise/i)).toBeInTheDocument();
    expect(screen.getByRole('table', { name: /recent local requests/i })).toBeInTheDocument();
    expect(screen.getAllByText('request-existing')).toHaveLength(2);
    expect(screen.getByText(/maximum stage error/i)).toBeInTheDocument();
    expect(screen.getByText(/maximum fixture-score error/i)).toBeInTheDocument();
  });

  it('selects any recent request for inspection and download', async () => {
    const newerEvidence: DeviceLabInferenceRecord = {
      ...evidence,
      request_id: 'request-newer',
      generated_labels: ['swarm', 'moon'],
    };
    const historyStatus: DeviceLabStatus = {
      ...status,
      completed_request_count: 2,
      recent_requests: [evidence, newerEvidence],
    };
    render(
      <DeviceLabWorkspace
        operatorToken="memory-token"
        client={fakeClient({ status: vi.fn(async () => historyStatus) })}
      />,
    );

    expect(await screen.findByText('swarm moon')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /inspect request-existing/i }));
    expect(screen.getByText('moon swarm')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /inspect request-existing/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /download local evidence json/i })).toBeEnabled();
  });

  it('creates one-use invites within 1-6 bounds and copies only on explicit action', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const client = fakeClient();
    render(<DeviceLabWorkspace operatorToken="memory-token" client={client} />);
    await screen.findByText('2 joined');

    const count = screen.getByLabelText(/invite count/i);
    expect(count).toHaveAttribute('min', '1');
    expect(count).toHaveAttribute('max', '6');
    fireEvent.change(count, { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: /create 3 one-use links/i }));

    const inviteList = await screen.findByRole('region', { name: /created device links/i });
    expect(client.createInvite).toHaveBeenCalledTimes(3);
    expect(client.createInvite).toHaveBeenNthCalledWith(1, 300, expect.any(AbortSignal));
    expect(within(inviteList).getAllByText(/one use/i)).toHaveLength(3);
    expect(writeText).not.toHaveBeenCalled();
    fireEvent.click(within(inviteList).getByRole('button', { name: /copy device 1 link/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://lab.test/#join/one-use-token'));
  });

  it('submits a bounded in-memory prompt with an explicit request id and supports cancellation', async () => {
    let finishInference: ((value: DeviceLabInferenceRecord) => void) | undefined;
    const pending = new Promise<DeviceLabInferenceRecord>((resolve) => {
      finishInference = resolve;
    });
    const client = fakeClient({ infer: vi.fn(async () => pending) });
    const storage = vi.spyOn(Storage.prototype, 'setItem');
    render(
      <DeviceLabWorkspace
        operatorToken="memory-token"
        client={client}
        createRequestId={() => 'request-generated'}
      />,
    );
    await screen.findByText('2 joined');
    fireEvent.change(screen.getByLabelText(/^prompt seed$/i), { target: { value: 'private local seed' } });
    fireEvent.change(screen.getByLabelText(/maximum fixture tokens/i), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText(/minimum distinct peer sessions/i), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: /run local evidence request/i }));

    await waitFor(() => expect(client.infer).toHaveBeenCalledWith({
      prompt: 'private local seed',
      max_new_tokens: 3,
      required_distinct_peers: 2,
      request_id: 'request-generated',
    }, expect.any(AbortSignal)));
    expect(screen.getByText(/active request-generated/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /cancel active request/i }));
    await waitFor(() => expect(client.cancel).toHaveBeenCalledWith(
      'request-generated',
      expect.any(AbortSignal),
    ));
    await waitFor(() => expect(screen.queryByText(/active request-generated/i)).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: /run local evidence request/i })).toBeEnabled();
    expect(storage).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain('private local seed');

    await act(async () => {
      finishInference?.({ ...evidence, request_id: 'request-generated' });
      await pending;
    });
    expect(screen.getByRole('status', { name: /device lab notice/i })).toHaveTextContent(
      /cancellation accepted/i,
    );
    expect(screen.queryByText(/completed with/i)).not.toBeInTheDocument();
  });

  it('does not let a cancelled request late-resolution overwrite a newer active request', async () => {
    let finishFirst: ((value: DeviceLabInferenceRecord) => void) | undefined;
    const first = new Promise<DeviceLabInferenceRecord>((resolve) => {
      finishFirst = resolve;
    });
    const second = new Promise<DeviceLabInferenceRecord>(() => undefined);
    const infer = vi.fn()
      .mockImplementationOnce(async () => first)
      .mockImplementationOnce(async () => second);
    let sequence = 0;
    render(
      <DeviceLabWorkspace
        operatorToken="memory-token"
        client={fakeClient({ infer })}
        createRequestId={() => `request-${++sequence}`}
      />,
    );
    await screen.findByText('2 joined');

    fireEvent.click(screen.getByRole('button', { name: /run local evidence request/i }));
    await screen.findByText(/active request-1/i);
    fireEvent.click(screen.getByRole('button', { name: /cancel active request/i }));
    await waitFor(() => expect(screen.queryByText(/active request-1/i)).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /run local evidence request/i }));
    expect(await screen.findByText(/active request-2/i)).toBeInTheDocument();
    expect(infer).toHaveBeenCalledTimes(2);

    await act(async () => {
      finishFirst?.({ ...evidence, request_id: 'request-1' });
      await first;
    });
    expect(screen.getByText(/active request-2/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /run local evidence request/i })).toBeDisabled();
    expect(screen.queryByText(/completed with/i)).not.toBeInTheDocument();
  });

  it('polls live status and aborts/clears polling on unmount', async () => {
    vi.useFakeTimers();
    const client = fakeClient({ status: vi.fn(async () => status) });
    const { unmount } = render(
      <DeviceLabWorkspace operatorToken="memory-token" client={client} pollIntervalMs={1000} />,
    );
    await act(async () => Promise.resolve());
    expect(client.status).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(client.status).toHaveBeenCalledTimes(3);
    const callsBeforeUnmount = vi.mocked(client.status).mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(3000);
    expect(client.status).toHaveBeenCalledTimes(callsBeforeUnmount);
    const lastSignal = vi.mocked(client.status).mock.calls.at(-1)?.[0];
    expect(lastSignal?.aborted).toBe(true);
  });

  it('keeps loading, stale-status, action-error, and success messages distinct', async () => {
    let statusCall = 0;
    const client = fakeClient({
      status: vi.fn(async () => {
        statusCall += 1;
        if (statusCall === 1) return status;
        throw new Error('status_temporarily_unavailable');
      }),
      createInvite: vi.fn(async () => { throw new Error('invite_creation_failed'); }),
    });
    render(<DeviceLabWorkspace operatorToken="memory-token" client={client} />);
    expect(screen.getByRole('status')).toHaveTextContent(/loading live device status/i);
    await screen.findByText('2 joined');
    fireEvent.click(screen.getByRole('button', { name: /refresh live status/i }));
    expect(await screen.findByRole('alert', { name: /status refresh error/i })).toHaveTextContent(
      /last verified status remains visible/i,
    );
    fireEvent.click(screen.getByRole('button', { name: /create 2 one-use links/i }));
    expect(await screen.findByRole('alert', { name: /device lab action error/i })).toHaveTextContent(
      'invite_creation_failed',
    );
  });

  it('downloads only the selected unsigned local evidence JSON', async () => {
    const createObjectURL = vi.fn(() => 'blob:local-evidence');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<DeviceLabWorkspace operatorToken="memory-token" client={fakeClient()} />);
    await screen.findByText('moon swarm');

    fireEvent.click(screen.getByRole('button', { name: /download local evidence json/i }));

    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(click).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status', { name: /device lab notice/i })).toHaveTextContent(
      /downloaded locally/i,
    );
  });

  it('ships responsive, reduced-motion, and forced-colors safeguards', () => {
    expect(workspaceCss).toMatch(/@media\s*\(max-width:\s*900px\)/);
    expect(workspaceCss).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    expect(workspaceCss).toMatch(/@media\s*\(forced-colors:\s*active\)/);
  });
});
