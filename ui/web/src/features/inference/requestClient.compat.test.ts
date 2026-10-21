// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it, vi } from 'vitest';
import { MAX_NEW_TOKENS, MAX_PROMPT_UTF8_BYTES, PRODUCT_API_PATHS,
  PRODUCT_BOOTSTRAP_PROTOCOL, PRODUCT_ERROR_PROTOCOL, PRODUCT_INFERENCE_PROTOCOL,
  PRODUCT_QUALIFIER_AUTHORITY, buildInferenceSubmission, decodeProductQualification,
} from '../../app/contracts';
import { ProductInferenceClient } from './requestClient';

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


describe('explicit V1 public compatibility mode', () => {
  it('pins the supported V1 wire contract without removing qualification or CSRF', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(jsonResponse(bootstrap()))
      .mockResolvedValueOnce(jsonResponse({ protocol: PRODUCT_INFERENCE_PROTOCOL,
        request_id: 'request-a', accepted: true,
        event_path: '/api/v1/inference/request-a/events',
        cancel_path: '/api/v1/inference/request-a/cancel' }, 202));
    const options = { fetch, now: () => NOW, submissionProtocol: 'mycelium.request_gateway.v1' as const };
    const client = new ProductInferenceClient(options);
    const submission = buildInferenceSubmission('fixture prompt', 24, decodeProductQualification(qualification()), NOW);
    await client.submit(submission);
    const [url, init] = fetch.mock.calls[1];
    expect(url).toBe('/api/v1/inference');
    expect(JSON.parse(init.body)).toEqual({ protocol: 'mycelium.request_gateway.v1',
      prompt: submission.prompt, max_new_tokens: submission.max_new_tokens,
      qualification: submission.qualification });
    expect(init.credentials).toBe('same-origin');
    const headers = new Headers(init.headers);
    expect(headers.get('X-Mycelium-CSRF')).toBe('bounded-local-csrf-token');
    expect(headers.has('Authorization')).toBe(false);
    expect(fetch.mock.calls.filter(([,i]) => i?.method === 'POST')).toHaveLength(1);
  });

  it('does not retry or switch protocols after a V1 refusal', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(jsonResponse(bootstrap()))
      .mockResolvedValueOnce(jsonResponse({ protocol: PRODUCT_ERROR_PROTOCOL,
        code: 'qualification_not_ready', retryable: false }, 409));
    const options = { fetch, now: () => NOW, submissionProtocol: 'mycelium.request_gateway.v1' as const };
    const client = new ProductInferenceClient(options);
    const submission = buildInferenceSubmission('fixture prompt', 24, decodeProductQualification(qualification()), NOW);
    await expect(client.submit(submission)).rejects.toMatchObject({ code: 'qualification_not_ready' });
    const posts = fetch.mock.calls.filter(([,i]) => i?.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0][1].body).protocol).toBe('mycelium.request_gateway.v1');
  });
});
