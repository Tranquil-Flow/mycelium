export type RecordedTransport = 'fetch' | 'xhr' | 'eventsource' | 'websocket' | 'beacon';

export interface RecordedProductRequest {
  readonly transport: RecordedTransport;
  readonly method: string;
  readonly url: string;
  readonly header_names: readonly string[];
  readonly body_present: boolean;
}

export class ProductNetworkPolicyError extends Error {
  constructor(readonly code: 'cross_origin_request_blocked' | 'upstream_authorization_forbidden') {
    super(code);
    this.name = 'ProductNetworkPolicyError';
  }
}

export interface ProductNetworkRecorder {
  readonly requests: readonly RecordedProductRequest[];
  restore(): void;
}

interface MutableRecord {
  readonly transport: RecordedTransport;
  readonly method: string;
  readonly url: string;
  readonly header_names: readonly string[];
  readonly body_present: boolean;
}

function normalizedHeaderNames(headers: HeadersInit | undefined): readonly string[] {
  if (headers === undefined) return Object.freeze([]);
  const names = [...new Headers(headers).keys()].map((name) => name.toLowerCase()).sort();
  return Object.freeze(names);
}

function assertAllowedUrl(rawUrl: string, origin: URL, transport: RecordedTransport): URL {
  const target = new URL(rawUrl, origin);
  const isWebSocket = transport === 'websocket';
  const allowedProtocol = isWebSocket
    ? target.protocol === (origin.protocol === 'https:' ? 'wss:' : 'ws:')
    : target.protocol === origin.protocol;
  if (
    !allowedProtocol ||
    target.hostname !== origin.hostname ||
    target.port !== origin.port ||
    target.username !== '' ||
    target.password !== ''
  ) {
    throw new ProductNetworkPolicyError('cross_origin_request_blocked');
  }
  return target;
}

function assertNoAuthorization(headers: readonly string[]): void {
  if (headers.some((name) => name === 'authorization' || name === 'proxy-authorization')) {
    throw new ProductNetworkPolicyError('upstream_authorization_forbidden');
  }
}

