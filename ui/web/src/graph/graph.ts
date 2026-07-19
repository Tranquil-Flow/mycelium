import { geoEqualEarth } from 'd3-geo';
import type {
  EvidenceLink,
  EvidenceNode,
  EvidenceRoute,
  EvidenceRouteStage,
  EvidenceSnapshot,
  NodeLocation,
} from '../model/types';

/** Renderer-neutral graph input. Source semantics are never inferred from visual adjacency. */
export interface GraphIRNode {
  readonly id: string;
  readonly kind: 'stage' | 'device';
  readonly order: number;
  readonly routeId: string;
  readonly nodeId: string;
  readonly stageId: string | null;
  readonly stage: EvidenceRouteStage | null;
  readonly evidenceNode: EvidenceNode;
  readonly location: NodeLocation;
  readonly routeRole: 'primary' | 'alternative' | 'unassigned';
}

export type GraphIREdgeKind =
  | 'stage_handoff'
  | 'decode_closure'
  | 'branch'
  | 'rejoin'
  | 'control'
  | 'identity_rail';
export type LogicalPhase = 'both' | 'prefill' | 'decode' | 'closure' | 'control';
export type RouteRole = 'primary' | 'alternative' | 'identity';

export interface GraphIREdge {
  readonly id: string;
  readonly layer: 'logical';
  readonly kind: GraphIREdgeKind;
  readonly order: number;
  readonly source: string;
  readonly target: string;
  readonly sourceStageId: string | null;
  readonly targetStageId: string | null;
  readonly sourceBoundary: number | null;
  readonly targetBoundary: number | null;
  readonly sourceNodeId: string;
  readonly targetNodeId: string;
  readonly phase: LogicalPhase;
  readonly routeRole: RouteRole;
  readonly reservedFraction: number | null;
}

export interface GraphIRPhysicalEdge {
  readonly id: string;
  readonly layer: 'physical';
  readonly kind: 'physical_link';
  readonly order: number;
  readonly source: string;
  readonly target: string;
  readonly sourceNodeId: string;
  readonly targetNodeId: string;
  /** Exact directed evidence only. A reverse or bidirectional record is never substituted. */
  readonly link: EvidenceLink | null;
  readonly missingReason: string | null;
}

export interface GraphIR {
  readonly id: string;
  readonly routeId: string;
  readonly ringId: string;
  readonly route: EvidenceRoute;
  readonly nodes: readonly GraphIRNode[];
  readonly edges: readonly GraphIREdge[];
  readonly physicalEdges: readonly GraphIRPhysicalEdge[];
}

export type GraphLayout = 'pipeline' | 'ring' | 'scc' | 'geo' | 'map';
export type SceneDetail = 'detailed' | 'compact';
export type SceneEdgePath = 'forward' | 'outer' | 'chord' | 'self_loop' | 'identity' | 'great_circle';

export interface SceneNode extends GraphIRNode {
  /** Top-left scene coordinate, independent of any rendering library. */
  readonly x: number;
  /** Top-left scene coordinate, independent of any rendering library. */
  readonly y: number;
  readonly width: number;
  readonly height: number;
  readonly startBoundary: number | null;
  readonly endBoundary: number | null;
  /** Exact visual span of the half-open layer interval on a pipeline boundary axis. */
  readonly layerSpanWidth: number | null;
  readonly locationUnknown: boolean;
  readonly tray: 'unknown-location' | 'physical-only' | null;
  readonly anchorX: number | null;
  readonly anchorY: number | null;
  readonly clusterId: string | null;
}

export interface BundledEdge {
  readonly path: SceneEdgePath;
  readonly bundleKey: string;
  readonly bundleIndex: number;
  readonly bundleCount: number;
}

export interface SceneEdge extends GraphIREdge, BundledEdge {
  /** Exact directed physical evidence corresponding to this logical rail, when supplied. */
  readonly link: EvidenceLink | null;
}
export interface ScenePhysicalEdge extends GraphIRPhysicalEdge, BundledEdge {}

