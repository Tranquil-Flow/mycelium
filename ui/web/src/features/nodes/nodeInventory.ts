import type {
  EvidenceNode,
  EvidenceSnapshot,
  ProvisioningEvidence,
} from '../../model/types';
import type { ReadinessState } from '../readiness/readinessModel';

export interface UnknownInventoryValue {
  readonly state: 'unknown';
  readonly reason: string;
}

export interface KnownInventoryValue<T> {
  readonly state: 'known';
  readonly value: T;
}

export type InventoryValue<T> = KnownInventoryValue<T> | UnknownInventoryValue;
export type NodeInventoryScope = 'simulation' | 'artifact_provisioning';
export type NodeSortKey = 'id' | 'scope' | 'memory' | 'assignment' | 'readiness';

export interface NodeInventoryItem {
  readonly key: string;
  readonly id: string;
  readonly alias: string;
  readonly scope: NodeInventoryScope;
  readonly identityMapping: 'not_established';
  readonly deviceClass: InventoryValue<string>;
  readonly platform: InventoryValue<string>;
  readonly architecture: InventoryValue<string>;
  readonly runtimeBackend: InventoryValue<string>;
  readonly precision: InventoryValue<string>;
  readonly memory: {
    readonly architecture: InventoryValue<'unified' | 'discrete'>;
    readonly availableGb: number | null;
    readonly ramAvailableGb: number | null;
    readonly vramAvailableGb: number | null;
  };
  readonly compute: {
    readonly gpuTeraflops: number | null;
    readonly cpuTeraflops: number | null;
  };
  readonly assignment: {
    readonly exactRange: string;
    readonly humanRange: string;
    readonly layerCount: number;
  } | null;
  readonly routeRole: 'primary' | 'alternative' | 'unassigned';
  readonly locationPrecision: string;
  readonly locationLabel: string;
  readonly evidenceSource: string;
  readonly evidenceTimestamp: string | null;
  readonly readiness: {
    readonly artifactsVerified: ReadinessState;
    readonly runtimeLoaded: ReadinessState;
    readonly stageProbed: ReadinessState;
    readonly routeReady: ReadinessState;
  };
}

const unknown = (reason: string): UnknownInventoryValue => Object.freeze({ state: 'unknown', reason });
const known = <T,>(value: T): KnownInventoryValue<T> => Object.freeze({ state: 'known', value });

function primaryRoute(snapshot: EvidenceSnapshot) {
  return (
    snapshot.routes.find((route) => route.id === 'throughput_pruned_local') ??
    [...snapshot.routes].sort(
      (left, right) =>
        right.metrics.combinedTokensPerSecond.value - left.metrics.combinedTokensPerSecond.value,
    )[0]
  );
}

function simulationItem(
  node: EvidenceNode,
  snapshot: EvidenceSnapshot,
): NodeInventoryItem {
  const selected = primaryRoute(snapshot);
  const stage = selected?.stages.find((candidate) => candidate.nodeId === node.id);
  const appearsElsewhere = snapshot.routes.some((route) => route.nodeOrder.includes(node.id));
  const locationLabel =
    node.location.state === 'known'
      ? `${node.location.city}, ${node.location.country}`
      : 'Unknown location';
  const locationPrecision =
    node.location.state === 'known' ? node.location.precision : `Unknown · ${node.location.reason}`;
  const availableGb = node.resources.unifiedMemory
    ? Math.max(node.resources.ramAvailableGb, node.resources.vramAvailableGb)
    : node.resources.ramAvailableGb + node.resources.vramAvailableGb;

  return Object.freeze({
    key: `simulation:${node.id}`,
    id: node.id,
    alias: node.id,
    scope: 'simulation',
    identityMapping: 'not_established',
    deviceClass: unknown('Device class is not supplied by the simulator projection.'),
    platform: unknown('Platform is not supplied by the simulator projection.'),
    architecture: unknown('CPU architecture is not supplied by the simulator projection.'),
    runtimeBackend: unknown('Runtime backend proof is not supplied.'),
    precision: unknown('Runtime precision proof is not supplied.'),
    memory: Object.freeze({
      architecture: known<'unified' | 'discrete'>(
        node.resources.unifiedMemory ? 'unified' : 'discrete',
      ),
      availableGb,
      ramAvailableGb: node.resources.ramAvailableGb,
      vramAvailableGb: node.resources.vramAvailableGb,
    }),
    compute: Object.freeze({
      gpuTeraflops: node.resources.gpuTeraflops,
      cpuTeraflops: node.resources.cpuTeraflops,
    }),
    assignment:
      stage === undefined
        ? null
        : Object.freeze({
            exactRange: `[${stage.startLayer},${stage.endLayerExclusive})`,
            humanRange: `L${stage.startLayer}–L${stage.endLayerExclusive - 1}`,
            layerCount: stage.layerCount,
          }),
    routeRole: stage === undefined ? (appearsElsewhere ? 'alternative' : 'unassigned') : 'primary',
    locationPrecision,
    locationLabel,
    evidenceSource: snapshot.source.reportProtocol,
    evidenceTimestamp: snapshot.source.generatedAt,
    readiness: Object.freeze({
      artifactsVerified: 'NOT_APPLICABLE',
      runtimeLoaded: 'NOT_PROVEN',
      stageProbed: 'NOT_PROVEN',
      routeReady: 'NOT_PROVEN',
    }),
  });
}

