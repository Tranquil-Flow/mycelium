import { deepFreeze } from '../model/runtime';
import {
  DEFAULT_OBSERVATORY_EVENTS_URL,
  DEFAULT_OBSERVATORY_SNAPSHOT_URL,
  browserEventStreamFactory,
  browserFetch,
  type CancelSchedule,
  type LiveEventStream,
  type LiveEventStreamFactory,
  type LiveFetchInit,
  type Schedule,
  type ScheduleHandle,
} from './observatorySource';
import {
  decodeObservatoryAdapterEvent,
  parseObservatoryAdapterEventHeader,
  type ObservatoryAdapterBundle,
  type ObservatoryAdapterEvent,
} from './observatoryEventProjection';

export interface LiveObservatoryEventState {
  readonly source_mode: 'live';
  readonly status: 'connecting' | 'connected' | 'disconnected';
  readonly generation: number;
  readonly source_cursor: number;
  readonly projection: ObservatoryAdapterBundle;
  readonly route_ready: false;
  readonly freshness: 'current' | 'stale';
  readonly reason?: string;
}

export type ObservatoryEventListener = (state: LiveObservatoryEventState) => void;

export interface ObservatoryEventFetchResponse {
  readonly ok: boolean;
  readonly status: number;
  readonly headers: Pick<Headers, 'get'>;
  readonly body: ReadableStream<Uint8Array> | null;
}

export type ObservatoryEventFetch = (
  url: string,
  init: LiveFetchInit,
) => Promise<ObservatoryEventFetchResponse>;

export interface LiveObservatoryEventSourceOptions {
  readonly snapshotUrl?: string;
  readonly eventsUrl?: string;
  readonly fetcher?: ObservatoryEventFetch;
  readonly eventStreamFactory?: LiveEventStreamFactory;
  readonly now?: () => number;
  readonly schedule?: Schedule;
  readonly cancelSchedule?: CancelSchedule;
  readonly maxEventBytes?: number;
  readonly maxEventAgeMs?: number;
}

const browserSchedule: Schedule = (callback, delayMs) => setTimeout(callback, delayMs);
const browserCancelSchedule: CancelSchedule = (handle) => clearTimeout(handle);

function positiveInteger(value: unknown, name: string, maximum?: number): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < 1 ||
    (maximum !== undefined && (value as number) > maximum)
  ) {
    throw new TypeError(
      maximum === undefined
        ? `${name} must be a positive safe integer`
        : `${name} must be within the positive bound ${maximum}`,
    );
  }
  return value as number;
}

function sameOriginUrl(value: string, name: string): string {
  if (
    typeof value !== 'string' ||
    value.trim().length === 0 ||
    !value.startsWith('/') ||
    value.startsWith('//')
  ) {
    throw new TypeError(`${name} must be a safe root-relative same-origin URL`);
  }
  const base = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
  let candidate: URL;
  try {
    candidate = new URL(value, base);
  } catch {
    throw new TypeError(`${name} must be a valid same-origin URL`);
  }
  if (
    candidate.origin !== base ||
    !['http:', 'https:'].includes(candidate.protocol) ||
    candidate.username !== '' ||
    candidate.password !== '' ||
    candidate.search !== '' ||
    candidate.hash !== ''
  ) {
    throw new TypeError(`${name} must be a safe same-origin URL without private components`);
  }
  return candidate.pathname;
}

function candidateGeneration(value: unknown): number | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, 'generation');
    if (descriptor === undefined || !('value' in descriptor)) return null;
    const generation = descriptor.value as unknown;
    return Number.isSafeInteger(generation) && (generation as number) >= 1
      ? (generation as number)
      : null;
  } catch {
    return null;
  }
}

async function readBoundedJson(
  response: ObservatoryEventFetchResponse,
  maximumBytes: number,
): Promise<unknown> {
  const declaredLength = response.headers.get('content-length');
  if (declaredLength !== null) {
    if (!/^(?:0|[1-9][0-9]*)$/.test(declaredLength)) {
      throw new Error('Invalid Observatory snapshot content length');
    }
    const length = Number(declaredLength);
    if (!Number.isSafeInteger(length) || length > maximumBytes) {
      throw new Error('Oversized Observatory snapshot');
    }
  }
  if (response.body === null) {
    throw new Error('Observatory snapshot body unavailable');
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      if (next.value === undefined) {
        throw new Error('Invalid Observatory snapshot body');
      }
      totalBytes += next.value.byteLength;
      if (totalBytes > maximumBytes) {
        await reader.cancel().catch(() => undefined);
        throw new Error('Oversized Observatory snapshot');
      }
      chunks.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw new Error('Invalid UTF-8 in Observatory snapshot');
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new Error('Invalid Observatory snapshot JSON');
  }
}