export interface SceneCluster {
  readonly id: string;
  readonly kind: 'scale' | 'scc';
  readonly label: string;
  readonly nodeIds: readonly string[];
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface SceneGeoMetadata {
  readonly mode: 'elastic' | 'true-map';
  readonly projection: string;
  readonly distancesToScale: false;
  readonly focusNodeId: string | null;
  readonly compressionKneeKm: number | null;
}

export interface SceneGraph {
  readonly id: string;
  readonly routeId: string;
  readonly layout: GraphLayout;
  readonly topologySignature: string;
  readonly detail: SceneDetail;
  readonly width: number;
  readonly height: number;
  readonly nodes: readonly SceneNode[];
  readonly edges: readonly SceneEdge[];
  readonly physicalEdges: readonly ScenePhysicalEdge[];
  readonly clusters: readonly SceneCluster[];
  readonly warnings: readonly string[];
  readonly geo: SceneGeoMetadata | null;
}

export interface ProjectRouteOptions {
  readonly physicalScope?: 'route' | 'all';
}

const DETAIL_NODE_WIDTH = 184;
const DETAIL_NODE_HEIGHT = 160;
const COMPACT_NODE_WIDTH = 104;
const COMPACT_NODE_HEIGHT = 70;
const SCENE_MARGIN = 40;
const SCALE_CLUSTER_SIZE = 12;
const LARGE_GRAPH_THRESHOLD = 48;
const ELASTIC_WIDTH = 1_220;
const GEO_HEIGHT = 720;
const GEO_MAP_RIGHT = 900;
const UNKNOWN_TRAY_X = 980;
const UNKNOWN_TRAY_GAP = 20;
const EARTH_RADIUS_KM = 6_371.0088;
const ELASTIC_KNEE_KM = 1_000;

function exactDirectedLink(
  links: readonly EvidenceLink[],
  source: string,
  target: string,
): EvidenceLink | null {
  return links.find((link) => link.source === source && link.target === target) ?? null;
}

function graphNodeId(routeId: string, stage: EvidenceRouteStage): string {
  return `stage:${routeId}:${stage.id}`;
}

function classifyRouteRole(pathClass: string): 'primary' | 'alternative' {
  const normalized = pathClass.toLowerCase();
  return normalized.includes('secondary') || normalized.includes('alternative')
    ? 'alternative'
    : 'primary';
}

function logicalRole(source: GraphIRNode, target: GraphIRNode): RouteRole {
  return source.routeRole === 'alternative' || target.routeRole === 'alternative'
    ? 'alternative'
    : 'primary';
}

function logicalEdge(
  routeId: string,
  source: GraphIRNode,
  target: GraphIRNode,
  order: number,
): GraphIREdge {
  const sourceBoundary = source.stage?.endLayerExclusive ?? null;
  const targetBoundary = target.stage?.startLayer ?? null;
  return {
    id: `${routeId}:handoff:${order}:${source.id}->${target.id}`,
    layer: 'logical',
    kind: 'stage_handoff',
    order,
    source: source.id,
    target: target.id,
    sourceStageId: source.stageId,
    targetStageId: target.stageId,
    sourceBoundary,
    targetBoundary,
    sourceNodeId: source.nodeId,
    targetNodeId: target.nodeId,
    phase: 'both',
    routeRole: logicalRole(source, target),
    reservedFraction: logicalRole(source, target) === 'alternative' ? 0 : null,
  };
}

function physicalForLogical(
  logical: GraphIREdge,
  links: readonly EvidenceLink[],
  order: number,
): GraphIRPhysicalEdge {
  const link = exactDirectedLink(links, logical.sourceNodeId, logical.targetNodeId);
  return {
    id: `physical:${logical.id}`,
    layer: 'physical',
    kind: 'physical_link',
    order,
    source: logical.source,
    target: logical.target,
    sourceNodeId: logical.sourceNodeId,
    targetNodeId: logical.targetNodeId,
    link,
    missingReason:
      link === null
        ? 'Exact directed link evidence was not supplied; reverse records are not substituted.'
        : null,
  };
}

/**
 * Projects one normalized evidence route into semantic logical and physical layers.
 * Stage instances use stage identity, so one physical device may truthfully own disjoint intervals.
 */
export function projectRouteGraph(
  snapshot: EvidenceSnapshot,
  routeId: string,
  options: ProjectRouteOptions = {},
): GraphIR {
  const route = snapshot.routes.find((candidate) => candidate.id === routeId);
  if (route === undefined) {
    throw new Error(`Cannot project unknown route: ${routeId}`);
  }

  const evidenceNodes = new Map(snapshot.nodes.map((node) => [node.id, node]));
  const sortedStages = [...route.stages].sort(
    (left, right) =>
      left.startLayer - right.startLayer ||
      left.endLayerExclusive - right.endLayerExclusive ||
      left.id.localeCompare(right.id),
  );
  const nodes: GraphIRNode[] = sortedStages.map((stage, order) => {
    const evidenceNode = evidenceNodes.get(stage.nodeId);
    if (evidenceNode === undefined) {
      throw new Error(`Route ${routeId} refers to unknown node: ${stage.nodeId}`);
    }
    return {
      id: graphNodeId(routeId, stage),
      kind: 'stage',
      order,
      routeId,
      nodeId: stage.nodeId,
      stageId: stage.id,
      stage,
      evidenceNode,
      location: evidenceNode.location,
      routeRole: classifyRouteRole(stage.pathClass),
    };
  });

  const duplicateStageIds = nodes.filter(
    (node, index) => nodes.findIndex((candidate) => candidate.id === node.id) !== index,
  );
  if (duplicateStageIds.length > 0) {
    throw new Error(`Route ${routeId} contains duplicate stage identity: ${duplicateStageIds[0].stageId}`);
  }

  const edges: GraphIREdge[] = [];
  for (let order = 0; order < nodes.length - 1; order += 1) {
    edges.push(logicalEdge(routeId, nodes[order], nodes[order + 1], order));
  }
  if (nodes.length > 0) {
    const source = nodes[nodes.length - 1];
    const target = nodes[0];
    edges.push({
      id: `${routeId}:decode-closure:${source.id}->${target.id}`,
      layer: 'logical',
      kind: 'decode_closure',
      order: edges.length,
      source: source.id,
      target: target.id,
      sourceStageId: source.stageId,
      targetStageId: target.stageId,
      sourceBoundary: source.stage?.endLayerExclusive ?? null,
      targetBoundary: target.stage?.startLayer ?? null,
      sourceNodeId: source.nodeId,
      targetNodeId: target.nodeId,
      phase: 'closure',
      routeRole: logicalRole(source, target),
      reservedFraction: null,
    });
  }

  const stagesByDevice = new Map<string, GraphIRNode[]>();
  for (const node of nodes) {
    const stages = stagesByDevice.get(node.nodeId) ?? [];
    stages.push(node);
    stagesByDevice.set(node.nodeId, stages);
  }
  for (const stages of stagesByDevice.values()) {
    for (let index = 1; index < stages.length; index += 1) {
      const source = stages[index - 1];
      const target = stages[index];
      edges.push({
        id: `${routeId}:identity:${source.id}->${target.id}`,
        layer: 'logical',
        kind: 'identity_rail',
        order: edges.length,
        source: source.id,
        target: target.id,
        sourceStageId: source.stageId,
        targetStageId: target.stageId,
        sourceBoundary: source.stage?.endLayerExclusive ?? null,
        targetBoundary: target.stage?.startLayer ?? null,
        sourceNodeId: source.nodeId,
        targetNodeId: target.nodeId,
        phase: 'control',
        routeRole: 'identity',
        reservedFraction: null,
      });
    }
  }

  const physicalEdges = edges
    .filter((edge) => edge.kind === 'stage_handoff' || edge.kind === 'decode_closure')
    .map((edge, order) => physicalForLogical(edge, snapshot.links, order));

  if (options.physicalScope === 'all') {
    const firstStageByDevice = new Map<string, GraphIRNode>();
    for (const node of nodes) {
      if (!firstStageByDevice.has(node.nodeId)) firstStageByDevice.set(node.nodeId, node);
    }
    for (const evidenceNode of snapshot.nodes) {
      if (firstStageByDevice.has(evidenceNode.id)) continue;
      const node: GraphIRNode = {
        id: `device:${evidenceNode.id}`,
        kind: 'device',
        order: nodes.length,
        routeId,
        nodeId: evidenceNode.id,
        stageId: null,
        stage: null,
        evidenceNode,
        location: evidenceNode.location,
        routeRole: 'unassigned',
      };
      nodes.push(node);
      firstStageByDevice.set(evidenceNode.id, node);
    }

    const representedPairs = new Set(
      physicalEdges
        .filter((edge) => edge.link !== null)
        .map((edge) => `${edge.sourceNodeId}\u0000${edge.targetNodeId}`),
    );
    for (const link of snapshot.links) {
      const pair = `${link.source}\u0000${link.target}`;
      if (representedPairs.has(pair)) continue;
      const source = firstStageByDevice.get(link.source);
      const target = firstStageByDevice.get(link.target);
      if (source === undefined || target === undefined) continue;
      physicalEdges.push({
        id: `physical:observation:${link.id}`,
        layer: 'physical',
        kind: 'physical_link',
        order: physicalEdges.length,
        source: source.id,
        target: target.id,
        sourceNodeId: source.nodeId,
        targetNodeId: target.nodeId,
        link,
        missingReason: null,
      });
      representedPairs.add(pair);
    }
  }

  return {
    id: route.id,
    routeId: route.id,
    ringId: route.ringId,
    route,
    nodes,
    edges,
    physicalEdges,
  };
}

function validateGraph(graph: GraphIR): void {
  const nodeIds = new Set<string>();
  for (const node of graph.nodes) {
    if (nodeIds.has(node.id)) throw new Error(`Graph ${graph.id} contains duplicate node id: ${node.id}`);
    nodeIds.add(node.id);
  }
  for (const edge of [...graph.edges, ...graph.physicalEdges]) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
      throw new Error(
        `Graph ${graph.id} edge ${edge.id} refers to a missing endpoint: ${edge.source} -> ${edge.target}`,
      );
    }
  }
}

