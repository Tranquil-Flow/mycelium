import {
  PRODUCT_API_PATHS,
  PRODUCT_OBSERVATORY_PROTOCOL,
  decodeProductObservatory,
  type ProductFreshness,
  type ProductObservatoryEnvelope,
  type ProductSourceMode,
} from '../../../app/contracts';
import {
  decodeObservatoryAdapterEvent,
  OBSERVATORY_STREAM_PROTOCOL,
} from '../../../data/observatoryEventProjection';
import {
  LiveObservatoryEventSource,
  type LiveObservatoryEventSourceOptions,
  type LiveObservatoryEventState,
  type ObservatoryEventListener,
} from '../../../data/observatoryEventSource';
import { StaticObservatorySource } from '../../../data/observatorySource';
import { deepFreeze } from '../../../model/runtime';
import {
  calculateObservatoryChangeSet,
  emptyObservatoryChangeSet,
  type ObservatoryChangeSet,
} from './changeSet';
import {
  productObservatoryRevision,
  projectFixtureObservatoryBundle,
  projectLiveObservatoryBundle,
  type ProductObservatoryMetrics,
  type ProductObservatoryProjection,
} from './projection';

export type ObservatoryControllerReasonCode =
  | 'event_source_disconnected'
  | 'invalid_projection'
  | 'non_monotonic_update'
  | 'snapshot_bootstrap_failed'
  | 'snapshot_stale'
  | 'replay_unavailable';

export interface ObservatoryControllerState {
  readonly source_mode: ProductSourceMode;
  readonly status: ProductObservatoryEnvelope['status'];
  /** Generation of the currently visible projection. */
  readonly generation: number | null;
  /** Highest accepted generation, including updates received while frozen. */
  readonly latest_generation: number | null;
  /** Highest accepted source cursor, including updates received while frozen. */
  readonly source_cursor: number | null;
  readonly visible_source_cursor: number | null;
  readonly projection: ProductObservatoryProjection | null;
  readonly envelope: ProductObservatoryEnvelope | null;
  /** Literal source value. Source mode/freshness must be checked separately by action authorities. */
  readonly route_ready: boolean;
  readonly freshness: ProductFreshness;
  readonly frozen: boolean;
  readonly replay_of_generation: number | null;
  readonly reason_code: ObservatoryControllerReasonCode | null;
  readonly change_set: ObservatoryChangeSet;
}

export interface ObservatoryControllerSource {
  loadInitial(): Promise<LiveObservatoryEventState> | LiveObservatoryEventState;
  getState(): LiveObservatoryEventState | null;
  subscribe(listener: ObservatoryEventListener): () => void;
}

export interface ProductObservatoryReplayFrame {
  readonly envelope: ProductObservatoryEnvelope;
  readonly projection: ProductObservatoryProjection | null;
  readonly route_ready: boolean;
  readonly source_cursor: number | null;
}

export interface ObservatoryControllerOptions {
  readonly source_mode?: ProductSourceMode;
  readonly source?: ObservatoryControllerSource;
  readonly source_options?: LiveObservatoryEventSourceOptions;
  readonly replay_frames?: readonly ProductObservatoryReplayFrame[];
  readonly replay_generation?: number;
  readonly stale_after_ms?: number;
  readonly history_limit?: number;
  readonly now?: () => number;
  readonly schedule?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout> | number;
  readonly cancelSchedule?: (handle: ReturnType<typeof setTimeout> | number) => void;
}

interface AcceptedFrame {
  readonly generation: number;
  readonly source_cursor: number | null;
  readonly projection: ProductObservatoryProjection | null;
  readonly route_ready: boolean;
  readonly observed_at_unix_ms: number | null;
  readonly metrics: ProductObservatoryMetrics;
  readonly status: ProductObservatoryEnvelope['status'];
  readonly content_revision: string | null;
}

const MAX_TIMER_DELAY_MS = 2_147_483_647;
const DEFAULT_STALE_AFTER_MS = 300_000;
const DEFAULT_HISTORY_LIMIT = 128;
const MAX_HISTORY_LIMIT = 1_024;
const browserSchedule = (callback: () => void, delayMs: number) => setTimeout(callback, delayMs);
const browserCancelSchedule = (handle: ReturnType<typeof setTimeout> | number) => clearTimeout(handle);

