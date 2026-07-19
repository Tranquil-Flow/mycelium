import { describe, expect, it } from 'vitest';
import { loadStaticObservatoryBundle } from '../../data/observatorySource';
import {
  analyzePlan,
  compareStrategies,
  rankStrategies,
} from './planModel';

describe('plan analysis', () => {
  const { snapshot } = loadStaticObservatoryBundle();

  it('ranks every strategy by modeled combined throughput without changing source routes', () => {
    const ranked = rankStrategies(snapshot.routes);

    expect(ranked).toHaveLength(snapshot.routes.length);
    expect(ranked.map((item) => item.rank)).toEqual(ranked.map((_, index) => index + 1));
    expect(ranked.map((item) => item.route.metrics.combinedTokensPerSecond.value)).toEqual(
      [...ranked]
        .map((item) => item.route.metrics.combinedTokensPerSecond.value)
        .sort((left, right) => right - left),
    );
    expect(snapshot.routes[0].id).toBe('global_best_shortest_subset');
  });

  it('creates explicit signed deltas for synchronized two-strategy comparison', () => {
    const ranked = rankStrategies(snapshot.routes);
    const comparison = compareStrategies(ranked[0].route, ranked[1].route);

    expect(comparison.primaryId).toBe(ranked[0].route.id);
    expect(comparison.baselineId).toBe(ranked[1].route.id);
    expect(comparison.metrics.combinedTokensPerSecond.delta).toBeCloseTo(
      ranked[0].route.metrics.combinedTokensPerSecond.value -
        ranked[1].route.metrics.combinedTokensPerSecond.value,
    );
    expect(comparison.metrics.decodeLatencyMsPerToken.lowerIsBetter).toBe(true);
    expect(comparison.metrics.decodeLatencyMsPerToken.deltaLabel).toMatch(/^[+−0]/);
  });

  it('reports route allocation, alternatives, bottleneck, assumptions, and unavailable pruning honestly', () => {
    const ranked = rankStrategies(snapshot.routes);
    const analysis = analyzePlan(snapshot, ranked[0].route.id);
    const maximumDecodeStage = Math.max(
      ...analysis.route.stages.map(
        (stage) => stage.metrics.decodeComputeMs.value + stage.metrics.decodeOutgoingMs.value,
      ),
    );

    expect(analysis.allocations[0].exactRange).toMatch(/^\[\d+,\d+\)$/);
    expect(analysis.alternatives.map((route) => route.id)).not.toContain(analysis.route.id);
    expect(analysis.bottleneck.decodeStageMs).toBeCloseTo(maximumDecodeStage);
    expect(analysis.assumptions.some((item) => item.label === 'Microbatch size')).toBe(true);
    expect(analysis.pruningTrace.state).toBe('not_supplied');
    expect(analysis.pruningTrace.reason).toMatch(/not present/i);
  });
});
