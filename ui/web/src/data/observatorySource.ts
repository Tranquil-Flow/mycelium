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
import type { EvidenceSnapshot, FailoverIncident, ProvisioningEvidence } from '../model/types';

export interface ObservatoryBundle {
  readonly snapshot: EvidenceSnapshot;
  readonly incidents: readonly FailoverIncident[];
  readonly provisioning: ProvisioningEvidence;
}

export interface ObservatorySnapshotEnvelope {
  readonly generation: number;
  readonly bundle: ObservatoryBundle;
}

export interface ConnectedObservatorySourceState extends ObservatorySnapshotEnvelope {
  readonly status: 'connected';
}

export interface DisconnectedObservatorySourceState extends ObservatorySnapshotEnvelope {
  readonly status: 'disconnected';
  readonly reason: string;
}

export type ObservatorySourceState =
  | ConnectedObservatorySourceState
  | DisconnectedObservatorySourceState;
export type ObservatorySourceListener = (state: ObservatorySourceState) => void;
export type ObservatorySourceKind = 'static' | 'live';

export interface ObservatoryDataSource {
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
  readonly kind = 'static' as const;
  readonly subscribe = undefined;
  private state: ConnectedObservatorySourceState | null = null;

  constructor(
    private readonly loadBundle: () => ObservatoryBundle = loadStaticObservatoryBundle,
  ) {}

  loadInitial(): ConnectedObservatorySourceState {
    this.state ??= deepFreeze({
      status: 'connected',
      generation: 0,
      bundle: this.loadBundle(),
    });
    return this.state;
  }

  getState(): ConnectedObservatorySourceState | null {
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
}

export type LiveFetch = (url: string, init?: LiveFetchInit) => Promise<LiveFetchResponse>;

export interface LiveEventStream {
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}

export type LiveEventStreamFactory = (url: string) => LiveEventStream;
export type ObservatorySnapshotDecoder = (payload: unknown) => ObservatorySnapshotEnvelope;

export interface LiveObservatorySourceOptions {
  readonly snapshotUrl: string;
  readonly eventsUrl?: string;
  readonly fetcher?: LiveFetch;
  readonly eventStreamFactory?: LiveEventStreamFactory;
  readonly decodeSnapshot: ObservatorySnapshotDecoder;
  readonly decodeEvent?: ObservatorySnapshotDecoder;
}

function requiredUrl(value: string, name: string): string {
  if (value.trim().length === 0) {
    throw new TypeError(`${name} must be a non-empty constructor input`);
  }
  return value;
}

function validateGeneration(envelope: ObservatorySnapshotEnvelope): number {
  if (!Number.isSafeInteger(envelope?.generation) || envelope.generation < 0) {
    throw new TypeError('Observatory source generation must be a non-negative safe integer');
  }
  return envelope.generation;
}

function validateEnvelope(envelope: ObservatorySnapshotEnvelope): ObservatorySnapshotEnvelope {
  validateGeneration(envelope);
  const bundle = envelope.bundle;
  const hasSnapshot =
    typeof bundle?.snapshot === 'object' && bundle.snapshot !== null && !Array.isArray(bundle.snapshot);
  const hasIncidents = Array.isArray(bundle?.incidents);
  const hasProvisioning =
    typeof bundle?.provisioning === 'object' &&
    bundle.provisioning !== null &&
    !Array.isArray(bundle.provisioning);
  if (!hasSnapshot || !hasIncidents || !hasProvisioning) {
    throw new TypeError(
      'Observatory source decoder must return one coherent bundle with snapshot, incidents, and provisioning',
    );
  }
  return envelope;
}

const browserFetch: LiveFetch = async (url, init) => fetch(url, init);
const browserEventStreamFactory: LiveEventStreamFactory = (url) => new EventSource(url);

export class LiveObservatorySource implements ObservatoryDataSource {
  readonly kind = 'live' as const;
  readonly snapshotUrl: string;
  readonly eventsUrl?: string;
  readonly subscribe?: (listener: ObservatorySourceListener) => () => void;

  private readonly fetcher: LiveFetch;
  private readonly eventStreamFactory: LiveEventStreamFactory;
  private readonly decodeSnapshot: ObservatorySnapshotDecoder;
  private readonly decodeEvent?: ObservatorySnapshotDecoder;
  private readonly listeners = new Set<ObservatorySourceListener>();
  private state: ObservatorySourceState | null = null;
  private initialPromise: Promise<void> | null = null;
  private stream: LiveEventStream | null = null;
  private pendingDisconnect: { readonly reason: string; readonly generation: number | null } | null =
    null;

  constructor(options: LiveObservatorySourceOptions) {
    this.snapshotUrl = requiredUrl(options.snapshotUrl, 'snapshotUrl');
    this.eventsUrl =
      options.eventsUrl === undefined ? undefined : requiredUrl(options.eventsUrl, 'eventsUrl');
    this.fetcher = options.fetcher ?? browserFetch;
    this.eventStreamFactory = options.eventStreamFactory ?? browserEventStreamFactory;
    this.decodeSnapshot = options.decodeSnapshot;
    this.decodeEvent = options.decodeEvent;

    if (this.eventsUrl !== undefined) {
      if (this.decodeEvent === undefined) {
        throw new TypeError('decodeEvent is required when eventsUrl is configured');
      }
      this.subscribe = (listener) => this.addListener(listener);
    }
  }

