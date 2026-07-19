import type {
  EvidenceRoute,
  EvidenceRouteStage,
  EvidenceSnapshot,
} from '../../model/types';

export type StrategyMetricKey =
  | 'combinedTokensPerSecond'
  | 'prefillTokensPerSecond'
  | 'decodeTokensPerSecond'
  | 'singleRequestTokensPerSecond'
  | 'decodeLatencyMsPerToken';

export interface RankedStrategy {
  readonly rank: number;
  readonly route: EvidenceRoute;
}

export interface ComparedMetric {
  readonly primary: number;
  readonly baseline: number;
  readonly delta: number;
  readonly deltaLabel: string;
  readonly lowerIsBetter: boolean;
}

export interface StrategyComparison {
  readonly primaryId: string;
  readonly baselineId: string;
  readonly metrics: Readonly<Record<StrategyMetricKey, ComparedMetric>>;
}

export interface PlanAllocation {
  readonly stageId: string;
  readonly nodeId: string;
  readonly humanRange: string;
  readonly exactRange: string;
  readonly layerCount: number;
  readonly memoryGb: number;
  readonly decodeStageMs: number;
  readonly prefillStageMs: number;
}

export interface PlanAssumption {
  readonly label: string;
  readonly value: string;
  readonly qualification: 'supplied' | 'not_supplied';
}

export interface PlanAnalysis {
  readonly route: EvidenceRoute;
  readonly allocations: readonly PlanAllocation[];
  readonly alternatives: readonly EvidenceRoute[];
  readonly bottleneck: {
    readonly stage: EvidenceRouteStage;
    readonly decodeStageMs: number;
  };
  readonly assumptions: readonly PlanAssumption[];
  readonly pruningTrace: {
    readonly state: 'not_supplied';
    readonly reason: string;
  };
}

const comparisonMetrics: readonly {
  readonly key: StrategyMetricKey;
  readonly lowerIsBetter: boolean;
}[] = [
  { key: 'combinedTokensPerSecond', lowerIsBetter: false },
  { key: 'prefillTokensPerSecond', lowerIsBetter: false },
  { key: 'decodeTokensPerSecond', lowerIsBetter: false },
  { key: 'singleRequestTokensPerSecond', lowerIsBetter: false },
  { key: 'decodeLatencyMsPerToken', lowerIsBetter: true },
];

function signed(value: number): string {
  if (Object.is(value, -0) || Math.abs(value) < 0.000_000_1) return '0.0';
  const magnitude = Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  return `${value > 0 ? '+' : '−'}${magnitude}`;
}

function metric(route: EvidenceRoute, key: StrategyMetricKey): number {
  return route.metrics[key].value;
}

export function rankStrategies(routes: readonly EvidenceRoute[]): readonly RankedStrategy[] {
  return Object.freeze(
    [...routes]
      .sort((left, right) => {
        const throughputDelta =
          right.metrics.combinedTokensPerSecond.value - left.metrics.combinedTokensPerSecond.value;
        return throughputDelta === 0 ? left.id.localeCompare(right.id) : throughputDelta;
      })
      .map((route, index) => Object.freeze({ rank: index + 1, route })),
  );
}

export function compareStrategies(
  primary: EvidenceRoute,
  baseline: EvidenceRoute,
): StrategyComparison {
  const metrics = Object.fromEntries(
    comparisonMetrics.map(({ key, lowerIsBetter }) => {
      const primaryValue = metric(primary, key);
      const baselineValue = metric(baseline, key);
      const delta = primaryValue - baselineValue;
      return [
        key,
        Object.freeze({
          primary: primaryValue,
          baseline: baselineValue,
          delta,
          deltaLabel: signed(delta),
          lowerIsBetter,
        }),
      ];
    }),
  ) as unknown as Readonly<Record<StrategyMetricKey, ComparedMetric>>;

  return Object.freeze({
    primaryId: primary.id,
    baselineId: baseline.id,
    metrics: Object.freeze(metrics),
  });
}

function stageDecodeMs(stage: EvidenceRouteStage): number {
  return stage.metrics.decodeComputeMs.value + stage.metrics.decodeOutgoingMs.value;
}

function stagePrefillMs(stage: EvidenceRouteStage): number {
  return stage.metrics.prefillComputeMs.value + stage.metrics.prefillOutgoingMs.value;
}

function stageMemoryGb(stage: EvidenceRouteStage): number {
  return stage.memory.weightsGb + stage.memory.kvCacheGb;
}

export function analyzePlan(snapshot: EvidenceSnapshot, selectedId: string): PlanAnalysis {
  const route = snapshot.routes.find((candidate) => candidate.id === selectedId) ?? snapshot.routes[0];
  if (route === undefined || route.stages.length === 0) {
    throw new TypeError('Plan analysis requires at least one route with one stage');
  }
  const bottleneckStage = route.stages.reduce((slowest, candidate) =>
    stageDecodeMs(candidate) > stageDecodeMs(slowest) ? candidate : slowest,
  );

  return Object.freeze({
    route,
    allocations: Object.freeze(
      route.stages.map((stage) =>
        Object.freeze({
          stageId: stage.id,
          nodeId: stage.nodeId,
          humanRange: `L${stage.startLayer}–L${stage.endLayerExclusive - 1}`,
          exactRange: `[${stage.startLayer},${stage.endLayerExclusive})`,
          layerCount: stage.layerCount,
          memoryGb: stageMemoryGb(stage),
          decodeStageMs: stageDecodeMs(stage),
          prefillStageMs: stagePrefillMs(stage),
        }),
      ),
    ),
    alternatives: Object.freeze(snapshot.routes.filter((candidate) => candidate.id !== route.id)),
    bottleneck: Object.freeze({
      stage: bottleneckStage,
      decodeStageMs: stageDecodeMs(bottleneckStage),
    }),
    assumptions: Object.freeze([
      Object.freeze({
        label: 'Model',
        value: `${snapshot.model.id} · ${snapshot.model.numLayers} layers · hidden ${snapshot.model.hiddenSize}`,
        qualification: 'supplied' as const,
      }),
      Object.freeze({
        label: 'Workload',
        value: `${snapshot.workload.concurrentRequests} analytically concurrent requests · ${snapshot.workload.contextWindow} context · ${snapshot.workload.outputTokens} output tokens`,
        qualification: 'supplied' as const,
      }),
      Object.freeze({
        label: 'KV capacity',
        value: `context fraction ${snapshot.workload.contextFractionPerRequest} · safety multiplier ${snapshot.workload.kvSafetyMultiplier}`,
        qualification: 'supplied' as const,
      }),
      Object.freeze({
        label: 'Microbatch size',
        value: 'Not supplied. Concurrent requests are not substituted for microbatch B.',
        qualification: 'not_supplied' as const,
      }),
      Object.freeze({
        label: 'Runtime activation',
        value: 'Not supplied. Planned routes are not active-route evidence.',
        qualification: 'not_supplied' as const,
      }),
      Object.freeze({
        label: 'Disk offload',
        value: 'Not supplied by this projection; no active offload is implied.',
        qualification: 'not_supplied' as const,
      }),
    ]),
    pruningTrace: Object.freeze({
      state: 'not_supplied' as const,
      reason:
        'A pruning trace is not present in the browser evidence projection. No removed node or before/after result is reconstructed.',
    }),
  });
}