function boundedPositiveInteger(value: number, field: string, maximum?: number): number {
  if (!Number.isSafeInteger(value) || value < 1 || (maximum !== undefined && value > maximum)) {
    throw new TypeError(`${field} must be a bounded positive safe integer`);
  }
  return value;
}

function nullableCursor(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) >= -1 ? (value as number) : null;
}

function candidateGeneration(value: unknown): number | null {
  return Number.isSafeInteger(value) && (value as number) >= 1 ? (value as number) : null;
}

function cloneProductEnvelope(value: unknown): ProductObservatoryEnvelope {
  return decodeProductObservatory(value);
}

/**
 * Resolve the production build setting. Replay is explicit controller input and
 * can never be selected by an ambient deployment value.
 */
export function resolveProductObservatorySourceMode(value: unknown): 'fixture' | 'live' {
  // A product build is a live client by default. Recorded fixture mode must be
  // chosen explicitly so a missing build-time variable can never make a live
  // gateway look like a fixed, non-interactive demonstration.
  if (value === undefined || value === '') return 'live';
  if (value === 'fixture' || value === 'live') return value;
  throw new TypeError('Unknown product Observatory source mode');
}

/** The production live transport uses only the frozen same-origin product BFF paths. */
export function createProductObservatoryLiveSource(
  options: LiveObservatoryEventSourceOptions = {},
): LiveObservatoryEventSource {
  return new LiveObservatoryEventSource({
    ...options,
    snapshotUrl: options.snapshotUrl ?? PRODUCT_API_PATHS.observatory_snapshot,
    eventsUrl: options.eventsUrl ?? PRODUCT_API_PATHS.observatory_events,
  });
}

export function createProductObservatoryReplayFrame(
  envelope: ProductObservatoryEnvelope,
): ProductObservatoryReplayFrame {
  const decoded = cloneProductEnvelope(envelope);
  return deepFreeze({
    envelope: decoded,
    projection: null,
    // The frozen product envelope carries no qualification authority. Do not infer one.
    route_ready: false,
    source_cursor: null,
  });
}

export function createProductionObservatoryController(
  sourceMode: unknown = import.meta.env.VITE_OBSERVATORY_SOURCE_MODE,
  options: Omit<ObservatoryControllerOptions, 'source_mode'> = {},
): ObservatoryController {
  return new ObservatoryController({
    ...options,
    source_mode: resolveProductObservatorySourceMode(sourceMode),
  });
}

function frameFromReplay(value: ProductObservatoryReplayFrame): AcceptedFrame {
  const envelope = cloneProductEnvelope(value.envelope);
  const sourceCursor = value.source_cursor;
  if (
    sourceCursor !== null &&
    (!Number.isSafeInteger(sourceCursor) || sourceCursor < -1)
  ) {
    throw new TypeError('Replay source cursor must be a safe integer or null');
  }
  if (typeof value.route_ready !== 'boolean') {
    throw new TypeError('Replay route_ready must be literal boolean evidence');
  }
  // Public replay frames deliberately have no projection. Retained controller
  // history supplies already validated projections through a separate path.
  if (value.projection !== null) {
    throw new TypeError('External replay projections are not accepted');
  }
  return deepFreeze({
    generation: envelope.generation,
    source_cursor: sourceCursor,
    projection: null,
    route_ready: value.route_ready,
    observed_at_unix_ms: envelope.source.observed_at_unix_ms,
    metrics: envelope.metrics,
    status: envelope.status,
    content_revision: null,
  });
}

function replayFrameFromAccepted(frame: AcceptedFrame): AcceptedFrame {
  return frame;
}

export class ObservatoryController {
  private sourceMode: ProductSourceMode;
  private readonly providedSource: ObservatoryControllerSource | undefined;
  private readonly sourceOptions: LiveObservatoryEventSourceOptions | undefined;
  private source: ObservatoryControllerSource | null = null;
  private readonly configuredReplay = new Map<number, AcceptedFrame>();
  private readonly replayGeneration: number | undefined;
  private readonly staleAfterMs: number;
  private readonly historyLimit: number;
  private readonly now: () => number;
  private readonly schedule: NonNullable<ObservatoryControllerOptions['schedule']>;
  private readonly cancelSchedule: NonNullable<ObservatoryControllerOptions['cancelSchedule']>;
  private readonly listeners = new Set<(state: ObservatoryControllerState) => void>();
  private readonly history = new Map<number, AcceptedFrame>();

