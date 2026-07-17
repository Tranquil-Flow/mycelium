import { describe, expect, it } from 'vitest';
import scenarioFixture from '../../../tests/fixtures/source/hypothetical-six-node.json';
import simulationFixture from '../../../tests/fixtures/source/planner-simulation.json';
import geographyFixture from '../../../tests/fixtures/source/synthetic-geo.json';
import fixtureManifest from '../../../tests/fixtures/source/ui-fixture-manifest.json';
import failoverFixture from '../../../tests/fixtures/failover/failover-scenarios.json';
import manualProvisioningRouteFixture from '../../../tests/fixtures/source/manual-provisioning-route-v1.json';
import provisioningAuditFixture from '../../../tests/fixtures/source/provisioning-audit.json';
import { adaptSimulator } from '../model/adapter';
import { adaptFailoverScenarios } from '../model/failover';
import { adaptProvisioningEvidence } from '../model/provisioning';
import {
  LiveObservatorySource,
  StaticObservatorySource,
  createObservatorySource,
  type LiveEventStream,
  type LiveEventStreamFactory,
  type LiveFetch,
  type LiveFetchInit,
  type ObservatoryBundle,
  type ObservatorySnapshotEnvelope,
  type ObservatorySourceState,
} from './observatorySource';

function directFixtureBundle(): ObservatoryBundle {
  const snapshot = adaptSimulator(
    scenarioFixture,
    simulationFixture,
    geographyFixture,
    fixtureManifest,
  );
  return {
    snapshot,
    incidents: adaptFailoverScenarios(failoverFixture, {
      knownNodeIds: snapshot.nodes.map((node) => node.id),
      numLayers: snapshot.model.numLayers,
    }),
    provisioning: adaptProvisioningEvidence(
      manualProvisioningRouteFixture,
      provisioningAuditFixture,
    ),
  };
}

class FakeEventStream implements LiveEventStream {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;

  message(payload: unknown): void {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }));
  }

  disconnect(): void {
    this.onerror?.(new Event('error'));
  }

  close(): void {
    this.closed = true;
  }
}

interface LiveHarness {
  readonly source: LiveObservatorySource;
  readonly stream: FakeEventStream;
  readonly fetchCalls: Array<{ readonly url: string; readonly init: LiveFetchInit | undefined }>;
  readonly eventStreamUrls: string[];
  readonly initialBundle: ObservatoryBundle;
  readonly alternateBundle: ObservatoryBundle;
}

function liveHarness(initialGeneration = 10): LiveHarness {
  const bundle = directFixtureBundle();
  const alternateBundle: ObservatoryBundle = {
    ...bundle,
    snapshot: {
      ...bundle.snapshot,
      source: { ...bundle.snapshot.source, scenarioName: 'alternate-generation' },
    },
  };
  const fetchCalls: Array<{ url: string; init: LiveFetchInit | undefined }> = [];
  const fetcher: LiveFetch = async (url, init) => {
    fetchCalls.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => ({ generation: initialGeneration }),
    };
  };
  const stream = new FakeEventStream();
  const eventStreamUrls: string[] = [];
  const eventStreamFactory: LiveEventStreamFactory = (url) => {
    eventStreamUrls.push(url);
    return stream;
  };
  const decode = (payload: unknown): ObservatorySnapshotEnvelope => {
    const candidate = payload as {
      generation: number;
      alternate?: boolean;
      invalidBundle?: boolean;
      decoderFailure?: boolean;
    };
    if (candidate.decoderFailure) throw new TypeError('decoder rejected payload');
    return {
      generation: candidate.generation,
      bundle: candidate.invalidBundle
        ? ({} as ObservatoryBundle)
        : candidate.alternate
          ? alternateBundle
          : bundle,
    };
  };

  return {
    source: new LiveObservatorySource({
      snapshotUrl: 'https://gateway.invalid/constructor-supplied-snapshot',
      eventsUrl: 'https://gateway.invalid/constructor-supplied-events',
      fetcher,
      eventStreamFactory,
      decodeSnapshot: decode,
      decodeEvent: decode,
    }),
    stream,
    fetchCalls,
    eventStreamUrls,
    initialBundle: bundle,
    alternateBundle,
  };
}

