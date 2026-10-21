// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from 'vitest';
import {
  PRODUCT_INFERENCE_EVENT_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  type InferenceAcceptedResponse,
  type InferenceCancelResponse,
  type InferenceEvent,
  type InferenceSubmission,
  type ProductQualification,
} from '../../app/contracts';
import { InferenceClientError, type InferenceClient } from '../inference/requestClient';
import { TimingInferenceClient } from './clientTiming';

const hash = `sha256:${'a'.repeat(64)}`;
const qualification: ProductQualification = {
  protocol: PRODUCT_INFERENCE_PROTOCOL,
  issued_at_unix_ms: 10_000,
  evidence_class: 'physical_qualification',
  route_ready: true,
  reason_codes: [],
  binding: {
    qualification_id: 'test-qualification',
    qualification_digest: hash,
    deployment_id: 'test-deployment',
    deployment_epoch: 1,
    topology_version: 1,
    model_id: 'test-only-model',
    resolved_commit: 'test-commit',
    manifest_digest: hash,
    path_manifest_digest: hash,
    stage_load_proof_digests: [hash],
  },
};
const accepted: InferenceAcceptedResponse = {
  protocol: PRODUCT_INFERENCE_PROTOCOL,
  request_id: 'req-timing',
  accepted: true,
  event_path: '/api/v1/inference/req-timing/events',
  cancel_path: '/api/v1/inference/req-timing/cancel',
};
const submission: InferenceSubmission = {
  protocol: 'mycelium.request_gateway.v2',
  prompt: 'A test-only prompt',
  max_new_tokens: 8,
  qualification: qualification.binding,
  workload_profile_id: 'interactive-default',
  qos_class: 'interactive',
};

function event(sequence: number, payload: Partial<InferenceEvent>): InferenceEvent {
  return {
    protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
    request_id: accepted.request_id,
    publisher_generation: 1,
    sequence,
    ...payload,
  } as InferenceEvent;
}

const token = (sequence: number): InferenceEvent =>
  event(sequence, { type: 'token', token_index: sequence - 1, text: 'x' });

/** A delegate whose submit resolution and stream deliveries the test drives by hand. */
class ManualDelegate implements InferenceClient {
  resolveSubmit: ((value: InferenceAcceptedResponse) => void) | null = null;
  rejectSubmit: ((reason: unknown) => void) | null = null;
  deliver: ((value: InferenceEvent) => void) | null = null;
  endStream: (() => void) | null = null;
  streams: { cursor: number | null; generation: number | undefined }[] = [];
  qualificationSignals: (AbortSignal | undefined)[] = [];
  cancelled: string[] = [];
  submitFailure: unknown = null;

  loadQualification = async (signal?: AbortSignal): Promise<ProductQualification> => {
    this.qualificationSignals.push(signal);
    return qualification;
  };

  submit = (_submission: InferenceSubmission, _signal?: AbortSignal) =>
    new Promise<InferenceAcceptedResponse>((resolve, reject) => {
      this.resolveSubmit = resolve;
      this.rejectSubmit = reject;
    });

  stream = async (
    _request: InferenceAcceptedResponse,
    cursor: number | null,
    onEvent: (value: InferenceEvent) => void,
    _signal?: AbortSignal,
    generation?: number,
  ): Promise<void> => {
    this.streams.push({ cursor, generation });
    this.deliver = onEvent;
    await new Promise<void>((resolve) => { this.endStream = resolve; });
  };

  cancel = async (request: InferenceAcceptedResponse): Promise<InferenceCancelResponse> => {
    this.cancelled.push(request.request_id);
    return {
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      request_id: request.request_id,
      cancelled: true,
    };
  };
}

function fixture() {
  let clock = 10_000;
  const delegate = new ManualDelegate();
  const client = new TimingInferenceClient(delegate, () => clock);
  const setClock = (value: number) => { clock = value; };
  return { delegate, client, setClock };
}

