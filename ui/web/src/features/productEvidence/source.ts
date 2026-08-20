import { PRODUCT_API_PATHS } from '../../app/contracts';
import {
  decodeProductSnapshot,
  decodeProductSnapshotEvent,
  type ProductSnapshot,
  type ProductSnapshotMode,
} from './contracts';

export interface ProductEvidenceState {
  readonly status: 'connecting' | 'connected' | 'disconnected';
  readonly source_mode: ProductSnapshotMode;
  readonly freshness: 'current' | 'stale' | 'replay' | 'degraded';
  readonly generation: number;
  readonly cursor: number;
  readonly snapshot: ProductSnapshot;
  readonly reason_code: string | null;
}

export interface ProductEvidenceFetchResponse {
  readonly ok: boolean;
  readonly status: number;
  readonly headers: Pick<Headers, 'get'>;
  readonly body: ReadableStream<Uint8Array> | null;
}

export type ProductEvidenceFetch = (
  url: string,
  init: {
    readonly method: 'GET';
    readonly headers: Readonly<Record<string, string>>;
    readonly cache: 'no-store';
    readonly credentials: 'same-origin';
  },
) => Promise<ProductEvidenceFetchResponse>;

export interface ProductEvidenceSourceOptions {
  readonly snapshotUrl?: string;
  readonly eventsUrl?: string;
  readonly fetcher?: ProductEvidenceFetch;
  readonly now?: () => number;
  readonly maxPayloadBytes?: number;
  readonly staleAfterMs?: number;
  readonly pollIntervalMs?: number;
  readonly schedule?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout> | number;
  readonly cancelSchedule?: (handle: ReturnType<typeof setTimeout> | number) => void;
}

const browserFetch: ProductEvidenceFetch = (url, init) => fetch(url, init);
const browserSchedule = (callback: () => void, delayMs: number) => setTimeout(callback, delayMs);
const browserCancelSchedule = (handle: ReturnType<typeof setTimeout> | number) => clearTimeout(handle);

function sameOriginPath(value: string, field: string): string {
  if (!value.startsWith('/') || value.startsWith('//')) throw new TypeError(`${field}_invalid`);
  const base = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
  const parsed = new URL(value, base);
  if (
    parsed.origin !== base
    || parsed.username !== ''
    || parsed.password !== ''
    || parsed.search !== ''
    || parsed.hash !== ''
  ) throw new TypeError(`${field}_invalid`);
  return parsed.pathname;
}

function boundedPositiveInteger(value: number, field: string, minimum: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new TypeError(`${field}_invalid`);
  }
  return value;
}

async function readBoundedBytes(
  response: ProductEvidenceFetchResponse,
  maximumBytes: number,
): Promise<Uint8Array> {
  const declared = response.headers.get('content-length');
  if (declared !== null) {
    if (!/^(?:0|[1-9][0-9]*)$/.test(declared) || Number(declared) > maximumBytes) {
      throw new Error('product_payload_size_invalid');
    }
  }
  if (response.body === null) throw new Error('product_payload_body_missing');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      if (next.value === undefined) throw new Error('product_payload_body_invalid');
      size += next.value.byteLength;
      if (size > maximumBytes) {
        await reader.cancel().catch(() => undefined);
        throw new Error('product_payload_size_invalid');
      }
      chunks.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return joined;
}

async function readBoundedJson(
  response: ProductEvidenceFetchResponse,
  maximumBytes: number,
): Promise<unknown> {
  try {
    return JSON.parse(
      new TextDecoder('utf-8', { fatal: true }).decode(
        await readBoundedBytes(response, maximumBytes),
      ),
    ) as unknown;
  } catch {
    throw new Error('product_snapshot_json_invalid');
  }
}

