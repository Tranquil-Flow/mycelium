import { describe, expect, it } from 'vitest';
import scenario from '../../../tests/fixtures/source/hypothetical-six-node.json';
import report from '../../../tests/fixtures/source/planner-simulation.json';
import geo from '../../../tests/fixtures/source/synthetic-geo.json';
import manifest from '../../../tests/fixtures/source/ui-fixture-manifest.json';
import { adaptSimulator } from '../model/adapter';
import type { EvidenceSnapshot } from '../model/types';
import {
  layoutGraph,
  projectRouteGraph,
  stronglyConnectedComponents,
  topologySignature,
  type GraphIR,
} from './graph';
import { createPerformanceGraphFixture } from './performanceFixtures';

const snapshot = adaptSimulator(scenario, report, geo, manifest);

describe('truthful semantic graph projection', () => {
  it('orders pipeline stages by exact half-open boundaries and exposes boundary ports', async () => {
    const route = snapshot.routes.find((candidate) => candidate.id === 'global_best_shortest_subset')!;
    const reversedSnapshot: EvidenceSnapshot = {
      ...snapshot,
      routes: [{ ...route, stages: [...route.stages].reverse() }, ...snapshot.routes.filter((item) => item.id !== route.id)],
    };

    const graph = projectRouteGraph(reversedSnapshot, route.id);
    const scene = await layoutGraph(graph, 'pipeline');

    expect(graph.nodes.map((node) => node.stage?.startLayer)).toEqual([0, 5, 22, 25]);
    expect(scene.nodes.map((node) => node.startBoundary)).toEqual([0, 5, 22, 25]);
    expect(scene.nodes.map((node) => node.endBoundary)).toEqual([5, 22, 25, 28]);
    expect(scene.nodes.map((node) => node.layerSpanWidth)).toEqual([120, 408, 72, 72]);
    expect(scene.nodes.every((node, index) => index === 0 || node.x > scene.nodes[index - 1].x)).toBe(true);
    expect(scene.edges.find((edge) => edge.kind === 'decode_closure')).toMatchObject({
      sourceBoundary: 28,
      targetBoundary: 0,
      path: 'outer',
    });
  });

  it('keeps repeated devices as distinct stage instances instead of rejecting disjoint intervals', () => {
    const route = snapshot.routes.find((candidate) => candidate.id === 'global_best_shortest_subset')!;
    const repeated = {
      ...route,
      nodeOrder: [route.stages[0].nodeId, route.stages[0].nodeId, ...route.nodeOrder.slice(2)],
      stages: [
        route.stages[0],
        { ...route.stages[1], nodeId: route.stages[0].nodeId, id: `${route.stages[1].id}-same-device` },
        ...route.stages.slice(2),
      ],
    };
    const graph = projectRouteGraph(
      { ...snapshot, routes: [repeated, ...snapshot.routes.filter((item) => item.id !== route.id)] },
      route.id,
    );

    expect(graph.nodes[0].id).not.toBe(graph.nodes[1].id);
    expect(graph.nodes[0].nodeId).toBe(graph.nodes[1].nodeId);
    expect(graph.edges.some((edge) => edge.kind === 'identity_rail')).toBe(true);
  });

  it('never treats reverse evidence as a measured or synthetic forward rail', () => {
    const graph = projectRouteGraph(snapshot, 'global_best_shortest_subset');
    const cedarToAtlas = graph.physicalEdges.find(
      (edge) => edge.sourceNodeId === 'cedar-3060' && edge.targetNodeId === 'atlas-4090',
    );

    expect(cedarToAtlas).toBeDefined();
    expect(cedarToAtlas?.link).toBeNull();
    expect(cedarToAtlas?.missingReason).toMatch(/directed.*not supplied/i);
  });

  it('keeps metric-only updates on the same topology signature and positions', async () => {
    const graph = projectRouteGraph(snapshot, 'global_best_shortest_subset');
    const changedSnapshot: EvidenceSnapshot = {
      ...snapshot,
      links: snapshot.links.map((link, index) =>
        index === 0
          ? {
              ...link,
              metrics: {
                ...link.metrics,
                roundTripTimeMs: { ...link.metrics.roundTripTimeMs, value: 999 },
              },
            }
          : link,
      ),
    };
    const changedGraph = projectRouteGraph(changedSnapshot, 'global_best_shortest_subset');

    expect(topologySignature(changedGraph)).toBe(topologySignature(graph));
    const [before, after] = await Promise.all([
      layoutGraph(graph, 'ring'),
      layoutGraph(changedGraph, 'ring'),
    ]);
    expect(after.nodes.map(({ id, x, y }) => ({ id, x, y }))).toEqual(
      before.nodes.map(({ id, x, y }) => ({ id, x, y })),
    );
  });
});