  private state: ObservatoryControllerState;
  private latestFrame: AcceptedFrame | null = null;
  private visibleFrame: AcceptedFrame | null = null;
  private status: ProductObservatoryEnvelope['status'] = 'connecting';
  private freshness: ProductFreshness = 'unknown';
  private reasonCode: ObservatoryControllerReasonCode | null = null;
  private frozen = false;
  private started = false;
  private startPromise: Promise<ObservatoryControllerState> | null = null;
  private unsubscribeSource: (() => void) | null = null;
  private freshnessTimer: ReturnType<typeof setTimeout> | number | null = null;
  private blockedGeneration: number | null = null;
  private blockedCursor: number | null = null;
  private changeSet: ObservatoryChangeSet = emptyObservatoryChangeSet(null, null);

  constructor(options: ObservatoryControllerOptions = {}) {
    this.sourceMode = options.source_mode ?? 'fixture';
    if (!['fixture', 'live', 'replay'].includes(this.sourceMode)) {
      throw new TypeError('Unknown product Observatory source mode');
    }
    this.providedSource = options.source;
    this.sourceOptions = options.source_options;
    this.replayGeneration = options.replay_generation;
    this.staleAfterMs = boundedPositiveInteger(
      options.stale_after_ms ?? DEFAULT_STALE_AFTER_MS,
      'stale_after_ms',
    );
    this.historyLimit = boundedPositiveInteger(
      options.history_limit ?? DEFAULT_HISTORY_LIMIT,
      'history_limit',
      MAX_HISTORY_LIMIT,
    );
    this.now = options.now ?? Date.now;
    this.schedule = options.schedule ?? browserSchedule;
    this.cancelSchedule = options.cancelSchedule ?? browserCancelSchedule;

    for (const replay of options.replay_frames ?? []) {
      const frame = frameFromReplay(replay);
      if (this.configuredReplay.has(frame.generation)) {
        throw new TypeError(`Duplicate Observatory replay generation ${frame.generation}`);
      }
      this.configuredReplay.set(frame.generation, frame);
    }
    if (this.configuredReplay.size > this.historyLimit) {
      throw new TypeError('Observatory replay frames exceed history_limit');
    }

    this.state = this.buildState();
  }

  getState(): ObservatoryControllerState {
    return this.state;
  }

  subscribe(listener: (state: ObservatoryControllerState) => void): () => void {
    this.listeners.add(listener);
    try {
      listener(this.state);
    } catch {
      // Read consumers cannot perturb transport state.
    }
    return () => this.listeners.delete(listener);
  }

  start(): Promise<ObservatoryControllerState> {
    this.startPromise ??= this.startInternal();
    return this.startPromise;
  }

  stop(): void {
    this.unsubscribeSource?.();
    this.unsubscribeSource = null;
    this.cancelFreshnessTimer();
    if (this.sourceMode === 'live' && this.latestFrame !== null) {
      this.markBoundary('event_source_disconnected', null, null);
    }
  }

  freeze(): ObservatoryControllerState {
    if (this.sourceMode === 'replay' || this.frozen) return this.state;
    this.frozen = true;
    this.publish();
    return this.state;
  }

  unfreeze(): ObservatoryControllerState {
    if (!this.frozen) return this.state;
    const previous = this.visibleFrame;
    this.frozen = false;
    if (this.latestFrame !== null && this.latestFrame !== this.visibleFrame) {
      this.visibleFrame = this.latestFrame;
      this.updateChangeSet(previous, this.visibleFrame);
    }
    this.publish();
    return this.state;
  }

  enterReplay(generation?: number): ObservatoryControllerState {
    this.unsubscribeSource?.();
    this.unsubscribeSource = null;
    this.cancelFreshnessTimer();
    this.frozen = false;
    this.sourceMode = 'replay';
    const selected = generation ?? this.latestHistoryGeneration();
    if (selected === null) {
      this.status = 'disconnected';
      this.freshness = 'unknown';
      this.reasonCode = 'replay_unavailable';
      this.latestFrame = null;
      this.visibleFrame = null;
      this.changeSet = emptyObservatoryChangeSet(null, null);
      this.publish();
      return this.state;
    }
    return this.selectReplay(selected);
  }

