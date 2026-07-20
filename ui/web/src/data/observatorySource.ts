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
import { deepFreeze } from '../model/runtime';
import {
  decodeObservatoryEvent,
  parseObservatoryEventHeader,
  qualifySemanticSnapshot,
  type ObservatorySemanticEvent,
  type ObservatorySemanticSnapshot,
} from '../model/semanticProjection';
import type { EvidenceSnapshot, FailoverIncident, ProvisioningEvidence } from '../model/types';

export const DEFAULT_OBSERVATORY_SNAPSHOT_URL = '/v1/observatory/snapshot';
export const DEFAULT_OBSERVATORY_EVENTS_URL = '/v1/observatory/events';

export interface ObservatoryBundle {
  readonly snapshot: EvidenceSnapshot;
  readonly incidents: readonly FailoverIncident[];
  readonly provisioning: ProvisioningEvidence;
}

export interface FixtureObservatorySourceState {
  readonly source_mode: 'fixture';
  readonly status: 'connected';
  readonly generation: 0;
  readonly bundle: ObservatoryBundle;
  readonly live_qualified: false;
  readonly qualification_reasons: readonly ['fixture_source'];
  readonly freshness: 'fixture';
}

export interface ReplayObservatorySourceState {
  readonly source_mode: 'replay';
  readonly status: 'connected';
  readonly generation: 0;
  readonly bundle: ObservatoryBundle;
  readonly live_qualified: false;
  readonly qualification_reasons: readonly ['replay_source'];
  readonly freshness: 'replay';
}

export interface LiveObservatorySourceState {
  readonly source_mode: 'live';
  readonly status: 'connecting' | 'connected' | 'disconnected';
  readonly generation: number;
  readonly snapshot: ObservatorySemanticSnapshot;
  readonly live_qualified: boolean;
  readonly qualification_reasons: readonly string[];
  readonly freshness: 'current' | 'stale';
  readonly reason?: string;
}

export type ObservatorySourceState = FixtureObservatorySourceState | ReplayObservatorySourceState | LiveObservatorySourceState;
export type ObservatorySourceListener = (state: ObservatorySourceState) => void;
export type ObservatorySourceMode = 'fixture' | 'replay' | 'live';
export type ObservatorySourceKind = 'static' | 'live';

export interface ObservatoryDataSource {
  readonly source_mode: ObservatorySourceMode;
  readonly kind: ObservatorySourceKind;
  readonly subscribe?: (listener: ObservatorySourceListener) => () => void;
  loadInitial(): ObservatorySourceState | Promise<ObservatorySourceState>;
  getState(): ObservatorySourceState | null;
}

export function loadStaticObservatoryBundle(): ObservatoryBundle {
  const snapshot = adaptSimulator(
    scenarioFixture,
    simulationFixture,
    geographyFixture,
    fixtureManifest,
  );
  const incidents = adaptFailoverScenarios(failoverFixture, {
    knownNodeIds: snapshot.nodes.map((node) => node.id),
    numLayers: snapshot.model.numLayers,
  });
  const provisioning = adaptProvisioningEvidence(
    manualProvisioningRouteFixture,
    provisioningAuditFixture,
  );
  return deepFreeze({ snapshot, incidents, provisioning });
}

export class StaticObservatorySource implements ObservatoryDataSource {
  readonly source_mode = 'fixture' as const;
  readonly kind = 'static' as const;
  readonly subscribe = undefined;
  private state: FixtureObservatorySourceState | null = null;

  constructor(
    private readonly loadBundle: () => ObservatoryBundle = loadStaticObservatoryBundle,
  ) {}

  loadInitial(): FixtureObservatorySourceState {
    this.state ??= deepFreeze({
      source_mode: 'fixture',
      status: 'connected',
      generation: 0,
      bundle: this.loadBundle(),
      live_qualified: false,
      qualification_reasons: ['fixture_source'] as const,
      freshness: 'fixture',
    });
    return this.state;
  }

  getState(): FixtureObservatorySourceState | null {
    return this.state;
  }
}

