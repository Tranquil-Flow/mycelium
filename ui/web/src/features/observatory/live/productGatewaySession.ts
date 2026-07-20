import {
  decodeProductBootstrap,
  type ProductBootstrap,
} from '../../../app/contracts';

export const PRODUCT_BOOTSTRAP_PATH = '/api/v1/bootstrap';

type BootstrapFetch = (
  input: string,
  init: RequestInit,
) => Promise<Pick<Response, 'ok' | 'json'>>;

export async function bootstrapProductGatewaySession(
  fetchImplementation: BootstrapFetch = globalThis.fetch.bind(globalThis),
): Promise<ProductBootstrap> {
  const response = await fetchImplementation(PRODUCT_BOOTSTRAP_PATH, {
    method: 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error('product_gateway_bootstrap_failed');
  return decodeProductBootstrap(await response.json());
}