  selectReplay(generation: number): ObservatoryControllerState {
    if (this.sourceMode !== 'replay') {
      throw new TypeError('Replay selection requires replay source mode');
    }
    if (!Number.isSafeInteger(generation) || generation < 0) {
      throw new TypeError('Replay generation must be a non-negative safe integer');
    }
    const frame = this.history.get(generation) ?? this.configuredReplay.get(generation);
    if (frame === undefined) {
      throw new RangeError(`Observatory replay generation ${generation} is unavailable`);
    }
    const previous = this.visibleFrame;
    this.latestFrame = replayFrameFromAccepted(frame);
    this.visibleFrame = this.latestFrame;
    this.status = frame.status;
    this.freshness = 'replay';
    this.reasonCode = null;
    this.blockedGeneration = null;
    this.blockedCursor = null;
    this.updateChangeSet(previous, this.visibleFrame);
    this.publish();
    return this.state;
  }

  private async startInternal(): Promise<ObservatoryControllerState> {
    if (this.started) return this.state;
    this.started = true;
    if (this.sourceMode === 'fixture') {
      this.startFixture();
      return this.state;
    }
    if (this.sourceMode === 'replay') {
      const generation = this.replayGeneration ?? this.latestConfiguredReplayGeneration();
      if (generation === null) {
        this.status = 'disconnected';
        this.freshness = 'unknown';
        this.reasonCode = 'replay_unavailable';
        this.publish();
        return this.state;
      }
      return this.selectReplay(generation);
    }

    this.source = this.providedSource ?? createProductObservatoryLiveSource(this.sourceOptions);
    try {
      const initial = await this.source.loadInitial();
      this.acceptLiveState(initial, true);
    } catch {
      this.status = 'disconnected';
      this.freshness = 'unknown';
      this.reasonCode = 'snapshot_bootstrap_failed';
      this.publish();
    }
    try {
      this.unsubscribeSource = this.source.subscribe(this.onSourceState);
    } catch {
      this.markBoundary('event_source_disconnected', null, null);
    }
    return this.state;
  }

  private startFixture(): void {
    const source = new StaticObservatorySource();
    const fixture = source.loadInitial();
    const projection = projectFixtureObservatoryBundle(fixture.bundle);
    const frame: AcceptedFrame = deepFreeze({
      generation: fixture.generation,
      source_cursor: null,
      projection,
      route_ready: projection.route_ready,
      observed_at_unix_ms: null,
      metrics: projection.metrics,
      status: fixture.status,
      content_revision: productObservatoryRevision(projection.source),
    });
    this.latestFrame = frame;
    this.visibleFrame = frame;
    this.status = 'connected';
    this.freshness = 'fixture';
    this.reasonCode = null;
    this.changeSet = emptyObservatoryChangeSet(null, frame.generation);
    this.retain(frame);
    this.publish();
  }

  private readonly onSourceState: ObservatoryEventListener = (next): void => {
    this.acceptLiveState(next, false);
  };

