import { deepFreeze } from '../../model/runtime';

export const PRODUCT_SNAPSHOT_PROTOCOL = 'mycelium.product_snapshot.v1' as const;
export const PRODUCT_EVENT_PROTOCOL = 'mycelium.product_event.v1' as const;
export const PRODUCT_ENTITY_KINDS = Object.freeze([
  'artifact',
  'assignment',
  'device',
  'directed_link',
  'incident',
  'load_proof',
  'qualification',
  'request',
  'route',
  'runtime_kv_ownership',
  'source_provenance',
  'stage',
] as const);

export type ProductSnapshotMode = 'fixture' | 'replay' | 'degraded' | 'live';
export type ProductEntityKind = (typeof PRODUCT_ENTITY_KINDS)[number];
export type ProductSourceStatus =
  | 'current'
  | 'stale'
  | 'missing'
  | 'conflict'
  | 'unsupported'
  | 'replay';

export interface ProductBinding {
  readonly deployment_id: string | null;
  readonly deployment_epoch: number | null;
  readonly route_id: string | null;
  readonly route_generation: number | null;
  readonly topology_version: number | null;
}

export interface ProductFreshnessRecord {
  readonly status: ProductSourceStatus;
  readonly observed_at_unix_ms: number | null;
  readonly valid_until_unix_ms: number | null;
}

export interface ProductSourceState extends ProductFreshnessRecord {
  readonly source_id: string;
  readonly authority: string;
  readonly generation: number | null;
  readonly reason_code: string | null;
}

export interface ProductEntity {
  readonly entity_id: string;
  readonly kind: ProductEntityKind;
  readonly label: string;
  readonly source_id: string;
  readonly binding: ProductBinding;
  readonly freshness: ProductFreshnessRecord;
  readonly attributes: Readonly<Record<string, unknown>>;
}

export interface ProductRelation {
  readonly relation_id: string;
  readonly kind:
    | 'assigned_to'
    | 'observes'
    | 'owns'
    | 'placed_on'
    | 'produced_by'
    | 'qualifies'
    | 'reports';
  readonly from_entity_id: string;
  readonly to_entity_id: string;
  readonly source_id: string;
}

export interface ProductReadinessRecord {
  readonly scope_id: string;
  readonly dimension:
    | 'artifacts'
    | 'membership'
    | 'product_source'
    | 'qualification'
    | 'route_challenge'
    | 'runtime'
    | 'transport';
  readonly state: 'ready' | 'not_ready' | 'unknown' | 'unsupported';
  readonly reason_code: string | null;
  readonly source_id: string;
}

export interface ProductNotice {
  readonly notice_id: string;
  readonly scope_id: string;
  readonly severity: 'info' | 'warning' | 'error';
  readonly code: string;
  readonly source_id: string;
}

export interface ProductSnapshot {
  readonly protocol: typeof PRODUCT_SNAPSHOT_PROTOCOL;
  readonly publication: {
    readonly snapshot_id: string;
    readonly generation: number;
    readonly cursor: number;
    readonly published_at_unix_ms: number;
    readonly source_mode: ProductSnapshotMode;
  };
  readonly supported_entity_kinds: readonly ProductEntityKind[];
  readonly source_states: readonly ProductSourceState[];
  readonly entities: readonly ProductEntity[];
  readonly relations: readonly ProductRelation[];
  readonly readiness: readonly ProductReadinessRecord[];
  readonly notices: readonly ProductNotice[];
  readonly provenance: {
    readonly projector: string;
    readonly projector_version: string;
    readonly source_mode: ProductSnapshotMode;
  };
}

export interface ProductSnapshotEvent {
  readonly protocol: typeof PRODUCT_EVENT_PROTOCOL;
  readonly cursor: number;
  readonly previous_cursor: number;
  readonly event_kind: 'conflict' | 'snapshot_published' | 'source_degraded' | 'source_recovered';
  readonly snapshot: ProductSnapshot;
}

export class ProductEvidenceContractError extends TypeError {
  constructor(readonly field: string, detail: string) {
    super(`${field}: ${detail}`);
    this.name = 'ProductEvidenceContractError';
  }
}

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$/;
const CODE = /^[a-z][a-z0-9_]{0,63}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:\/[A-Za-z0-9][A-Za-z0-9._-]{0,127})?$/;
const MAX_ITEMS = 4_096;

function fail(field: string, detail: string): never {
  throw new ProductEvidenceContractError(field, detail);
}

