import { describe, expect, it, vi } from 'vitest';
import { PRODUCT_API_PATHS, PRODUCT_OBSERVATORY_PROTOCOL } from '../../../app/contracts';
import {
  decodeObservatoryAdapterEvent,
  type ObservatoryAdapterBundle,
} from '../../../data/observatoryEventProjection';
import type {
  LiveObservatoryEventState,
  ObservatoryEventListener,
} from '../../../data/observatoryEventSource';
import { validObservatoryAdapterEvent } from '../../../test/observatoryEventFixture';
import {
  ObservatoryController,
  createProductObservatoryLiveSource,
  createProductObservatoryReplayFrame,
  resolveProductObservatorySourceMode,
  type ObservatoryControllerSource,
  type ObservatoryControllerState,
} from './ObservatoryController';

function liveState(
  generation: number,
  sourceCursor: number,
  status: LiveObservatoryEventState['status'] = 'connected',
  mutate?: (bundle: ObservatoryAdapterBundle) => void,
): LiveObservatoryEventState {
  const event = decodeObservatoryAdapterEvent(
    validObservatoryAdapterEvent(generation, sourceCursor),
  );
  const bundle = structuredClone(event.bundle) as ObservatoryAdapterBundle;
  mutate?.(bundle);
  return {
    source_mode: 'live',
    status,
    generation,
    source_cursor: sourceCursor,
    projection: bundle,
    route_ready: false,
    freshness: 'current',
  };
}

class FakeControllerSource implements ObservatoryControllerSource {
  loadCalls = 0;
  subscribeCalls = 0;
  unsubscribeCalls = 0;
  private listener: ObservatoryEventListener | null = null;

  constructor(private current: LiveObservatoryEventState) {}

  async loadInitial(): Promise<LiveObservatoryEventState> {
    this.loadCalls += 1;
    return this.current;
  }

  getState(): LiveObservatoryEventState {
    return this.current;
  }

  subscribe(listener: ObservatoryEventListener): () => void {
    this.subscribeCalls += 1;
    this.listener = listener;
    return () => {
      this.unsubscribeCalls += 1;
      if (this.listener === listener) this.listener = null;
    };
  }

  emit(state: LiveObservatoryEventState): void {
    this.current = state;
    this.listener?.(state);
  }
}

function liveHarness(now = 1_000) {
  const source = new FakeControllerSource(liveState(1, 3, 'connecting'));
  const controller = new ObservatoryController({
    source_mode: 'live',
    source,
    now: () => now,
    schedule: () => 1,
    cancelSchedule: () => undefined,
  });
  return { controller, source };
}

function expectProductLabel(
  state: ObservatoryControllerState,
  mode: 'fixture' | 'live' | 'replay',
): void {
  expect(state.source_mode).toBe(mode);
  expect(state.envelope).toMatchObject({
    protocol: PRODUCT_OBSERVATORY_PROTOCOL,
    source: { mode },
  });
}