function provisioningItems(provisioning: ProvisioningEvidence): readonly NodeInventoryItem[] {
  return provisioning.nodeIds.map((nodeId) => {
    const assignment = provisioning.assignments.find((candidate) => candidate.nodeId === nodeId);
    return Object.freeze({
      key: `artifact_provisioning:${nodeId}`,
      id: nodeId,
      alias: nodeId,
      scope: 'artifact_provisioning' as const,
      identityMapping: 'not_established' as const,
      deviceClass: unknown('Hardware profile is not supplied in this provisioning projection.'),
      platform: unknown('Platform is not supplied in this provisioning projection.'),
      architecture: unknown('Architecture is not supplied in this provisioning projection.'),
      runtimeBackend: unknown('Runtime backend proof is not supplied.'),
      precision: unknown('Runtime precision proof is not supplied.'),
      memory: Object.freeze({
        architecture: unknown('Memory architecture is not supplied.'),
        availableGb: null,
        ramAvailableGb: null,
        vramAvailableGb: null,
      }),
      compute: Object.freeze({ gpuTeraflops: null, cpuTeraflops: null }),
      assignment:
        assignment === undefined
          ? null
          : Object.freeze({
              exactRange: `[${assignment.startLayer},${assignment.endLayerExclusive})`,
              humanRange: `L${assignment.startLayer}–L${assignment.endLayerExclusive - 1}`,
              layerCount: assignment.layerCount,
            }),
      routeRole: assignment === undefined ? ('unassigned' as const) : ('primary' as const),
      locationPrecision: 'Unknown · not supplied',
      locationLabel: 'Unknown location',
      evidenceSource: provisioning.protocols.provisioningAudit,
      evidenceTimestamp: provisioning.auditedAt,
      readiness: Object.freeze({
        artifactsVerified: provisioning.allAssignmentsVerified ? 'PROVEN' : 'NOT_PROVEN',
        runtimeLoaded: 'NOT_PROVEN',
        stageProbed: 'NOT_PROVEN',
        routeReady: provisioning.routeReady ? 'PROVEN' : 'NOT_PROVEN',
      }),
    });
  });
}

export function projectNodeInventory(
  snapshot: EvidenceSnapshot,
  provisioning: ProvisioningEvidence,
): readonly NodeInventoryItem[] {
  return Object.freeze([
    ...snapshot.nodes.map((node) => simulationItem(node, snapshot)),
    ...provisioningItems(provisioning),
  ]);
}

export interface NodeInventoryQuery {
  readonly query: string;
  readonly key: NodeSortKey;
  readonly direction: 'asc' | 'desc';
}

function sortValue(node: NodeInventoryItem, key: NodeSortKey): string | number {
  switch (key) {
    case 'id':
      return node.id;
    case 'scope':
      return node.scope;
    case 'memory':
      return node.memory.availableGb ?? Number.NEGATIVE_INFINITY;
    case 'assignment':
      return node.assignment?.exactRange ?? '';
    case 'readiness':
      return node.readiness.routeReady;
  }
}

export function filterAndSortNodes(
  nodes: readonly NodeInventoryItem[],
  query: NodeInventoryQuery,
): readonly NodeInventoryItem[] {
  const needle = query.query.trim().toLocaleLowerCase();
  const filtered = nodes.filter((node) => {
    if (needle.length === 0) return true;
    const searchable = [
      node.id,
      node.alias,
      node.scope.replaceAll('_', ' '),
      node.routeRole,
      node.locationLabel,
      node.locationPrecision,
      node.assignment?.exactRange ?? '',
      node.evidenceSource,
    ]
      .join(' ')
      .toLocaleLowerCase();
    return searchable.includes(needle);
  });
  const direction = query.direction === 'asc' ? 1 : -1;
  filtered.sort((left, right) => {
    const leftValue = sortValue(left, query.key);
    const rightValue = sortValue(right, query.key);
    const compared =
      typeof leftValue === 'number' && typeof rightValue === 'number'
        ? leftValue - rightValue
        : String(leftValue).localeCompare(String(rightValue));
    return compared === 0 ? left.key.localeCompare(right.key) : compared * direction;
  });
  return Object.freeze(filtered);
}

export function redactedNodeDetail(node: NodeInventoryItem) {
  return Object.freeze({
    redaction: 'Allowlisted redacted projection; secrets, private addresses, and local paths omitted.',
    identity: Object.freeze({
      displayId: node.id,
      scope: node.scope,
      mapping: node.identityMapping,
    }),
    hardware: Object.freeze({
      deviceClass: node.deviceClass,
      platform: node.platform,
      architecture: node.architecture,
      compute: node.compute,
      memory: node.memory,
    }),
    runtime: Object.freeze({ backend: node.runtimeBackend, precision: node.precision }),
    assignment: node.assignment,
    routeRole: node.routeRole,
    location: Object.freeze({
      label: node.locationLabel,
      precision: node.locationPrecision,
    }),
    readiness: node.readiness,
    evidence: Object.freeze({
      source: node.evidenceSource,
      timestamp: node.evidenceTimestamp,
    }),
  });
}
