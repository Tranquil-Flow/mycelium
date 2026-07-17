import { describe, expect, it } from 'vitest';
import scenario from '../../../tests/fixtures/source/hypothetical-six-node.json';
import report from '../../../tests/fixtures/source/planner-simulation.json';
import geo from '../../../tests/fixtures/source/synthetic-geo.json';
import manifest from '../../../tests/fixtures/source/ui-fixture-manifest.json';
import { adaptSimulator } from '../model/adapter';
import { layoutGraph, projectRouteGraph, type GraphIR } from './graph';

describe('semantic graph projection', () => {
  const snapshot = adaptSimulator(scenario, report, geo, manifest);
  const graph = projectRouteGraph(snapshot, 'global_best_shortest_subset');

  it('preserves the decode closure as a semantic back-edge', () => {
    expect(graph.nodes).toHaveLength(4);
    expect(graph.edges).toHaveLength(4);
    expect(graph.edges.at(-1)).toMatchObject({
      kind: 'decode_closure',
      source: 'ember-laptop',
      target: 'cedar-3060',
    });
  });

  it('lays out a deterministic ring without changing semantic direction', async () => {
    const scene = await layoutGraph(graph, 'ring');
    const closure = scene.edges.find((edge) => edge.kind === 'decode_closure');

    expect(new Set(scene.nodes.map((node) => `${node.x},${node.y}`)).size).toBe(4);
    expect(closure).toMatchObject({ source: 'ember-laptop', target: 'cedar-3060' });
  });

  it('projects pipeline mode as a true DAG while keeping closure semantics in GraphIR', async () => {
    const scene = await layoutGraph(graph, 'pipeline');

    expect(scene.edges).toHaveLength(scene.nodes.length - 1);
    expect(scene.edges.every((edge) => edge.kind === 'stage_handoff')).toBe(true);
    expect(graph.edges.some((edge) => edge.kind === 'decode_closure')).toBe(true);
  });

  it('keeps a 128-stage pipeline finite, ordered, and complete', async () => {
    const seedNode = graph.nodes[0];
    const nodes = Array.from({ length: 128 }, (_, order) => ({
      ...seedNode,
      id: `scale-node-${order}`,
      nodeId: `scale-node-${order}`,
      stageId: `scale-stage-${order}`,
      order,
      stage: {
        ...seedNode.stage,
        id: `scale-stage-${order}`,
        nodeId: `scale-node-${order}`,
      },
    }));
    const handoffs = nodes.slice(0, -1).map((source, order) => {
      const target = nodes[order + 1];
      return {
        id: `scale:handoff:${order}`,
        kind: 'stage_handoff' as const,
        order,
        source: source.id,
        target: target.id,
        sourceStageId: source.stageId,
        targetStageId: target.stageId,
        link: null,
      };
    });
    const scaleGraph: GraphIR = {
      ...graph,
      id: 'scale-128',
      routeId: 'scale-128',
      nodes,
      edges: [
        ...handoffs,
        {
          id: 'scale:decode-closure',
          kind: 'decode_closure',
          order: handoffs.length,
          source: nodes.at(-1)!.id,
          target: nodes[0].id,
          sourceStageId: nodes.at(-1)!.stageId,
          targetStageId: nodes[0].stageId,
          link: null,
        },
      ],
    };

    const scene = await layoutGraph(scaleGraph, 'pipeline');

    expect(scene.nodes).toHaveLength(128);
    expect(scene.edges).toHaveLength(127);
    expect(scene.nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(true);
    expect(new Set(scene.nodes.map((node) => node.x)).size).toBe(128);
    expect(scene.nodes.every((node, index) => index === 0 || node.x > scene.nodes[index - 1].x)).toBe(true);
  });

  it('prefers exact directed evidence over an earlier reverse bidirectional record', () => {
    const reverse = graph.edges[0].link;
    expect(reverse).toBeDefined();
    const exact = {
      ...reverse!,
      id: 'cedar-3060->atlas-4090:exact',
      source: 'cedar-3060',
      target: 'atlas-4090',
      metrics: {
        ...reverse!.metrics,
        roundTripTimeMs: { value: 7, provenance: 'synthetic' as const },
      },
    };
    const projected = projectRouteGraph(
      { ...snapshot, links: [reverse!, exact, ...snapshot.links] },
      'global_best_shortest_subset',
    );

    expect(projected.edges[0].link?.id).toBe(exact.id);
    expect(projected.edges[0].link?.metrics.roundTripTimeMs.value).toBe(7);
  });

  it('puts unknown-location nodes in a tray instead of inventing coordinates', async () => {
    const randomGraph = projectRouteGraph(snapshot, 'random_ring');
    const scene = await layoutGraph(randomGraph, 'geo');
    const fern = scene.nodes.find((node) => node.id === 'fern-mobile');

    expect(fern?.locationUnknown).toBe(true);
    expect(fern?.x).toBeGreaterThan(850);
  });
});