  loadInitial(): Promise<ObservatorySourceState> {
    const initialPromise =
      this.initialPromise ??
      this.fetcher(this.snapshotUrl, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      }).then(async (response) => {
        if (!response.ok) {
          throw new Error(`Observatory snapshot request failed with status ${response.status}`);
        }
        const decoded = this.decodeSnapshot(await response.json());
        const generation = validateGeneration(decoded);
        if (this.state !== null && generation <= this.state.generation) return;
        this.acceptSnapshot(validateEnvelope(decoded), 'snapshot');
      });
    this.initialPromise = initialPromise;
    return initialPromise.then(() => {
      if (this.state === null) {
        throw new Error('Observatory snapshot did not produce a source state');
      }
      return this.state;
    });
  }

  getState(): ObservatorySourceState | null {
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
    const decodeEvent = this.decodeEvent;
    if (this.eventsUrl === undefined || decodeEvent === undefined) return;

    let stream: LiveEventStream;
    try {
      stream = this.eventStreamFactory(this.eventsUrl);
    } catch (reason: unknown) {
      const detail = reason instanceof Error ? `: ${reason.message}` : '';
      this.markDisconnected(`Observatory event stream unavailable${detail}`);
      return;
    }

    stream.onmessage = (event) => {
      let envelope: ObservatorySnapshotEnvelope;
      let candidateGeneration: number | undefined;
      try {
        const decoded = decodeEvent(JSON.parse(event.data));
        candidateGeneration = validateGeneration(decoded);
        if (this.state !== null && candidateGeneration <= this.state.generation) return;
        envelope = validateEnvelope(decoded);
      } catch (reason: unknown) {
        this.markDisconnected(
          reason instanceof Error ? `Invalid Observatory event: ${reason.message}` : 'Invalid Observatory event',
          candidateGeneration,
        );
        return;
      }
      this.acceptSnapshot(envelope, 'event');
    };
    stream.onerror = () => this.markDisconnected('Observatory event stream disconnected');
    this.stream = stream;
  }

  private closeEventStream(): void {
    if (this.stream === null) return;
    this.stream.onmessage = null;
    this.stream.onerror = null;
    this.stream.close();
    this.stream = null;
  }

  private acceptSnapshot(
    envelope: ObservatorySnapshotEnvelope,
    origin: 'snapshot' | 'event',
  ): boolean {
    if (this.state !== null && envelope.generation <= this.state.generation) return false;
    if (
      origin === 'event' ||
      (this.pendingDisconnect?.generation !== null &&
        this.pendingDisconnect?.generation !== undefined &&
        envelope.generation >= this.pendingDisconnect.generation)
    ) {
      this.pendingDisconnect = null;
    }

    this.state = deepFreeze(
      this.pendingDisconnect === null
        ? {
            status: 'connected',
            generation: envelope.generation,
            bundle: envelope.bundle,
          }
        : {
            status: 'disconnected',
            generation: envelope.generation,
            bundle: envelope.bundle,
            reason: this.pendingDisconnect.reason,
          },
    );
    this.notify();
    return true;
  }

  private markDisconnected(reason: string, generation?: number): void {
    const candidate = { reason, generation: generation ?? null } as const;
    const existing = this.pendingDisconnect;
    this.pendingDisconnect =
      existing === null
        ? candidate
        : existing.generation === null
          ? existing
          : candidate.generation === null || candidate.generation >= existing.generation
            ? candidate
            : existing;

    if (this.state === null) return;
    const pendingReason = this.pendingDisconnect.reason;
    if (this.state.status === 'disconnected' && this.state.reason === pendingReason) return;
    this.state = deepFreeze({
      status: 'disconnected',
      generation: this.state.generation,
      bundle: this.state.bundle,
      reason: pendingReason,
    });
    this.notify();
  }

  private notify(): void {
    if (this.state === null) return;
    for (const listener of this.listeners) {
      try {
        listener(this.state);
      } catch {
        // Listener failures belong to consumers and must not alter transport state.
      }
    }
  }
}

export type ObservatorySourceConfig =
  | { readonly kind?: 'static' }
  | { readonly kind: 'live'; readonly options: LiveObservatorySourceOptions };

export function createObservatorySource(): StaticObservatorySource;
export function createObservatorySource(config: { readonly kind?: 'static' }): StaticObservatorySource;
export function createObservatorySource(
  config: { readonly kind: 'live'; readonly options: LiveObservatorySourceOptions },
): LiveObservatorySource;
export function createObservatorySource(
  config: ObservatorySourceConfig = { kind: 'static' },
): ObservatoryDataSource {
  if (config.kind === undefined || config.kind === 'static') return new StaticObservatorySource();
  if (config.kind === 'live') return new LiveObservatorySource(config.options);
  throw new TypeError(`Unknown Observatory source: ${String((config as { kind?: unknown }).kind)}`);
}
