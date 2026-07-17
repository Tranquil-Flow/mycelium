import { describe, expect, it } from 'vitest';
import scenario from '../../../tests/fixtures/source/hypothetical-six-node.json';
import report from '../../../tests/fixtures/source/planner-simulation.json';
import geo from '../../../tests/fixtures/source/synthetic-geo.json';
import manifest from '../../../tests/fixtures/source/ui-fixture-manifest.json';
import { adaptSimulator } from './adapter';

describe('adaptSimulator', () => {
  it('normalizes inclusive simulator ranges to half-open ranges with provenance', () => {
    const snapshot = adaptSimulator(scenario, report, geo, manifest);
    const route = snapshot.routes.find((item) => item.id === 'global_best_shortest_subset');

    expect(snapshot.protocol).toBe('mycelium.ui_evidence_snapshot.v1');
    expect(snapshot.nodes).toHaveLength(6);
    expect(route?.stages.map((stage) => [stage.startLayer, stage.endLayerExclusive])).toEqual([
      [0, 5],
      [5, 22],
      [22, 25],
      [25, 28],
    ]);
    expect(route?.metrics.combinedTokensPerSecond.provenance).toBe('synthetic');
  });

  it('keeps missing locations unknown and synthetic locations explicitly synthetic', () => {
    const snapshot = adaptSimulator(scenario, report, geo, manifest);
    const fern = snapshot.nodes.find((node) => node.id === 'fern-mobile');
    const cedar = snapshot.nodes.find((node) => node.id === 'cedar-3060');

    expect(fern?.location.state).toBe('unknown');
    expect(cedar?.location.state).toBe('known');
    expect(cedar?.location.provenance).toBe('synthetic');
    expect(snapshot.claimBoundary).toContain('offline');
  });

  it('rejects a report whose model dimensions or workload differ from the scenario', () => {
    const wrongModel = structuredClone(report);
    wrongModel.model.hidden_size += 1;
    expect(() => adaptSimulator(scenario, wrongModel, geo, manifest)).toThrow(/hidden_size/);

    const wrongWorkload = structuredClone(report);
    wrongWorkload.workload.output_tokens += 1;
    expect(() => adaptSimulator(scenario, wrongWorkload, geo, manifest)).toThrow(/output_tokens/);

    const unlabeledCapture = structuredClone(manifest);
    unlabeledCapture.provenance = 'measured';
    expect(() => adaptSimulator(scenario, report, geo, unlabeledCapture)).toThrow(/manifest\.provenance/);
  });

  it('returns a deeply immutable evidence capture', () => {
    const snapshot = adaptSimulator(scenario, report, geo, manifest);

    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(Object.isFrozen(snapshot.routes)).toBe(true);
    expect(Object.isFrozen(snapshot.routes[0].stages[0].metrics)).toBe(true);
  });
});
