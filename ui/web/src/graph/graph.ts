import { geoMercator } from 'd3-geo';
import { scaleSymlog } from 'd3-scale';
import type { ElkNode } from 'elkjs/lib/elk.bundled.js';
import type {
  EvidenceLink,
  EvidenceNode,
  EvidenceRoute,
  EvidenceRouteStage,
  EvidenceSnapshot,
  NodeLocation,
} from '../model/types';

/** The graph contains computation semantics only; no renderer-specific objects. */
export interface GraphIRNode {
  readonly id: string;
  readonly kind: 'stage';
  readonly order: number;
  readonly routeId: string;
  readonly nodeId: string;
  readonly stageId: string;
  readonly stage: EvidenceRouteStage;
  readonly evidenceNode: EvidenceNode;
  readonly location: NodeLocation;
}

export type GraphIREdgeKind = 'stage_handoff' | 'decode_closure';

/**
 * A directed computation edge. `source` and `target` are semantic endpoints,
 * not suggestions to a layout engine, and layouts must never reverse them.
 */
export interface GraphIREdge {
  readonly id: string;
  readonly kind: GraphIREdgeKind;
  readonly order: number;
  readonly source: string;
  readonly target: string;
  readonly sourceStageId: string;
  readonly targetStageId: string;
  readonly link: EvidenceLink | null;
}

export interface GraphIR {
  readonly id: string;
  readonly routeId: string;
  readonly ringId: string;
  readonly route: EvidenceRoute;
  readonly nodes: readonly GraphIRNode[];
  readonly edges: readonly GraphIREdge[];
}

export type GraphLayout = 'pipeline' | 'ring' | 'geo';

export interface SceneNode extends GraphIRNode {
  /** Top-left scene coordinate, independent of any rendering library. */
  readonly x: number;
  /** Top-left scene coordinate, independent of any rendering library. */
  readonly y: number;
  readonly width: number;
  readonly height: number;
  readonly locationUnknown: boolean;
}

export interface SceneEdge extends GraphIREdge {}

export interface SceneGraph {
  readonly id: string;
  readonly routeId: string;
  readonly layout: GraphLayout;
  readonly width: number;
  readonly height: number;
  readonly nodes: readonly SceneNode[];
  readonly edges: readonly SceneEdge[];
}

const NODE_WIDTH = 184;
const NODE_HEIGHT = 96;
const SCENE_MARGIN = 40;

type ElkBundle = typeof import('elkjs/lib/elk.bundled.js');
let elkBundlePromise: Promise<ElkBundle> | null = null;

function loadElkBundle(): Promise<ElkBundle> {
  elkBundlePromise ??= import('elkjs/lib/elk.bundled.js');
  return elkBundlePromise;
}

function routeLink(
  links: readonly EvidenceLink[],
  source: string,
  target: string,
): EvidenceLink | null {
  const exact = links.find((link) => link.source === source && link.target === target);
  if (exact !== undefined) {
    return exact;
  }

  return (
    links.find(
      (link) => link.bidirectional && link.source === target && link.target === source,
    ) ?? null
  );
}

/**
 * Projects one evidence route into a renderer-neutral semantic graph.
 *
 * Route stages are kept in model order. Consecutive stages have directed
 * handoff edges and the final stage has an explicit decode-only closure back
 * to the first stage. The closure is deliberately represented in the IR
 * rather than inferred by a circular renderer.
 */
