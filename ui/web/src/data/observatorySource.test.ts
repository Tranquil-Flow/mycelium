import { describe, expect, it, vi } from 'vitest';
import { validSemanticEvent } from '../test/semanticFixture';
import {
  LiveObservatorySource,
  StaticObservatorySource,
  createObservatorySource,
  type LiveEventStream,
  type LiveEventStreamFactory,
  type LiveFetch,
  type LiveFetchInit,
  type ObservatorySourceState,
} from './observatorySource';

const NOW = Date.parse('2026-07-18T12:00:00Z');
const CANARY = 'PHASE9_PRIVACY_CANARY_DO_NOT_PUBLISH';

class FakeEventStream implements LiveEventStream {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private readonly snapshotListeners = new Set<(event: MessageEvent<string>) => void>();

  addEventListener(type: 'snapshot', listener: (event: MessageEvent<string>) => void): void {
    if (type === 'snapshot') this.snapshotListeners.add(listener);
  }

  removeEventListener(type: 'snapshot', listener: (event: MessageEvent<string>) => void): void {
    if (type === 'snapshot') this.snapshotListeners.delete(listener);
  }

  open(): void {
    this.onopen?.(new Event('open'));
  }

  message(payload: unknown, lastEventId?: string): void {
    const event = new MessageEvent<string>('snapshot', {
      data: JSON.stringify(payload),
      lastEventId,
    });
    for (const listener of this.snapshotListeners) listener(event);
  }

  malformed(data: string, lastEventId?: string): void {
    const event = new MessageEvent<string>('snapshot', { data, lastEventId });
    for (const listener of this.snapshotListeners) listener(event);
  }

  disconnect(): void {
    this.onerror?.(new Event('error'));
  }

  close(): void {
    this.closed = true;
  }
}

interface Harness {
  readonly source: LiveObservatorySource;
  readonly stream: FakeEventStream;
  readonly fetchCalls: Array<{ readonly url: string; readonly init: LiveFetchInit | undefined }>;
  readonly eventUrls: string[];
}

function harness(initialGeneration = 10, initialPayload: unknown = validSemanticEvent(initialGeneration)): Harness {
  const stream = new FakeEventStream();
  const fetchCalls: Array<{ url: string; init: LiveFetchInit | undefined }> = [];
  const eventUrls: string[] = [];
  const fetcher: LiveFetch = async (url, init) => {
    fetchCalls.push({ url, init });
    return { ok: true, status: 200, json: async () => structuredClone(initialPayload) };
  };
  const eventStreamFactory: LiveEventStreamFactory = (url) => {
    eventUrls.push(url);
    return stream;
  };
  return {
    source: new LiveObservatorySource({
      fetcher,
      eventStreamFactory,
      now: () => NOW,
      schedule: () => 1,
      cancelSchedule: () => undefined,
    }),
    stream,
    fetchCalls,
    eventUrls,
  };
}

function stateSequence(states: readonly ObservatorySourceState[]): string[] {
  return states.map((state) => `${state.status}:${state.generation}:${state.live_qualified}`);
}