function exact(value: unknown, keys: readonly string[], field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return fail(field, 'expected object');
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    return fail(field, 'expected plain object');
  }
  const candidate = value as Record<string, unknown>;
  const actual = Object.keys(candidate);
  if (
    actual.length !== keys.length
    || actual.some((key) => !keys.includes(key))
    || keys.some((key) => !Object.prototype.hasOwnProperty.call(candidate, key))
  ) {
    return fail(field, 'unexpected fields');
  }
  return candidate;
}

function own(value: Record<string, unknown>, key: string, field: string): unknown {
  try {
    return value[key];
  } catch {
    return fail(field, 'unreadable field');
  }
}

function text(value: unknown, field: string, pattern = IDENTIFIER): string {
  if (typeof value !== 'string' || !pattern.test(value)) return fail(field, 'invalid string');
  return value;
}

function label(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length < 1 || value.length > 256) {
    return fail(field, 'invalid public label');
  }
  return value;
}

function integer(value: unknown, field: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    return fail(field, 'invalid safe integer');
  }
  return value as number;
}

function nullableInteger(value: unknown, field: string): number | null {
  return value === null ? null : integer(value, field);
}

function nullableIdentifier(value: unknown, field: string): string | null {
  return value === null ? null : text(value, field);
}

function literalBoolean(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') return fail(field, 'invalid boolean');
  return value;
}

function enumeration<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  field: string,
): T[number] {
  if (typeof value !== 'string' || !allowed.includes(value)) {
    return fail(field, 'unsupported enum');
  }
  return value as T[number];
}

function array(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value) || value.length > MAX_ITEMS) return fail(field, 'invalid array');
  return value;
}

function uniqueStrings(
  value: unknown,
  field: string,
  decoder: (item: unknown, itemField: string) => string = text,
): readonly string[] {
  const decoded = array(value, field).map((item, index) => decoder(item, `${field}[${index}]`));
  if (new Set(decoded).size !== decoded.length) return fail(field, 'duplicate value');
  return decoded;
}

function binding(value: unknown, field: string): ProductBinding {
  const candidate = exact(
    value,
    ['deployment_id', 'deployment_epoch', 'route_id', 'route_generation', 'topology_version'],
    field,
  );
  return {
    deployment_id: nullableIdentifier(own(candidate, 'deployment_id', field), `${field}.deployment_id`),
    deployment_epoch: nullableInteger(own(candidate, 'deployment_epoch', field), `${field}.deployment_epoch`),
    route_id: nullableIdentifier(own(candidate, 'route_id', field), `${field}.route_id`),
    route_generation: nullableInteger(own(candidate, 'route_generation', field), `${field}.route_generation`),
    topology_version: nullableInteger(own(candidate, 'topology_version', field), `${field}.topology_version`),
  };
}

function freshness(value: unknown, field: string): ProductFreshnessRecord {
  const candidate = exact(value, ['status', 'observed_at_unix_ms', 'valid_until_unix_ms'], field);
  const observed = nullableInteger(own(candidate, 'observed_at_unix_ms', field), `${field}.observed_at_unix_ms`);
  const validUntil = nullableInteger(own(candidate, 'valid_until_unix_ms', field), `${field}.valid_until_unix_ms`);
  if (observed !== null && validUntil !== null && validUntil < observed) {
    return fail(field, 'invalid freshness interval');
  }
  return {
    status: enumeration(
      own(candidate, 'status', field),
      ['current', 'stale', 'missing', 'conflict', 'unsupported', 'replay'] as const,
      `${field}.status`,
    ),
    observed_at_unix_ms: observed,
    valid_until_unix_ms: validUntil,
  };
}

function decodeSource(value: unknown, index: number): ProductSourceState {
  const field = `snapshot.source_states[${index}]`;
  const candidate = exact(
    value,
    ['source_id', 'authority', 'status', 'observed_at_unix_ms', 'valid_until_unix_ms', 'generation', 'reason_code'],
    field,
  );
  const decodedFreshness = freshness(
    {
      status: own(candidate, 'status', field),
      observed_at_unix_ms: own(candidate, 'observed_at_unix_ms', field),
      valid_until_unix_ms: own(candidate, 'valid_until_unix_ms', field),
    },
    field,
  );
  const reasonValue = own(candidate, 'reason_code', field);
  const reason = reasonValue === null ? null : text(reasonValue, `${field}.reason_code`, CODE);
  if ((decodedFreshness.status === 'current' || decodedFreshness.status === 'replay') !== (reason === null)) {
    return fail(field, 'inconsistent source reason');
  }
  return {
    source_id: text(own(candidate, 'source_id', field), `${field}.source_id`),
    authority: label(own(candidate, 'authority', field), `${field}.authority`),
    ...decodedFreshness,
    generation: nullableInteger(own(candidate, 'generation', field), `${field}.generation`),
    reason_code: reason,
  };
}