/** Stable across metrics, provenance values, timestamps, and other non-topological updates. */
export function topologySignature(graph: GraphIR): string {
  const nodes = [...graph.nodes]
    .map((node) => `${node.id}:${node.stage?.startLayer ?? 'device'}:${node.stage?.endLayerExclusive ?? 'device'}`)
    .sort();
  const logical = [...graph.edges]
    .map((edge) => `${edge.id}:${edge.kind}:${edge.source}>${edge.target}`)
    .sort();
  const physical = [...graph.physicalEdges]
    .map((edge) => `${edge.id}:${edge.source}>${edge.target}:${edge.link === null ? 'unknown' : 'observed'}`)
    .sort();
  return JSON.stringify([nodes, logical, physical]);
}

function rounded(value: number): number {
  const result = Math.round(value * 1_000_000) / 1_000_000;
  return Object.is(result, -0) ? 0 : result;
}

function dimensions(detail: SceneDetail): { width: number; height: number } {
  return detail === 'detailed'
    ? { width: DETAIL_NODE_WIDTH, height: DETAIL_NODE_HEIGHT }
    : { width: COMPACT_NODE_WIDTH, height: COMPACT_NODE_HEIGHT };
}

function sceneNode(
  node: GraphIRNode,
  x: number,
  y: number,
  detail: SceneDetail,
  options: {
    readonly layerScale?: number;
    readonly tray?: SceneNode['tray'];
    readonly anchorX?: number | null;
    readonly anchorY?: number | null;
    readonly clusterId?: string | null;
  } = {},
): SceneNode {
  const size = dimensions(detail);
  return {
    ...node,
    x: rounded(x),
    y: rounded(y),
    width: size.width,
    height: size.height,
    startBoundary: node.stage?.startLayer ?? null,
    endBoundary: node.stage?.endLayerExclusive ?? null,
    layerSpanWidth:
      node.stage === null || options.layerScale === undefined
        ? null
        : rounded(node.stage.layerCount * options.layerScale),
    locationUnknown: node.location.state === 'unknown',
    tray: options.tray ?? null,
    anchorX: options.anchorX ?? null,
    anchorY: options.anchorY ?? null,
    clusterId: options.clusterId ?? null,
  };
}

