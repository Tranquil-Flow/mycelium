import { describe, expect, it } from 'vitest';
import { validObservatoryAdapterEvent } from '../test/observatoryEventFixture';
import type { LiveEventStream, LiveFetchInit } from './observatorySource';
import {
  LiveObservatoryEventSource,
  type ObservatoryEventFetch,
} from './observatoryEventSource';

const CANARY = 'OBSERVATORY_UI_SOURCE_PRIVATE_CANARY';

class FakeEventStream implements LiveEventStream {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private listener: ((event: MessageEvent<string>) => void) | null = null;

  addEventListener(
    type: 'snapshot',
    listener: (event: MessageEvent<string>) => void,
  ): void {
    if (type === 'snapshot') this.listener = listener;
  }

  removeEventListener(
    type: 'snapshot',
    listener: (event: MessageEvent<string>) => void,
  ): void {
    if (type === 'snapshot' && this.listener === listener) this.listener = null;
  }

  close(): void {
    this.listener = null;
  }

  open(): void {
    this.onopen?.(new Event('open'));
  }

  fail(): void {
    this.onerror?.(new Event('error'));
  }

  emit(payload: unknown, lastEventId: string): void {
    this.listener?.(
      new MessageEvent<string>('snapshot', {
        data: JSON.stringify(payload),
        lastEventId,
      }),
    );
  }

  emitRaw(data: string, lastEventId: string): void {
    this.listener?.(new MessageEvent<string>('snapshot', { data, lastEventId }));
  }
}

function harness(options: {
  initial?: unknown;
  initialSequence?: readonly unknown[];
  initialBytes?: readonly Uint8Array[];
  contentLength?: string;
  now?: () => number;
  schedule?: (callback: () => void, delayMs: number) => number;
  maxEventBytes?: number;
} = {}) {
  const calls: Array<{ url: string; init: LiveFetchInit }> = [];
  const fetcher: ObservatoryEventFetch = async (url, init) => {
    calls.push({ url, init });
    const sequenceValue = options.initialSequence?.[
      Math.min(calls.length - 1, options.initialSequence.length - 1)
    ];
    const defaultBody = new TextEncoder().encode(
      JSON.stringify(structuredClone(sequenceValue ?? options.initial ?? validObservatoryAdapterEvent())),
    );
    const chunks = options.initialBytes ?? [defaultBody];
    return {
      ok: true,
      status: 200,
      headers: new Headers(
        options.contentLength === undefined
          ? {}
          : { 'Content-Length': options.contentLength },
      ),
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(chunk);
          controller.close();
        },
      }),
    };
  };
  const stream = new FakeEventStream();
  const source = new LiveObservatoryEventSource({
    fetcher,
    eventStreamFactory: () => stream,
    now: options.now,
    schedule: options.schedule,
    cancelSchedule: () => undefined,
    maxEventBytes: options.maxEventBytes,
  });
  return { source, stream, calls };
}