describe('client-side request timing', () => {
  it('measures TTFT and TPOT from real event arrivals on the browser clock', async () => {
    const { delegate, client, setClock } = fixture();
    expect(client.measure()).toMatchObject({
      basis: 'unavailable',
      unavailable_reason: 'no_request_accepted',
      ttft_ms: null,
      tokens: 0,
    });

    const pending = client.submit(submission);
    setClock(10_250);
    delegate.resolveSubmit?.(accepted);
    await pending;
    expect(client.measure().unavailable_reason).toBe('no_token_observed');

    const streaming = client.stream(accepted, null, () => undefined);
    setClock(12_516.8);
    delegate.deliver?.(token(1));
    expect(client.measure()).toMatchObject({
      basis: 'unavailable',
      unavailable_reason: 'single_token_observed',
      tokens: 1,
    });
    expect(client.measure().ttft_ms).toBeCloseTo(2_266.8, 6);

    for (let index = 2; index <= 19; index += 1) {
      setClock(12_516.8 + (index - 1) * 196.02);
      delegate.deliver?.(token(index));
    }
    delegate.deliver?.(event(20, { type: 'completed' }));
    const measured = client.measure();
    expect(measured.basis).toBe('client_measured');
    expect(measured.tokens).toBe(19);
    expect(measured.ttft_ms).toBeCloseTo(2_266.8, 6);
    expect(measured.tpot_ms).toBeCloseTo(196.02, 6);
    expect(measured.last_token_ms).toBeCloseTo(5_795.16, 6);
    expect(measured.unavailable_reason).toBeNull();
    expect(measured.resumed_streams).toBe(0);

    delegate.endStream?.();
    await streaming;
  });

  it('keeps one request\'s measurement across a cursor resume and ignores replayed tokens', async () => {
    const { delegate, client, setClock } = fixture();
    const first = client.submit(submission);
    delegate.resolveSubmit?.(accepted);
    await first;

    const streaming = client.stream(accepted, null, () => undefined);
    setClock(11_000);
    delegate.deliver?.(token(1));
    delegate.endStream?.();
    await streaming;

    const resumed = client.stream(accepted, 1, () => undefined, undefined, 1);
    delegate.deliver?.(token(1));
    expect(client.measure().tokens).toBe(1);
    setClock(11_500);
    delegate.deliver?.(token(2));
    const measured = client.measure();
    expect(measured.tokens).toBe(2);
    expect(measured.resumed_streams).toBe(1);
    expect(measured.ttft_ms).toBeCloseTo(1_000, 6);
    expect(measured.tpot_ms).toBeCloseTo(500, 6);

    delegate.endStream?.();
    await resumed;
    expect(delegate.streams).toEqual([
      { cursor: null, generation: undefined },
      { cursor: 1, generation: 1 },
    ]);
  });

  it('reports no TTFT rather than estimating when no token is ever applied', async () => {
    const { delegate, client } = fixture();
    const pending = client.submit(submission);
    delegate.resolveSubmit?.(accepted);
    await pending;
    const streaming = client.stream(accepted, null, () => undefined);
    delegate.deliver?.(event(0, { type: 'accepted' }));
    delegate.deliver?.(event(1, { type: 'failed', code: 'worker_unavailable' }));
    const measured = client.measure();
    expect(measured.ttft_ms).toBeNull();
    expect(measured.tpot_ms).toBeNull();
    expect(measured.last_token_ms).toBeNull();
    expect(measured.unavailable_reason).toBe('no_token_observed');
    delegate.endStream?.();
    await streaming;
  });

  it('refuses a negative TTFT when the browser clock moves backwards', async () => {
    const { delegate, client, setClock } = fixture();
    const pending = client.submit(submission);
    setClock(20_000);
    delegate.resolveSubmit?.(accepted);
    await pending;
    const streaming = client.stream(accepted, null, () => undefined);
    setClock(19_000);
    delegate.deliver?.(token(1));
    const measured = client.measure();
    expect(measured.basis).toBe('unavailable');
    expect(measured.ttft_ms).toBeNull();
    expect(measured.unavailable_reason).toBe('non_monotonic_clock');
    delegate.endStream?.();
    await streaming;
  });

  it('passes the delegate contract through unchanged and forwards its named failure', async () => {
    const { delegate, client } = fixture();
    const signal = new AbortController().signal;
    await client.loadQualification(signal);
    expect(delegate.qualificationSignals).toEqual([signal]);

    const pending = client.submit(submission);
    delegate.rejectSubmit?.(new InferenceClientError('route_unavailable', true, 503));
    await expect(pending).rejects.toThrow('route_unavailable');
    expect(client.measure().unavailable_reason).toBe('no_request_accepted');

    await expect(client.cancel(accepted)).resolves.toEqual({
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      request_id: accepted.request_id,
      cancelled: true,
    });
    expect(delegate.cancelled).toEqual([accepted.request_id]);
  });

  it('forwards every event to the caller even when it is not counted', () => {
    const { delegate, client } = fixture();
    const seen: InferenceEvent[] = [];
    void client.stream(accepted, null, (value) => seen.push(value));
    delegate.deliver?.(event(0, { type: 'accepted' }));
    delegate.deliver?.(token(1));
    delegate.deliver?.(token(1));
    expect(seen).toHaveLength(3);
    expect(client.measure().tokens).toBe(1);
  });
});