describe('Observatory data sources', () => {
  it('defaults to explicit fixture mode and never qualifies fixtures as live', () => {
    const source = createObservatorySource({ source_mode: 'fixture' });
    const state = source.loadInitial();

    expect(source).toBeInstanceOf(StaticObservatorySource);
    expect(source.source_mode).toBe('fixture');
    expect(state).toMatchObject({
      source_mode: 'fixture',
      status: 'connected',
      generation: 0,
      live_qualified: false,
    });
    expect(source.subscribe).toBeUndefined();
  });

  it('retains no implicit or unknown live configuration path', () => {
    expect(createObservatorySource()).toBeInstanceOf(StaticObservatorySource);
    expect(() => createObservatorySource({ source_mode: 'future' } as never)).toThrow(
      /unknown.*source_mode/i,
    );
  });

  it('uses strict semantic decoders and same-origin read-only defaults', async () => {
    const { source, stream, fetchCalls, eventUrls } = harness(10);
    const observed: ObservatorySourceState[] = [];
    const unsubscribe = source.subscribe?.((state) => observed.push(state));
    stream.open();
    const state = await source.loadInitial();

    expect(source.source_mode).toBe('live');
    expect(fetchCalls).toEqual([
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
    expect(eventUrls).toEqual(['/v1/observatory/events']);
    expect(state).toMatchObject({
      source_mode: 'live',
      status: 'connected',
      generation: 10,
      live_qualified: true,
      qualification_reasons: [],
    });
    expect(state).not.toHaveProperty('bundle');
    expect(JSON.stringify(state)).not.toContain('hypothetical-six-node');
    unsubscribe?.();
  });

  it.each(['https://gateway.invalid/snapshot', '//gateway.invalid/snapshot'])(
    'rejects cross-origin URL %s',
    (snapshotUrl) => {
      expect(() => new LiveObservatorySource({ snapshotUrl })).toThrow(/same-origin/i);
    },
  );

  it('ignores stale malformed events before deep decode and accepts only newer generations', async () => {
    const { source, stream } = harness(10);
    const observed: ObservatorySourceState[] = [];
    source.subscribe?.((state) => observed.push(state));
    stream.open();
    await source.loadInitial();
    observed.length = 0;

    stream.message({ ...validSemanticEvent(9), snapshot: { malformed: true } }, '9');
    stream.message(validSemanticEvent(10), '10');
    expect(observed).toEqual([]);

    stream.message(validSemanticEvent(11), '11');
    expect(stateSequence(observed)).toEqual(['connected:11:true']);
  });

  it('preserves semantic state but revokes live qualification across SSE disconnect/reconnect', async () => {
    const { source, stream } = harness(20);
    const observed: ObservatorySourceState[] = [];
    source.subscribe?.((state) => observed.push(state));
    stream.open();
    await source.loadInitial();
    observed.length = 0;

    stream.disconnect();
    stream.open();
    stream.message(validSemanticEvent(20), '20');
    expect(source.getState()).toMatchObject({
      status: 'disconnected',
      generation: 20,
      live_qualified: false,
    });

    stream.message(validSemanticEvent(21), '21');
    expect(source.getState()).toMatchObject({
      status: 'connected',
      generation: 21,
      live_qualified: true,
    });
    expect(stateSequence(observed)).toEqual(['disconnected:20:false', 'connected:21:true']);
  });

  it('revokes a cached live label until a replacement SSE stream opens', async () => {
    const { source, stream } = harness(25);
    const firstUnsubscribe = source.subscribe?.(() => undefined);
    stream.open();
    await source.loadInitial();
    expect(source.getState()?.live_qualified).toBe(true);

    firstUnsubscribe?.();
    const observed: ObservatorySourceState[] = [];
    source.subscribe?.((state) => observed.push(state));

    expect(source.getState()).toMatchObject({
      status: 'connecting',
      generation: 25,
      live_qualified: false,
    });
    expect(observed.at(-1)).toMatchObject({
      status: 'connecting',
      generation: 25,
      live_qualified: false,
    });

    stream.open();
    expect(source.getState()).toMatchObject({
      status: 'connected',
      generation: 25,
      live_qualified: true,
    });
  });

  it('fails closed on unknown major and requires a strictly newer valid event to recover', async () => {
    const { source, stream } = harness(30);
    source.subscribe?.(() => undefined);
    stream.open();
    await source.loadInitial();

    const unknown = validSemanticEvent(31);
    unknown.protocol = 'mycelium.observatory.event.v2';
    stream.message(unknown, '31');
    expect(source.getState()).toMatchObject({
      status: 'disconnected',
      generation: 30,
      live_qualified: false,
    });

    stream.message(validSemanticEvent(31), '31');
    expect(source.getState()).toMatchObject({ status: 'disconnected', generation: 30 });
    stream.message(validSemanticEvent(32), '32');
    expect(source.getState()).toMatchObject({
      status: 'connected',
      generation: 32,
      live_qualified: true,
    });
  });

  it('fails closed on SSE id/generation mismatch', async () => {
    const { source, stream } = harness(40);
    source.subscribe?.(() => undefined);
    stream.open();
    await source.loadInitial();

    stream.message(validSemanticEvent(41), '400');
    expect(source.getState()).toMatchObject({
      status: 'disconnected',
      generation: 40,
      live_qualified: false,
    });
  });

  it('rejects privacy canaries without storing or publishing them', async () => {
    const { source, stream } = harness(50);
    const observed: ObservatorySourceState[] = [];
    source.subscribe?.((state) => observed.push(state));
    stream.open();
    await source.loadInitial();
    observed.length = 0;

    const poisoned = validSemanticEvent(51) as Record<string, unknown>;
    poisoned.prompt = CANARY;
    stream.message(poisoned, '51');

    expect(source.getState()).toMatchObject({
      status: 'disconnected',
      generation: 50,
      live_qualified: false,
    });
    expect(JSON.stringify(source.getState())).not.toContain(CANARY);
    expect(JSON.stringify(observed)).not.toContain(CANARY);
  });

  it('revokes live qualification exactly when freshness expires', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-18T12:00:00Z'));
    try {
      const stream = new FakeEventStream();
      const source = new LiveObservatorySource({
        fetcher: async () => ({ ok: true, status: 200, json: async () => validSemanticEvent(60) }),
        eventStreamFactory: () => stream,
        now: () => Date.now(),
      });
      source.subscribe?.(() => undefined);
      stream.open();
      await source.loadInitial();
      expect(source.getState()?.live_qualified).toBe(true);

      vi.advanceTimersByTime(5 * 60 * 1000 + 1);
      expect(source.getState()).toMatchObject({
        status: 'connected',
        generation: 60,
        freshness: 'stale',
        live_qualified: false,
      });
      expect(source.getState()?.qualification_reasons).toContain('snapshot_stale');
    } finally {
      vi.useRealTimers();
    }
  });
});