export class LiveObservatoryEventSource {
  readonly source_mode = 'live' as const;
  readonly snapshotUrl: string;
  readonly eventsUrl: string;
  readonly subscribe = (listener: ObservatoryEventListener): (() => void) =>
    this.addListener(listener);

  private readonly fetcher: ObservatoryEventFetch;
  private readonly eventStreamFactory: LiveEventStreamFactory;
  private readonly now: () => number;
  private readonly schedule: Schedule;
  private readonly cancelSchedule: CancelSchedule;
  private readonly maxEventBytes: number;
  private readonly maxEventAgeMs: number;
  private readonly listeners = new Set<ObservatoryEventListener>();
  private state: LiveObservatoryEventState | null = null;
  private initialPromise: Promise<void> | null = null;
  private stream: LiveEventStream | null = null;
  private streamOpen = false;
  private highestSeenGeneration = -1;
  private blockedGeneration: number | null = null;
  private freshnessTimer: ScheduleHandle | null = null;

  constructor(options: LiveObservatoryEventSourceOptions = {}) {
    this.snapshotUrl = sameOriginUrl(
      options.snapshotUrl ?? DEFAULT_OBSERVATORY_SNAPSHOT_URL,
      'snapshotUrl',
    );
    this.eventsUrl = sameOriginUrl(
      options.eventsUrl ?? DEFAULT_OBSERVATORY_EVENTS_URL,
      'eventsUrl',
    );
    this.fetcher =
      options.fetcher ?? (browserFetch as unknown as ObservatoryEventFetch);
    this.eventStreamFactory = options.eventStreamFactory ?? browserEventStreamFactory;
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? browserSchedule;
    this.cancelSchedule = options.cancelSchedule ?? browserCancelSchedule;
    this.maxEventBytes = positiveInteger(
      options.maxEventBytes ?? 512 * 1024,
      'maxEventBytes',
      2 * 1024 * 1024,
    );
    this.maxEventAgeMs = positiveInteger(options.maxEventAgeMs ?? 300_000, 'maxEventAgeMs');
  }

