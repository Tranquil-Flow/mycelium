import { describe, expect, it, vi } from 'vitest';
import {
  DeviceLabClientError,
  OperatorDeviceLabClient,
  type DeviceLabInferenceRecord,
  type DeviceLabStatus,
} from './deviceLabClient';

const record: DeviceLabInferenceRecord = {
  protocol: 'mycelium.interactive_inference_record.v1',
  request_id: 'request-test',
  prompt_digest: `sha256:${'a'.repeat(64)}`,
  prompt_bytes: 12,
  initial_tokens: [1, 2],
  generated_tokens: [4, 5],
  generated_labels: ['moon', 'swarm'],
  max_intermediate_error: 1e-7,
  max_logit_error: 2e-7,
  peer_ids: ['peer-a', 'peer-b'],
  required_distinct_peers: 2,
  observed_distinct_peers: 2,
  stage_pack_digest: `sha256:${'b'.repeat(64)}`,
  token_records: [
    {
      token_index: 0,
      stage_request_id: 'request-test:token-0',
      browser_peer_id: 'peer-a',
      browser_job_id: 'job-a',
      browser_output_digest: `sha256:${'c'.repeat(64)}`,
      selected_token: 4,
      selected_label: 'moon',
      context_length: 3,
      intermediate_error: 1e-7,
      logit_error: 2e-7,
      route_ready: false,
    },
    {
      token_index: 1,
      stage_request_id: 'request-test:token-1',
      browser_peer_id: 'peer-b',
      browser_job_id: 'job-b',
      browser_output_digest: `sha256:${'d'.repeat(64)}`,
      selected_token: 5,
      selected_label: 'swarm',
      context_length: 4,
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
  stage_pack_digest: `sha256:${'b'.repeat(64)}`,
  vocabulary: ['<pad>', 'moon', 'swarm'],
  peers: [
    {
      peer_id: 'peer-a',
      state: 'connected',
      completed_jobs: 2,
      assigned_layer: { start_layer: 1, end_layer_exclusive: 2 },
      pack_digest: `sha256:${'b'.repeat(64)}`,
    },
  ],
  recent_requests: [record],
};

function jsonResponse(value: unknown, statusCode = 200): Response {
  return new Response(JSON.stringify(value), {
    status: statusCode,
    headers: { 'content-type': 'application/json' },
  });
}

describe('OperatorDeviceLabClient', () => {
  it('uses the bearer capability on direct same-origin GET and POST endpoints', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/interactive/status') return jsonResponse({ ok: true, status });
      if (path === '/api/interactive/invite') {
        return jsonResponse({
          ok: true,
          invite: {
            url: 'https://lab.test/#join/one-use',
            expires_at: 1_800_000_300,
            route_ready: false,
          },
        });
      }
      if (path === '/api/interactive/infer') return jsonResponse({ ok: true, record });
      if (path === '/api/interactive/cancel') {
        return jsonResponse({ ok: true, cancelled: true, route_ready: false });
      }
      if (path === '/api/interactive/requests/request-test') {
        return jsonResponse({ ok: true, record });
      }
      throw new Error(`unexpected_path:${path}`);
    });
    const client = new OperatorDeviceLabClient({ operatorToken: 'memory-only-token', fetcher });

    await expect(client.status()).resolves.toEqual(status);
    await expect(client.createInvite(300)).resolves.toMatchObject({ route_ready: false });
    await expect(client.infer({
      prompt: 'private prompt',
      max_new_tokens: 2,
      required_distinct_peers: 2,
      request_id: 'request-test',
    })).resolves.toEqual(record);
    await expect(client.cancel('request-test')).resolves.toBe(true);
    await expect(client.request('request-test')).resolves.toEqual(record);

    expect(fetcher).toHaveBeenCalledTimes(5);
    for (const [, init] of fetcher.mock.calls) {
      expect(new Headers(init?.headers).get('authorization')).toBe('Bearer memory-only-token');
      expect(init).toMatchObject({
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
      });
    }
    expect(JSON.parse(String(fetcher.mock.calls[2][1]?.body))).toEqual({
      prompt: 'private prompt',
      max_new_tokens: 2,
      required_distinct_peers: 2,
      request_id: 'request-test',
    });
  });

  it('invokes browser fetch with the global receiver instead of the client instance', async () => {
    let observedThis: unknown;
    const fetcher = vi.fn(function (this: unknown) {
      observedThis = this;
      return Promise.resolve(jsonResponse({ ok: true, status }));
    });
    const client = new OperatorDeviceLabClient({ operatorToken: 'memory-only-token', fetcher });

    await expect(client.status()).resolves.toEqual(status);
    expect(observedThis).toBe(globalThis);
  });

  it('fails closed on missing capabilities, unsafe request ids, and readiness claims', async () => {
    expect(() => new OperatorDeviceLabClient({ operatorToken: '' })).toThrow(
      new DeviceLabClientError('operator_capability_missing'),
    );
    const fetcher = vi.fn(async () => jsonResponse({
      ok: true,
      status: { ...status, route_ready: true },
    }));
    const client = new OperatorDeviceLabClient({ operatorToken: 'token', fetcher });

    await expect(client.status()).rejects.toMatchObject({ code: 'invalid_device_lab_contract' });
    await expect(client.request('../status')).rejects.toMatchObject({ code: 'request_id_invalid' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('fails closed when completed evidence does not prove one exact peer cohort', async () => {
    const malformedStatus = {
      ...status,
      recent_requests: [{ ...record, observed_distinct_peers: 3 }],
    };
    const statusClient = new OperatorDeviceLabClient({
      operatorToken: 'token',
      fetcher: vi.fn(async () => jsonResponse({ ok: true, status: malformedStatus })),
    });
    await expect(statusClient.status()).rejects.toMatchObject({ code: 'device_lab_record_invalid' });

    const malformedRecord = {
      ...record,
      token_records: [
        record.token_records[0],
        { ...record.token_records[1], browser_peer_id: 'peer-outside-cohort' },
      ],
    };
    const recordClient = new OperatorDeviceLabClient({
      operatorToken: 'token',
      fetcher: vi.fn(async () => jsonResponse({ ok: true, record: malformedRecord })),
    });
    await expect(recordClient.infer({
      prompt: 'private prompt',
      max_new_tokens: 2,
      required_distinct_peers: 2,
      request_id: 'request-test',
    })).rejects.toMatchObject({ code: 'device_lab_record_invalid' });

    const missingContributor = {
      ...record,
      token_records: [
        record.token_records[0],
        { ...record.token_records[1], browser_peer_id: 'peer-a' },
      ],
    };
    const missingContributorClient = new OperatorDeviceLabClient({
      operatorToken: 'token',
      fetcher: vi.fn(async () => jsonResponse({ ok: true, record: missingContributor })),
    });
    await expect(missingContributorClient.request('request-test')).rejects.toMatchObject({
      code: 'device_lab_record_invalid',
    });
  });

  it('surfaces bounded public API errors without leaking the operator token', async () => {
    const fetcher = vi.fn(async () => jsonResponse({ ok: false, error: 'operator_unauthorized' }, 401));
    const client = new OperatorDeviceLabClient({ operatorToken: 'do-not-leak', fetcher });

    await expect(client.status()).rejects.toEqual(
      new DeviceLabClientError('operator_unauthorized', 401, false),
    );
    await client.status().catch((error: unknown) => {
      expect(String(error)).not.toContain('do-not-leak');
    });
  });
});
