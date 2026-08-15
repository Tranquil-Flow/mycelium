import {
  PRODUCT_API_PATHS,
  ProductContractError,
  decodeInferenceAccepted,
  decodeInferenceCancelResponse,
  decodeInferenceEvent,
  decodeProductBootstrap,
  decodeProductError,
  decodeProductQualification,
  type InferenceAcceptedResponse,
  type InferenceCancelResponse,
  type InferenceEvent,
  type InferenceSubmission,
  type ProductBootstrap,
  type ProductQualification,
} from '../../app/contracts';

const PRODUCT_BOOTSTRAP_PATH = '/api/v1/bootstrap';
const MAX_JSON_RESPONSE_UTF8_BYTES = 1_048_576;
const MAX_SSE_EVENT_UTF8_BYTES = 300_000;
const SSE_EVENT_ID = /^(0|[1-9][0-9]{0,15})$/;

export type InferenceFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export interface InferenceClient {
  loadQualification(signal?: AbortSignal): Promise<ProductQualification>;
  submit(
    submission: InferenceSubmission,
    signal?: AbortSignal,
  ): Promise<InferenceAcceptedResponse>;
  stream(
    request: InferenceAcceptedResponse,
    lastEventId: number | null,
    onEvent: (event: InferenceEvent) => void,
    signal?: AbortSignal,
  ): Promise<void>;
  cancel(
    request: InferenceAcceptedResponse,
    signal?: AbortSignal,
  ): Promise<InferenceCancelResponse>;
}

export class InferenceClientError extends Error {
  constructor(
    readonly code: string,
    readonly retryable: boolean,
    readonly status: number | null = null,
  ) {
    super(code);
    this.name = 'InferenceClientError';
  }
}

export interface ProductInferenceClientOptions {
  readonly fetch?: InferenceFetch;
  readonly now?: () => number;
}

function clientError(
  code: string,
  retryable: boolean,
  status: number | null = null,
): InferenceClientError {
  return new InferenceClientError(code, retryable, status);
}

function isAbort(error: unknown, signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true ||
    (error instanceof DOMException && error.name === 'AbortError');
}

function exactSameOriginPath(path: string, expected: string): string {
  if (path !== expected || !path.startsWith('/api/v1/') || path.includes('://')) {
    throw clientError('invalid_same_origin_path', false);
  }
  return path;
}

function safeResponseOrigin(response: Response): void {
  if (response.redirected) throw clientError('cross_origin_redirect_blocked', false);
  if (response.url === '') return;
  const currentOrigin = globalThis.location?.origin;
  if (currentOrigin === undefined) return;
  let responseOrigin: string;
  try {
    responseOrigin = new URL(response.url).origin;
  } catch {
    throw clientError('invalid_response_origin', false);
  }
  if (responseOrigin !== currentOrigin) {
    throw clientError('cross_origin_redirect_blocked', false);
  }
}

async function responseText(response: Response): Promise<string> {
  const contentLength = response.headers.get('Content-Length');
  if (contentLength !== null) {
    const declared = Number(contentLength);
    if (!Number.isSafeInteger(declared) || declared < 0 || declared > MAX_JSON_RESPONSE_UTF8_BYTES) {
      throw clientError('response_too_large', false, response.status);
    }
  }
  const text = await response.text();
  if (new TextEncoder().encode(text).byteLength > MAX_JSON_RESPONSE_UTF8_BYTES) {
    throw clientError('response_too_large', false, response.status);
  }
  return text;
}

async function responseJson(response: Response): Promise<unknown> {
  safeResponseOrigin(response);
  const contentType = response.headers.get('Content-Type')?.toLowerCase() ?? '';
  if (!contentType.startsWith('application/json')) {
    throw clientError('invalid_response_content_type', false, response.status);
  }
  const text = await responseText(response);
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw clientError('invalid_json_response', false, response.status);
  }
}

async function throwResponseError(response: Response): Promise<never> {
  try {
    const decoded = decodeProductError(await responseJson(response));
    throw clientError(decoded.code, decoded.retryable, response.status);
  } catch (error) {
    if (error instanceof InferenceClientError) throw error;
    throw clientError('request_failed', response.status >= 500, response.status);
  }
}

async function fetchSafe(
  fetchImplementation: InferenceFetch,
  path: string,
  init: RequestInit,
  signal?: AbortSignal,
): Promise<Response> {
  try {
    const response = await fetchImplementation(path, {
      ...init,
      referrerPolicy: 'no-referrer',
      signal,
    });
    safeResponseOrigin(response);
    return response;
  } catch (error) {
    if (error instanceof InferenceClientError) throw error;
    if (isAbort(error, signal)) throw clientError('request_aborted', false);
    throw clientError('network_unavailable', true);
  }
}

