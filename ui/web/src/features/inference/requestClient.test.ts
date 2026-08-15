import { describe, expect, it, vi } from 'vitest';
import {
  MAX_NEW_TOKENS,
  MAX_PROMPT_UTF8_BYTES,
  PRODUCT_API_PATHS,
  PRODUCT_BOOTSTRAP_PROTOCOL,
  PRODUCT_ERROR_PROTOCOL,
  PRODUCT_INFERENCE_EVENT_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  PRODUCT_QUALIFIER_AUTHORITY,
  buildInferenceSubmission,
  decodeProductQualification,
  type InferenceEvent,
} from '../../app/contracts';
import {
  InferenceClientError,
  ProductInferenceClient,
  type InferenceFetch,
} from './requestClient';

const NOW = 1_800_000_000_000;
const DIGEST_A = `sha256:${'a'.repeat(64)}`;

function bootstrap() {
  return {
    protocol: PRODUCT_BOOTSTRAP_PROTOCOL,
    source_mode: 'live',
    session: {
      csrf_header: 'X-Mycelium-CSRF',
      csrf_token: 'bounded-local-csrf-token',
      expires_at_unix_ms: NOW + 60_000,
    },
    api: PRODUCT_API_PATHS,
    limits: {
      max_prompt_utf8_bytes: MAX_PROMPT_UTF8_BYTES,
      max_new_tokens: MAX_NEW_TOKENS,
    },
    qualification_authority: PRODUCT_QUALIFIER_AUTHORITY,
  } as const;
}

function qualification() {
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
      deployment_epoch: 4,
      topology_version: 9,
      model_id: 'org/model-a',
      resolved_commit: 'commit-a',
      manifest_digest: DIGEST_A,
      path_manifest_digest: DIGEST_A,
      stage_load_proof_digests: [DIGEST_A],
    },
  } as const;
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function headers(init: RequestInit | undefined): Headers {
  return new Headers(init?.headers);
}