const ATTRIBUTE_KEYS: Readonly<Record<ProductEntityKind, readonly string[]>> = Object.freeze({
  artifact: ['artifact_digest', 'size_bytes', 'cache_state'],
  assignment: ['device_id', 'stage_id', 'membership_generation', 'load_generation', 'assignment_digest', 'stage_pack_digest'],
  device: ['peer_class', 'membership_generation', 'authority_generation', 'incarnation', 'lifecycle', 'lease_freshness', 'runtime_backend', 'transport', 'activation_protocol', 'activation_eligible', 'placement_id'],
  directed_link: ['src_device_id', 'dst_device_id', 'connectivity', 'measurement_digest'],
  incident: ['state', 'reason_code', 'observed_at_unix_ms'],
  load_proof: ['proof_digest', 'assignment_id', 'load_generation', 'ready'],
  qualification: ['qualification_digest', 'route_ready', 'issued_at_unix_ms', 'expires_at_unix_ms', 'reason_codes'],
  request: ['state', 'path_attempt', 'sequence', 'qualification_id'],
  route: ['deployment_id', 'model_id', 'topology_version', 'decode_mode', 'placement_provenance', 'route_alive', 'concurrency_eligible', 'cancellation_cleanup_bound_ms', 'publisher_generation_fenced', 'scoped_liveness_proven'],
  runtime_kv_ownership: ['device_id', 'stage_id', 'decode_mode', 'kv_state_count', 'kv_byte_budget', 'proof_digest'],
  source_provenance: ['authority', 'source_protocol', 'source_generation', 'evidence_digest'],
  stage: ['stage_index', 'start_layer', 'end_layer_exclusive', 'component_roles', 'decode_mode'],
});

function digestOrNull(value: unknown, field: string): string | null {
  return value === null ? null : text(value, field, DIGEST);
}