export function projectRouteGraph(snapshot: EvidenceSnapshot, routeId: string): GraphIR {
  const route = snapshot.routes.find((candidate) => candidate.id === routeId);
  if (route === undefined) {
    throw new Error(`Cannot project unknown route: ${routeId}`);
  }

  const evidenceNodes = new Map(snapshot.nodes.map((node) => [node.id, node]));
  const nodes = route.stages.map((stage, order): GraphIRNode => {
    const evidenceNode = evidenceNodes.get(stage.nodeId);
    if (evidenceNode === undefined) {
      throw new Error(`Route ${routeId} refers to unknown node: ${stage.nodeId}`);
    }

    return {
      id: stage.nodeId,
      kind: 'stage',
      order,
      routeId,
      nodeId: stage.nodeId,
      stageId: stage.id,
      stage,
      evidenceNode,
      location: evidenceNode.location,
    };
  });

  const duplicateIds = nodes.filter(
    (node, index) => nodes.findIndex((candidate) => candidate.id === node.id) !== index,
  );
  if (duplicateIds.length > 0) {
    throw new Error(`Route ${routeId} contains a repeated stage node: ${duplicateIds[0].id}`);
  }

  const edges: GraphIREdge[] = [];
  for (let order = 0; order < nodes.length - 1; order += 1) {
    const source = nodes[order];
    const target = nodes[order + 1];
    edges.push({
      id: `${routeId}:handoff:${order}`,
      kind: 'stage_handoff',
      order,
      source: source.id,
      target: target.id,
      sourceStageId: source.stageId,
      targetStageId: target.stageId,
      link: routeLink(snapshot.links, source.id, target.id),
    });
  }

  if (nodes.length > 0) {
    const source = nodes[nodes.length - 1];
    const target = nodes[0];
    edges.push({
      id: `${routeId}:decode-closure`,
      kind: 'decode_closure',
      order: edges.length,
      source: source.id,
      target: target.id,
      sourceStageId: source.stageId,
      targetStageId: target.stageId,
      link: routeLink(snapshot.links, source.id, target.id),
    });
  }

  return {
    id: route.id,
    routeId: route.id,
    ringId: route.ringId,
    route,
    nodes,
    edges,
  };
}

function validateGraph(graph: GraphIR): void {
  const nodeIds = new Set<string>();
  for (const node of graph.nodes) {
    if (nodeIds.has(node.id)) {
      throw new Error(`Graph ${graph.id} contains duplicate node id: ${node.id}`);
    }
    nodeIds.add(node.id);
  }

  for (const edge of graph.edges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
      throw new Error(
        `Graph ${graph.id} edge ${edge.id} refers to a missing endpoint: ${edge.source} -> ${edge.target}`,
      );
    }
  }
}

function sceneEdges(graph: GraphIR, kind?: GraphIREdgeKind): SceneEdge[] {
  // Copy semantic edges from the IR. In particular, never consume ELK's
  // potentially cycle-adjusted edge representation as application semantics.
  return graph.edges
    .filter((edge) => kind === undefined || edge.kind === kind)
    .map((edge) => ({ ...edge }));
}

function sceneNode(node: GraphIRNode, x: number, y: number): SceneNode {
  return {
    ...node,
    x,
    y,
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    locationUnknown: node.location.state === 'unknown',
  };
}

function finiteCoordinate(value: number | undefined, context: string): number {
  if (value === undefined || !Number.isFinite(value)) {
    throw new Error(`Layout did not produce a finite ${context}`);
  }
  return value;
}

async function pipelineLayout(graph: GraphIR): Promise<SceneGraph> {
  const { default: ELK } = await loadElkBundle();
  const elk = new ELK();
  const edges = sceneEdges(graph, 'stage_handoff');
  const layoutInput: ElkNode = {
    id: `${graph.id}:pipeline-layout`,
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.padding': `[top=${SCENE_MARGIN},left=${SCENE_MARGIN},bottom=${SCENE_MARGIN},right=${SCENE_MARGIN}]`,
      'elk.spacing.nodeNode': '48',
      'elk.layered.spacing.nodeNodeBetweenLayers': '88',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
    },
    children: graph.nodes.map((node) => ({
      id: node.id,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    })),
    // The pipeline scene is a true DAG: decode closure remains explicit in
    // GraphIR and in the ring/geo scenes, but is not part of this layout.
    edges: edges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  };

  const laidOut = await elk.layout(layoutInput);
  const positions = new Map(
    (laidOut.children ?? []).map((child) => [
      child.id,
      {
        x: finiteCoordinate(child.x, `x coordinate for ${child.id}`),
        y: finiteCoordinate(child.y, `y coordinate for ${child.id}`),
      },
    ]),
  );

  const nodes = graph.nodes.map((node) => {
    const position = positions.get(node.id);
    if (position === undefined) {
      throw new Error(`ELK omitted graph node: ${node.id}`);
    }
    return sceneNode(node, position.x, position.y);
  });

  return {
    id: graph.id,
    routeId: graph.routeId,
    layout: 'pipeline',
    width: finiteCoordinate(laidOut.width, 'graph width'),
    height: finiteCoordinate(laidOut.height, 'graph height'),
    nodes,
    edges,
  };
}