describe('live read-only Observatory event source', () => {
  it('loads only by GET, resumes SSE generation, and ignores duplicate stale payloads', async () => {
    const { source, stream, calls } = harness();
    const initial = await source.loadInitial();
    const states: unknown[] = [];
    const unsubscribe = source.subscribe((state) => states.push(state));
    stream.open();

    const next = validObservatoryAdapterEvent(2, 4);
    next.bundle.snapshot.sessions[0].state = 'failed';
    stream.emit(next, '2');
    const applied = source.getState()!;

    const duplicate = validObservatoryAdapterEvent(2, 4) as unknown as Record<string, unknown>;
    (duplicate.bundle as Record<string, unknown>).prompt = CANARY;
    stream.emit(duplicate, '2');

    expect(calls).toEqual([
      {
        url: '/v1/observatory/snapshot',
        init: {
          method: 'GET',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
          credentials: 'same-origin',
        },
      },
    ]);
    expect(initial.generation).toBe(1);
    expect(applied.status).toBe('connected');
    expect(applied.generation).toBe(2);
    expect(applied.source_cursor).toBe(4);
    expect(source.getState()).toBe(applied);
    expect(JSON.stringify(source.getState())).not.toContain(CANARY);
    expect(states.length).toBeGreaterThan(0);
    unsubscribe();
    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.route_ready).toBe(false);
  });

  it('bounds the initial GET body before decoding or retaining private data', async () => {
    const privateBytes = new TextEncoder().encode(CANARY.repeat(8));
    const byHeader = harness({
      initialBytes: [privateBytes],
      contentLength: String(privateBytes.byteLength),
      maxEventBytes: 64,
    });
    await expect(byHeader.source.loadInitial()).rejects.toThrow(/oversized/i);
    expect(byHeader.source.getState()).toBeNull();

    const byStream = harness({
      initialBytes: [privateBytes.slice(0, 40), privateBytes.slice(40)],
      maxEventBytes: 64,
    });
    await expect(byStream.source.loadInitial()).rejects.toThrow(/oversized/i);
    expect(byStream.source.getState()).toBeNull();
  });

  it('rejects malformed UTF-8 in the initial GET body', async () => {
    const { source } = harness({
      initialBytes: [new Uint8Array([0xc3, 0x28])],
    });
    await expect(source.loadInitial()).rejects.toThrow(/UTF-8/i);
    expect(source.getState()).toBeNull();
  });

  it('fails closed on stale source cursor, id mismatch, and unknown newer protocol', async () => {
    const { source, stream } = harness();
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();

    stream.emit(validObservatoryAdapterEvent(2, 2), '2');
    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.reason).toMatch(/source cursor/i);

    const recovered = validObservatoryAdapterEvent(3, 4);
    stream.emit(recovered, '4');
    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.reason).toMatch(/id.*generation/i);

    const unknown = validObservatoryAdapterEvent(4, 5);
    unknown.protocol = 'mycelium.observatory_stream.v2';
    stream.emit(unknown, '4');
    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.reason).toMatch(/protocol/i);
  });

  it('requires an exact decimal SSE id on every snapshot event', async () => {
    for (const eventId of ['', '02', ' 2', '2e0', '0x2', '+2']) {
      const { source, stream } = harness();
      await source.loadInitial();
      source.subscribe(() => undefined);
      stream.open();
      stream.emit(validObservatoryAdapterEvent(2, 4), eventId);
      expect(source.getState()!.status).toBe('disconnected');
      expect(source.getState()!.reason).toMatch(/id.*generation/i);
    }
  });

  it('rejects observation-time rollback at a higher generation', async () => {
    const { source, stream } = harness();
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();
    const rollback = validObservatoryAdapterEvent(2, 4);
    rollback.bundle.snapshot.observed_at_unix_ms = 999;
    stream.emit(rollback, '2');

    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.reason).toMatch(/observation time/i);
  });

  it('recovers only on a higher valid generation after transport disconnect', async () => {
    const { source, stream } = harness();
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();
    stream.fail();
    expect(source.getState()!.status).toBe('disconnected');

    stream.open();
    stream.emit(validObservatoryAdapterEvent(2, 4), '2');
    expect(source.getState()!.status).toBe('connected');
    expect(source.getState()!.generation).toBe(2);
  });

  it('reconstructs a restarted backend only from a strictly newer same-origin snapshot', async () => {
    const beforeRestart = validObservatoryAdapterEvent(9, 12);
    beforeRestart.bundle.snapshot.observed_at_unix_ms = 1_000;
    const staleRestart = validObservatoryAdapterEvent(1, 1);
    staleRestart.bundle.snapshot.observed_at_unix_ms = 999;
    const afterRestart = validObservatoryAdapterEvent(1, 1);
    afterRestart.bundle.snapshot.observed_at_unix_ms = 2_000;
    const { source, stream, calls } = harness({
      initialSequence: [beforeRestart, staleRestart, afterRestart],
      now: () => 2_000,
    });
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();
    stream.fail();
    stream.open();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.reason).toMatch(/restart snapshot unavailable/i);
    expect(calls).toHaveLength(2);

    stream.open();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(source.getState()!.status).toBe('connected');
    expect(source.getState()!.generation).toBe(1);
    expect(source.getState()!.source_cursor).toBe(1);
    expect(calls).toHaveLength(3);
  });

  it('promotes only current accepted physical evidence and revokes readiness on disconnect', async () => {
    const { source, stream } = harness({ now: () => 1_000 });
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();
    const accepted = validObservatoryAdapterEvent(2, 4);
    accepted.bundle.snapshot.qualification!.evidence_class = 'physical_qualification';
    accepted.bundle.snapshot.qualification!.route_ready = true;
    accepted.bundle.snapshot.qualification!.reason_codes = [];
    accepted.bundle.provisioning.route_ready = true;

    stream.emit(accepted, '2');
    expect(source.getState()!.route_ready).toBe(true);
    stream.fail();
    expect(source.getState()!.route_ready).toBe(false);
  });

  it('marks projection stale on a bounded timer while route_ready remains false', async () => {
    let now = 1_000;
    let callback: (() => void) | null = null;
    let delay = -1;
    const { source, stream } = harness({
      now: () => now,
      schedule: (scheduled, delayMs) => {
        callback = scheduled;
        delay = delayMs;
        return 1;
      },
    });
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();

    expect(source.getState()!.freshness).toBe('current');
    expect(source.getState()!.route_ready).toBe(false);
    expect(delay).toBe(300_001);
    now = 301_001;
    expect(callback).not.toBeNull();
    (callback as unknown as () => void)();
    expect(source.getState()!.freshness).toBe('stale');
    expect(source.getState()!.route_ready).toBe(false);
  });

  it('re-arms an early freshness timer instead of remaining current forever', async () => {
    let callback: (() => void) | null = null;
    let schedules = 0;
    const { source } = harness({
      now: () => 1_000,
      schedule: (scheduled) => {
        callback = scheduled;
        schedules += 1;
        return schedules;
      },
    });
    await source.loadInitial();
    expect(schedules).toBe(1);
    (callback as unknown as () => void)();

    expect(source.getState()!.freshness).toBe('current');
    expect(schedules).toBe(2);
  });

  it('rejects oversized private SSE data without retaining it', async () => {
    const { source, stream } = harness({ maxEventBytes: 4_096 });
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();
    stream.emitRaw(CANARY.repeat(200), '2');

    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.reason).toMatch(/oversized/i);
    expect(JSON.stringify(source.getState())).not.toContain(CANARY);
  });

  it('measures oversized SSE payloads in UTF-8 bytes', async () => {
    const { source, stream } = harness({ maxEventBytes: 4_096 });
    await source.loadInitial();
    source.subscribe(() => undefined);
    stream.open();
    stream.emitRaw('é'.repeat(2_500), '2');

    expect(source.getState()!.status).toBe('disconnected');
    expect(source.getState()!.reason).toMatch(/oversized/i);
  });

  it('rejects unsafe configuration bounds and exposes no control methods', () => {
    expect(
      () => new LiveObservatoryEventSource({ maxEventBytes: 2 * 1024 * 1024 + 1 }),
    ).toThrow(/maxEventBytes.*bound|maximum/i);
    for (const snapshotUrl of [
      'https://private.invalid/state',
      'http://user:secret@localhost/v1/observatory/snapshot',
      '/v1/observatory/snapshot?credential=private',
      '/v1/observatory/snapshot#private',
    ]) {
      expect(() => new LiveObservatoryEventSource({ snapshotUrl })).toThrow(/same-origin|safe/i);
    }
    const { source } = harness();
    for (const name of ['submit', 'cancel', 'post', 'put', 'patch', 'delete']) {
      expect(name in source).toBe(false);
    }
  });
});
