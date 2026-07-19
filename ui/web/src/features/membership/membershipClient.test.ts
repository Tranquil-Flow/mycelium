import { describe, expect, it, vi } from 'vitest';
import { HttpMembershipClient } from './membershipClient';

const status = { protocol: 'mycelium.product_membership.v1', generated_at: '2026-07-19T00:00:00Z', members: [], unknowns: [] };

describe('HttpMembershipClient', () => {
  it('uses same-origin JSON requests without bearer credentials', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(status), { headers: { 'content-type': 'application/json' } }));
    const client = new HttpMembershipClient({ fetcher });
    await client.status();
    expect(fetcher).toHaveBeenCalledWith('/api/v1/membership/status', expect.objectContaining({ credentials: 'same-origin', referrerPolicy: 'no-referrer' }));
    expect(JSON.stringify(fetcher.mock.calls)).not.toMatch(/authorization|bearer/i);
  });

  it('rejects absolute configured paths and oversized bodies', async () => {
    expect(() => new HttpMembershipClient({ statusPath: 'https://peer.invalid/status' })).toThrow(/same-origin/i);
    const client = new HttpMembershipClient({ fetcher: async () => new Response('x'.repeat(70_000)) });
    await expect(client.status()).rejects.toMatchObject({ code: 'response_too_large' });
  });
});
