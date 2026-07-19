import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  PRODUCT_INFERENCE_EVENT_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  type InferenceAcceptedResponse,
  type InferenceCancelResponse,
  type InferenceEvent,
  type InferenceSubmission,
  type ProductQualification,
} from '../../app/contracts';
import { InferenceClientError, type InferenceClient } from './requestClient';
import { useInferenceSession } from './useInferenceSession';

const NOW = 1_800_000_000_000;
const DIGEST_A = `sha256:${'a'.repeat(64)}`;
const DIGEST_B = `sha256:${'b'.repeat(64)}`;

function qualification(
  overrides: Partial<ProductQualification> = {},
): ProductQualification {
  return {
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    issued_at_unix_ms: NOW,
    evidence_class: 'physical_qualification',
    route_ready: true,
    reason_codes: [],
    binding: {
      qualification_id: 'qualification-a',
      qualification_digest: DIGEST_A,
      deployment_id: 'deployment-a',
      deployment_epoch: 1,
      topology_version: 1,
      model_id: 'model-a',
      resolved_commit: 'commit-a',
      manifest_digest: DIGEST_A,
      path_manifest_digest: DIGEST_A,
      stage_load_proof_digests: [DIGEST_A],
    },
    ...overrides,
  };
}

const accepted: InferenceAcceptedResponse = {
  protocol: PRODUCT_INFERENCE_PROTOCOL,
  request_id: 'request-a',
  accepted: true,
  event_path: '/api/v1/inference/request-a/events',
  cancel_path: '/api/v1/inference/request-a/cancel',
};

function event(
  sequence: number,
  value:
    | { type: 'accepted' | 'completed' | 'cancelled' }
    | { type: 'failed'; code: string }
    | { type: 'token'; token_index: number; text: string },
): InferenceEvent {
  return {
    protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
    request_id: accepted.request_id,
    sequence,
    ...value,
  } as InferenceEvent;
}

class FakeClient implements InferenceClient {
  qualifications: ProductQualification[] = [qualification()];
  readonly submissions: InferenceSubmission[] = [];
  readonly streamCursors: Array<number | null> = [];
  readonly streamBatches: InferenceEvent[][] = [];
  readonly streamFailures: unknown[] = [];
  readonly cancelFailures: unknown[] = [];
  cancelCalls = 0;

  async loadQualification(): Promise<ProductQualification> {
    return this.qualifications.shift() ?? qualification();
  }

  async submit(value: InferenceSubmission): Promise<InferenceAcceptedResponse> {
    this.submissions.push(value);
    return accepted;
  }

  async stream(
    _request: InferenceAcceptedResponse,
    lastEventId: number | null,
    onEvent: (value: InferenceEvent) => void,
  ): Promise<void> {
    this.streamCursors.push(lastEventId);
    for (const item of this.streamBatches.shift() ?? []) onEvent(item);
    const failure = this.streamFailures.shift();
    if (failure !== undefined) throw failure;
  }

  async cancel(): Promise<InferenceCancelResponse> {
    this.cancelCalls += 1;
    const failure = this.cancelFailures.shift();
    if (failure !== undefined) throw failure;
    return {
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      request_id: accepted.request_id,
      cancelled: true,
    };
  }
}

async function loaded(client: FakeClient) {
  const hook = renderHook(() =>
    useInferenceSession({ client, now: () => NOW + 1 }),
  );
  await waitFor(() => expect(hook.result.current.qualification_status).toBe('ready'));
  return hook;
}