function edgePath(edge: GraphIREdge): SceneEdgePath {
  if (edge.source === edge.target) return 'self_loop';
  if (edge.kind === 'decode_closure') return 'outer';
  if (edge.kind === 'identity_rail') return 'identity';
  if (edge.kind === 'branch' || edge.kind === 'rejoin') return 'chord';
  return 'forward';
}

function clusterAssignments(nodes: readonly GraphIRNode[]): Map<string, string> {
  const assignments = new Map<string, string>();
  if (nodes.length <= LARGE_GRAPH_THRESHOLD) return assignments;
  const ordered = [...nodes].sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
  ordered.forEach((node, index) => assignments.set(node.id, `scale-cluster-${Math.floor(index / SCALE_CLUSTER_SIZE) + 1}`));
  return assignments;
}

function bundle<T extends GraphIREdge | GraphIRPhysicalEdge>(
  edges: readonly T[],
  nodeCluster: ReadonlyMap<string, string>,
  pathFor: (edge: T) => SceneEdgePath,
): Array<T & BundledEdge> {
  const groups = new Map<string, T[]>();
  for (const edge of edges) {
    const sourceCluster = nodeCluster.get(edge.source) ?? edge.source;
    const targetCluster = nodeCluster.get(edge.target) ?? edge.target;
    const key = `${sourceCluster}->${targetCluster}:${pathFor(edge)}`;
    const members = groups.get(key) ?? [];
    members.push(edge);
    groups.set(key, members);
  }
  return edges.map((edge) => {
    const sourceCluster = nodeCluster.get(edge.source) ?? edge.source;
    const targetCluster = nodeCluster.get(edge.target) ?? edge.target;
    const key = `${sourceCluster}->${targetCluster}:${pathFor(edge)}`;
    const members = groups.get(key)!;
    return {
      ...edge,
      path: pathFor(edge),
      bundleKey: key,
      bundleIndex: members.findIndex((member) => member.id === edge.id),
      bundleCount: members.length,
    };
  });
}