describe('ProductInferenceClient', () => {
  it('bootstraps before qualification and sends only same-origin, cookie-session requests', async () => {
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetch: InferenceFetch = vi.fn(async (input, init) => {
      calls.push([input, init]);
      return calls.length === 1 ? jsonResponse(bootstrap()) : jsonResponse(qualification());
    });
    const client = new ProductInferenceClient({ fetch });

    await expect(client.loadQualification()).resolves.toEqual(
      decodeProductQualification(qualification()),
    );

    expect(calls.map(([input]) => input)).toEqual([
      '/api/v1/bootstrap',
      '/api/v1/qualification/current',
    ]);
    for (const [input, init] of calls) {
      expect(String(input)).toMatch(/^\/api\/v1\//);
      expect(init?.credentials).toBe('same-origin');
      expect(init?.redirect).toBe('error');
      expect(init?.referrerPolicy).toBe('no-referrer');
      expect(headers(init).has('Authorization')).toBe(false);
    }
  });

  it('renews the product session after a backend restart invalidates the cached bootstrap', async () => {
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetch: InferenceFetch = vi.fn(async (input, init) => {
      calls.push([input, init]);
      if (calls.length === 1 || calls.length === 3) return jsonResponse(bootstrap());
      if (calls.length === 2) {
        return jsonResponse(
          { protocol: 'mycelium.product_ui.error.v1', code: 'session_invalid', retryable: true },
          401,
        );
      }
      return jsonResponse(qualification());
    });
    const client = new ProductInferenceClient({ fetch });

    await expect(client.loadQualification()).resolves.toEqual(
      decodeProductQualification(qualification()),
    );

    expect(calls.map(([input]) => input)).toEqual([
      '/api/v1/bootstrap',
      '/api/v1/qualification/current',
      '/api/v1/bootstrap',
      '/api/v1/qualification/current',
    ]);
  });

  it('submits the exact captured qualification with CSRF and no prompt-bearing URL', async () => {
    const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = [];
    const fetch: InferenceFetch = vi.fn(async (input, init) => {
      calls.push([input, init]);
      if (calls.length === 1) return jsonResponse(bootstrap());
      if (calls.length === 2) return jsonResponse(qualification());
      return jsonResponse(
        {
          protocol: PRODUCT_INFERENCE_PROTOCOL,
          request_id: 'request-a',
          accepted: true,
          event_path: '/api/v1/inference/request-a/events',
          cancel_path: '/api/v1/inference/request-a/cancel',
        },
        202,
      );
    });
    const client = new ProductInferenceClient({ fetch });
    const qualified = await client.loadQualification();
    const submission = buildInferenceSubmission('private synthetic input', 32, qualified, NOW + 1);

    const accepted = await client.submit(submission);

    expect(accepted.request_id).toBe('request-a');
    const [url, init] = calls[2];
    expect(url).toBe('/api/v1/inference');
    expect(String(url)).not.toContain(submission.prompt);
    expect(init?.method).toBe('POST');
    expect(init?.cache).toBe('no-store');
    expect(init?.referrerPolicy).toBe('no-referrer');
    expect(headers(init).get('X-Mycelium-CSRF')).toBe('bounded-local-csrf-token');
    expect(headers(init).has('Authorization')).toBe(false);
    expect(JSON.parse(String(init?.body))).toEqual(submission);
  });

  it('decodes bounded SSE frames and resumes after the last fully applied event ID', async () => {
    const sse = [
      'id: 0',
      'event: accepted',
      `data: ${JSON.stringify({
        protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
        request_id: 'request-a',
        sequence: 0,
        type: 'accepted',
      })}`,
      '',
      'id: 1',
      'event: token',
      `data: ${JSON.stringify({
        protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
        request_id: 'request-a',
        sequence: 1,
        type: 'token',
        token_index: 0,
        text: 'synthetic fragment',
      })}`,
      '',
      '',
    ].join('\n');
    const fetch: InferenceFetch = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(bootstrap()))
      .mockResolvedValueOnce(jsonResponse(qualification()))
      .mockResolvedValueOnce(
        new Response(sse, {
          headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
        }),
      );
    const client = new ProductInferenceClient({ fetch });
    await client.loadQualification();
    const events: InferenceEvent[] = [];

    await client.stream(
      {
        protocol: PRODUCT_INFERENCE_PROTOCOL,
        request_id: 'request-a',
        accepted: true,
        event_path: '/api/v1/inference/request-a/events',
        cancel_path: '/api/v1/inference/request-a/cancel',
      },
      0,
      (event) => events.push(event),
    );

    expect(events.map((event) => event.sequence)).toEqual([0, 1]);
    const [, streamInit] = vi.mocked(fetch).mock.calls[2];
    expect(headers(streamInit).get('Last-Event-ID')).toBe('0');
    expect(headers(streamInit).get('Accept')).toBe('text/event-stream');
    expect(streamInit?.referrerPolicy).toBe('no-referrer');
  });

  it('rejects forged request-specific paths before making a stream or cancellation request', async () => {
    const fetch: InferenceFetch = vi.fn();
    const client = new ProductInferenceClient({ fetch });
    const forged = {
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      request_id: 'request-a',
      accepted: true,
      event_path: '/api/v1/inference/request-b/events',
      cancel_path: '/api/v1/inference/request-b/cancel',
    } as const;

    await expect(client.stream(forged, null, () => undefined)).rejects.toMatchObject({
      code: 'invalid_acceptance_contract',
    });
    await expect(client.cancel(forged)).rejects.toMatchObject({
      code: 'invalid_acceptance_contract',
    });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('fails closed for mismatched SSE IDs, oversized frames, and non-contract errors', async () => {
    const invalidStreams = [
      [
        'id: 3',
        'event: completed',
        `data: ${JSON.stringify({
          protocol: PRODUCT_INFERENCE_EVENT_PROTOCOL,
          request_id: 'request-a',
          sequence: 2,
          type: 'completed',
        })}`,
        '',
        '',
      ].join('\n'),
      `data: ${'x'.repeat(300_001)}\n\n`,
    ];

    for (const sse of invalidStreams) {
      const fetch: InferenceFetch = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(bootstrap()))
        .mockResolvedValueOnce(jsonResponse(qualification()))
        .mockResolvedValueOnce(
          new Response(sse, { headers: { 'Content-Type': 'text/event-stream' } }),
        );
      const client = new ProductInferenceClient({ fetch });
      await client.loadQualification();
      await expect(
        client.stream(
          {
            protocol: PRODUCT_INFERENCE_PROTOCOL,
            request_id: 'request-a',
            accepted: true,
            event_path: '/api/v1/inference/request-a/events',
            cancel_path: '/api/v1/inference/request-a/cancel',
          },
          null,
          () => undefined,
        ),
      ).rejects.toMatchObject({ code: 'invalid_event_stream' });
    }
  });

  it('uses idempotent same-origin cancellation and exposes stable public error codes only', async () => {
    const fetch: InferenceFetch = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(bootstrap()))
      .mockResolvedValueOnce(jsonResponse(qualification()))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            protocol: PRODUCT_INFERENCE_PROTOCOL,
            request_id: 'request-a',
            cancelled: true,
          },
          202,
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          { protocol: PRODUCT_ERROR_PROTOCOL, code: 'route_dropped', retryable: false },
          409,
        ),
      );
    const client = new ProductInferenceClient({ fetch });
    await client.loadQualification();
    const accepted = {
      protocol: PRODUCT_INFERENCE_PROTOCOL,
      request_id: 'request-a',
      accepted: true,
      event_path: '/api/v1/inference/request-a/events',
      cancel_path: '/api/v1/inference/request-a/cancel',
    } as const;

    await expect(client.cancel(accepted)).resolves.toMatchObject({ cancelled: true });
    const [cancelUrl, cancelInit] = vi.mocked(fetch).mock.calls[2];
    expect(cancelUrl).toBe('/api/v1/inference/request-a/cancel');
    expect(cancelInit?.method).toBe('DELETE');
    expect(cancelInit?.referrerPolicy).toBe('no-referrer');
    expect(headers(cancelInit).get('X-Mycelium-CSRF')).toBe('bounded-local-csrf-token');

    await expect(client.cancel(accepted)).rejects.toEqual(
      expect.objectContaining<Partial<InferenceClientError>>({
        name: 'InferenceClientError',
        code: 'route_dropped',
        retryable: false,
      }),
    );
  });
});