interface SseFrame {
  id: string | null;
  event: string | null;
  data: string[];
}

function emptyFrame(): SseFrame {
  return { id: null, event: null, data: [] };
}

function frameHasData(frame: SseFrame): boolean {
  return frame.id !== null || frame.event !== null || frame.data.length > 0;
}

function decodeFrame(frame: SseFrame, requestId: string): InferenceEvent {
  if (frame.id === null || frame.event === null || frame.data.length === 0) {
    throw clientError('invalid_event_stream', false);
  }
  if (!SSE_EVENT_ID.test(frame.id)) throw clientError('invalid_event_stream', false);
  const sequence = Number(frame.id);
  if (!Number.isSafeInteger(sequence)) throw clientError('invalid_event_stream', false);
  let payload: unknown;
  try {
    payload = JSON.parse(frame.data.join('\n')) as unknown;
  } catch {
    throw clientError('invalid_event_stream', false);
  }
  let decoded: InferenceEvent;
  try {
    decoded = decodeInferenceEvent(payload);
  } catch {
    throw clientError('invalid_event_stream', false);
  }
  if (
    decoded.request_id !== requestId ||
    decoded.sequence !== sequence ||
    decoded.type !== frame.event
  ) {
    throw clientError('invalid_event_stream', false);
  }
  return decoded;
}

export class ProductInferenceClient implements InferenceClient {
  private readonly fetchImplementation: InferenceFetch;
  private readonly now: () => number;
  private bootstrap: ProductBootstrap | null = null;
  private bootstrapPromise: Promise<ProductBootstrap> | null = null;

  constructor(options: ProductInferenceClientOptions = {}) {
    this.fetchImplementation = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.now = options.now ?? Date.now;
  }

  private async loadBootstrap(signal?: AbortSignal): Promise<ProductBootstrap> {
    if (this.bootstrap !== null) return this.bootstrap;
    if (this.bootstrapPromise !== null) return this.bootstrapPromise;
    this.bootstrapPromise = (async () => {
      const response = await fetchSafe(
        this.fetchImplementation,
        exactSameOriginPath(PRODUCT_BOOTSTRAP_PATH, PRODUCT_BOOTSTRAP_PATH),
        {
          method: 'GET',
          credentials: 'same-origin',
          cache: 'no-store',
          redirect: 'error',
          headers: { Accept: 'application/json' },
        },
        signal,
      );
      if (!response.ok) return throwResponseError(response);
      try {
        const decoded = decodeProductBootstrap(await responseJson(response));
        this.bootstrap = decoded;
        return decoded;
      } catch (error) {
        if (error instanceof InferenceClientError) throw error;
        throw clientError('invalid_bootstrap_contract', false, response.status);
      }
    })();
    try {
      return await this.bootstrapPromise;
    } finally {
      this.bootstrapPromise = null;
    }
  }