function decodeAttributes(kind: ProductEntityKind, value: unknown, field: string): Record<string, unknown> {
  const candidate = exact(value, ATTRIBUTE_KEYS[kind], field);
  const read = (key: string) => own(candidate, key, `${field}.${key}`);
  switch (kind) {
    case 'device':
      text(read('peer_class'), `${field}.peer_class`);
      integer(read('membership_generation'), `${field}.membership_generation`, 1);
      integer(read('authority_generation'), `${field}.authority_generation`, 1);
      text(read('incarnation'), `${field}.incarnation`);
      text(read('lifecycle'), `${field}.lifecycle`, CODE);
      enumeration(read('lease_freshness'), ['fresh', 'stale', 'expired'] as const, `${field}.lease_freshness`);
      text(read('runtime_backend'), `${field}.runtime_backend`);
      text(read('transport'), `${field}.transport`);
      nullableIdentifier(read('activation_protocol'), `${field}.activation_protocol`);
      literalBoolean(read('activation_eligible'), `${field}.activation_eligible`);
      nullableIdentifier(read('placement_id'), `${field}.placement_id`);
      break;
    case 'directed_link':
      text(read('src_device_id'), `${field}.src_device_id`);
      text(read('dst_device_id'), `${field}.dst_device_id`);
      enumeration(read('connectivity'), ['unknown', 'measured'] as const, `${field}.connectivity`);
      digestOrNull(read('measurement_digest'), `${field}.measurement_digest`);
      break;
    case 'stage': {
      const start = integer(read('start_layer'), `${field}.start_layer`);
      const end = integer(read('end_layer_exclusive'), `${field}.end_layer_exclusive`, 1);
      if (end <= start) return fail(field, 'invalid layer interval');
      integer(read('stage_index'), `${field}.stage_index`);
      uniqueStrings(read('component_roles'), `${field}.component_roles`);
      enumeration(read('decode_mode'), ['stage_local_kv', 'complete_context_replay'] as const, `${field}.decode_mode`);
      break;
    }
    case 'route':
      text(read('deployment_id'), `${field}.deployment_id`);
      text(read('model_id'), `${field}.model_id`, MODEL_ID);
      integer(read('topology_version'), `${field}.topology_version`);
      enumeration(read('decode_mode'), ['stage_local_kv', 'complete_context_replay'] as const, `${field}.decode_mode`);
      enumeration(read('placement_provenance'), ['operator_selected', 'planner_v2', 'frozen_fixture'] as const, `${field}.placement_provenance`);
      literalBoolean(read('route_alive'), `${field}.route_alive`);
      literalBoolean(read('concurrency_eligible'), `${field}.concurrency_eligible`);
      integer(read('cancellation_cleanup_bound_ms'), `${field}.cancellation_cleanup_bound_ms`);
      literalBoolean(read('publisher_generation_fenced'), `${field}.publisher_generation_fenced`);
      literalBoolean(read('scoped_liveness_proven'), `${field}.scoped_liveness_proven`);
      break;
    case 'qualification':
      text(read('qualification_digest'), `${field}.qualification_digest`, DIGEST);
      literalBoolean(read('route_ready'), `${field}.route_ready`);
      integer(read('issued_at_unix_ms'), `${field}.issued_at_unix_ms`);
      nullableInteger(read('expires_at_unix_ms'), `${field}.expires_at_unix_ms`);
      uniqueStrings(read('reason_codes'), `${field}.reason_codes`, (item, itemField) => text(item, itemField, CODE));
      break;
    case 'incident':
      text(read('state'), `${field}.state`, CODE);
      text(read('reason_code'), `${field}.reason_code`, CODE);
      integer(read('observed_at_unix_ms'), `${field}.observed_at_unix_ms`);
      break;
    case 'assignment':
      text(read('device_id'), `${field}.device_id`);
      text(read('stage_id'), `${field}.stage_id`);
      integer(read('membership_generation'), `${field}.membership_generation`, 1);
      integer(read('load_generation'), `${field}.load_generation`, 1);
      text(read('assignment_digest'), `${field}.assignment_digest`, DIGEST);
      text(read('stage_pack_digest'), `${field}.stage_pack_digest`, DIGEST);
      break;
    case 'artifact':
      text(read('artifact_digest'), `${field}.artifact_digest`, DIGEST);
      integer(read('size_bytes'), `${field}.size_bytes`);
      enumeration(read('cache_state'), ['verified', 'missing', 'corrupt', 'unknown'] as const, `${field}.cache_state`);
      break;
    case 'load_proof':
      text(read('proof_digest'), `${field}.proof_digest`, DIGEST);
      text(read('assignment_id'), `${field}.assignment_id`);
      integer(read('load_generation'), `${field}.load_generation`, 1);
      literalBoolean(read('ready'), `${field}.ready`);
      break;
    case 'runtime_kv_ownership':
      text(read('device_id'), `${field}.device_id`);
      text(read('stage_id'), `${field}.stage_id`);
      enumeration(read('decode_mode'), ['stage_local_kv', 'complete_context_replay'] as const, `${field}.decode_mode`);
      integer(read('kv_state_count'), `${field}.kv_state_count`);
      integer(read('kv_byte_budget'), `${field}.kv_byte_budget`);
      digestOrNull(read('proof_digest'), `${field}.proof_digest`);
      break;
    case 'request':
      enumeration(read('state'), ['accepted', 'running', 'completed', 'failed', 'cancelled'] as const, `${field}.state`);
      integer(read('path_attempt'), `${field}.path_attempt`, 1);
      integer(read('sequence'), `${field}.sequence`);
      text(read('qualification_id'), `${field}.qualification_id`);
      break;
    case 'source_provenance':
      label(read('authority'), `${field}.authority`);
      text(read('source_protocol'), `${field}.source_protocol`);
      nullableInteger(read('source_generation'), `${field}.source_generation`);
      digestOrNull(read('evidence_digest'), `${field}.evidence_digest`);
      break;
  }
  return { ...candidate };
}

function decodeEntity(value: unknown, index: number): ProductEntity {
  const field = `snapshot.entities[${index}]`;
  const candidate = exact(value, ['entity_id', 'kind', 'label', 'source_id', 'binding', 'freshness', 'attributes'], field);
  const kind = enumeration(own(candidate, 'kind', field), PRODUCT_ENTITY_KINDS, `${field}.kind`);
  return {
    entity_id: text(own(candidate, 'entity_id', field), `${field}.entity_id`),
    kind,
    label: label(own(candidate, 'label', field), `${field}.label`),
    source_id: text(own(candidate, 'source_id', field), `${field}.source_id`),
    binding: binding(own(candidate, 'binding', field), `${field}.binding`),
    freshness: freshness(own(candidate, 'freshness', field), `${field}.freshness`),
    attributes: decodeAttributes(kind, own(candidate, 'attributes', field), `${field}.attributes`),
  };
}