function boundsCluster(
  id: string,
  kind: SceneCluster['kind'],
  label: string,
  nodeIds: readonly string[],
  nodes: readonly SceneNode[],
): SceneCluster {
  const members = nodes.filter((node) => nodeIds.includes(node.id));
  const minX = Math.min(...members.map((node) => node.x)) - 16;
  const minY = Math.min(...members.map((node) => node.y)) - 20;
  const maxX = Math.max(...members.map((node) => node.x + node.width)) + 16;
  const maxY = Math.max(...members.map((node) => node.y + node.height)) + 16;
  return { id, kind, label, nodeIds, x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

function scaleClusters(nodes: readonly SceneNode[]): SceneCluster[] {
  const ids = [...new Set(nodes.map((node) => node.clusterId).filter((id): id is string => id !== null))];
  return ids.map((id, index) =>
    boundsCluster(
      id,
      'scale',
      `Compact cluster ${index + 1}`,
      nodes.filter((node) => node.clusterId === id).map((node) => node.id),
      nodes,
    ),
  );
}

function finishScene(
  graph: GraphIR,
  layout: GraphLayout,
  nodes: readonly SceneNode[],
  clusters: readonly SceneCluster[],
  warnings: readonly string[] = [],
  geo: SceneGeoMetadata | null = null,
): SceneGraph {
  const clusterMap = new Map(nodes.map((node) => [node.id, node.clusterId]).filter((entry): entry is [string, string] => entry[1] !== null));
  const edges: SceneEdge[] = bundle(graph.edges, clusterMap, edgePath).map((edge) => ({
    ...edge,
    link: graph.physicalEdges.find(
      (physical) => physical.source === edge.source && physical.target === edge.target,
    )?.link ?? null,
  }));
  const physicalEdges = bundle(
    graph.physicalEdges,
    clusterMap,
    () => (layout === 'geo' || layout === 'map' ? 'great_circle' : 'forward'),
  );
  const right = Math.max(SCENE_MARGIN, ...nodes.map((node) => node.x + node.width + SCENE_MARGIN));
  const bottom = Math.max(SCENE_MARGIN, ...nodes.map((node) => node.y + node.height + SCENE_MARGIN));
  return {
    id: graph.id,
    routeId: graph.routeId,
    layout,
    topologySignature: topologySignature(graph),
    detail: graph.nodes.length <= 24 ? 'detailed' : 'compact',
    width: right,
    height: bottom,
    nodes,
    edges,
    physicalEdges,
    clusters,
    warnings,
    geo,
  };
}

function pipelineLayout(graph: GraphIR): SceneGraph {
  const detail: SceneDetail = graph.nodes.length <= 24 ? 'detailed' : 'compact';
  const size = dimensions(detail);
  const layerScale = detail === 'detailed' ? 24 : 12;
  const assignment = clusterAssignments(graph.nodes);
  const lanes: number[] = [];
  let physicalOnlyIndex = 0;
  const nodes = [...graph.nodes]
    .sort((left, right) => {
      const leftStart = left.stage?.startLayer ?? Number.MAX_SAFE_INTEGER;
      const rightStart = right.stage?.startLayer ?? Number.MAX_SAFE_INTEGER;
      return leftStart - rightStart || left.order - right.order || left.id.localeCompare(right.id);
    })
    .map((node) => {
      if (node.stage === null) {
        const x = SCENE_MARGIN + Math.max(1, ...graph.nodes.map((candidate) => candidate.stage?.endLayerExclusive ?? 0)) * layerScale + 100;
        const y = SCENE_MARGIN + physicalOnlyIndex * (size.height + 20);
        physicalOnlyIndex += 1;
        return sceneNode(node, x, y, detail, {
          tray: 'physical-only',
          clusterId: assignment.get(node.id) ?? null,
        });
      }
      const x = SCENE_MARGIN + node.stage.startLayer * layerScale;
      let lane = lanes.findIndex((lastRight) => x > lastRight + 18);
      if (lane < 0) {
        lane = lanes.length;
        lanes.push(0);
      }
      lanes[lane] = x + size.width;
      const y = SCENE_MARGIN + lane * (size.height + 34);
      return sceneNode(node, x, y, detail, {
        layerScale,
        clusterId: assignment.get(node.id) ?? null,
      });
    });
  return finishScene(graph, 'pipeline', nodes, scaleClusters(nodes));
}

function ringLayout(graph: GraphIR): SceneGraph {
  const detail: SceneDetail = graph.nodes.length <= 24 ? 'detailed' : 'compact';
  const size = dimensions(detail);
  const routeNodes = graph.nodes.filter((node) => node.stage !== null).sort((left, right) => (left.stage?.startLayer ?? 0) - (right.stage?.startLayer ?? 0));
  const extraNodes = graph.nodes.filter((node) => node.stage === null);
  const count = routeNodes.length;
  const radius = count < 2 ? 0 : Math.max(220, (count * (size.width + 34)) / (2 * Math.PI));
  const centerX = SCENE_MARGIN + size.width / 2 + radius;
  const centerY = SCENE_MARGIN + size.height / 2 + radius;
  const assignment = clusterAssignments(graph.nodes);
  const layerScale = detail === 'detailed' ? 24 : 12;
  const nodes = routeNodes.map((node, index) => {
    const angle = -Math.PI / 2 + (index * 2 * Math.PI) / Math.max(count, 1);
    return sceneNode(
      node,
      centerX + radius * Math.cos(angle) - size.width / 2,
      centerY + radius * Math.sin(angle) - size.height / 2,
      detail,
      { layerScale, clusterId: assignment.get(node.id) ?? null },
    );
  });
  extraNodes.forEach((node, index) => {
    nodes.push(
      sceneNode(
        node,
        centerX + radius * 2 + 120,
        SCENE_MARGIN + index * (size.height + 20),
        detail,
        { tray: 'physical-only', clusterId: assignment.get(node.id) ?? null },
      ),
    );
  });
  return finishScene(graph, 'ring', nodes, scaleClusters(nodes));
}

/** Deterministic Tarjan decomposition; component and member order follow graph order. */
export function stronglyConnectedComponents(graph: GraphIR): string[][] {
  const adjacency = new Map(graph.nodes.map((node) => [node.id, [] as string[]]));
  for (const edge of graph.edges) adjacency.get(edge.source)?.push(edge.target);
  const indexById = new Map<string, number>();
  const lowById = new Map<string, number>();
  const stack: string[] = [];
  const onStack = new Set<string>();
  const components: string[][] = [];
  let nextIndex = 0;

  const visit = (id: string): void => {
    indexById.set(id, nextIndex);
    lowById.set(id, nextIndex);
    nextIndex += 1;
    stack.push(id);
    onStack.add(id);
    for (const target of adjacency.get(id) ?? []) {
      if (!indexById.has(target)) {
        visit(target);
        lowById.set(id, Math.min(lowById.get(id)!, lowById.get(target)!));
      } else if (onStack.has(target)) {
        lowById.set(id, Math.min(lowById.get(id)!, indexById.get(target)!));
      }
    }
    if (lowById.get(id) !== indexById.get(id)) return;
    const component: string[] = [];
    while (stack.length > 0) {
      const member = stack.pop()!;
      onStack.delete(member);
      component.push(member);
      if (member === id) break;
    }
    const order = new Map(graph.nodes.map((node, position) => [node.id, position]));
    component.sort((left, right) => order.get(left)! - order.get(right)!);
    components.push(component);
  };

  for (const node of graph.nodes) if (!indexById.has(node.id)) visit(node.id);
  const order = new Map(graph.nodes.map((node, position) => [node.id, position]));
  return components.sort((left, right) => order.get(left[0])! - order.get(right[0])!);
}

function sccLayout(graph: GraphIR): SceneGraph {
  const detail: SceneDetail = graph.nodes.length <= 24 ? 'detailed' : 'compact';
  const size = dimensions(detail);
  const components = stronglyConnectedComponents(graph);
  const componentByNode = new Map<string, number>();
  components.forEach((component, index) => component.forEach((nodeId) => componentByNode.set(nodeId, index)));
  const incoming = components.map(() => new Set<number>());
  for (const edge of graph.edges) {
    const source = componentByNode.get(edge.source)!;
    const target = componentByNode.get(edge.target)!;
    if (source !== target) incoming[target].add(source);
  }
  const rankMemo = new Map<number, number>();
  const rank = (index: number): number => {
    const cached = rankMemo.get(index);
    if (cached !== undefined) return cached;
    const value = incoming[index].size === 0 ? 0 : 1 + Math.max(...[...incoming[index]].map(rank));
    rankMemo.set(index, value);
    return value;
  };
  const rowByRank = new Map<number, number>();
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  const nodes: SceneNode[] = [];
  const componentNodes: Array<{ id: string; ids: string[] }> = [];
  components.forEach((component, componentIndex) => {
    const componentRank = rank(componentIndex);
    const row = rowByRank.get(componentRank) ?? 0;
    rowByRank.set(componentRank, row + 1);
    const baseX = SCENE_MARGIN + componentRank * (size.width + 260);
    const baseY = SCENE_MARGIN + row * (size.height + 240);
    const radius = component.length > 1 ? Math.max(80, component.length * 24) : 0;
    const clusterId = component.length > 1 ? `scc-${componentIndex + 1}` : null;
    component.forEach((id, memberIndex) => {
      const node = nodesById.get(id)!;
      const angle = -Math.PI / 2 + (memberIndex * 2 * Math.PI) / component.length;
      nodes.push(
        sceneNode(
          node,
          baseX + radius + radius * Math.cos(angle),
          baseY + radius + radius * Math.sin(angle),
          detail,
          { layerScale: detail === 'detailed' ? 24 : 12, clusterId },
        ),
      );
    });
    if (clusterId !== null) componentNodes.push({ id: clusterId, ids: component });
  });
  const clusters = componentNodes.map((component, index) =>
    boundsCluster(component.id, 'scc', `Strongly connected component ${index + 1}`, component.ids, nodes),
  );
  return finishScene(graph, 'scc', nodes, clusters);
}

function radians(degrees: number): number {
  return (degrees * Math.PI) / 180;
}

export function geodesicDistanceKm(
  source: { readonly latitude: number; readonly longitude: number },
  target: { readonly latitude: number; readonly longitude: number },
): number {
  const latitude1 = radians(source.latitude);
  const latitude2 = radians(target.latitude);
  const deltaLatitude = latitude2 - latitude1;
  const deltaLongitude = radians(target.longitude - source.longitude);
  const haversine =
    Math.sin(deltaLatitude / 2) ** 2 +
    Math.cos(latitude1) * Math.cos(latitude2) * Math.sin(deltaLongitude / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(haversine)));
}

function bearingRadians(
  source: { readonly latitude: number; readonly longitude: number },
  target: { readonly latitude: number; readonly longitude: number },
): number {
  const latitude1 = radians(source.latitude);
  const latitude2 = radians(target.latitude);
  const deltaLongitude = radians(target.longitude - source.longitude);
  return Math.atan2(
    Math.sin(deltaLongitude) * Math.cos(latitude2),
    Math.cos(latitude1) * Math.sin(latitude2) -
      Math.sin(latitude1) * Math.cos(latitude2) * Math.cos(deltaLongitude),
  );
}

function medoid(nodes: readonly GraphIRNode[]): GraphIRNode | null {
  if (nodes.length === 0) return null;
  return [...nodes].sort((left, right) => {
    if (left.location.state !== 'known' || right.location.state !== 'known') return left.id.localeCompare(right.id);
    const leftLocation = left.location;
    const rightLocation = right.location;
    const leftTotal = nodes.reduce(
      (sum, node) =>
        node.location.state === 'known'
          ? sum + geodesicDistanceKm(leftLocation, node.location)
          : sum,
      0,
    );
    const rightTotal = nodes.reduce(
      (sum, node) =>
        node.location.state === 'known'
          ? sum + geodesicDistanceKm(rightLocation, node.location)
          : sum,
      0,
    );
    return leftTotal - rightTotal || left.id.localeCompare(right.id);
  })[0];
}

function fanOffset(index: number, count: number): [number, number] {
  if (count <= 1) return [0, 0];
  const angle = -Math.PI / 2 + (index * 2 * Math.PI) / count;
  return [Math.cos(angle) * 28, Math.sin(angle) * 28];
}

function geoLayout(graph: GraphIR, mode: 'elastic' | 'true-map'): SceneGraph {
  const detail: SceneDetail = graph.nodes.length <= 24 ? 'detailed' : 'compact';
  const known = graph.nodes.filter(
    (node): node is GraphIRNode & { location: Extract<NodeLocation, { state: 'known' }> } =>
      node.location.state === 'known',
  );
  const unknown = graph.nodes.filter((node) => node.location.state === 'unknown');
  const focus = medoid(known);
  const focusLocation = focus?.location.state === 'known' ? focus.location : null;
  const maxDistance =
    focusLocation === null
      ? 0
      : Math.max(1, ...known.map((node) => geodesicDistanceKm(focusLocation, node.location)));
  const coordinateGroups = new Map<string, GraphIRNode[]>();
  for (const node of known) {
    const key = `${node.location.latitude},${node.location.longitude}`;
    const members = coordinateGroups.get(key) ?? [];
    members.push(node);
    coordinateGroups.set(key, members);
  }

  const equalEarth = geoEqualEarth().scale(155).translate([GEO_MAP_RIGHT / 2, GEO_HEIGHT / 2]);
  const nodes: SceneNode[] = [];
  for (const node of known) {
    let anchorX: number;
    let anchorY: number;
    if (mode === 'elastic' && focusLocation !== null) {
      const distance = geodesicDistanceKm(focusLocation, node.location);
      const bearing = bearingRadians(focusLocation, node.location);
      const radius =
        distance === 0
          ? 0
          : 300 * Math.asinh(distance / ELASTIC_KNEE_KM) / Math.asinh(maxDistance / ELASTIC_KNEE_KM);
      anchorX = 430 + radius * Math.sin(bearing);
      anchorY = 350 - radius * Math.cos(bearing);
    } else {
      const projected = equalEarth([node.location.longitude, node.location.latitude]);
      if (projected === null || !Number.isFinite(projected[0]) || !Number.isFinite(projected[1])) {
        throw new Error(`Could not project known location for node: ${node.id}`);
      }
      [anchorX, anchorY] = projected;
    }
    const group = coordinateGroups.get(`${node.location.latitude},${node.location.longitude}`)!;
    const [offsetX, offsetY] = fanOffset(group.findIndex((member) => member.id === node.id), group.length);
    nodes.push(
      sceneNode(node, anchorX + offsetX, anchorY + offsetY, detail, {
        layerScale: detail === 'detailed' ? 24 : 12,
        anchorX,
        anchorY,
      }),
    );
  }
  unknown.forEach((node, index) => {
    nodes.push(
      sceneNode(
        node,
        UNKNOWN_TRAY_X,
        SCENE_MARGIN + index * (dimensions(detail).height + UNKNOWN_TRAY_GAP),
        detail,
        { tray: 'unknown-location' },
      ),
    );
  });

  const layout = mode === 'elastic' ? 'geo' : 'map';
  const warnings =
    mode === 'elastic'
      ? ['ELASTIC GEO — distances compressed', 'Edge lengths are not to scale']
      : ['TRUE MAP — projected WGS84 coordinates; geodesic distance remains inspectable'];
  const metadata: SceneGeoMetadata = {
    mode,
    projection: mode === 'elastic' ? 'geodesic-medoid azimuthal bearing + asinh radius' : 'Equal Earth',
    distancesToScale: false,
    focusNodeId: focus?.id ?? null,
    compressionKneeKm: mode === 'elastic' ? ELASTIC_KNEE_KM : null,
  };
  const scene = finishScene(graph, layout, nodes, [], warnings, metadata);
  return {
    ...scene,
    width: Math.max(scene.width, ELASTIC_WIDTH),
    height: Math.max(scene.height, GEO_HEIGHT),
  };
}

interface CachedGeometry {
  readonly scene: SceneGraph;
}

const layoutCache = new Map<string, CachedGeometry>();

export function clearLayoutCache(): void {
  layoutCache.clear();
}

function rebindScene(scene: SceneGraph, graph: GraphIR): SceneGraph {
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const edges = new Map(graph.edges.map((edge) => [edge.id, edge]));
  const physical = new Map(graph.physicalEdges.map((edge) => [edge.id, edge]));
  return {
    ...scene,
    nodes: scene.nodes.map((node) => ({ ...node, ...nodes.get(node.id) })),
    edges: scene.edges.map((edge) => ({ ...edge, ...edges.get(edge.id) })),
    physicalEdges: scene.physicalEdges.map((edge) => ({ ...edge, ...physical.get(edge.id) })),
  };
}

/** Computes stable positions while preserving semantic direction and current metric evidence. */
export async function layoutGraph(graph: GraphIR, layout: GraphLayout): Promise<SceneGraph> {
  validateGraph(graph);
  const signature = topologySignature(graph);
  const cacheKey = `${layout}:${signature}`;
  const cached = layoutCache.get(cacheKey);
  if (cached !== undefined) return rebindScene(cached.scene, graph);

  let scene: SceneGraph;
  switch (layout) {
    case 'pipeline':
      scene = pipelineLayout(graph);
      break;
    case 'ring':
      scene = ringLayout(graph);
      break;
    case 'scc':
      scene = sccLayout(graph);
      break;
    case 'geo':
      scene = geoLayout(graph, 'elastic');
      break;
    case 'map':
      scene = geoLayout(graph, 'true-map');
      break;
  }
  layoutCache.set(cacheKey, { scene });
  return scene;
}