  loadInitial(): Promise<LiveObservatoryEventState> {
    this.initialPromise ??= this.fetcher(this.snapshotUrl, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'same-origin',
    }).then(async (response) => {
      if (!response.ok) {
        throw new Error(`Observatory event snapshot request failed with status ${response.status}`);
      }
      this.processPayload(await readBoundedJson(response, this.maxEventBytes), null, 'snapshot');
    });
    return this.initialPromise.then(() => {
      if (this.state === null) {
        throw new Error('Observatory event snapshot did not produce valid read-only state');
      }
      return this.state;
    });
  }

  getState(): LiveObservatoryEventState | null {
    return this.state;
  }

  private addListener(listener: ObservatoryEventListener): () => void {
    this.listeners.add(listener);
    if (this.stream === null) this.openEventStream();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.closeEventStream();
    };
  }

  private openEventStream(): void {
    let stream: LiveEventStream;
    try {
      stream = this.eventStreamFactory(this.eventsUrl);
    } catch {
      this.markDisconnected('Observatory event stream unavailable', this.highestSeenGeneration);
      return;
    }
    stream.onopen = () => {
      this.streamOpen = true;
      this.refreshTransportState();
    };
    stream.onerror = () => {
      this.streamOpen = false;
      this.markDisconnected('Observatory event stream disconnected', this.highestSeenGeneration);
    };
    stream.addEventListener('snapshot', this.onSnapshotEvent);
    this.stream = stream;
    this.streamOpen = false;
    if (this.state !== null) {
      this.state = this.buildState(
        this.state.generation,
        this.state.source_cursor,
        this.state.projection,
        'connecting',
        'Observatory event stream connecting',
      );
      this.notify();
    }
  }

  private closeEventStream(): void {
    if (this.stream === null) return;
    this.stream.removeEventListener('snapshot', this.onSnapshotEvent);
    this.stream.onopen = null;
    this.stream.onerror = null;
    this.stream.close();
    this.stream = null;
    this.streamOpen = false;
  }

  private readonly onSnapshotEvent = (message: MessageEvent<string>): void => {
    if (
      typeof message.data !== 'string' ||
      new TextEncoder().encode(message.data).byteLength > this.maxEventBytes
    ) {
      this.markDisconnected('Oversized Observatory event', this.highestSeenGeneration);
      return;
    }
    let payload: unknown;
    try {
      payload = JSON.parse(message.data);
    } catch {
      this.markDisconnected('Invalid Observatory event JSON', this.highestSeenGeneration);
      return;
    }
    this.processPayload(payload, message.lastEventId || null, 'event');
  };

  private processPayload(
    payload: unknown,
    lastEventId: string | null,
    origin: 'snapshot' | 'event',
  ): void {
    const candidate = candidateGeneration(payload);
    if (candidate !== null && candidate <= this.highestSeenGeneration) return;

    let generation: number;
    try {
      generation = parseObservatoryAdapterEventHeader(payload).generation;
    } catch {
      if (candidate !== null) this.highestSeenGeneration = Math.max(this.highestSeenGeneration, candidate);
      this.markDisconnected('Invalid Observatory event protocol', candidate);
      return;
    }
    if (generation <= this.highestSeenGeneration) return;
    this.highestSeenGeneration = generation;

    if (origin === 'event' && lastEventId !== String(generation)) {
      this.markDisconnected('Observatory event id/generation mismatch', generation);
      return;
    }

    let event: ObservatoryAdapterEvent;
    try {
      event = decodeObservatoryAdapterEvent(payload);
    } catch {
      this.markDisconnected('Invalid Observatory adapter event', generation);
      return;
    }
    if (this.state !== null && event.bundle.snapshot.source_cursor < this.state.source_cursor) {
      this.markDisconnected('Stale Observatory source cursor', generation);
      return;
    }
    if (
      this.state !== null &&
      event.bundle.snapshot.observed_at_unix_ms <
        this.state.projection.snapshot.observed_at_unix_ms
    ) {
      this.markDisconnected('Observatory observation time rollback', generation);
      return;
    }
    this.acceptEvent(event);
  }

  private acceptEvent(event: ObservatoryAdapterEvent): void {
    if (this.blockedGeneration !== null && event.generation <= this.blockedGeneration) return;
    if (this.blockedGeneration !== null) this.blockedGeneration = null;
    const status = this.streamOpen ? 'connected' : 'connecting';
    this.state = this.buildState(
      event.generation,
      event.bundle.snapshot.source_cursor,
      event.bundle,
      status,
    );
    this.armFreshnessTimer();
    this.notify();
  }

  private buildState(
    generation: number,
    sourceCursor: number,
    projection: ObservatoryAdapterBundle,
    status: LiveObservatoryEventState['status'],
    reason?: string,
  ): LiveObservatoryEventState {
    const observedAt = projection.snapshot.observed_at_unix_ms;
    const currentTime = this.now();
    const stale = observedAt > currentTime || currentTime - observedAt > this.maxEventAgeMs;
    return deepFreeze({
      source_mode: 'live' as const,
      status,
      generation,
      source_cursor: sourceCursor,
      projection,
      route_ready: false as const,
      freshness: stale ? ('stale' as const) : ('current' as const),
      ...(reason === undefined ? {} : { reason }),
    });
  }

  private markDisconnected(reason: string, generation: number | null): void {
    if (generation !== null && generation >= 1) {
      this.blockedGeneration = Math.max(this.blockedGeneration ?? -1, generation);
    } else if (this.blockedGeneration === null) {
      this.blockedGeneration = this.highestSeenGeneration;
    }
    if (this.state === null) return;
    if (this.state.status === 'disconnected' && this.state.reason === reason) return;
    this.state = this.buildState(
      this.state.generation,
      this.state.source_cursor,
      this.state.projection,
      'disconnected',
      reason,
    );
    this.notify();
  }

  private refreshTransportState(): void {
    if (this.state === null) return;
    const status =
      this.streamOpen && this.blockedGeneration === null ? 'connected' : 'disconnected';
    this.state = this.buildState(
      this.state.generation,
      this.state.source_cursor,
      this.state.projection,
      status,
      status === 'disconnected'
        ? this.state.reason ?? 'Observatory event stream not current'
        : undefined,
    );
    this.notify();
  }

  private armFreshnessTimer(): void {
    if (this.freshnessTimer !== null) this.cancelSchedule(this.freshnessTimer);
    this.freshnessTimer = null;
    if (this.state === null || this.state.freshness === 'stale') return;
    const expiresAt =
      this.state.projection.snapshot.observed_at_unix_ms + this.maxEventAgeMs;
    const delay = Math.max(0, expiresAt - this.now() + 1);
    this.freshnessTimer = this.schedule(() => {
      this.freshnessTimer = null;
      if (this.state === null) return;
      this.state = this.buildState(
        this.state.generation,
        this.state.source_cursor,
        this.state.projection,
        this.state.status,
        this.state.reason,
      );
      this.notify();
      this.armFreshnessTimer();
    }, Math.min(delay, 2_147_483_647));
  }

  private notify(): void {
    if (this.state === null) return;
    for (const listener of this.listeners) {
      try {
        listener(this.state);
      } catch {
        // Read consumers cannot perturb source transport state.
      }
    }
  }
}