  async loadQualification(signal?: AbortSignal): Promise<ProductQualification> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const bootstrap = await this.loadBootstrap(signal);
      const path = exactSameOriginPath(
        bootstrap.api.qualification_current,
        PRODUCT_API_PATHS.qualification_current,
      );
      const response = await fetchSafe(
        this.fetchImplementation,
        path,
        {
          method: 'GET',
          credentials: 'same-origin',
          cache: 'no-store',
          redirect: 'error',
          headers: { Accept: 'application/json' },
        },
        signal,
      );
      if (response.status === 401 && attempt === 0) {
        this.bootstrap = null;
        continue;
      }
      if (!response.ok) return throwResponseError(response);
      try {
        return decodeProductQualification(await responseJson(response));
      } catch (error) {
        if (error instanceof InferenceClientError) throw error;
        throw clientError('invalid_qualification_contract', false, response.status);
      }
    }
    throw clientError('request_failed', true, 401);
  }

  private async mutationHeaders(signal?: AbortSignal): Promise<Record<string, string>> {
    const bootstrap = await this.loadBootstrap(signal);
    if (this.now() >= bootstrap.session.expires_at_unix_ms) {
      throw clientError('product_session_expired', false);
    }
    return {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      [bootstrap.session.csrf_header]: bootstrap.session.csrf_token,
    };
  }

  async submit(
    submission: InferenceSubmission,
    signal?: AbortSignal,
  ): Promise<InferenceAcceptedResponse> {
    const bootstrap = await this.loadBootstrap(signal);
    const path = exactSameOriginPath(
      bootstrap.api.inference_submit,
      PRODUCT_API_PATHS.inference_submit,
    );
    const response = await fetchSafe(
      this.fetchImplementation,
      path,
      {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: await this.mutationHeaders(signal),
        body: JSON.stringify(submission),
      },
      signal,
    );
    if (!response.ok) return throwResponseError(response);
    try {
      return decodeInferenceAccepted(await responseJson(response));
    } catch (error) {
      if (error instanceof InferenceClientError) throw error;
      throw clientError('invalid_acceptance_contract', false, response.status);
    }
  }

  async stream(
    request: InferenceAcceptedResponse,
    lastEventId: number | null,
    onEvent: (event: InferenceEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    let accepted: InferenceAcceptedResponse;
    try {
      accepted = decodeInferenceAccepted(request);
    } catch {
      throw clientError('invalid_acceptance_contract', false);
    }
    if (
      lastEventId !== null &&
      (!Number.isSafeInteger(lastEventId) || lastEventId < 0)
    ) {
      throw clientError('invalid_resume_cursor', false);
    }
    const headers: Record<string, string> = { Accept: 'text/event-stream' };
    if (lastEventId !== null) headers['Last-Event-ID'] = String(lastEventId);
    const response = await fetchSafe(
      this.fetchImplementation,
      accepted.event_path,
      {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers,
      },
      signal,
    );
    if (!response.ok) return throwResponseError(response);
    const contentType = response.headers.get('Content-Type')?.toLowerCase() ?? '';
    if (!contentType.startsWith('text/event-stream')) {
      throw clientError('invalid_event_stream', false, response.status);
    }
    if (response.body === null) throw clientError('invalid_event_stream', false, response.status);

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', { fatal: true });
    const encoder = new TextEncoder();
    let pending = '';
    let frame = emptyFrame();
    let frameBytes = 0;

    const dispatch = (): void => {
      if (!frameHasData(frame)) return;
      const decoded = decodeFrame(frame, accepted.request_id);
      frame = emptyFrame();
      frameBytes = 0;
      onEvent(decoded);
    };

    const processLine = (rawLine: string): void => {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
      frameBytes += encoder.encode(rawLine).byteLength + 1;
      if (frameBytes > MAX_SSE_EVENT_UTF8_BYTES) {
        throw clientError('invalid_event_stream', false);
      }
      if (line === '') {
        dispatch();
        return;
      }
      if (line.startsWith(':')) return;
      const separator = line.indexOf(':');
      const field = separator === -1 ? line : line.slice(0, separator);
      let value = separator === -1 ? '' : line.slice(separator + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'data') {
        frame.data.push(value);
      } else if (field === 'id') {
        if (frame.id !== null) throw clientError('invalid_event_stream', false);
        frame.id = value;
      } else if (field === 'event') {
        if (frame.event !== null) throw clientError('invalid_event_stream', false);
        frame.event = value;
      } else {
        throw clientError('invalid_event_stream', false);
      }
    };

    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        try {
          pending += decoder.decode(chunk.value, { stream: true });
        } catch {
          throw clientError('invalid_event_stream', false);
        }
        if (encoder.encode(pending).byteLength > MAX_SSE_EVENT_UTF8_BYTES) {
          throw clientError('invalid_event_stream', false);
        }
        let newline = pending.indexOf('\n');
        while (newline !== -1) {
          processLine(pending.slice(0, newline));
          pending = pending.slice(newline + 1);
          newline = pending.indexOf('\n');
        }
      }
      try {
        pending += decoder.decode();
      } catch {
        throw clientError('invalid_event_stream', false);
      }
      if (pending !== '') processLine(pending);
      dispatch();
    } catch (error) {
      if (error instanceof InferenceClientError) throw error;
      if (isAbort(error, signal)) throw clientError('request_aborted', false);
      if (error instanceof ProductContractError) {
        throw clientError('invalid_event_stream', false);
      }
      throw error;
    } finally {
      reader.releaseLock();
    }
  }

  async cancel(
    request: InferenceAcceptedResponse,
    signal?: AbortSignal,
  ): Promise<InferenceCancelResponse> {
    let accepted: InferenceAcceptedResponse;
    try {
      accepted = decodeInferenceAccepted(request);
    } catch {
      throw clientError('invalid_acceptance_contract', false);
    }
    const response = await fetchSafe(
      this.fetchImplementation,
      accepted.cancel_path,
      {
        method: 'DELETE',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: await this.mutationHeaders(signal),
      },
      signal,
    );
    if (!response.ok) return throwResponseError(response);
    try {
      const decoded = decodeInferenceCancelResponse(await responseJson(response));
      if (decoded.request_id !== accepted.request_id) {
        throw clientError('invalid_cancellation_contract', false, response.status);
      }
      return decoded;
    } catch (error) {
      if (error instanceof InferenceClientError) throw error;
      throw clientError('invalid_cancellation_contract', false, response.status);
    }
  }
}
