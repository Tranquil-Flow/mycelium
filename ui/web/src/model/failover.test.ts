import { describe, expect, it } from 'vitest';
import fixture from '../../../tests/fixtures/failover/failover-scenarios.json';
import scenario from '../../../tests/fixtures/source/hypothetical-six-node.json';
import { adaptFailoverScenarios, projectFailoverOverlay } from './failover';

const validationContext = {
  knownNodeIds: scenario.nodes.map((node) => node.node_id),
  numLayers: scenario.model.num_layers,
};

describe('failover evidence', () => {
  const incidents = adaptFailoverScenarios(fixture, validationContext);

  it('supports stable drain, active failover, and circuit break without conflation', () => {
    expect(incidents.map((incident) => incident.mode)).toEqual([
      'stable_drain',
      'active_failover',
      'circuit_break',
    ]);
    expect(incidents[0].oldRoute.state).toBe('draining');
    expect(incidents[2].newRoute).toBeNull();
  });

  it('keeps old and replacement generations visible during active failover', () => {
    const overlay = projectFailoverOverlay(incidents[1]);

    expect(overlay.routes).toHaveLength(2);
    expect(overlay.routes.map((route) => route.generation)).toEqual([42, 44]);
    expect(overlay.failedPeerId).toBe('birch-m4pro');
    expect(overlay.checkpointLabel).toContain('layer 21');
    expect(overlay.checkpointLabel).toContain('token 37');
  });

  it('does not fabricate a replacement route for circuit break', () => {
    const overlay = projectFailoverOverlay(incidents[2]);

    expect(overlay.routes).toHaveLength(1);
    expect(overlay.outcome).toBe('Output rejected · 503 · no reroute claimed');
  });

  it('rejects internally incoherent routes, triggers, checkpoints, and terminal states', () => {
    const triggerStillPresent = structuredClone(fixture);
    triggerStillPresent.scenarios[1].new_route!.nodes[2] = 'birch-m4pro';
    expect(() => adaptFailoverScenarios(triggerStillPresent, validationContext)).toThrow(
      /new_route\.nodes/,
    );

    const unknownNode = structuredClone(fixture);
    unknownNode.scenarios[0].new_route!.nodes[2] = 'unobserved-peer';
    expect(() => adaptFailoverScenarios(unknownNode, validationContext)).toThrow(/known node/);

    const impossibleCheckpoint = structuredClone(fixture);
    impossibleCheckpoint.scenarios[1].cutover.last_good_layer = scenario.model.num_layers;
    expect(() => adaptFailoverScenarios(impossibleCheckpoint, validationContext)).toThrow(
      /last_good_layer/,
    );

    const wrongTerminal = structuredClone(fixture);
    wrongTerminal.scenarios[1].transitions.at(-1)!.state = 'ABORTED';
    expect(() => adaptFailoverScenarios(wrongTerminal, validationContext)).toThrow(
      /terminal transition/,
    );
  });

  it('returns deeply immutable incidents', () => {
    expect(Object.isFrozen(incidents)).toBe(true);
    expect(Object.isFrozen(incidents[1].newRoute)).toBe(true);
    expect(Object.isFrozen(incidents[1].transitions)).toBe(true);
  });
});