export class ReplayObservatorySource implements ObservatoryDataSource {
  readonly source_mode = 'replay' as const;
  readonly kind = 'static' as const;
  readonly subscribe = undefined;
  private readonly state: ReplayObservatorySourceState;

  constructor(loadBundle: () => ObservatoryBundle = loadStaticObservatoryBundle) {
    this.state = deepFreeze({
      source_mode: 'replay',
      status: 'connected',
      generation: 0,
      bundle: loadBundle(),
      live_qualified: false,
      qualification_reasons: ['replay_source'] as const,
      freshness: 'replay',
    });
  }

  loadInitial(): ReplayObservatorySourceState {
    return this.state;
  }

  getState(): ReplayObservatorySourceState {
    return this.state;
  }
}

export interface LiveFetchResponse {
  readonly ok: boolean;
  readonly status: number;
  json(): Promise<unknown>;
}

export interface LiveFetchInit {
  readonly method: 'GET';
  readonly headers: Readonly<Record<string, string>>;
  readonly cache: 'no-store';
  readonly credentials: 'same-origin';
}

export type LiveFetch = (url: string, init: LiveFetchInit) => Promise<LiveFetchResponse>;

export interface LiveEventStream {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: 'snapshot', listener: (event: MessageEvent<string>) => void): void;
  removeEventListener(type: 'snapshot', listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

export type LiveEventStreamFactory = (url: string) => LiveEventStream;
export type ScheduleHandle = ReturnType<typeof setTimeout> | number;
export type Schedule = (callback: () => void, delayMs: number) => ScheduleHandle;
export type CancelSchedule = (handle: ScheduleHandle) => void;

export interface LiveObservatorySourceOptions {
  readonly snapshotUrl?: string;
  readonly eventsUrl?: string;
  readonly fetcher?: LiveFetch;
  readonly eventStreamFactory?: LiveEventStreamFactory;
  readonly now?: () => number;
  readonly schedule?: Schedule;
  readonly cancelSchedule?: CancelSchedule;
}

export const browserFetch: LiveFetch = async (url, init) => fetch(url, init);
export const browserEventStreamFactory: LiveEventStreamFactory = (url) =>
  new EventSource(url) as unknown as LiveEventStream;
const browserSchedule: Schedule = (callback, delayMs) => setTimeout(callback, delayMs);
const browserCancelSchedule: CancelSchedule = (handle) => clearTimeout(handle);

function safeGeneration(value: unknown): number | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  let generation: unknown;
  try {
    generation = (value as Record<string, unknown>).generation;
  } catch {
    return null;
  }
  return Number.isSafeInteger(generation) && (generation as number) >= 0
    ? (generation as number)
    : null;
}

function sameOriginUrl(value: string, name: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new TypeError(`${name} must be a non-empty same-origin URL`);
  }
  const base = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
  let candidate: URL;
  try {
    candidate = new URL(value, base);
  } catch {
    throw new TypeError(`${name} must be a valid same-origin URL`);
  }
  if (candidate.origin !== base || !['http:', 'https:'].includes(candidate.protocol)) {
    throw new TypeError(`${name} must be same-origin`);
  }
  return value;
}

function expiryMilliseconds(snapshot: ObservatorySemanticSnapshot): number {
  const expiries = [
    snapshot.freshness.valid_until,
    snapshot.route_challenge.freshness.valid_until,
    snapshot.request_lifecycle.freshness.valid_until,
    ...snapshot.claims.map((claim) => claim.freshness.valid_until),
  ].map((value) => Date.parse(value));
  return Math.min(...expiries);
}

export class LiveObservatorySource implements ObservatoryDataSource {
  readonly source_mode = 'live' as const;
  readonly kind = 'live' as const;
  readonly snapshotUrl: string;
  readonly eventsUrl: string;
  readonly subscribe = (listener: ObservatorySourceListener): (() => void) =>
    this.addListener(listener);