  private acceptLiveState(next: LiveObservatoryEventState, bootstrap: boolean): void {
    const generation = candidateGeneration(next?.generation);
    const cursor = nullableCursor(next?.source_cursor);
    if (generation === null || cursor === null) {
      this.markBoundary('invalid_projection', generation, cursor);
      return;
    }

    const currentGeneration = this.latestFrame?.generation ?? null;
    const currentCursor = this.latestFrame?.source_cursor ?? null;
    const exactCurrent = generation === currentGeneration && cursor === currentCursor;
    const staleCandidate =
      currentGeneration !== null &&
      currentCursor !== null &&
      generation <= currentGeneration &&
      cursor <= currentCursor &&
      !exactCurrent;
    if (staleCandidate) return;

    if (
      this.blockedGeneration !== null &&
      this.blockedCursor !== null &&
      (generation <= this.blockedGeneration || cursor <= this.blockedCursor)
    ) {
      return;
    }

    if (
      currentGeneration !== null &&
      currentCursor !== null &&
      !exactCurrent &&
      (generation <= currentGeneration || cursor <= currentCursor)
    ) {
      this.markBoundary('non_monotonic_update', generation, cursor);
      return;
    }

    if (
      next.source_mode !== 'live' ||
      !['connecting', 'connected', 'disconnected'].includes(next.status) ||
      !['current', 'stale'].includes(next.freshness) ||
      typeof next.route_ready !== 'boolean'
    ) {
      this.markBoundary('invalid_projection', generation, cursor);
      return;
    }

    let projection: ProductObservatoryProjection;
    try {
      const decoded = decodeObservatoryAdapterEvent({
        protocol: OBSERVATORY_STREAM_PROTOCOL,
        generation,
        bundle: next.projection,
      });
      projection = projectLiveObservatoryBundle(decoded.bundle);
      if (
        projection.route_ready !== next.route_ready ||
        decoded.bundle.snapshot.source_cursor !== cursor
      ) {
        throw new TypeError('source state does not match its validated projection');
      }
    } catch {
      this.markBoundary('invalid_projection', generation, cursor);
      return;
    }

    const contentRevision = productObservatoryRevision(projection.source);
    if (exactCurrent) {
      if (this.latestFrame?.content_revision !== contentRevision) {
        this.markBoundary('non_monotonic_update', generation, cursor);
        return;
      }
      if (next.status === 'disconnected') {
        this.markBoundary('event_source_disconnected', generation, cursor);
        return;
      }
      this.status = next.status;
      this.freshness = next.freshness;
      this.reasonCode = next.freshness === 'stale' ? 'snapshot_stale' : null;
      this.replaceLatestStatus(next.status);
      this.armFreshnessTimer();
      this.publish();
      return;
    }

    if (
      this.latestFrame?.observed_at_unix_ms !== null &&
      this.latestFrame?.observed_at_unix_ms !== undefined &&
      projection.observed_at_unix_ms !== null &&
      projection.observed_at_unix_ms < this.latestFrame.observed_at_unix_ms
    ) {
      this.markBoundary('non_monotonic_update', generation, cursor);
      return;
    }

    this.blockedGeneration = null;
    this.blockedCursor = null;
    const frame: AcceptedFrame = deepFreeze({
      generation,
      source_cursor: cursor,
      projection,
      route_ready: projection.route_ready,
      observed_at_unix_ms: projection.observed_at_unix_ms,
      metrics: projection.metrics,
      status: next.status,
      content_revision: contentRevision,
    });
    const previousVisible = this.visibleFrame;
    this.latestFrame = frame;
    if (!this.frozen) {
      this.visibleFrame = frame;
      this.updateChangeSet(bootstrap ? null : previousVisible, frame);
    }
    this.status = next.status;
    this.freshness = next.status === 'disconnected' ? 'stale' : next.freshness;
    this.reasonCode =
      next.status === 'disconnected'
        ? 'event_source_disconnected'
        : next.freshness === 'stale'
          ? 'snapshot_stale'
          : null;
    this.retain(frame);
    if (next.status === 'disconnected') {
      this.blockedGeneration = generation;
      this.blockedCursor = cursor;
    }
    this.armFreshnessTimer();
    this.publish();
  }

  private replaceLatestStatus(status: AcceptedFrame['status']): void {
    if (this.latestFrame === null || this.latestFrame.status === status) return;
    const replacement: AcceptedFrame = deepFreeze({ ...this.latestFrame, status });
    const visibleWasLatest = this.visibleFrame === this.latestFrame;
    this.latestFrame = replacement;
    if (visibleWasLatest) this.visibleFrame = replacement;
    this.retain(replacement);
  }

  private markBoundary(
    reason: Exclude<ObservatoryControllerReasonCode, 'snapshot_stale' | 'replay_unavailable'>,
    generation: number | null,
    cursor: number | null,
  ): void {
    const currentGeneration = this.latestFrame?.generation ?? null;
    const currentCursor = this.latestFrame?.source_cursor ?? null;
    this.blockedGeneration = Math.max(
      this.blockedGeneration ?? -1,
      generation ?? currentGeneration ?? -1,
    );
    this.blockedCursor = Math.max(this.blockedCursor ?? -1, cursor ?? currentCursor ?? -1);
    this.status = 'disconnected';
    this.freshness = this.latestFrame === null ? 'unknown' : 'stale';
    this.reasonCode = reason;
    this.publish();
  }

  private updateChangeSet(previous: AcceptedFrame | null, next: AcceptedFrame | null): void {
    if (next === null) {
      this.changeSet = emptyObservatoryChangeSet(previous?.generation ?? null, null);
      return;
    }
    if (previous?.projection === null || previous === null || next.projection === null) {
      this.changeSet = emptyObservatoryChangeSet(previous?.generation ?? null, next.generation);
      return;
    }
    this.changeSet = calculateObservatoryChangeSet(
      previous.projection.entities,
      next.projection.entities,
      previous.generation,
      next.generation,
    );
  }