export function decodeProductSnapshot(value: unknown): ProductSnapshot {
  const candidate = exact(
    value,
    ['protocol', 'publication', 'supported_entity_kinds', 'source_states', 'entities', 'relations', 'readiness', 'notices', 'provenance'],
    'snapshot',
  );
  if (own(candidate, 'protocol', 'snapshot.protocol') !== PRODUCT_SNAPSHOT_PROTOCOL) {
    return fail('snapshot.protocol', 'unsupported protocol');
  }
  const publication = exact(
    own(candidate, 'publication', 'snapshot.publication'),
    ['snapshot_id', 'generation', 'cursor', 'published_at_unix_ms', 'source_mode'],
    'snapshot.publication',
  );
  const mode = enumeration(
    own(publication, 'source_mode', 'snapshot.publication'),
    ['fixture', 'replay', 'degraded', 'live'] as const,
    'snapshot.publication.source_mode',
  );
  const supported = uniqueStrings(
    own(candidate, 'supported_entity_kinds', 'snapshot.supported_entity_kinds'),
    'snapshot.supported_entity_kinds',
    (item, field) => enumeration(item, PRODUCT_ENTITY_KINDS, field),
  ) as readonly ProductEntityKind[];
  if (
    supported.length !== PRODUCT_ENTITY_KINDS.length
    || supported.some((kind, index) => kind !== PRODUCT_ENTITY_KINDS[index])
  ) return fail('snapshot.supported_entity_kinds', 'unsupported entity family');
  const sources = array(own(candidate, 'source_states', 'snapshot.source_states'), 'snapshot.source_states').map(decodeSource);
  const entities = array(own(candidate, 'entities', 'snapshot.entities'), 'snapshot.entities').map(decodeEntity);
  const sourceIds = new Set(sources.map((source) => source.source_id));
  const entityIds = new Set(entities.map((entity) => entity.entity_id));
  if (sourceIds.size !== sources.length || entityIds.size !== entities.length) {
    return fail('snapshot', 'duplicate entity or source ID');
  }
  if (entities.some((entity) => !sourceIds.has(entity.source_id))) {
    return fail('snapshot.entities', 'unbound source');
  }
  const relations = array(own(candidate, 'relations', 'snapshot.relations'), 'snapshot.relations').map((value, index): ProductRelation => {
    const field = `snapshot.relations[${index}]`;
    const item = exact(value, ['relation_id', 'kind', 'from_entity_id', 'to_entity_id', 'source_id'], field);
    return {
      relation_id: text(own(item, 'relation_id', field), `${field}.relation_id`),
      kind: enumeration(own(item, 'kind', field), ['assigned_to', 'observes', 'owns', 'placed_on', 'produced_by', 'qualifies', 'reports'] as const, `${field}.kind`),
      from_entity_id: text(own(item, 'from_entity_id', field), `${field}.from_entity_id`),
      to_entity_id: text(own(item, 'to_entity_id', field), `${field}.to_entity_id`),
      source_id: text(own(item, 'source_id', field), `${field}.source_id`),
    };
  });
  if (
    new Set(relations.map((relation) => relation.relation_id)).size !== relations.length
    || relations.some((relation) => !sourceIds.has(relation.source_id)
      || !entityIds.has(relation.from_entity_id)
      || !entityIds.has(relation.to_entity_id)
      || relation.from_entity_id === relation.to_entity_id)
  ) return fail('snapshot.relations', 'invalid relation binding');
  const readiness = array(own(candidate, 'readiness', 'snapshot.readiness'), 'snapshot.readiness').map((value, index): ProductReadinessRecord => {
    const field = `snapshot.readiness[${index}]`;
    const item = exact(value, ['scope_id', 'dimension', 'state', 'reason_code', 'source_id'], field);
    const reasonValue = own(item, 'reason_code', field);
    return {
      scope_id: text(own(item, 'scope_id', field), `${field}.scope_id`),
      dimension: enumeration(own(item, 'dimension', field), ['artifacts', 'membership', 'product_source', 'qualification', 'route_challenge', 'runtime', 'transport'] as const, `${field}.dimension`),
      state: enumeration(own(item, 'state', field), ['ready', 'not_ready', 'unknown', 'unsupported'] as const, `${field}.state`),
      reason_code: reasonValue === null ? null : text(reasonValue, `${field}.reason_code`, CODE),
      source_id: text(own(item, 'source_id', field), `${field}.source_id`),
    };
  });
  if (readiness.some((record) => !sourceIds.has(record.source_id))) {
    return fail('snapshot.readiness', 'unbound source');
  }
  const notices = array(own(candidate, 'notices', 'snapshot.notices'), 'snapshot.notices').map((value, index): ProductNotice => {
    const field = `snapshot.notices[${index}]`;
    const item = exact(value, ['notice_id', 'scope_id', 'severity', 'code', 'source_id'], field);
    return {
      notice_id: text(own(item, 'notice_id', field), `${field}.notice_id`),
      scope_id: text(own(item, 'scope_id', field), `${field}.scope_id`),
      severity: enumeration(own(item, 'severity', field), ['info', 'warning', 'error'] as const, `${field}.severity`),
      code: text(own(item, 'code', field), `${field}.code`, CODE),
      source_id: text(own(item, 'source_id', field), `${field}.source_id`),
    };
  });
  if (
    new Set(notices.map((notice) => notice.notice_id)).size !== notices.length
    || notices.some((notice) => !sourceIds.has(notice.source_id))
  ) return fail('snapshot.notices', 'invalid notice binding');
  const provenance = exact(
    own(candidate, 'provenance', 'snapshot.provenance'),
    ['projector', 'projector_version', 'source_mode'],
    'snapshot.provenance',
  );
  const provenanceMode = enumeration(
    own(provenance, 'source_mode', 'snapshot.provenance'),
    ['fixture', 'replay', 'degraded', 'live'] as const,
    'snapshot.provenance.source_mode',
  );
  if (provenanceMode !== mode) return fail('snapshot.provenance', 'source mode mismatch');
  return deepFreeze({
    protocol: PRODUCT_SNAPSHOT_PROTOCOL,
    publication: {
      snapshot_id: text(own(publication, 'snapshot_id', 'snapshot.publication'), 'snapshot.publication.snapshot_id'),
      generation: integer(own(publication, 'generation', 'snapshot.publication'), 'snapshot.publication.generation', 1),
      cursor: integer(own(publication, 'cursor', 'snapshot.publication'), 'snapshot.publication.cursor'),
      published_at_unix_ms: integer(own(publication, 'published_at_unix_ms', 'snapshot.publication'), 'snapshot.publication.published_at_unix_ms'),
      source_mode: mode,
    },
    supported_entity_kinds: supported,
    source_states: sources,
    entities,
    relations,
    readiness,
    notices,
    provenance: {
      projector: label(own(provenance, 'projector', 'snapshot.provenance'), 'snapshot.provenance.projector'),
      projector_version: text(own(provenance, 'projector_version', 'snapshot.provenance'), 'snapshot.provenance.projector_version'),
      source_mode: provenanceMode,
    },
  });
}

export function decodeProductSnapshotEvent(value: unknown): ProductSnapshotEvent {
  const candidate = exact(value, ['protocol', 'cursor', 'previous_cursor', 'event_kind', 'snapshot'], 'event');
  if (own(candidate, 'protocol', 'event.protocol') !== PRODUCT_EVENT_PROTOCOL) {
    return fail('event.protocol', 'unsupported protocol');
  }
  const cursor = integer(own(candidate, 'cursor', 'event.cursor'), 'event.cursor', 1);
  const previous = integer(own(candidate, 'previous_cursor', 'event.previous_cursor'), 'event.previous_cursor');
  const snapshot = decodeProductSnapshot(own(candidate, 'snapshot', 'event.snapshot'));
  if (cursor !== previous + 1 || snapshot.publication.cursor !== cursor) {
    return fail('event.cursor', 'non-contiguous or unbound cursor');
  }
  return deepFreeze({
    protocol: PRODUCT_EVENT_PROTOCOL,
    cursor,
    previous_cursor: previous,
    event_kind: enumeration(
      own(candidate, 'event_kind', 'event.event_kind'),
      ['conflict', 'snapshot_published', 'source_degraded', 'source_recovered'] as const,
      'event.event_kind',
    ),
    snapshot,
  });
}