function decodeEventFrames(raw: Uint8Array, maximumFrameBytes: number): readonly ProductSnapshot[] {
  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(raw);
  } catch {
    throw new Error('product_event_utf8_invalid');
  }
  if (text.length === 0) return [];
  if (!text.endsWith('\n\n')) throw new Error('product_event_frame_invalid');
  const frames = text.slice(0, -2).split('\n\n');
  return frames.map((frame) => {
    if (new TextEncoder().encode(frame).byteLength > maximumFrameBytes) {
      throw new Error('product_event_size_invalid');
    }
    const lines = frame.split('\n');
    if (
      lines.length !== 3
      || !/^id: [1-9][0-9]*$/.test(lines[0])
      || lines[1] !== 'event: product_snapshot'
      || !lines[2].startsWith('data: ')
    ) throw new Error('product_event_frame_invalid');
    const event = decodeProductSnapshotEvent(JSON.parse(lines[2].slice(6)) as unknown);
    if (String(event.cursor) !== lines[0].slice(4)) {
      throw new Error('product_event_id_mismatch');
    }
    return event.snapshot;
  });
}

function freshness(snapshot: ProductSnapshot, stale: boolean): ProductEvidenceState['freshness'] {
  if (snapshot.publication.source_mode === 'replay') return 'replay';
  if (snapshot.publication.source_mode === 'degraded') return 'degraded';
  return stale ? 'stale' : 'current';
}

export class LiveProductEvidenceSource {
  readonly snapshotUrl: string;
  readonly eventsUrl: string;

  private readonly fetcher: ProductEvidenceFetch;
  private readonly now: () => number;
  private readonly maxPayloadBytes: number;
  private readonly staleAfterMs: number;
  private readonly pollIntervalMs: number;
  private readonly schedule: NonNullable<ProductEvidenceSourceOptions['schedule']>;
  private readonly cancelSchedule: NonNullable<ProductEvidenceSourceOptions['cancelSchedule']>;
  private readonly listeners = new Set<(state: ProductEvidenceState) => void>();
  private state: ProductEvidenceState | null = null;
  private initialPromise: Promise<ProductEvidenceState> | null = null;
  private pollHandle: ReturnType<typeof setTimeout> | number | null = null;
  private pollActive = false;

  constructor(options: ProductEvidenceSourceOptions = {}) {
    this.snapshotUrl = sameOriginPath(options.snapshotUrl ?? PRODUCT_API_PATHS.product_snapshot, 'product_snapshot_url');
    this.eventsUrl = sameOriginPath(options.eventsUrl ?? PRODUCT_API_PATHS.product_events, 'product_events_url');
    this.fetcher = options.fetcher ?? browserFetch;
    this.now = options.now ?? Date.now;
    this.maxPayloadBytes = boundedPositiveInteger(options.maxPayloadBytes ?? 8 * 1024 * 1024, 'product_payload_limit', 1, 16 * 1024 * 1024);
    this.staleAfterMs = boundedPositiveInteger(options.staleAfterMs ?? 300_000, 'product_stale_after', 1, 86_400_000);
    this.pollIntervalMs = boundedPositiveInteger(options.pollIntervalMs ?? 2_000, 'product_poll_interval', 250, 60_000);
    this.schedule = options.schedule ?? browserSchedule;
    this.cancelSchedule = options.cancelSchedule ?? browserCancelSchedule;
  }

  getState(): ProductEvidenceState | null {
    return this.state;
  }

  loadInitial(): Promise<ProductEvidenceState> {
    this.initialPromise ??= this.fetchSnapshot();
    return this.initialPromise;
  }

