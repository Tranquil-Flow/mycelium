import { deepFreeze } from '../../../model/runtime';

export const OBSERVATORY_ENTITY_REVISION = /^v1:[0-9a-f]{32}$/;

export interface ObservatoryVersionedEntity {
  readonly id: string;
  /** Privacy-reduced deterministic revision; never raw evidence or telemetry. */
  readonly revision: string;
}

export interface ObservatoryChangeInventory {
  readonly nodes: readonly ObservatoryVersionedEntity[];
  readonly edges: readonly ObservatoryVersionedEntity[];
  readonly routes: readonly ObservatoryVersionedEntity[];
  readonly readiness: readonly ObservatoryVersionedEntity[];
  readonly evidence: readonly ObservatoryVersionedEntity[];
}

export interface ObservatoryEntityChange {
  readonly added: readonly string[];
  readonly removed: readonly string[];
  readonly changed: readonly string[];
}

export interface ObservatoryChangeSet {
  readonly from_generation: number | null;
  readonly to_generation: number | null;
  readonly empty: boolean;
  readonly nodes: ObservatoryEntityChange;
  readonly edges: ObservatoryEntityChange;
  readonly routes: ObservatoryEntityChange;
  readonly readiness: ObservatoryEntityChange;
  readonly evidence: ObservatoryEntityChange;
}

const CATEGORIES = ['nodes', 'edges', 'routes', 'readiness', 'evidence'] as const;
type ObservatoryChangeCategory = (typeof CATEGORIES)[number];

function emptyCategory(): ObservatoryEntityChange {
  return { added: [], removed: [], changed: [] };
}

function indexEntities(
  entities: readonly ObservatoryVersionedEntity[],
  category: ObservatoryChangeCategory,
): ReadonlyMap<string, string> {
  const indexed = new Map<string, string>();
  for (const entity of entities) {
    if (typeof entity.id !== 'string' || entity.id.length === 0) {
      throw new TypeError(`${category} entity id must be non-empty`);
    }
    if (!OBSERVATORY_ENTITY_REVISION.test(entity.revision)) {
      throw new TypeError(`${category} entity ${entity.id} has an invalid privacy-reduced revision`);
    }
    if (indexed.has(entity.id)) {
      throw new TypeError(`${category} contains duplicate entity id ${entity.id}`);
    }
    indexed.set(entity.id, entity.revision);
  }
  return indexed;
}

function compareCategory(
  previous: readonly ObservatoryVersionedEntity[],
  next: readonly ObservatoryVersionedEntity[],
  category: ObservatoryChangeCategory,
): ObservatoryEntityChange {
  const before = indexEntities(previous, category);
  const after = indexEntities(next, category);
  const added: string[] = [];
  const removed: string[] = [];
  const changed: string[] = [];

  for (const [id, revision] of after) {
    const previousRevision = before.get(id);
    if (previousRevision === undefined) added.push(id);
    else if (previousRevision !== revision) changed.push(id);
  }
  for (const id of before.keys()) {
    if (!after.has(id)) removed.push(id);
  }
  added.sort();
  removed.sort();
  changed.sort();
  return { added, removed, changed };
}

function validGeneration(value: number | null, name: string): number | null {
  if (value === null) return null;
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${name} must be a non-negative safe integer or null`);
  }
  return value;
}

export function emptyObservatoryChangeSet(
  fromGeneration: number | null,
  toGeneration: number | null,
): ObservatoryChangeSet {
  return deepFreeze({
    from_generation: validGeneration(fromGeneration, 'fromGeneration'),
    to_generation: validGeneration(toGeneration, 'toGeneration'),
    empty: true,
    nodes: emptyCategory(),
    edges: emptyCategory(),
    routes: emptyCategory(),
    readiness: emptyCategory(),
    evidence: emptyCategory(),
  });
}

export function calculateObservatoryChangeSet(
  previous: ObservatoryChangeInventory,
  next: ObservatoryChangeInventory,
  fromGeneration: number | null,
  toGeneration: number | null,
): ObservatoryChangeSet {
  const compared = {
    nodes: compareCategory(previous.nodes, next.nodes, 'nodes'),
    edges: compareCategory(previous.edges, next.edges, 'edges'),
    routes: compareCategory(previous.routes, next.routes, 'routes'),
    readiness: compareCategory(previous.readiness, next.readiness, 'readiness'),
    evidence: compareCategory(previous.evidence, next.evidence, 'evidence'),
  };
  const empty = CATEGORIES.every((category) => {
    const item = compared[category];
    return item.added.length === 0 && item.removed.length === 0 && item.changed.length === 0;
  });
  return deepFreeze({
    from_generation: validGeneration(fromGeneration, 'fromGeneration'),
    to_generation: validGeneration(toGeneration, 'toGeneration'),
    empty,
    ...compared,
  });
}
