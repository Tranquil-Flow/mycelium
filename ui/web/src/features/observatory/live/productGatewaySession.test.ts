import { describe, expect, it, vi } from 'vitest';
import {
  MAX_NEW_TOKENS,
  MAX_PROMPT_UTF8_BYTES,
  PRODUCT_API_PATHS,
  PRODUCT_BOOTSTRAP_PROTOCOL,
  PRODUCT_QUALIFIER_AUTHORITY,
} from '../../../app/contracts';
import {
  PRODUCT_BOOTSTRAP_PATH,
  bootstrapProductGatewaySession,
} from './productGatewaySession';

const bootstrap = {
  protocol: PRODUCT_BOOTSTRAP_PROTOCOL,
  source_mode: 'live',
  session: {
    csrf_header: 'X-Mycelium-CSRF',
    csrf_token: 'bounded-local-csrf-token',
    expires_at_unix_ms: 1_900_000_000_000,
  },
  api: PRODUCT_API_PATHS,
  limits: {
    max_prompt_utf8_bytes: MAX_PROMPT_UTF8_BYTES,
    max_new_tokens: MAX_NEW_TOKENS,
  },
  qualification_authority: PRODUCT_QUALIFIER_AUTHORITY,
} as const;

describe('product gateway session bootstrap', () => {
  it('uses only the fixed same-origin bootstrap endpoint and decodes the closed contract', async () => {
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => bootstrap }));

    await expect(bootstrapProductGatewaySession(fetcher)).resolves.toEqual(bootstrap);
    expect(fetcher).toHaveBeenCalledWith(PRODUCT_BOOTSTRAP_PATH, {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      headers: { Accept: 'application/json' },
    });
  });

  it('fails closed instead of attempting a direct-service fallback', async () => {
    const fetcher = vi.fn(async () => ({ ok: false, json: async () => ({}) }));

    await expect(bootstrapProductGatewaySession(fetcher)).rejects.toThrow(
      'product_gateway_bootstrap_failed',
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