  subscribe(listener: (state: ProductEvidenceState) => void): () => void {
    this.listeners.add(listener);
    if (this.state !== null) listener(this.state);
    void this.loadInitial().then(() => this.schedulePoll(0)).catch(() => {
      this.disconnect('product_snapshot_unavailable');
    });
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0 && this.pollHandle !== null) {
        this.cancelSchedule(this.pollHandle);
        this.pollHandle = null;
      }
    };
  }

  private async fetchSnapshot(connected = false): Promise<ProductEvidenceState> {
    const response = await this.fetcher(this.snapshotUrl, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error(`product_snapshot_http_${response.status}`);
    const snapshot = decodeProductSnapshot(await readBoundedJson(response, this.maxPayloadBytes));
    this.accept(snapshot, 'snapshot', connected);
    if (this.state === null) throw new Error('product_snapshot_not_accepted');
    return this.state;
  }

  private schedulePoll(delayMs: number): void {
    if (this.listeners.size === 0 || this.pollHandle !== null || this.pollActive) return;
    this.pollHandle = this.schedule(() => {
      this.pollHandle = null;
      void this.poll();
    }, delayMs);
  }

  private async poll(): Promise<void> {
    if (this.pollActive || this.listeners.size === 0) return;
    this.pollActive = true;
    try {
      const cursor = this.state?.cursor ?? null;
      const headers: Record<string, string> = { Accept: 'text/event-stream' };
      if (cursor !== null) headers['Last-Event-ID'] = String(cursor);
      const response = await this.fetcher(this.eventsUrl, {
        method: 'GET',
        headers,
        cache: 'no-store',
        credentials: 'same-origin',
      });
      if (response.status === 409) {
        await this.fetchSnapshot(true);
      } else {
        if (!response.ok) throw new Error(`product_events_http_${response.status}`);
        const contentType = response.headers.get('content-type');
        if (contentType?.split(';', 1)[0].trim().toLowerCase() !== 'text/event-stream') {
          throw new Error('product_events_media_type_invalid');
        }
        const snapshots = decodeEventFrames(
          await readBoundedBytes(response, this.maxPayloadBytes),
          this.maxPayloadBytes,
        );
        for (const snapshot of snapshots) this.accept(snapshot, 'event', true);
        if (snapshots.length === 0) this.markConnected();
      }
    } catch {
      this.disconnect('product_event_poll_failed');
    } finally {
      this.pollActive = false;
      this.schedulePoll(this.pollIntervalMs);
    }
  }

  private accept(
    snapshot: ProductSnapshot,
    source: 'snapshot' | 'event',
    connected: boolean,
  ): void {
    const current = this.state?.snapshot ?? null;
    if (current !== null && snapshot.publication.cursor <= current.publication.cursor) {
      if (
        snapshot.publication.cursor === current.publication.cursor
        && snapshot.publication.snapshot_id === current.publication.snapshot_id
      ) return;
      if (source === 'snapshot' && snapshot.publication.cursor < current.publication.cursor) return;
      this.disconnect('product_cursor_regression');
      return;
    }
    if (
      source === 'event'
      && current !== null
      && snapshot.publication.cursor !== current.publication.cursor + 1
    ) {
      // Recover from a concurrent-publisher cursor gap with one fresh snapshot
      // fetch instead of livelocking on the mismatched replay frame.
      const gapCursor = current.publication.cursor;
      void this.fetchSnapshot(true)
        .then(() => {
          const state = this.state;
          if (
            state === null
            || state.snapshot.publication.cursor <= gapCursor
            || state.status !== 'connected'
          ) {
            this.disconnect('product_cursor_gap');
          }
        })
        .catch(() => {
          this.disconnect('product_cursor_gap');
        });
      return;
    }
    const stale = this.now() - snapshot.publication.published_at_unix_ms > this.staleAfterMs;
    this.state = Object.freeze({
      status: connected ? 'connected' : 'connecting',
      source_mode: snapshot.publication.source_mode,
      freshness: freshness(snapshot, stale),
      generation: snapshot.publication.generation,
      cursor: snapshot.publication.cursor,
      snapshot,
      reason_code: stale ? 'product_snapshot_stale' : null,
    });
    this.notify();
  }

  private markConnected(): void {
    if (this.state === null) return;
    this.state = Object.freeze({
      ...this.state,
      status: 'connected',
      reason_code: this.state.freshness === 'stale' ? 'product_snapshot_stale' : null,
    });
    this.notify();
  }

  private disconnect(reason: string): void {
    if (this.state === null) return;
    this.state = Object.freeze({ ...this.state, status: 'disconnected', reason_code: reason });
    this.notify();
  }

  private notify(): void {
    if (this.state === null) return;
    for (const listener of this.listeners) listener(this.state);
  }
}

export function createLiveProductEvidenceSource(options: ProductEvidenceSourceOptions = {}): LiveProductEvidenceSource {
  return new LiveProductEvidenceSource(options);
}