describe('ObservatoryController', () => {
  it('resolves production source mode fail closed and wires the event source to BFF paths', () => {
    expect(resolveProductObservatorySourceMode(undefined)).toBe('live');
    expect(resolveProductObservatorySourceMode('')).toBe('live');
    expect(resolveProductObservatorySourceMode('fixture')).toBe('fixture');
    expect(resolveProductObservatorySourceMode('live')).toBe('live');
    expect(() => resolveProductObservatorySourceMode('replay')).toThrow(/source mode/i);
    expect(() => resolveProductObservatorySourceMode('future')).toThrow(/source mode/i);

    const source = createProductObservatoryLiveSource();
    expect(source.snapshotUrl).toBe(PRODUCT_API_PATHS.observatory_snapshot);
    expect(source.eventsUrl).toBe(PRODUCT_API_PATHS.observatory_events);
  });

  it('bootstraps the snapshot, then publishes privacy-reduced live state with unknowns intact', async () => {
    const { controller, source } = liveHarness();
    const states: ObservatoryControllerState[] = [];
    controller.subscribe((state) => states.push(state));

    const state = await controller.start();

    expect(source.loadCalls).toBe(1);
    expect(source.subscribeCalls).toBe(1);
    expectProductLabel(state, 'live');
    expect(state).toMatchObject({
      status: 'connecting',
      generation: 1,
      latest_generation: 1,
      source_cursor: 3,
      visible_source_cursor: 3,
      freshness: 'current',
      frozen: false,
      route_ready: false,
      reason_code: null,
    });
    expect(state.envelope?.metrics).toEqual({
      native_node_count: null,
      browser_worker_count: null,
      incident_count: 1,
    });
    expect(state.change_set.empty).toBe(true);
    expect(states.at(-1)).toBe(state);
    expect(Object.isFrozen(state)).toBe(true);
    expect(Object.isFrozen(state.projection?.entities.evidence)).toBe(true);
  });

  it('enforces generation and source-cursor monotonicity and recovers only above bad watermarks', async () => {
    const { controller, source } = liveHarness();
    await controller.start();

    source.emit(liveState(2, 4));
    expect(controller.getState()).toMatchObject({
      status: 'connected',
      generation: 2,
      latest_generation: 2,
      source_cursor: 4,
      reason_code: null,
    });

    source.emit(liveState(1, 3));
    expect(controller.getState()).toMatchObject({ generation: 2, source_cursor: 4 });

    source.emit(liveState(3, 4));
    expect(controller.getState()).toMatchObject({
      status: 'disconnected',
      generation: 2,
      latest_generation: 2,
      source_cursor: 4,
      freshness: 'stale',
      reason_code: 'non_monotonic_update',
    });

    source.emit(liveState(3, 5));
    expect(controller.getState()).toMatchObject({ generation: 2, source_cursor: 4 });

    source.emit(liveState(4, 6));
    expect(controller.getState()).toMatchObject({
      status: 'connected',
      generation: 4,
      latest_generation: 4,
      source_cursor: 6,
      freshness: 'current',
      reason_code: null,
    });
  });

  it('preserves last-known data while reflecting disconnect and reconnect status', async () => {
    const { controller, source } = liveHarness();
    await controller.start();
    source.emit(liveState(2, 4));
    const projection = controller.getState().projection;

    source.emit(liveState(2, 4, 'disconnected'));
    expect(controller.getState()).toMatchObject({
      status: 'disconnected',
      generation: 2,
      source_cursor: 4,
      freshness: 'stale',
      reason_code: 'event_source_disconnected',
    });
    expect(controller.getState().projection).toBe(projection);

    source.emit(liveState(3, 5, 'connected'));
    expect(controller.getState()).toMatchObject({
      status: 'connected',
      generation: 3,
      source_cursor: 5,
      freshness: 'current',
      reason_code: null,
    });
  });

  it('freezes visible mutation while retaining transport status and resume watermarks', async () => {
    const { controller, source } = liveHarness();
    await controller.start();
    controller.freeze();
    const frozenProjection = controller.getState().projection;

    source.emit(
      liveState(2, 4, 'connected', (bundle) => {
        (bundle.snapshot.sessions[0] as { state: string }).state = 'failed';
        (bundle.snapshot.sessions[0] as { terminal: boolean }).terminal = true;
      }),
    );
    source.emit(liveState(2, 4, 'disconnected', (bundle) => {
      (bundle.snapshot.sessions[0] as { state: string }).state = 'failed';
      (bundle.snapshot.sessions[0] as { terminal: boolean }).terminal = true;
    }));

    expect(controller.getState()).toMatchObject({
      frozen: true,
      status: 'disconnected',
      generation: 1,
      latest_generation: 2,
      source_cursor: 4,
      visible_source_cursor: 3,
    });
    expect(controller.getState().projection).toBe(frozenProjection);

    controller.unfreeze();
    expect(controller.getState()).toMatchObject({
      frozen: false,
      generation: 2,
      latest_generation: 2,
      source_cursor: 4,
      visible_source_cursor: 4,
    });
    expect(controller.getState().projection).not.toBe(frozenProjection);
    expect(controller.getState().change_set.evidence.changed).toContain('request~request-a');
  });

  it('runs replay with zero fetch or EventSource calls and supports deterministic selection', async () => {
    const fetcher = vi.fn();
    const EventSourceConstructor = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    vi.stubGlobal('EventSource', EventSourceConstructor);
    try {
      const first = createProductObservatoryReplayFrame({
        protocol: PRODUCT_OBSERVATORY_PROTOCOL,
        generation: 7,
        status: 'connected',
        source: {
          mode: 'fixture',
          freshness: 'fixture',
          observed_at_unix_ms: null,
          replay_of_generation: null,
        },
        metrics: {
          native_node_count: null,
          browser_worker_count: null,
          incident_count: null,
        },
      });
      const second = createProductObservatoryReplayFrame({
        protocol: PRODUCT_OBSERVATORY_PROTOCOL,
        generation: 8,
        status: 'connected',
        source: {
          mode: 'live',
          freshness: 'current',
          observed_at_unix_ms: 1_000,
          replay_of_generation: null,
        },
        metrics: {
          native_node_count: 2,
          browser_worker_count: null,
          incident_count: 1,
        },
      });
      const controller = new ObservatoryController({
        source_mode: 'replay',
        replay_frames: [first, second],
        replay_generation: 7,
      });

      await controller.start();
      expectProductLabel(controller.getState(), 'replay');
      expect(controller.getState()).toMatchObject({
        status: 'connected',
        generation: 7,
        replay_of_generation: 7,
        freshness: 'replay',
      });

      controller.selectReplay(8);
      expect(controller.getState()).toMatchObject({
        generation: 8,
        replay_of_generation: 8,
        freshness: 'replay',
      });
      expect(controller.getState().envelope?.metrics.native_node_count).toBe(2);
      expect(fetcher).not.toHaveBeenCalled();
      expect(EventSourceConstructor).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('enters replay from retained live history without another network operation', async () => {
    const { controller, source } = liveHarness();
    await controller.start();
    source.emit(liveState(2, 4));
    const callsBeforeReplay = source.loadCalls + source.subscribeCalls;

    controller.enterReplay(1);

    expectProductLabel(controller.getState(), 'replay');
    expect(controller.getState()).toMatchObject({
      generation: 1,
      replay_of_generation: 1,
      freshness: 'replay',
    });
    expect(source.loadCalls + source.subscribeCalls).toBe(callsBeforeReplay);
    expect(source.unsubscribeCalls).toBe(1);
  });

  it('marks a current live frame stale on the exact bounded timer', async () => {
    let now = 1_000;
    let callback: (() => void) | null = null;
    let delay = -1;
    const source = new FakeControllerSource(liveState(1, 3));
    const controller = new ObservatoryController({
      source_mode: 'live',
      source,
      now: () => now,
      stale_after_ms: 300_000,
      schedule: (scheduled, delayMs) => {
        callback = scheduled;
        delay = delayMs;
        return 1;
      },
      cancelSchedule: () => undefined,
    });
    await controller.start();

    expect(controller.getState().freshness).toBe('current');
    expect(delay).toBe(300_001);
    now = 301_001;
    (callback as unknown as () => void)();

    expect(controller.getState()).toMatchObject({
      generation: 1,
      status: 'connected',
      freshness: 'stale',
      reason_code: 'snapshot_stale',
    });
    expect(controller.getState().envelope?.source.freshness).toBe('stale');
  });

  it('fails closed without retaining rejected private fields', async () => {
    const { controller, source } = liveHarness();
    await controller.start();
    const poisoned = liveState(2, 4) as LiveObservatoryEventState & {
      projection: ObservatoryAdapterBundle & { prompt?: string };
    };
    poisoned.projection.prompt = 'OBSERVATORY_CONTROLLER_PRIVATE_CANARY';

    source.emit(poisoned);

    expect(controller.getState()).toMatchObject({
      status: 'disconnected',
      generation: 1,
      source_cursor: 3,
      freshness: 'stale',
      reason_code: 'invalid_projection',
    });
    expect(JSON.stringify(controller.getState())).not.toContain(
      'OBSERVATORY_CONTROLLER_PRIVATE_CANARY',
    );
  });

  it('fails closed on snapshot bootstrap errors without inventing data or readiness', async () => {
    const source: ObservatoryControllerSource = {
      loadInitial: async () => {
        throw new Error('private upstream detail');
      },
      getState: () => null,
      subscribe: () => () => undefined,
    };
    const controller = new ObservatoryController({ source_mode: 'live', source });

    const state = await controller.start();

    expect(state).toMatchObject({
      source_mode: 'live',
      status: 'disconnected',
      generation: null,
      latest_generation: null,
      projection: null,
      route_ready: false,
      freshness: 'unknown',
      reason_code: 'snapshot_bootstrap_failed',
    });
    expect(state.envelope).toBeNull();
    expect(JSON.stringify(state)).not.toContain('private upstream detail');
  });

  it('keeps an explicitly selected fixture projection offline and never relabels it live', async () => {
    const controller = new ObservatoryController({ source_mode: 'fixture' });
    const state = await controller.start();

    expectProductLabel(state, 'fixture');
    expect(state).toMatchObject({
      status: 'connected',
      generation: 0,
      freshness: 'fixture',
      frozen: false,
    });
    expect(state.envelope?.metrics.native_node_count).toBeGreaterThan(0);
    expect(state.envelope?.metrics.browser_worker_count).toBeNull();
  });
});