describe('layout families', () => {
  const graph = projectRouteGraph(snapshot, 'global_best_shortest_subset');

  it('starts a ring at layer zero at 12 o’clock and uses an outer decode arc', async () => {
    const scene = await layoutGraph(graph, 'ring');
    const first = scene.nodes.find((node) => node.startBoundary === 0)!;

    expect(first.y).toBe(Math.min(...scene.nodes.map((node) => node.y)));
    expect(scene.edges.find((edge) => edge.kind === 'decode_closure')).toMatchObject({
      path: 'outer',
      phase: 'closure',
    });
  });

  it('condenses arbitrary SCCs, expands multi-node components as mini-rings, and marks self loops', async () => {
    const nodes = graph.nodes.slice(0, 4);
    const edge = (id: string, source: string, target: string) => ({
      ...graph.edges[0],
      id,
      source,
      target,
      sourceStageId: source,
      targetStageId: target,
    });
    const cyclic: GraphIR = {
      ...graph,
      nodes,
      edges: [
        edge('a-b', nodes[0].id, nodes[1].id),
        edge('b-a', nodes[1].id, nodes[0].id),
        edge('b-c', nodes[1].id, nodes[2].id),
        edge('c-self', nodes[2].id, nodes[2].id),
        edge('c-d', nodes[2].id, nodes[3].id),
      ],
      physicalEdges: [],
    };

    expect(stronglyConnectedComponents(cyclic).map((component) => component.length).sort()).toEqual([1, 1, 2]);
    const scene = await layoutGraph(cyclic, 'scc');
    expect(scene.clusters.some((cluster) => cluster.kind === 'scc' && cluster.nodeIds.length === 2)).toBe(true);
    expect(scene.edges.find((item) => item.id === 'c-self')?.path).toBe('self_loop');
    expect(new Set(scene.nodes.map((node) => `${node.x},${node.y}`)).size).toBe(4);
  });

  it('keeps elastic geography visibly distorted and unknown locations in a side tray', async () => {
    const randomGraph = projectRouteGraph(snapshot, 'random_ring', { physicalScope: 'all' });
    const scene = await layoutGraph(randomGraph, 'geo');
    const unknown = scene.nodes.find((node) => node.nodeId === 'fern-mobile');

    expect(scene.geo).toMatchObject({ mode: 'elastic', distancesToScale: false });
    expect(scene.warnings).toContain('ELASTIC GEO — distances compressed');
    expect(unknown).toMatchObject({ locationUnknown: true, tray: 'unknown-location' });
    expect(unknown?.x).toBeGreaterThan(900);
  });

  it('offers a distinct uncompressed true-map projection without fabricating unknown coordinates', async () => {
    const randomGraph = projectRouteGraph(snapshot, 'random_ring', { physicalScope: 'all' });
    const elastic = await layoutGraph(randomGraph, 'geo');
    const map = await layoutGraph(randomGraph, 'map');
    const unknown = map.nodes.find((node) => node.nodeId === 'fern-mobile');

    expect(map.geo).toMatchObject({ mode: 'true-map', distancesToScale: false });
    expect(map.warnings).toContain('TRUE MAP — projected WGS84 coordinates; geodesic distance remains inspectable');
    expect(unknown).toMatchObject({ locationUnknown: true, tray: 'unknown-location' });
    expect(map.nodes.map(({ nodeId, x, y }) => ({ nodeId, x, y }))).not.toEqual(
      elastic.nodes.map(({ nodeId, x, y }) => ({ nodeId, x, y })),
    );
  });
});

describe('scale fixtures', () => {
  const seed = projectRouteGraph(snapshot, 'global_best_shortest_subset');

  it('keeps the explicit 24-node fixture detailed, finite, complete, and direction preserving', async () => {
    const fixture = createPerformanceGraphFixture(seed, 24);
    const started = performance.now();
    const scene = await layoutGraph(fixture.graph, 'pipeline');
    const elapsed = performance.now() - started;

    expect(fixture.claimBoundary).toMatch(/synthetic layout fixture.*no telemetry/i);
    expect(scene.detail).toBe('detailed');
    expect(scene.nodes).toHaveLength(24);
    expect(scene.edges).toHaveLength(24);
    expect(scene.nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(true);
    expect(scene.edges[0]).toMatchObject({ source: scene.nodes[0].id, target: scene.nodes[1].id });
    expect(elapsed).toBeLessThan(1_500);
  });

  it('uses compact glyphs, deterministic clusters, and edge bundles for 100 nodes', async () => {
    const fixture = createPerformanceGraphFixture(seed, 100);
    const started = performance.now();
    const scene = await layoutGraph(fixture.graph, 'pipeline');
    const elapsed = performance.now() - started;

    expect(scene.detail).toBe('compact');
    expect(scene.nodes).toHaveLength(100);
    expect(scene.clusters.length).toBeGreaterThan(1);
    expect(scene.edges.some((edge) => edge.bundleCount > 1)).toBe(true);
    expect(scene.nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))).toBe(true);
    expect(elapsed).toBeLessThan(1_500);
  });
});