describe('useInferenceSession', () => {
  it('loads qualification first and reports the exact route_ready=false reason', async () => {
    const client = new FakeClient();
    client.qualifications = [
      qualification({
        evidence_class: 'synthetic_test_fixture',
        route_ready: false,
        reason_codes: ['physical_qualification_missing', 'route_probe_missing'],
      }),
    ];
    const hook = renderHook(() =>
      useInferenceSession({ client, now: () => NOW + 1 }),
    );

    await waitFor(() =>
      expect(hook.result.current.submit_block_reason).toBe(
        'Route is not ready: physical_qualification_missing, route_probe_missing',
      ),
    );
    expect(hook.result.current.can_submit).toBe(false);
    expect(client.submissions).toHaveLength(0);
  });

  it('revalidates before submit, blocks a changed binding, and captures only an acknowledged exact binding', async () => {
    const client = new FakeClient();
    const changed = qualification({
      binding: {
        ...qualification().binding,
        qualification_id: 'qualification-b',
        qualification_digest: DIGEST_B,
        deployment_epoch: 2,
      },
    });
    client.qualifications = [qualification(), changed, changed];
    const hook = await loaded(client);

    await act(() => hook.result.current.start('private synthetic input', 8));
    expect(client.submissions).toHaveLength(0);
    expect(hook.result.current.submit_block_reason).toBe(
      'Qualification changed; review and accept the current binding',
    );

    act(() => hook.result.current.accept_current_qualification());
    client.streamBatches.push([event(0, { type: 'accepted' })]);
    await act(() => hook.result.current.start('private synthetic input', 8));

    expect(client.submissions).toHaveLength(1);
    expect(client.submissions[0].qualification).toEqual(changed.binding);
    expect(hook.result.current.captured_binding).toEqual(changed.binding);
    expect(hook.result.current.phase).toBe('interrupted');
  });

  it('surfaces a newly false route reason ahead of binding acknowledgement', async () => {
    const client = new FakeClient();
    const dropped = qualification({
      route_ready: false,
      reason_codes: ['route_probe_missing'],
    });
    client.qualifications = [qualification(), dropped];
    const hook = await loaded(client);

    await act(() => hook.result.current.start('private synthetic input', 8));

    expect(client.submissions).toHaveLength(0);
    expect(hook.result.current.can_submit).toBe(false);
    expect(hook.result.current.submit_block_reason).toBe(
      'Route is not ready: route_probe_missing',
    );
  });

  it('applies only contiguous events, ignores duplicates, and fails closed on gaps', async () => {
    const client = new FakeClient();
    client.qualifications = [qualification(), qualification()];
    client.streamBatches.push([
      event(0, { type: 'accepted' }),
      event(1, { type: 'token', token_index: 0, text: 'A' }),
      event(1, { type: 'token', token_index: 0, text: 'A' }),
      event(3, { type: 'completed' }),
      event(2, { type: 'token', token_index: 1, text: 'must not apply' }),
    ]);
    const hook = await loaded(client);

    await act(() => hook.result.current.start('private synthetic input', 8));

    expect(hook.result.current.output).toBe('A');
    expect(hook.result.current.last_applied_sequence).toBe(1);
    expect(hook.result.current.phase).toBe('failed');
    expect(hook.result.current.error_code).toBe('stream_sequence_invalid');
  });

  it('resumes from the last fully applied sequence without duplicating output', async () => {
    const client = new FakeClient();
    client.qualifications = [qualification(), qualification()];
    client.streamBatches.push([
      event(0, { type: 'accepted' }),
      event(1, { type: 'token', token_index: 0, text: 'A' }),
    ]);
    client.streamBatches.push([
      event(1, { type: 'token', token_index: 0, text: 'A' }),
      event(2, { type: 'token', token_index: 1, text: 'B' }),
      event(3, { type: 'completed' }),
    ]);
    const hook = await loaded(client);

    await act(() => hook.result.current.start('private synthetic input', 8));
    expect(hook.result.current.phase).toBe('interrupted');
    await act(() => hook.result.current.resume());

    expect(client.streamCursors).toEqual([null, 1]);
    expect(hook.result.current.output).toBe('AB');
    expect(hook.result.current.last_applied_sequence).toBe(3);
    expect(hook.result.current.phase).toBe('completed');
  });

  it('fails with the stable client code when the stream fails before its accepted event', async () => {
    const client = new FakeClient();
    client.qualifications = [qualification(), qualification()];
    client.streamFailures.push(new InferenceClientError('invalid_event_stream', false));
    const hook = await loaded(client);

    await act(() => hook.result.current.start('private synthetic input', 8));

    expect(hook.result.current.phase).toBe('failed');
    expect(hook.result.current.error_code).toBe('invalid_event_stream');
    expect(hook.result.current.history).toHaveLength(1);
  });

  it('enforces the captured generation bound against an overlong stream', async () => {
    const client = new FakeClient();
    client.qualifications = [qualification(), qualification()];
    client.streamBatches.push([
      event(0, { type: 'accepted' }),
      event(1, { type: 'token', token_index: 0, text: 'A' }),
      event(2, { type: 'token', token_index: 1, text: 'must not apply' }),
    ]);
    const hook = await loaded(client);

    await act(() => hook.result.current.start('private synthetic input', 1));

    expect(hook.result.current.output).toBe('A');
    expect(hook.result.current.phase).toBe('failed');
    expect(hook.result.current.error_code).toBe('stream_token_limit_exceeded');
  });

  it('does not admit a second request while an accepted request is active', async () => {
    const client = new FakeClient();
    client.qualifications = [qualification(), qualification(), qualification()];
    client.streamBatches.push([event(0, { type: 'accepted' })]);
    const hook = await loaded(client);
    await act(() => hook.result.current.start('private synthetic input', 8));

    await act(() => hook.result.current.start('second private input', 8));

    expect(client.submissions).toHaveLength(1);
    expect(hook.result.current.form_error).toBe('A request is already active');
  });

  it('coalesces cancellation and keeps terminal state immutable', async () => {
    const client = new FakeClient();
    client.qualifications = [qualification(), qualification()];
    client.streamBatches.push([event(0, { type: 'accepted' })]);
    const hook = await loaded(client);
    await act(() => hook.result.current.start('private synthetic input', 8));

    await act(async () => {
      await Promise.all([
        hook.result.current.cancel(),
        hook.result.current.cancel(),
        hook.result.current.cancel(),
      ]);
    });
    expect(client.cancelCalls).toBe(1);

    client.streamBatches.push([
      event(1, { type: 'cancelled' }),
      event(2, { type: 'token', token_index: 0, text: 'must not apply' }),
    ]);
    await act(() => hook.result.current.resume());
    expect(hook.result.current.phase).toBe('cancelled');
    expect(hook.result.current.output).toBe('');
  });

  it('allows cancellation retry after a public cancellation failure', async () => {
    const client = new FakeClient();
    client.qualifications = [qualification(), qualification()];
    client.streamBatches.push([event(0, { type: 'accepted' })]);
    client.cancelFailures.push(new InferenceClientError('network_unavailable', true));
    const hook = await loaded(client);
    await act(() => hook.result.current.start('private synthetic input', 8));

    await act(() => hook.result.current.cancel());
    expect(hook.result.current.phase).toBe('interrupted');
    expect(hook.result.current.cancellation_requested).toBe(false);
    expect(hook.result.current.error_code).toBe('network_unavailable');

    await act(() => hook.result.current.cancel());
    expect(client.cancelCalls).toBe(2);
    expect(hook.result.current.phase).toBe('cancelling');
  });

  it('fails prompt/token bounds before network submission', async () => {
    const client = new FakeClient();
    const hook = await loaded(client);

    await act(() => hook.result.current.start('', 0));

    expect(client.submissions).toHaveLength(0);
    expect(hook.result.current.form_error).toBe('Prompt is required');
  });
});