  private readonly fetcher: LiveFetch;
  private readonly eventStreamFactory: LiveEventStreamFactory;
  private readonly now: () => number;
  private readonly schedule: Schedule;
  private readonly cancelSchedule: CancelSchedule;
  private readonly listeners = new Set<ObservatorySourceListener>();
  private state: LiveObservatorySourceState | null = null;
  private initialPromise: Promise<void> | null = null;
  private stream: LiveEventStream | null = null;
  private streamOpen = false;
  private highestSeenGeneration = -1;
  private blockedGeneration: number | null = null;
  private expiryTimer: ScheduleHandle | null = null;

  constructor(options: LiveObservatorySourceOptions = {}) {
    this.snapshotUrl = sameOriginUrl(
      options.snapshotUrl ?? DEFAULT_OBSERVATORY_SNAPSHOT_URL,
      'snapshotUrl',
    );
    this.eventsUrl = sameOriginUrl(
      options.eventsUrl ?? DEFAULT_OBSERVATORY_EVENTS_URL,
      'eventsUrl',
    );
    this.fetcher = options.fetcher ?? browserFetch;
    this.eventStreamFactory = options.eventStreamFactory ?? browserEventStreamFactory;
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? browserSchedule;
    this.cancelSchedule = options.cancelSchedule ?? browserCancelSchedule;
  }

  loadInitial(): Promise<LiveObservatorySourceState> {
    this.initialPromise ??= this.fetcher(this.snapshotUrl, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'same-origin',
    }).then(async (response) => {
      if (!response.ok) {
        throw new Error(`Observatory snapshot request failed with status ${response.status}`);
      }
      const payload = await response.json();
      this.processPayload(payload, null, 'snapshot');
    });