  private retain(frame: AcceptedFrame): void {
    this.history.delete(frame.generation);
    this.history.set(frame.generation, frame);
    while (this.history.size > this.historyLimit) {
      const oldest = this.history.keys().next().value as number | undefined;
      if (oldest === undefined) break;
      this.history.delete(oldest);
    }
  }

  private latestHistoryGeneration(): number | null {
    const generations = [...this.history.keys(), ...this.configuredReplay.keys()];
    return generations.length === 0 ? null : Math.max(...generations);
  }

  private latestConfiguredReplayGeneration(): number | null {
    const generations = [...this.configuredReplay.keys()];
    return generations.length === 0 ? null : Math.max(...generations);
  }

  private armFreshnessTimer(): void {
    this.cancelFreshnessTimer();
    if (
      this.sourceMode !== 'live' ||
      this.latestFrame?.observed_at_unix_ms === null ||
      this.latestFrame?.observed_at_unix_ms === undefined ||
      this.freshness === 'stale'
    ) {
      return;
    }
    const expiresAt = this.latestFrame.observed_at_unix_ms + this.staleAfterMs;
    const delay = Math.max(0, expiresAt - this.now() + 1);
    this.freshnessTimer = this.schedule(
      () => {
        this.freshnessTimer = null;
        const observedAt = this.latestFrame?.observed_at_unix_ms;
        if (observedAt === null || observedAt === undefined || this.sourceMode !== 'live') return;
        if (this.now() - observedAt > this.staleAfterMs) {
          this.freshness = 'stale';
          if (this.status !== 'disconnected') this.reasonCode = 'snapshot_stale';
          this.publish();
        } else {
          this.armFreshnessTimer();
        }
      },
      Math.min(delay, MAX_TIMER_DELAY_MS),
    );
  }

  private cancelFreshnessTimer(): void {
    if (this.freshnessTimer === null) return;
    this.cancelSchedule(this.freshnessTimer);
    this.freshnessTimer = null;
  }

  private buildEnvelope(): ProductObservatoryEnvelope | null {
    if (this.visibleFrame === null) return null;
    const freshness: ProductFreshness =
      this.sourceMode === 'fixture'
        ? 'fixture'
        : this.sourceMode === 'replay'
          ? 'replay'
          : this.freshness;
    return cloneProductEnvelope({
      protocol: PRODUCT_OBSERVATORY_PROTOCOL,
      generation: this.visibleFrame.generation,
      status: this.status,
      source: {
        mode: this.sourceMode,
        freshness,
        observed_at_unix_ms: this.visibleFrame.observed_at_unix_ms,
        replay_of_generation:
          this.sourceMode === 'replay' ? this.visibleFrame.generation : null,
      },
      metrics: this.visibleFrame.metrics,
    });
  }

  private buildState(): ObservatoryControllerState {
    const envelope = this.buildEnvelope();
    return deepFreeze({
      source_mode: this.sourceMode,
      status: this.status,
      generation: this.visibleFrame?.generation ?? null,
      latest_generation: this.latestFrame?.generation ?? null,
      source_cursor: this.latestFrame?.source_cursor ?? null,
      visible_source_cursor: this.visibleFrame?.source_cursor ?? null,
      projection: this.visibleFrame?.projection ?? null,
      envelope,
      route_ready: this.visibleFrame?.route_ready ?? false,
      freshness:
        this.sourceMode === 'fixture'
          ? this.visibleFrame === null
            ? 'unknown'
            : 'fixture'
          : this.sourceMode === 'replay'
            ? this.visibleFrame === null
              ? 'unknown'
              : 'replay'
            : this.freshness,
      frozen: this.frozen,
      replay_of_generation:
        this.sourceMode === 'replay' ? this.visibleFrame?.generation ?? null : null,
      reason_code: this.reasonCode,
      change_set: this.changeSet,
    });
  }

  private publish(): void {
    this.state = this.buildState();
    for (const listener of this.listeners) {
      try {
        listener(this.state);
      } catch {
        // Read consumers cannot perturb controller state.
      }
    }
  }
}
