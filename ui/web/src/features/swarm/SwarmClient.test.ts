import { describe, expect, it, vi } from 'vitest';
import {
  PRODUCT_BOOTSTRAP_PROTOCOL,
  PRODUCT_SWARM_PROTOCOL,
  type ProductBootstrap,
  type ProductSwarmStatus,
} from '../../app/contracts';
import { HttpSwarmClient, SwarmClientError, type SwarmFetcher } from './SwarmClient';

const status: ProductSwarmStatus = {
  protocol: PRODUCT_SWARM_PROTOCOL,
  native_nodes: [
    {
      member_id: 'native-1',
      capability: 'native_inference_node',
      membership_state: 'reachable',
      connectivity: 'direct',
      endpoint_id: 'endpoint-1',
    },
  ],
  browser_workers: [],
};

const bootstrap: ProductBootstrap = {
  protocol: PRODUCT_BOOTSTRAP_PROTOCOL,
  source_mode: 'live',
  session: {
    csrf_header: 'X-Mycelium-CSRF',
    csrf_token: 'bounded-local-csrf-token',
    expires_at_unix_ms: 2_000_000_000_000,
  },
  api: {
    observatory_snapshot: '/api/v1/observatory/snapshot',
    observatory_events: '/api/v1/observatory/events',
    qualification_current: '/api/v1/qualification/current',
    inference_submit: '/api/v1/inference',
    swarm_status: '/api/v1/swarm/status',
    swarm_invites: '/api/v1/swarm/invites',
    swarm_join: '/api/v1/swarm/join',
    swarm_leave: '/api/v1/swarm/leave',
  },
  limits: { max_prompt_utf8_bytes: 131_072, max_new_tokens: 4_096 },
  qualification_authority: 'mycelium_qualification.qualifier:RouteQualificationV1',
};

function jsonResponse(value: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  });
}

describe('HttpSwarmClient', () => {
  it('loads and validates same-origin status without an upstream bearer credential', async () => {
    const fetcher = vi.fn<SwarmFetcher>(async () => jsonResponse(status));
    const client = new HttpSwarmClient({ fetcher, now: () => 1_900_000_000_000 });

    await expect(client.status()).resolves.toEqual(status);
    expect(fetcher).toHaveBeenCalledWith(
      '/api/v1/swarm/status',
      expect.objectContaining({ method: 'GET', credentials: 'same-origin', cache: 'no-store' }),
    );
    const headers = new Headers(fetcher.mock.calls[0][1]?.headers);
    expect(headers.has('authorization')).toBe(false);
  });

  it('uses the bounded product session CSRF value for invite, join, and leave actions', async () => {
    const responses = [
      bootstrap,
      {
        protocol: PRODUCT_SWARM_PROTOCOL,
        invite_id: 'invite-1',
        invite_code: 'single-use-invite-code-1234',
        capability: 'native_inference_node',
        expires_at_unix_ms: 1_900_000_030_000,
      },
      {
        protocol: PRODUCT_SWARM_PROTOCOL,
        joined: true,
        member_id: 'native-2',
        capability: 'native_inference_node',
      },
      {
        protocol: PRODUCT_SWARM_PROTOCOL,
        member_id: 'native-2',
        left: true,
      },
    ];
    const fetcher = vi.fn<SwarmFetcher>(async () => jsonResponse(responses.shift()));
    const client = new HttpSwarmClient({ fetcher, now: () => 1_900_000_000_000 });

    await client.createInvite('native_inference_node', 30);
    await client.join('single-use-invite-code-1234', 'Desk node');
    await client.leave('native-2');

    expect(fetcher).toHaveBeenCalledTimes(4);
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/bootstrap');
    for (const call of fetcher.mock.calls.slice(1)) {
      const headers = new Headers(call[1]?.headers);
      expect(headers.get('X-Mycelium-CSRF')).toBe('bounded-local-csrf-token');
      expect(headers.has('authorization')).toBe(false);
      expect(call[1]?.credentials).toBe('same-origin');
    }
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toMatchObject({
      action: 'create_invite',
      capability: 'native_inference_node',
      expires_in_seconds: 30,
    });
  });

  it('fails closed on malformed, oversized, and stable public error responses', async () => {
    const malformed = new HttpSwarmClient({
      fetcher: async () => jsonResponse({ ...status, private_address: '10.0.0.8' }),
    });
    await expect(malformed.status()).rejects.toMatchObject({ code: 'invalid_product_contract' });

    const oversized = new HttpSwarmClient({
      maxResponseBytes: 64,
      fetcher: async () =>
        new Response('{"padding":"' + 'x'.repeat(128) + '"}', {
          headers: { 'content-type': 'application/json' },
        }),
    });
    await expect(oversized.status()).rejects.toMatchObject({ code: 'response_too_large' });

    const denied = new HttpSwarmClient({
      fetcher: async () =>
        jsonResponse(
          { protocol: 'mycelium.product_ui.error.v1', code: 'session_expired', retryable: false },
          { status: 401 },
        ),
    });
    await expect(denied.status()).rejects.toEqual(
      expect.objectContaining<Partial<SwarmClientError>>({ code: 'session_expired', status: 401 }),
    );
  });
});