export function installProductNetworkRecorder(
  originValue = window.location.origin,
): ProductNetworkRecorder {
  const origin = new URL(originValue);
  const records: MutableRecord[] = [];
  const globalDescriptors = new Map<string, PropertyDescriptor | undefined>();
  const navigatorBeaconDescriptor = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(navigator),
    'sendBeacon',
  );
  const navigatorOwnBeaconDescriptor = Object.getOwnPropertyDescriptor(navigator, 'sendBeacon');

  const replaceGlobal = (name: string, value: unknown) => {
    globalDescriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value,
    });
  };

  const capture = (
    transport: RecordedTransport,
    rawUrl: string,
    method: string,
    headers: HeadersInit | undefined,
    bodyPresent: boolean,
  ) => {
    const target = assertAllowedUrl(rawUrl, origin, transport);
    const headerNames = normalizedHeaderNames(headers);
    assertNoAuthorization(headerNames);
    records.push(
      Object.freeze({
        transport,
        method: method.toUpperCase(),
        url: `${target.origin}${target.pathname}`,
        header_names: headerNames,
        body_present: bodyPresent,
      }),
    );
  };

  const recordingFetch: typeof fetch = async (input, init) => {
    const request = input instanceof Request ? input : null;
    const rawUrl = request?.url ?? String(input);
    const headers = init?.headers ?? request?.headers;
    capture(
      'fetch',
      rawUrl,
      init?.method ?? request?.method ?? 'GET',
      headers,
      (init?.body !== undefined && init.body !== null) ||
        (init?.body === undefined && request?.body !== null && request?.body !== undefined),
    );
    return new Response(null, { status: 204 });
  };

  class RecordingXMLHttpRequest extends EventTarget {
    static readonly UNSENT = 0;
    static readonly OPENED = 1;
    static readonly HEADERS_RECEIVED = 2;
    static readonly LOADING = 3;
    static readonly DONE = 4;
    readonly UNSENT = 0;
    readonly OPENED = 1;
    readonly HEADERS_RECEIVED = 2;
    readonly LOADING = 3;
    readonly DONE = 4;
    readyState = 0;
    status = 0;
    responseText = '';
    private requestUrl = '';
    private requestMethod = 'GET';
    private readonly headers = new Headers();

    open(
      method: string,
      url: string | URL,
      _async = true,
      username: string | null = null,
      password: string | null = null,
    ): void {
      if (username !== null || password !== null) {
        throw new ProductNetworkPolicyError('upstream_authorization_forbidden');
      }
      this.requestMethod = method;
      this.requestUrl = String(url);
      this.readyState = 1;
    }

    setRequestHeader(name: string, value: string): void {
      this.headers.append(name, value);
    }

    send(body: Document | XMLHttpRequestBodyInit | null = null): void {
      capture('xhr', this.requestUrl, this.requestMethod, this.headers, body !== null);
      this.status = 204;
      this.readyState = 4;
      this.dispatchEvent(new Event('readystatechange'));
      this.dispatchEvent(new Event('load'));
      this.dispatchEvent(new Event('loadend'));
    }

    abort(): void {
      this.readyState = 0;
    }
  }

  class RecordingEventSource extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;
    readonly CONNECTING = 0;
    readonly OPEN = 1;
    readonly CLOSED = 2;
    readonly url: string;
    readonly withCredentials = false;
    readyState = 1;

    constructor(url: string | URL, init?: EventSourceInit) {
      super();
      if (init?.withCredentials === true) {
        throw new ProductNetworkPolicyError('upstream_authorization_forbidden');
      }
      const target = assertAllowedUrl(String(url), origin, 'eventsource');
      capture('eventsource', target.toString(), 'GET', undefined, false);
      this.url = target.toString();
    }

    close(): void {
      this.readyState = 2;
    }
  }

  class RecordingWebSocket extends EventTarget {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;
    readonly CONNECTING = 0;
    readonly OPEN = 1;
    readonly CLOSING = 2;
    readonly CLOSED = 3;
    readonly url: string;
    readonly protocol = '';
    readonly extensions = '';
    readonly bufferedAmount = 0;
    readonly binaryType = 'blob';
    readyState = 1;
    private readonly recordIndex: number;

    constructor(url: string | URL) {
      super();
      const target = assertAllowedUrl(String(url), origin, 'websocket');
      this.recordIndex = records.length;
      capture('websocket', target.toString(), 'GET', undefined, false);
      this.url = target.toString();
    }

    send(): void {
      const current = records[this.recordIndex];
      if (current !== undefined && !current.body_present) {
        records[this.recordIndex] = Object.freeze({ ...current, body_present: true });
      }
    }

    close(): void {
      this.readyState = 3;
    }
  }

  replaceGlobal('fetch', recordingFetch);
  replaceGlobal('XMLHttpRequest', RecordingXMLHttpRequest);
  replaceGlobal('EventSource', RecordingEventSource);
  replaceGlobal('WebSocket', RecordingWebSocket);
  Object.defineProperty(navigator, 'sendBeacon', {
    configurable: true,
    value: (url: string | URL, data?: BodyInit | null) => {
      capture('beacon', String(url), 'POST', undefined, data !== undefined && data !== null);
      return true;
    },
  });

  let restored = false;
  return Object.freeze({
    get requests() {
      return Object.freeze([...records]);
    },
    restore() {
      if (restored) return;
      restored = true;
      for (const [name, descriptor] of globalDescriptors) {
        if (descriptor === undefined) delete (globalThis as Record<string, unknown>)[name];
        else Object.defineProperty(globalThis, name, descriptor);
      }
      if (navigatorOwnBeaconDescriptor === undefined) delete (navigator as { sendBeacon?: unknown }).sendBeacon;
      else Object.defineProperty(navigator, 'sendBeacon', navigatorOwnBeaconDescriptor);
      if (navigatorOwnBeaconDescriptor === undefined && navigatorBeaconDescriptor !== undefined) {
        // Prototype implementation remains untouched; no action needed.
      }
    },
  });
}
