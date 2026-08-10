import {
  buildSwarmInviteRequest,
  buildSwarmJoinRequest,
  buildSwarmLeaveRequest,
  decodeProductBootstrap,
  decodeProductError,
  decodeProductSwarmStatus,
  decodeSwarmInviteResponse,
  decodeSwarmJoinResponse,
  decodeSwarmLeaveResponse,
  type ProductBootstrap,
  type ProductSwarmStatus,
  type SwarmInviteResponse,
  type SwarmJoinResponse,
  type SwarmLeaveResponse,
} from '../../app/contracts';

const DEFAULT_MAX_RESPONSE_BYTES = 1_048_576;
const BOOTSTRAP_PATH = '/api/v1/bootstrap';

type Capability = 'native_inference_node' | 'synthetic_browser_probe';
export type SwarmFetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export interface SwarmClient {
  status(): Promise<ProductSwarmStatus>;
  createInvite(capability: Capability, expiresInSeconds: number): Promise<SwarmInviteResponse>;
  join(inviteCode: string, displayName: string): Promise<SwarmJoinResponse>;
  leave(memberId: string): Promise<SwarmLeaveResponse>;
}

export interface HttpSwarmClientOptions {
  readonly fetcher?: SwarmFetcher;
  readonly now?: () => number;
  readonly maxResponseBytes?: number;
}

export class SwarmClientError extends Error {
  constructor(
    readonly code: string,
    readonly status: number | null = null,
    readonly retryable = false,
  ) {
    super(code);
    this.name = 'SwarmClientError';
  }
}

function requireSameOriginPath(path: string): string {
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) {
    throw new SwarmClientError('invalid_same_origin_path');
  }
  const parsed = new URL(path, window.location.origin);
  if (parsed.origin !== window.location.origin || parsed.username || parsed.password) {
    throw new SwarmClientError('invalid_same_origin_path');
  }
  return `${parsed.pathname}${parsed.search}`;
}

export class HttpSwarmClient implements SwarmClient {
  readonly #fetcher: SwarmFetcher;
  readonly #now: () => number;
  readonly #maxResponseBytes: number;
  #bootstrap: ProductBootstrap | null = null;
  #bootstrapPromise: Promise<ProductBootstrap> | null = null;

  constructor(options: HttpSwarmClientOptions = {}) {
    this.#fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
    this.#now = options.now ?? Date.now;
    this.#maxResponseBytes = options.maxResponseBytes ?? DEFAULT_MAX_RESPONSE_BYTES;
  }

  async status(): Promise<ProductSwarmStatus> {
    return this.#request('/api/v1/swarm/status', { method: 'GET' }, decodeProductSwarmStatus);
  }

  async createInvite(capability: Capability, expiresInSeconds: number): Promise<SwarmInviteResponse> {
    const bootstrap = await this.#session();
    return this.#mutate(
      bootstrap.api.swarm_invites,
      buildSwarmInviteRequest(capability, expiresInSeconds),
      decodeSwarmInviteResponse,
      bootstrap,
    );
  }

  async join(inviteCode: string, displayName: string): Promise<SwarmJoinResponse> {
    const bootstrap = await this.#session();
    return this.#mutate(
      bootstrap.api.swarm_join,
      buildSwarmJoinRequest(inviteCode, displayName),
      decodeSwarmJoinResponse,
      bootstrap,
    );
  }

  async leave(memberId: string): Promise<SwarmLeaveResponse> {
    const bootstrap = await this.#session();
    return this.#mutate(
      bootstrap.api.swarm_leave,
      buildSwarmLeaveRequest(memberId),
      decodeSwarmLeaveResponse,
      bootstrap,
    );
  }

  async #session(): Promise<ProductBootstrap> {
    if (this.#bootstrap !== null) {
      if (this.#bootstrap.session.expires_at_unix_ms <= this.#now()) {
        this.#bootstrap = null;
        throw new SwarmClientError('session_expired', 401, false);
      }
      return this.#bootstrap;
    }
    this.#bootstrapPromise ??= this.#request(
      BOOTSTRAP_PATH,
      { method: 'GET' },
      decodeProductBootstrap,
    );
    try {
      const value = await this.#bootstrapPromise;
      if (value.session.expires_at_unix_ms <= this.#now()) {
        throw new SwarmClientError('session_expired', 401, false);
      }
      this.#bootstrap = value;
      return value;
    } finally {
      this.#bootstrapPromise = null;
    }
  }

  async #mutate<T>(
    path: string,
    body: unknown,
    decode: (value: unknown) => T,
    bootstrap: ProductBootstrap,
  ): Promise<T> {
    const headers = new Headers({
      accept: 'application/json',
      'content-type': 'application/json',
    });
    headers.set(bootstrap.session.csrf_header, bootstrap.session.csrf_token);
    return this.#request(
      path,
      { method: 'POST', headers, body: JSON.stringify(body) },
      decode,
    );
  }

  async #request<T>(path: string, init: RequestInit, decode: (value: unknown) => T): Promise<T> {
    let response: Response;
    try {
      response = await this.#fetcher(requireSameOriginPath(path), {
        ...init,
        credentials: 'same-origin',
        cache: 'no-store',
        referrerPolicy: 'no-referrer',
        redirect: 'error',
        headers: init.headers ?? { accept: 'application/json' },
      });
    } catch (error) {
      if (error instanceof SwarmClientError) throw error;
      throw new SwarmClientError('network_unavailable', null, true);
    }

    const declaredLength = Number(response.headers.get('content-length'));
    if (Number.isFinite(declaredLength) && declaredLength > this.#maxResponseBytes) {
      throw new SwarmClientError('response_too_large', response.status);
    }
    const contentType = response.headers.get('content-type')?.split(';', 1)[0].trim();
    if (contentType !== 'application/json') {
      throw new SwarmClientError('invalid_content_type', response.status);
    }
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > this.#maxResponseBytes) {
      throw new SwarmClientError('response_too_large', response.status);
    }
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      throw new SwarmClientError('invalid_json', response.status);
    }
    if (!response.ok) {
      try {
        const publicError = decodeProductError(value);
        throw new SwarmClientError(publicError.code, response.status, publicError.retryable);
      } catch (error) {
        if (error instanceof SwarmClientError) throw error;
        throw new SwarmClientError('request_failed', response.status, response.status >= 500);
      }
    }
    try {
      return decode(value);
    } catch {
      throw new SwarmClientError('invalid_product_contract', response.status);
    }
  }
}
