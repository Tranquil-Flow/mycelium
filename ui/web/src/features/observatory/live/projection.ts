import type { ObservatoryAdapterBundle } from '../../../data/observatoryEventProjection';
import type { ObservatoryBundle } from '../../../data/observatorySource';
import { deepFreeze } from '../../../model/runtime';
import type {
  EvidenceLink,
  EvidenceNode,
  EvidenceRoute,
  FailoverIncident,
  ProvisioningAssignment,
} from '../../../model/types';
import type {
  ObservatoryChangeInventory,
  ObservatoryVersionedEntity,
} from './changeSet';

export interface ProductObservatoryEntity<T = unknown> extends ObservatoryVersionedEntity {
  readonly value: T;
}

export interface ProductObservatoryEntities extends ObservatoryChangeInventory {
  readonly nodes: readonly ProductObservatoryEntity<EvidenceNode>[];
  readonly edges: readonly ProductObservatoryEntity<EvidenceLink>[];
  readonly routes: readonly ProductObservatoryEntity<unknown>[];
  readonly readiness: readonly ProductObservatoryEntity<unknown>[];
  readonly evidence: readonly ProductObservatoryEntity<unknown>[];
}

export interface ProductObservatoryMetrics {
  readonly native_node_count: number | null;
  readonly browser_worker_count: number | null;
  readonly incident_count: number | null;
}

export interface ProductObservatoryProjection {
  readonly source_kind: 'fixture' | 'event_adapter';
  /** The already validated public projection. It contains no prompt, token, endpoint, or secret data. */
  readonly source: ObservatoryBundle | ObservatoryAdapterBundle;
  readonly observed_at_unix_ms: number | null;
  /** Copied literally from the validated source; this module never promotes readiness. */
  readonly route_ready: boolean;
  readonly metrics: ProductObservatoryMetrics;
  readonly entities: ProductObservatoryEntities;
}

const FNV_128_OFFSET = 0x6c62272e07bb014262b821756295c58dn;
const FNV_128_PRIME = 0x0000000001000000000000000000013bn;
const UINT128_MASK = (1n << 128n) - 1n;

function canonicalJson(value: unknown, seen = new Set<object>()): string {
  if (value === null) return 'null';
  if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('Observatory revision input must be finite');
    return JSON.stringify(value);
  }
  if (typeof value !== 'object') {
    throw new TypeError('Observatory revision input must be public JSON data');
  }
  if (seen.has(value)) throw new TypeError('Observatory revision input must be acyclic');
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      const keys = Reflect.ownKeys(value);
      if (
        keys.length !== value.length + 1 ||
        keys.some(
          (key) => key !== 'length' && (typeof key !== 'string' || !/^(?:0|[1-9][0-9]*)$/.test(key)),
        )
      ) {
        throw new TypeError('Observatory revision arrays must be dense public JSON data');
      }
      return `[${value.map((item) => canonicalJson(item, seen)).join(',')}]`;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError('Observatory revision objects must be plain public JSON data');
    }
    const keys = Reflect.ownKeys(value);
    if (keys.some((key) => typeof key !== 'string')) {
      throw new TypeError('Observatory revision objects must not have hidden fields');
    }
    return `{${(keys as string[])
      .sort()
      .map((key) => {
        const descriptor = Object.getOwnPropertyDescriptor(value, key);
        if (descriptor === undefined || !('value' in descriptor) || !descriptor.enumerable) {
          throw new TypeError('Observatory revision objects must contain enumerable data fields');
        }
        return `${JSON.stringify(key)}:${canonicalJson(descriptor.value, seen)}`;
      })
      .join(',')}}`;
  } finally {
    seen.delete(value);
  }
}

