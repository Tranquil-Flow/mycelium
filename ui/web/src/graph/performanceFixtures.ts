import type { GraphIR, GraphIREdge, GraphIRNode } from './graph';

export interface PerformanceGraphFixture {
  readonly graph: GraphIR;
  readonly claimBoundary: string;
}

/**
 * Creates topology-only scale evidence from an already-normalized synthetic seed.
 * Values are cloned solely to satisfy glyph shape; this fixture makes no device,
 * readiness, bandwidth, location, or runtime-telemetry claim.
 */
export function createPerformanceGraphFixture(
  seed: GraphIR,
  count: 24 | 100,
): PerformanceGraphFixture {
  const seedNode = seed.nodes.find((node) => node.stage !== null);
  if (seedNode === undefined || seedNode.stage === null) {
    throw new Error('Performance fixture requires one normalized stage seed');
  }
  const seedStage = seedNode.stage;

  const nodes: GraphIRNode[] = Array.from({ length: count }, (_, order) => {
    const nodeId = `synthetic-scale-node-${String(order + 1).padStart(3, '0')}`;
    const stageId = `synthetic-scale-stage-${String(order + 1).padStart(3, '0')}`;
    return {
      ...seedNode,
      id: `stage:synthetic-scale:${stageId}`,
      order,
      routeId: `synthetic-scale-${count}`,
      nodeId,
      stageId,
      stage: {
        ...seedStage,
        id: stageId,
        nodeId,
        startLayer: order,
        endLayerExclusive: order + 1,
        layerCount: 1,
      },
      evidenceNode: {
        ...seedNode.evidenceNode,
        id: nodeId,
        location: {
          state: 'unknown',
          provenance: 'unknown',
          reason: 'not_provided',
        },
      },
      location: {
        state: 'unknown',
        provenance: 'unknown',
        reason: 'not_provided',
      },
    };
  });

  const edges: GraphIREdge[] = nodes.slice(0, -1).map((source, order) => {
    const target = nodes[order + 1];
    return {
      id: `synthetic-scale-${count}:handoff:${order}`,
      layer: 'logical',
      kind: 'stage_handoff',
      order,
      source: source.id,
      target: target.id,
      sourceStageId: source.stageId,
      targetStageId: target.stageId,
      sourceBoundary: source.stage!.endLayerExclusive,
      targetBoundary: target.stage!.startLayer,
      sourceNodeId: source.nodeId,
      targetNodeId: target.nodeId,
      phase: 'both',
      routeRole: 'primary',
      reservedFraction: null,
    };
  });
  edges.push({
    id: `synthetic-scale-${count}:decode-closure`,
    layer: 'logical',
    kind: 'decode_closure',
    order: edges.length,
    source: nodes.at(-1)!.id,
    target: nodes[0].id,
    sourceStageId: nodes.at(-1)!.stageId,
    targetStageId: nodes[0].stageId,
    sourceBoundary: count,
    targetBoundary: 0,
    sourceNodeId: nodes.at(-1)!.nodeId,
    targetNodeId: nodes[0].nodeId,
    phase: 'closure',
    routeRole: 'primary',
    reservedFraction: null,
  });

  return {
    claimBoundary:
      'Synthetic layout fixture for 24/100-node renderer performance only; no telemetry, readiness, bandwidth, or physical location claim.',
    graph: {
      ...seed,
      id: `synthetic-scale-${count}`,
      routeId: `synthetic-scale-${count}`,
      ringId: `synthetic-scale-ring-${count}`,
      route: {
        ...seed.route,
        id: `synthetic-scale-${count}`,
        ringId: `synthetic-scale-ring-${count}`,
        nodeOrder: nodes.map((node) => node.nodeId),
        stages: nodes.map((node) => node.stage!),
      },
      nodes,
      edges,
      physicalEdges: [],
    },
  };
}