    return this.initialPromise.then(() => {
      if (this.state === null) {
        throw new Error('Observatory snapshot did not produce a valid semantic source state');
      }
      return this.state;
    });
  }

  getState(): LiveObservatorySourceState | null {
    return this.state;
  }

  private addListener(listener: ObservatorySourceListener): () => void {
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
      if (this.state !== null && this.blockedGeneration === null) this.refreshQualification();
    };
    stream.onerror = () => {
      this.streamOpen = false;
      this.markDisconnected('Observatory event stream disconnected', this.highestSeenGeneration);
    };
    stream.addEventListener('snapshot', this.onSnapshotEvent);
    this.stream = stream;
    this.streamOpen = false;
    if (this.state !== null) {
      const status = this.blockedGeneration === null ? 'connecting' : 'disconnected';
      this.state = this.buildState(
        this.state.generation,
        this.state.snapshot,
        status,
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
    let payload: unknown;
    try {
      payload = JSON.parse(message.data);
    } catch {
      this.markDisconnected('Invalid Observatory event', this.highestSeenGeneration);
      return;
    }
    this.processPayload(payload, message.lastEventId || null, 'event');
  };

  private processPayload(
    payload: unknown,
    lastEventId: string | null,
    origin: 'snapshot' | 'event',
  ): void {
    const candidateGeneration = safeGeneration(payload);
    if (candidateGeneration !== null && candidateGeneration <= this.highestSeenGeneration) return;

    let headerGeneration: number;
    try {
      headerGeneration = parseObservatoryEventHeader(payload).generation;
    } catch {
      if (candidateGeneration !== null) {
        this.highestSeenGeneration = Math.max(this.highestSeenGeneration, candidateGeneration);
      }
      this.markDisconnected('Invalid Observatory semantic protocol', candidateGeneration);
      return;
    }

    if (headerGeneration <= this.highestSeenGeneration) return;
    this.highestSeenGeneration = headerGeneration;

    if (origin === 'event' && lastEventId !== null) {
      const eventId = Number(lastEventId);
      if (!Number.isSafeInteger(eventId) || eventId !== headerGeneration) {
        this.markDisconnected('Observatory event id/generation mismatch', headerGeneration);
        return;
      }
    }

    let event: ObservatorySemanticEvent;
    try {
      event = decodeObservatoryEvent(payload);
    } catch {
      this.markDisconnected('Invalid Observatory semantic event', headerGeneration);
      return;
    }
    this.acceptEvent(event);
  }

  private acceptEvent(event: ObservatorySemanticEvent): void {
    if (this.blockedGeneration !== null && event.generation <= this.blockedGeneration) return;
    if (this.blockedGeneration !== null && event.generation > this.blockedGeneration) {
      this.blockedGeneration = null;
    }
    const connected = this.streamOpen && this.blockedGeneration === null;
    this.state = this.buildState(event.generation, event.snapshot, connected ? 'connected' : 'connecting');
    this.armExpiryTimer();
    this.notify();
  }

  private buildState(
    generation: number,
    snapshot: ObservatorySemanticSnapshot,
    status: LiveObservatorySourceState['status'],
    reason?: string,
  ): LiveObservatorySourceState {
    const qualification = qualifySemanticSnapshot(snapshot, this.now());
    const transportQualified = status === 'connected';
    const reasons = transportQualified
      ? [...qualification.reasons]
      : ['transport_not_current', ...qualification.reasons];
    return deepFreeze({
      source_mode: 'live',
      status,
      generation,
      snapshot,
      live_qualified: transportQualified && qualification.live,
      qualification_reasons: reasons,
      freshness: qualification.reasons.some((item) => item.includes('stale')) ? 'stale' : 'current',
      ...(reason === undefined ? {} : { reason }),
    });
  }

  private markDisconnected(reason: string, generation: number | null): void {
    if (generation !== null && generation >= 0) {
      this.blockedGeneration = Math.max(this.blockedGeneration ?? -1, generation);
    } else if (this.blockedGeneration === null) {
      this.blockedGeneration = this.highestSeenGeneration;
    }
    if (this.state === null) return;
    if (this.state.status === 'disconnected' && this.state.reason === reason) return;
    this.state = this.buildState(
      this.state.generation,
      this.state.snapshot,
      'disconnected',
      reason,
    );
    this.notify();
  }

  private armExpiryTimer(): void {
    if (this.expiryTimer !== null) this.cancelSchedule(this.expiryTimer);
    if (this.state === null) return;
    const delay = Math.max(0, expiryMilliseconds(this.state.snapshot) - this.now() + 1);
    this.expiryTimer = this.schedule(() => {
      this.expiryTimer = null;
      this.refreshQualification();
    }, delay);
  }

  private refreshQualification(): void {
    if (this.state === null) return;
    const status =
      this.blockedGeneration !== null || !this.streamOpen ? 'disconnected' : 'connected';
    this.state = this.buildState(
      this.state.generation,
      this.state.snapshot,
      status,
      status === 'disconnected' ? this.state.reason ?? 'Observatory event stream not current' : undefined,
    );
    this.notify();
  }

  private notify(): void {
    if (this.state === null) return;
    for (const listener of this.listeners) {
      try {
        listener(this.state);
      } catch {
        // Consumer failures cannot mutate source transport state.
      }
    }
  }
}

export type ObservatorySourceConfig =
  | { readonly source_mode: 'fixture' }
  | { readonly source_mode: 'replay' }
  | { readonly source_mode: 'live'; readonly options?: LiveObservatorySourceOptions };

export function createObservatorySource(): StaticObservatorySource;
export function createObservatorySource(config: { readonly source_mode: 'fixture' }): StaticObservatorySource;
export function createObservatorySource(config: { readonly source_mode: 'replay' }): ReplayObservatorySource;
export function createObservatorySource(config: {
  readonly source_mode: 'live';
  readonly options?: LiveObservatorySourceOptions;
}): LiveObservatorySource;
export function createObservatorySource(
  config: ObservatorySourceConfig = { source_mode: 'fixture' },
): ObservatoryDataSource {
  if (config.source_mode === 'fixture') return new StaticObservatorySource();
  if (config.source_mode === 'replay') return new ReplayObservatorySource();
  if (config.source_mode === 'live') return new LiveObservatorySource(config.options);
  throw new TypeError('Unknown Observatory source_mode');
}