/** A deterministic, fixed-size revision used only for change highlighting. */
export function productObservatoryRevision(value: unknown): string {
  let hash = FNV_128_OFFSET;
  for (const byte of new TextEncoder().encode(canonicalJson(value))) {
    hash ^= BigInt(byte);
    hash = (hash * FNV_128_PRIME) & UINT128_MASK;
  }
  return `v1:${hash.toString(16).padStart(32, '0')}`;
}

function entity<T>(id: string, value: T): ProductObservatoryEntity<T> {
  if (typeof id !== 'string' || id.length === 0) {
    throw new TypeError('Product Observatory entity id must be non-empty');
  }
  return { id, revision: productObservatoryRevision(value), value };
}

function byId<T extends ObservatoryVersionedEntity>(items: readonly T[]): readonly T[] {
  const sorted = [...items].sort((left, right) => left.id.localeCompare(right.id));
  for (let index = 1; index < sorted.length; index += 1) {
    if (sorted[index - 1].id === sorted[index].id) {
      throw new TypeError(`Product Observatory projection has duplicate entity id ${sorted[index].id}`);
    }
  }
  return sorted;
}

function fixtureReadiness(
  assignments: readonly ProvisioningAssignment[],
  source: ObservatoryBundle,
): readonly ProductObservatoryEntity<unknown>[] {
  return byId([
    entity('route-readiness', source.provisioning),
    ...assignments.map((assignment) =>
      entity(
        `assignment~${assignment.nodeId}~${assignment.startLayer}-${assignment.endLayerExclusive}`,
        assignment,
      ),
    ),
  ]);
}

function fixtureEvidence(
  incidents: readonly FailoverIncident[],
): readonly ProductObservatoryEntity<unknown>[] {
  return byId(incidents.map((incident) => entity(`incident~${incident.id}`, incident)));
}

export function projectFixtureObservatoryBundle(
  bundle: ObservatoryBundle,
): ProductObservatoryProjection {
  if (bundle.provisioning.routeReady !== false) {
    throw new TypeError('Fixture Observatory projection cannot claim route readiness');
  }
  return deepFreeze({
    source_kind: 'fixture' as const,
    source: bundle,
    observed_at_unix_ms: null,
    route_ready: bundle.provisioning.routeReady,
    metrics: {
      native_node_count: bundle.snapshot.nodes.length,
      browser_worker_count: null,
      incident_count: bundle.incidents.length,
    },
    entities: {
      nodes: byId(bundle.snapshot.nodes.map((node) => entity(node.id, node))),
      edges: byId(bundle.snapshot.links.map((link) => entity(link.id, link))),
      routes: byId(
        bundle.snapshot.routes.map((route: EvidenceRoute) => entity(route.id, route)),
      ),
      readiness: fixtureReadiness(bundle.provisioning.assignments, bundle),
      evidence: fixtureEvidence(bundle.incidents),
    },
  });
}

export function projectLiveObservatoryBundle(
  bundle: ObservatoryAdapterBundle,
): ProductObservatoryProjection {
  const qualification = bundle.snapshot.qualification;
  const routeEntities =
    qualification === null
      ? []
      : [entity(`qualification~${qualification.qualification_id}`, qualification.binding)];
  const evidence = [
    ...bundle.snapshot.sessions.map((session) => entity(`request~${session.request_id}`, session)),
    ...bundle.incidents.map((incident, index) =>
      entity(`incident~${incident.source_cursor}~${index}`, incident),
    ),
  ];
  return deepFreeze({
    source_kind: 'event_adapter' as const,
    source: bundle,
    observed_at_unix_ms: bundle.snapshot.observed_at_unix_ms,
    route_ready: bundle.provisioning.route_ready,
    metrics: {
      native_node_count: null,
      browser_worker_count: null,
      incident_count: bundle.incidents.length,
    },
    entities: {
      nodes: [],
      edges: [],
      routes: byId(routeEntities),
      readiness: [
        entity('route-readiness', {
          provisioning: bundle.provisioning,
          qualification,
        }),
      ],
      evidence: byId(evidence),
    },
  });
}