function rounded(value: number): number {
  const result = Math.round(value * 1_000_000) / 1_000_000;
  return Object.is(result, -0) ? 0 : result;
}

function ringLayout(graph: GraphIR): SceneGraph {
  const count = graph.nodes.length;
  const radius = count < 2 ? 0 : Math.max(180, (count * (NODE_WIDTH + 48)) / (2 * Math.PI));
  const centerX = SCENE_MARGIN + NODE_WIDTH / 2 + radius;
  const centerY = SCENE_MARGIN + NODE_HEIGHT / 2 + radius;

  const nodes = graph.nodes.map((node, index) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / Math.max(count, 1);
    return sceneNode(
      node,
      rounded(centerX + radius * Math.cos(angle) - NODE_WIDTH / 2),
      rounded(centerY + radius * Math.sin(angle) - NODE_HEIGHT / 2),
    );
  });

  return {
    id: graph.id,
    routeId: graph.routeId,
    layout: 'ring',
    width: rounded(2 * SCENE_MARGIN + NODE_WIDTH + 2 * radius),
    height: rounded(2 * SCENE_MARGIN + NODE_HEIGHT + 2 * radius),
    nodes,
    edges: sceneEdges(graph),
  };
}

const GEO_WIDTH = 1_160;
const GEO_KNOWN_MIN_X = 48;
const GEO_KNOWN_MAX_X = 720;
const GEO_KNOWN_MIN_Y = 48;
const GEO_KNOWN_MAX_Y = 520;
const GEO_TRAY_X = 920;
const GEO_TRAY_GAP = 24;

function geoLayout(graph: GraphIR): SceneGraph {
  // Mercator gives geographic coordinates in radians around the prime
  // meridian/equator. A fixed-world symlog transform then keeps distant cities
  // legible without changing their east/west or north/south ordering.
  const projection = geoMercator().scale(1).translate([0, 0]);
  const xScale = scaleSymlog()
    .constant(0.25)
    .domain([-Math.PI, Math.PI])
    .range([GEO_KNOWN_MIN_X, GEO_KNOWN_MAX_X])
    .clamp(true);
  const yScale = scaleSymlog()
    .constant(0.25)
    .domain([-Math.PI, Math.PI])
    .range([GEO_KNOWN_MIN_Y, GEO_KNOWN_MAX_Y])
    .clamp(true);

  let unknownIndex = 0;
  const nodes = graph.nodes.map((node) => {
    if (node.location.state === 'unknown') {
      const y = GEO_KNOWN_MIN_Y + unknownIndex * (NODE_HEIGHT + GEO_TRAY_GAP);
      unknownIndex += 1;
      return sceneNode(node, GEO_TRAY_X, y);
    }

    const projected = projection([node.location.longitude, node.location.latitude]);
    if (projected === null || !Number.isFinite(projected[0]) || !Number.isFinite(projected[1])) {
      throw new Error(`Could not project known location for node: ${node.id}`);
    }

    return sceneNode(node, rounded(xScale(projected[0])), rounded(yScale(projected[1])));
  });

  const trayHeight =
    unknownIndex === 0
      ? 0
      : GEO_KNOWN_MIN_Y + unknownIndex * NODE_HEIGHT + (unknownIndex - 1) * GEO_TRAY_GAP;

  return {
    id: graph.id,
    routeId: graph.routeId,
    layout: 'geo',
    width: GEO_WIDTH,
    height: Math.max(GEO_KNOWN_MAX_Y + NODE_HEIGHT + SCENE_MARGIN, trayHeight + SCENE_MARGIN),
    nodes,
    edges: sceneEdges(graph),
  };
}

/** Computes positions while preserving the GraphIR's node and edge semantics. */
export async function layoutGraph(graph: GraphIR, layout: GraphLayout): Promise<SceneGraph> {
  validateGraph(graph);

  switch (layout) {
    case 'pipeline':
      return pipelineLayout(graph);
    case 'ring':
      return ringLayout(graph);
    case 'geo':
      return geoLayout(graph);
  }
}