describe('Observatory data sources', () => {
  it('keeps static fixtures as the default with byte-for-byte bundle compatibility', () => {
    const source = createObservatorySource();

    expect(source).toBeInstanceOf(StaticObservatorySource);
    if (!(source instanceof StaticObservatorySource)) {
      throw new TypeError('default source must be static');
    }
    const initial = source.loadInitial();
    expect(initial.status).toBe('connected');
    expect(initial.generation).toBe(0);
    expect(JSON.stringify(initial.bundle)).toBe(JSON.stringify(directFixtureBundle()));
    expect(source.subscribe).toBeUndefined();
  });

  it('fails closed for an unknown source kind', () => {
    expect(() => createObservatorySource({ kind: 'future-write-gateway' } as never)).toThrow(
      /unknown observatory source/i,
    );
  });

  it('rejects an initial payload that is not one coherent Observatory bundle', async () => {
    const source = new LiveObservatorySource({
      snapshotUrl: 'https://gateway.invalid/coherent-snapshot',
      fetcher: async () => ({
        ok: true,
        status: 200,
        json: async () => ({ generation: 1 }),
      }),
      decodeSnapshot: () => ({
        generation: 1,
        bundle: {} as ObservatoryBundle,
      }),
    });

    await expect(source.loadInitial()).rejects.toThrow(/coherent bundle/i);
    expect(source.getState()).toBeNull();
    expect(source.subscribe).toBeUndefined();
  });

  it.each([-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1])(
    'rejects invalid generation %s',
    async (generation) => {
      const bundle = directFixtureBundle();
      const source = new LiveObservatorySource({
        snapshotUrl: 'https://gateway.invalid/generation',
        fetcher: async () => ({ ok: true, status: 200, json: async () => null }),
        decodeSnapshot: () => ({ generation, bundle }),
      });

      await expect(source.loadInitial()).rejects.toThrow(/non-negative safe integer/i);
      expect(source.getState()).toBeNull();
    },
  );

  it('rejects stale and duplicate live generations without replacing the bundle', async () => {
    const { source, stream, initialBundle, alternateBundle } = liveHarness(10);
    await source.loadInitial();
    const observed: ObservatorySourceState[] = [];
    const unsubscribe = source.subscribe?.((state) => observed.push(state));

    stream.message({ generation: 9, alternate: true });
    stream.message({ generation: 10, alternate: true });

    expect(observed).toEqual([]);
    expect(source.getState()?.generation).toBe(10);
    expect(source.getState()?.bundle).toBe(initialBundle);

    stream.message({ generation: 11, alternate: true });
    expect(observed.map((state) => state.generation)).toEqual([11]);
    expect(source.getState()?.bundle).toBe(alternateBundle);
    unsubscribe?.();
  });

  it('ignores a malformed stale event without changing connection state', async () => {
    const { source, stream, initialBundle } = liveHarness(10);
    await source.loadInitial();
    const observed: ObservatorySourceState[] = [];
    const unsubscribe = source.subscribe?.((state) => observed.push(state));

    stream.message({ generation: 9, invalidBundle: true });

    expect(source.getState()).toMatchObject({ status: 'connected', generation: 10 });
    expect(source.getState()?.bundle).toBe(initialBundle);
    expect(observed).toEqual([]);
    unsubscribe?.();
  });

  it('preserves the last coherent snapshot while disconnected', async () => {
    const { source, stream } = liveHarness(4);
    const initial = await source.loadInitial();
    const observed: ObservatorySourceState[] = [];
    const unsubscribe = source.subscribe?.((state) => observed.push(state));

    stream.disconnect();

    expect(source.getState()).toMatchObject({ status: 'disconnected', generation: 4 });
    expect(source.getState()?.bundle).toBe(initial.bundle);
    expect(observed).toHaveLength(1);
    expect(observed[0]).toMatchObject({ status: 'disconnected', generation: 4 });
    unsubscribe?.();
  });

  it('reconnects only when a strictly newer coherent generation arrives', async () => {
    const { source, stream } = liveHarness(20);
    await source.loadInitial();
    const observed: ObservatorySourceState[] = [];
    const unsubscribe = source.subscribe?.((state) => observed.push(state));

    stream.disconnect();
    stream.message({ generation: 19, alternate: true });
    stream.message({ generation: 20, alternate: true });
    expect(source.getState()).toMatchObject({ status: 'disconnected', generation: 20 });

    stream.message({ generation: 21 });
    expect(source.getState()).toMatchObject({ status: 'connected', generation: 21 });
    expect(observed.map((state) => `${state.status}:${state.generation}`)).toEqual([
      'disconnected:20',
      'connected:21',
    ]);
    unsubscribe?.();
  });

  it('subscribes before the GET settles and keeps the newest coherent generation', async () => {
    const bundle = directFixtureBundle();
    let resolvePayload: ((payload: unknown) => void) | undefined;
    const payload = new Promise<unknown>((resolve) => {
      resolvePayload = resolve;
    });
    const stream = new FakeEventStream();
    const source = new LiveObservatorySource({
      snapshotUrl: 'https://gateway.invalid/racing-snapshot',
      eventsUrl: 'https://gateway.invalid/racing-events',
      fetcher: async () => ({ ok: true, status: 200, json: () => payload }),
      eventStreamFactory: () => stream,
      decodeSnapshot: (value) => ({
        generation: (value as { generation: number }).generation,
        bundle,
      }),
      decodeEvent: (value) => ({
        generation: (value as { generation: number }).generation,
        bundle,
      }),
    });

    const unsubscribe = source.subscribe?.(() => undefined);
    const loading = source.loadInitial();
    stream.message({ generation: 11 });
    resolvePayload?.({ generation: 10 });

    await expect(loading).resolves.toMatchObject({ status: 'connected', generation: 11 });
    await expect(source.loadInitial()).resolves.toMatchObject({ generation: 11 });
    expect(source.getState()?.generation).toBe(11);
    unsubscribe?.();
  });

  it('ignores a malformed GET when a newer coherent event already won the race', async () => {
    const bundle = directFixtureBundle();
    let resolvePayload: ((payload: unknown) => void) | undefined;
    const payload = new Promise<unknown>((resolve) => {
      resolvePayload = resolve;
    });
    const stream = new FakeEventStream();
    const source = new LiveObservatorySource({
      snapshotUrl: 'https://gateway.invalid/stale-malformed-snapshot',
      eventsUrl: 'https://gateway.invalid/events',
      fetcher: async () => ({ ok: true, status: 200, json: () => payload }),
      eventStreamFactory: () => stream,
      decodeSnapshot: (value) => ({
        generation: (value as { generation: number }).generation,
        bundle: {} as ObservatoryBundle,
      }),
      decodeEvent: (value) => ({
        generation: (value as { generation: number }).generation,
        bundle,
      }),
    });

    const unsubscribe = source.subscribe?.(() => undefined);
    const loading = source.loadInitial();
    stream.message({ generation: 11 });
    resolvePayload?.({ generation: 10 });

    await expect(loading).resolves.toMatchObject({ status: 'connected', generation: 11 });
    await expect(source.loadInitial()).resolves.toMatchObject({ generation: 11 });
    unsubscribe?.();
  });

  it('lets a newer initial snapshot supersede a malformed pending event', async () => {
    const { source, stream } = liveHarness(10);
    const unsubscribe = source.subscribe?.(() => undefined);
    stream.message({ generation: 9, invalidBundle: true });

    await expect(source.loadInitial()).resolves.toMatchObject({
      status: 'connected',
      generation: 10,
    });
    unsubscribe?.();
  });

  it('keeps the highest malformed pending generation until coherent evidence catches up', async () => {
    const { source, stream } = liveHarness(11);
    const unsubscribe = source.subscribe?.(() => undefined);
    stream.message({ generation: 12, invalidBundle: true });
    stream.message({ generation: 11, invalidBundle: true });

    await expect(source.loadInitial()).resolves.toMatchObject({
      status: 'disconnected',
      generation: 11,
    });
    unsubscribe?.();
  });

  it('does not let a versioned malformed event erase an unversioned transport failure', async () => {
    const { source, stream } = liveHarness(10);
    const unsubscribe = source.subscribe?.(() => undefined);
    stream.disconnect();
    stream.message({ generation: 9, invalidBundle: true });

    await expect(source.loadInitial()).resolves.toMatchObject({
      status: 'disconnected',
      generation: 10,
      reason: 'Observatory event stream disconnected',
    });
    unsubscribe?.();
  });

  it('treats a decoder rejection as unversioned until a coherent event reconnects', async () => {
    const { source, stream } = liveHarness(10);
    const unsubscribe = source.subscribe?.(() => undefined);
    stream.message({ generation: 9, decoderFailure: true });

    await expect(source.loadInitial()).resolves.toMatchObject({
      status: 'disconnected',
      generation: 10,
      reason: expect.stringMatching(/decoder rejected payload/i),
    });
    stream.message({ generation: 11 });
    expect(source.getState()).toMatchObject({ status: 'connected', generation: 11 });
    unsubscribe?.();
  });

  it('preserves an early stream disconnect when the initial GET arrives', async () => {
    const { source, stream } = liveHarness(4);
    const unsubscribe = source.subscribe?.(() => undefined);
    stream.disconnect();

    await expect(source.loadInitial()).resolves.toMatchObject({
      status: 'disconnected',
      generation: 4,
    });
    unsubscribe?.();
  });

  it('turns event-stream construction failure into disconnected snapshot state', async () => {
    const bundle = directFixtureBundle();
    const source = new LiveObservatorySource({
      snapshotUrl: 'https://gateway.invalid/snapshot',
      eventsUrl: 'https://gateway.invalid/events',
      fetcher: async () => ({
        ok: true,
        status: 200,
        json: async () => ({ generation: 2 }),
      }),
      eventStreamFactory: () => {
        throw new TypeError('invalid event URL');
      },
      decodeSnapshot: () => ({ generation: 2, bundle }),
      decodeEvent: () => ({ generation: 3, bundle }),
    });

    const unsubscribe = source.subscribe?.(() => undefined);
    await expect(source.loadInitial()).resolves.toMatchObject({
      status: 'disconnected',
      generation: 2,
      reason: expect.stringMatching(/event stream.*invalid event URL/i),
    });
    unsubscribe?.();
  });

  it('isolates listener failures from source connectivity and other listeners', async () => {
    const { source, stream } = liveHarness(10);
    await source.loadInitial();
    const observed: ObservatorySourceState[] = [];
    const unsubscribeThrowing = source.subscribe?.(() => {
      throw new Error('consumer render failed');
    });
    const unsubscribeObserved = source.subscribe?.((state) => observed.push(state));

    stream.message({ generation: 11 });

    expect(source.getState()).toMatchObject({ status: 'connected', generation: 11 });
    expect(observed.map((state) => state.generation)).toEqual([11]);
    unsubscribeThrowing?.();
    unsubscribeObserved?.();
  });

  it('uses only constructor-supplied GET and inbound SSE transports', async () => {
    const { source, stream, fetchCalls, eventStreamUrls } = liveHarness(1);

    await source.loadInitial();
    const unsubscribe = source.subscribe?.(() => undefined);

    expect(fetchCalls).toEqual([
      {
        url: 'https://gateway.invalid/constructor-supplied-snapshot',
        init: {
          method: 'GET',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        },
      },
    ]);
    expect(eventStreamUrls).toEqual([
      'https://gateway.invalid/constructor-supplied-events',
    ]);
    expect('send' in stream).toBe(false);
    expect(
      Object.getOwnPropertyNames(Object.getPrototypeOf(source)).filter((name) =>
        /^(post|put|patch|delete|send)$/i.test(name),
      ),
    ).toEqual([]);

    unsubscribe?.();
    expect(stream.closed).toBe(true);
  });

  it('rejects missing URLs and incomplete event configuration', () => {
    const bundle = directFixtureBundle();
    const decode = (): ObservatorySnapshotEnvelope => ({ generation: 0, bundle });

    expect(
      () => new LiveObservatorySource({ snapshotUrl: ' ', decodeSnapshot: decode }),
    ).toThrow(/snapshotUrl.*non-empty/i);
    expect(
      () =>
        new LiveObservatorySource({
          snapshotUrl: 'https://gateway.invalid/snapshot',
          eventsUrl: ' ',
          decodeSnapshot: decode,
          decodeEvent: decode,
        }),
    ).toThrow(/eventsUrl.*non-empty/i);
    expect(
      () =>
        new LiveObservatorySource({
          snapshotUrl: 'https://gateway.invalid/snapshot',
          eventsUrl: 'https://gateway.invalid/events',
          decodeSnapshot: decode,
        }),
    ).toThrow(/decodeEvent is required/i);
  });
});
