import { deepFreeze } from './runtime';

export const OBSERVATORY_SNAPSHOT_PROTOCOL = 'mycelium.observatory.snapshot.v1' as const;
export const OBSERVATORY_EVENT_PROTOCOL = 'mycelium.observatory.event.v1' as const;

const SNAPSHOT_KEYS = [
  'protocol',
  'snapshot_id',
  'freshness',
  'binding',
  'claims',
  'conflicts',
  'route_challenge',
  'request_lifecycle',
  'provenance',
] as const;
const EVENT_KEYS = ['protocol', 'generation', 'snapshot'] as const;
const IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
const TIMESTAMP_RE =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$/;
const CREDENTIAL_RE =
  /(?:\bbearer\s+|\bsk-[a-z0-9_-]{12,}|\bgh[pousr]_[a-z0-9]{20,}|\bgithub_pat_[a-z0-9_]{20,}|-----BEGIN[ A-Z0-9_-]{0,48}PRIVATE KEY-----)/i;

const SCOPE_STATEMENTS = {
  deployment: 'deployment_bound',
  model: 'model_bound',
  route: 'route_challenge_succeeded',
  assignment: 'assignment_ready',
  request: 'request_lifecycle_observed',
} as const;
const SCOPE_PROVENANCE = {
  deployment: ['gateway_projection', 'mycelium_gateway'],
  model: ['provisioning_audit', 'mycelium_provisioning'],
  route: ['route_challenge', 'mycelium_router'],
  assignment: ['provisioning_audit', 'mycelium_provisioning'],
  request: ['router_runtime', 'mycelium_router'],
} as const satisfies Record<keyof typeof SCOPE_STATEMENTS, readonly [string, string]>;

const PROVENANCE_PAIRS = new Set([
  'gateway_projection\0mycelium_gateway',
  'provisioning_audit\0mycelium_provisioning',
  'route_challenge\0mycelium_router',
  'router_runtime\0mycelium_router',
]);
const LIFECYCLE_STATES = new Set<RequestLifecycleState>([
  'admitting',
  'prefill',
  'locked',
  'decoding',
  'completed',
  'failed',
  'cancelled',
]);
const CONFLICT_REASONS = new Set<ConflictReason>([
  'binding_mismatch',
  'value_mismatch',
  'freshness_overlap',
]);

export type SemanticScopeKind = keyof typeof SCOPE_STATEMENTS;
export type SemanticStatement = (typeof SCOPE_STATEMENTS)[SemanticScopeKind];
export type ClaimValue = 'confirmed' | 'rejected' | 'unknown';
export type ChallengeStatus = 'succeeded' | 'failed';
export type RequestLifecycleState =
  | 'admitting'
  | 'prefill'
  | 'locked'
  | 'decoding'
  | 'completed'
  | 'failed'
  | 'cancelled';
export type ConflictReason = 'binding_mismatch' | 'value_mismatch' | 'freshness_overlap';
export type QualificationReason =
  | 'snapshot_stale'
  | 'conflicts_present'
  | 'route_challenge_not_successful'
  | 'route_challenge_stale'
  | 'request_lifecycle_not_completed'
  | 'request_lifecycle_stale'
  | 'required_claim_missing'
  | 'required_claim_not_confirmed'
  | 'required_claim_stale';

export interface SemanticFreshness {
  readonly observed_at: string;
  readonly valid_until: string;
}

export interface SemanticProvenance {
  readonly kind: string;
  readonly producer: string;
}

export interface SemanticDeploymentBinding {
  readonly id: string;
  readonly epoch: number;
}

export interface SemanticModelBinding {
  readonly id: string;
  readonly revision: string;
  readonly manifest_digest: string;
  readonly num_layers: number;
}

export interface SemanticAssignmentBinding {
  readonly id: string;
  readonly peer_id: string;
  readonly start_layer: number;
  readonly end_layer_exclusive: number;
}

export interface SemanticRouteBinding {
  readonly id: string;
  readonly generation: number;
  readonly digest: string;
  readonly assignments: readonly SemanticAssignmentBinding[];
}

export interface SemanticBinding {
  readonly deployment: SemanticDeploymentBinding;
  readonly model: SemanticModelBinding;
  readonly route: SemanticRouteBinding;
}

export interface SemanticScope {
  readonly kind: SemanticScopeKind;
  readonly id: string;
}

export interface SemanticClaim {
  readonly id: string;
  readonly scope: SemanticScope;
  readonly statement: SemanticStatement;
  readonly value: ClaimValue;
  readonly freshness: SemanticFreshness;
  readonly provenance: SemanticProvenance;
}

export interface SemanticConflict {
  readonly claim_ids: readonly string[];
  readonly scope: SemanticScope;
  readonly reason: ConflictReason;
}

export interface SemanticRouteChallenge {
  readonly id: string;
  readonly status: ChallengeStatus;
  readonly freshness: SemanticFreshness;
  readonly binding: SemanticBinding;
  readonly provenance: SemanticProvenance;
}

export interface SemanticRequestLifecycle {
  readonly request_id: string;
  readonly state: RequestLifecycleState;
  readonly path_attempt: number;
  readonly freshness: SemanticFreshness;
  readonly binding: SemanticBinding;
  readonly provenance: SemanticProvenance;
}

export interface ObservatorySemanticSnapshot {
  readonly protocol: typeof OBSERVATORY_SNAPSHOT_PROTOCOL;
  readonly snapshot_id: string;
  readonly freshness: SemanticFreshness;
  readonly binding: SemanticBinding;
  readonly claims: readonly SemanticClaim[];
  readonly conflicts: readonly SemanticConflict[];
  readonly route_challenge: SemanticRouteChallenge;
  readonly request_lifecycle: SemanticRequestLifecycle;
  readonly provenance: SemanticProvenance;
}

export interface ObservatoryEventHeader {
  readonly protocol: typeof OBSERVATORY_EVENT_PROTOCOL;
  readonly generation: number;
}

export interface ObservatorySemanticEvent extends ObservatoryEventHeader {
  readonly snapshot: ObservatorySemanticSnapshot;
}

export interface SemanticQualification {
  readonly live: boolean;
  readonly reasons: readonly QualificationReason[];
}

/** A candidate is not an exact, privacy-safe Observatory semantic projection. */
export class SemanticProjectionValidationError extends TypeError {
  constructor(message: string) {
    super(message);
    this.name = 'SemanticProjectionValidationError';
  }
}

/** The protocol is absent or is not the single supported major version. */
export class UnsupportedObservatoryProtocolError extends SemanticProjectionValidationError {
  constructor(message: string) {
    super(message);
    this.name = 'UnsupportedObservatoryProtocolError';
  }
}

type Candidate = Record<string, unknown>;

interface ParsedTimestamp {
  readonly text: string;
  readonly epochMicroseconds: bigint;
}

interface ParsedFreshness {
  readonly value: SemanticFreshness;
  readonly observed: bigint;
  readonly valid: bigint;
}

function validationError(message: string): never {
  throw new SemanticProjectionValidationError(message);
}

function exactObject(value: unknown, keys: readonly string[], path: string): Candidate {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return validationError(`${path} must be an object`);
  }

  let ownKeys: readonly PropertyKey[];
  let prototype: object | null;
  try {
    ownKeys = Reflect.ownKeys(value);
    prototype = Object.getPrototypeOf(value) as object | null;
  } catch {
    return validationError(`${path} cannot be inspected as a semantic object`);
  }

  if (prototype !== Object.prototype && prototype !== null) {
    return validationError(`${path} must be a plain object`);
  }
  if (
    ownKeys.length !== keys.length ||
    ownKeys.some(
      (key) =>
        typeof key !== 'string' ||
        !/^[\x00-\x7f]*$/.test(key) ||
        !keys.includes(key),
    ) ||
    keys.some((key) => !ownKeys.includes(key))
  ) {
    return validationError(`${path} has unknown or missing fields`);
  }

  const copied: Candidate = {};
  try {
    for (const key of keys) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (descriptor === undefined || !('value' in descriptor) || !descriptor.enumerable) {
        return validationError(`${path} fields must be enumerable data fields`);
      }
      copied[key] = descriptor.value as unknown;
    }
  } catch {
    return validationError(`${path} fields cannot be safely inspected`);
  }
  return copied;
}

function exactArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) {
    return validationError(`${path} must be an array`);
  }

  let keys: readonly PropertyKey[];
  let length: number;
  try {
    keys = Reflect.ownKeys(value);
    const lengthDescriptor = Object.getOwnPropertyDescriptor(value, 'length');
    if (lengthDescriptor === undefined || !('value' in lengthDescriptor)) {
      return validationError(`${path} must be a dense array`);
    }
    length = lengthDescriptor.value as number;
  } catch {
    return validationError(`${path} cannot be inspected as an array`);
  }

  if (!Number.isSafeInteger(length) || length < 0 || keys.length !== length + 1) {
    return validationError(`${path} must be a dense array without extra fields`);
  }

  const copied: unknown[] = [];
  try {
    for (let index = 0; index < length; index += 1) {
      const key = String(index);
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (descriptor === undefined || !('value' in descriptor) || !descriptor.enumerable) {
        return validationError(`${path} must be a dense array without accessor fields`);
      }
      copied.push(descriptor.value as unknown);
    }
    if (keys.some((key) => key !== 'length' && (typeof key !== 'string' || !/^\d+$/.test(key)))) {
      return validationError(`${path} must be an array without extra fields`);
    }
  } catch {
    return validationError(`${path} elements cannot be safely inspected`);
  }
  return copied;
}

function protocolField(value: unknown, path: 'snapshot' | 'event'): unknown {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return undefined;
  }
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, 'protocol');
    return descriptor !== undefined && 'value' in descriptor ? descriptor.value : undefined;
  } catch {
    return undefined;
  }
}

function isIpv4(value: string): boolean {
  const parts = value.split('.');
  return (
    parts.length === 4 &&
    parts.every(
      (part) =>
        /^(?:0|[1-9]\d{0,2})$/.test(part) &&
        Number(part) >= 0 &&
        Number(part) <= 255,
    )
  );
}

function ipv6UnitCount(parts: readonly string[]): number | null {
  let count = 0;
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (/^[0-9a-f]{1,4}$/i.test(part)) {
      count += 1;
      continue;
    }
    if (index === parts.length - 1 && isIpv4(part)) {
      count += 2;
      continue;
    }
    return null;
  }
  return count;
}

function isIpv6(value: string): boolean {
  if (!value.includes(':')) return false;
  const scoped = value.split('%', 1)[0];
  const compression = scoped.indexOf('::');
  if (compression !== -1 && compression !== scoped.lastIndexOf('::')) return false;

  if (compression === -1) {
    const count = ipv6UnitCount(scoped.split(':'));
    return count === 8;
  }

  const left = scoped.slice(0, compression);
  const right = scoped.slice(compression + 2);
  const leftCount = left === '' ? 0 : ipv6UnitCount(left.split(':'));
  const rightCount = right === '' ? 0 : ipv6UnitCount(right.split(':'));
  return leftCount !== null && rightCount !== null && leftCount + rightCount < 8;
}

function isIpAddress(value: string): boolean {
  let candidate = value;
  if (candidate.startsWith('[') && candidate.endsWith(']')) {
    candidate = candidate.slice(1, -1);
  }
  return isIpv4(candidate) || isIpv6(candidate);
}

function rejectSensitiveText(value: string, path: string): void {
  if (CREDENTIAL_RE.test(value)) {
    validationError(`${path} contains prohibited credential-shaped material`);
  }
  if (value.includes('/') || value.includes('\\') || value.includes('://')) {
    validationError(`${path} must not contain an endpoint or path`);
  }
  if (isIpAddress(value)) {
    validationError(`${path} must not contain an IP address`);
  }
}

function identifier(value: unknown, path: string): string {
  if (typeof value !== 'string') {
    return validationError(`${path} must be a bounded public identifier`);
  }
  // Check sensitive shapes first so endpoint/path/IP rejection is explicitly fail-closed.
  rejectSensitiveText(value, path);
  if (!IDENTIFIER_RE.test(value)) {
    return validationError(`${path} must be a bounded public identifier`);
  }
  return value;
}

function digest(value: unknown, path: string): string {
  if (typeof value !== 'string' || !DIGEST_RE.test(value)) {
    return validationError(`${path} must be a lowercase sha256 digest`);
  }
  return value;
}

function safeInteger(value: unknown, path: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) {
    return validationError(`${path} must be a safe integer >= ${minimum}`);
  }
  return value;
}

function timestamp(value: unknown, path: string): ParsedTimestamp {
  if (typeof value !== 'string') {
    return validationError(`${path} must be an RFC3339 UTC timestamp`);
  }
  const match = TIMESTAMP_RE.exec(value);
  if (match === null) {
    return validationError(`${path} must be an RFC3339 UTC timestamp`);
  }

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const fraction = match[7] ?? '';
  const milliseconds = Number(fraction.padEnd(3, '0').slice(0, 3));
  const parsed = new Date(0);
  parsed.setUTCFullYear(year, month - 1, day);
  parsed.setUTCHours(hour, minute, second, milliseconds);

  if (
    year < 1 ||
    !Number.isFinite(parsed.getTime()) ||
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day ||
    parsed.getUTCHours() !== hour ||
    parsed.getUTCMinutes() !== minute ||
    parsed.getUTCSeconds() !== second ||
    parsed.getUTCMilliseconds() !== milliseconds
  ) {
    return validationError(`${path} must be an RFC3339 UTC timestamp`);
  }

  const fractionalMicroseconds = BigInt(fraction.padEnd(6, '0') || '0');
  const epochMicroseconds =
    BigInt(parsed.getTime() - milliseconds) * 1000n + fractionalMicroseconds;
  return { text: value, epochMicroseconds };
}

function freshness(value: unknown, path: string): ParsedFreshness {
  const candidate = exactObject(value, ['observed_at', 'valid_until'], path);
  const observed = timestamp(candidate.observed_at, `${path}.observed_at`);
  const valid = timestamp(candidate.valid_until, `${path}.valid_until`);
  if (observed.epochMicroseconds >= valid.epochMicroseconds) {
    return validationError(`${path} must have observed_at before valid_until`);
  }
  return {
    value: { observed_at: observed.text, valid_until: valid.text },
    observed: observed.epochMicroseconds,
    valid: valid.epochMicroseconds,
  };
}

function boundedFreshness(
  value: unknown,
  path: string,
  snapshotObserved: bigint,
  snapshotValid: bigint,
): SemanticFreshness {
  const parsed = freshness(value, path);
  if (parsed.observed < snapshotObserved || parsed.valid > snapshotValid) {
    return validationError(`${path} must remain inside snapshot freshness`);
  }
  return parsed.value;
}

function provenance(
  value: unknown,
  path: string,
  required?: readonly [kind: string, producer: string],
): SemanticProvenance {
  const candidate = exactObject(value, ['kind', 'producer'], path);
  const kind = identifier(candidate.kind, `${path}.kind`);
  const producer = identifier(candidate.producer, `${path}.producer`);
  const pair = `${kind}\0${producer}`;
  if (
    !PROVENANCE_PAIRS.has(pair) ||
    (required !== undefined && (kind !== required[0] || producer !== required[1]))
  ) {
    return validationError(`${path} has unsupported provenance`);
  }
  return { kind, producer };
}

function binding(value: unknown, path: string): SemanticBinding {
  const candidate = exactObject(value, ['deployment', 'model', 'route'], path);
  const deploymentCandidate = exactObject(
    candidate.deployment,
    ['id', 'epoch'],
    `${path}.deployment`,
  );
  const deployment: SemanticDeploymentBinding = {
    id: identifier(deploymentCandidate.id, `${path}.deployment.id`),
    epoch: safeInteger(deploymentCandidate.epoch, `${path}.deployment.epoch`, 1),
  };

  const modelCandidate = exactObject(
    candidate.model,
    ['id', 'revision', 'manifest_digest', 'num_layers'],
    `${path}.model`,
  );
  const model: SemanticModelBinding = {
    id: identifier(modelCandidate.id, `${path}.model.id`),
    revision: identifier(modelCandidate.revision, `${path}.model.revision`),
    manifest_digest: digest(modelCandidate.manifest_digest, `${path}.model.manifest_digest`),
    num_layers: safeInteger(modelCandidate.num_layers, `${path}.model.num_layers`, 1),
  };

  const routeCandidate = exactObject(
    candidate.route,
    ['id', 'generation', 'digest', 'assignments'],
    `${path}.route`,
  );
  const assignmentCandidates = exactArray(
    routeCandidate.assignments,
    `${path}.route.assignments`,
  );
  if (assignmentCandidates.length === 0) {
    return validationError(`${path}.route.assignments must not be empty`);
  }

  const assignments: SemanticAssignmentBinding[] = [];
  const assignmentIds = new Set<string>();
  let nextLayer = 0;
  for (let index = 0; index < assignmentCandidates.length; index += 1) {
    const assignmentPath = `${path}.route.assignments[${index}]`;
    const assignmentCandidate = exactObject(
      assignmentCandidates[index],
      ['id', 'peer_id', 'start_layer', 'end_layer_exclusive'],
      assignmentPath,
    );
    const id = identifier(assignmentCandidate.id, `${assignmentPath}.id`);
    if (assignmentIds.has(id)) {
      return validationError(`${path}.route assignment ids must be unique`);
    }
    assignmentIds.add(id);
    const start = safeInteger(assignmentCandidate.start_layer, `${assignmentPath}.start_layer`);
    const end = safeInteger(
      assignmentCandidate.end_layer_exclusive,
      `${assignmentPath}.end_layer_exclusive`,
      1,
    );
    if (start !== nextLayer || end <= start) {
      return validationError(
        `${path}.route assignments must provide ordered contiguous half-open coverage`,
      );
    }
    nextLayer = end;
    assignments.push({
      id,
      peer_id: identifier(assignmentCandidate.peer_id, `${assignmentPath}.peer_id`),
      start_layer: start,
      end_layer_exclusive: end,
    });
  }
  if (nextLayer !== model.num_layers) {
    return validationError(`${path}.route assignments must cover every model layer`);
  }

  const route: SemanticRouteBinding = {
    id: identifier(routeCandidate.id, `${path}.route.id`),
    generation: safeInteger(routeCandidate.generation, `${path}.route.generation`, 1),
    digest: digest(routeCandidate.digest, `${path}.route.digest`),
    assignments,
  };
  return { deployment, model, route };
}

function bindingsEqual(left: SemanticBinding, right: SemanticBinding): boolean {
  return (
    left.deployment.id === right.deployment.id &&
    left.deployment.epoch === right.deployment.epoch &&
    left.model.id === right.model.id &&
    left.model.revision === right.model.revision &&
    left.model.manifest_digest === right.model.manifest_digest &&
    left.model.num_layers === right.model.num_layers &&
    left.route.id === right.route.id &&
    left.route.generation === right.route.generation &&
    left.route.digest === right.route.digest &&
    left.route.assignments.length === right.route.assignments.length &&
    left.route.assignments.every((assignment, index) => {
      const other = right.route.assignments[index];
      return (
        assignment.id === other.id &&
        assignment.peer_id === other.peer_id &&
        assignment.start_layer === other.start_layer &&
        assignment.end_layer_exclusive === other.end_layer_exclusive
      );
    })
  );
}

function scope(
  value: unknown,
  path: string,
  snapshotBinding: SemanticBinding,
  requestId: string,
): SemanticScope {
  const candidate = exactObject(value, ['kind', 'id'], path);
  const kind = candidate.kind;
  if (typeof kind !== 'string' || !Object.hasOwn(SCOPE_STATEMENTS, kind)) {
    return validationError(`${path}.kind has unsupported claim scope`);
  }
  const typedKind = kind as SemanticScopeKind;
  const id = identifier(candidate.id, `${path}.id`);
  const allowedIds: Record<SemanticScopeKind, ReadonlySet<string>> = {
    deployment: new Set([snapshotBinding.deployment.id]),
    model: new Set([snapshotBinding.model.id]),
    route: new Set([snapshotBinding.route.id]),
    assignment: new Set(snapshotBinding.route.assignments.map((assignment) => assignment.id)),
    request: new Set([requestId]),
  };
  if (!allowedIds[typedKind].has(id)) {
    return validationError(`${path}.id is outside the exact snapshot binding`);
  }
  return { kind: typedKind, id };
}

function parseSnapshot(value: unknown): ObservatorySemanticSnapshot {
  if (protocolField(value, 'snapshot') !== OBSERVATORY_SNAPSHOT_PROTOCOL) {
    throw new UnsupportedObservatoryProtocolError('unsupported Observatory snapshot protocol');
  }
  const candidate = exactObject(value, SNAPSHOT_KEYS, 'snapshot');
  const snapshotId = identifier(candidate.snapshot_id, 'snapshot.snapshot_id');
  const snapshotFreshness = freshness(candidate.freshness, 'snapshot.freshness');
  const snapshotBinding = binding(candidate.binding, 'snapshot.binding');
  const topProvenance = provenance(candidate.provenance, 'snapshot.provenance', [
    'gateway_projection',
    'mycelium_gateway',
  ]);

  const lifecycleCandidate = exactObject(
    candidate.request_lifecycle,
    ['request_id', 'state', 'path_attempt', 'freshness', 'binding', 'provenance'],
    'snapshot.request_lifecycle',
  );
  const requestId = identifier(
    lifecycleCandidate.request_id,
    'snapshot.request_lifecycle.request_id',
  );
  const lifecycleState = lifecycleCandidate.state;
  if (typeof lifecycleState !== 'string' || !LIFECYCLE_STATES.has(lifecycleState as RequestLifecycleState)) {
    return validationError('snapshot.request_lifecycle.state is unsupported');
  }
  const typedLifecycleState = lifecycleState as RequestLifecycleState;
  const lifecycleBinding = binding(
    lifecycleCandidate.binding,
    'snapshot.request_lifecycle.binding',
  );
  if (!bindingsEqual(lifecycleBinding, snapshotBinding)) {
    return validationError(
      'request lifecycle binding does not exactly match snapshot binding',
    );
  }
  const requestLifecycle: SemanticRequestLifecycle = {
    request_id: requestId,
    state: typedLifecycleState,
    path_attempt: safeInteger(
      lifecycleCandidate.path_attempt,
      'snapshot.request_lifecycle.path_attempt',
      1,
    ),
    freshness: boundedFreshness(
      lifecycleCandidate.freshness,
      'snapshot.request_lifecycle.freshness',
      snapshotFreshness.observed,
      snapshotFreshness.valid,
    ),
    binding: lifecycleBinding,
    provenance: provenance(
      lifecycleCandidate.provenance,
      'snapshot.request_lifecycle.provenance',
      ['router_runtime', 'mycelium_router'],
    ),
  };

  const challengeCandidate = exactObject(
    candidate.route_challenge,
    ['id', 'status', 'freshness', 'binding', 'provenance'],
    'snapshot.route_challenge',
  );
  const challengeStatus = challengeCandidate.status;
  if (challengeStatus !== 'succeeded' && challengeStatus !== 'failed') {
    return validationError('snapshot.route_challenge.status is unsupported');
  }
  const challengeBinding = binding(
    challengeCandidate.binding,
    'snapshot.route_challenge.binding',
  );
  if (!bindingsEqual(challengeBinding, snapshotBinding)) {
    return validationError('route challenge binding does not exactly match snapshot binding');
  }
  const routeChallenge: SemanticRouteChallenge = {
    id: identifier(challengeCandidate.id, 'snapshot.route_challenge.id'),
    status: challengeStatus,
    freshness: boundedFreshness(
      challengeCandidate.freshness,
      'snapshot.route_challenge.freshness',
      snapshotFreshness.observed,
      snapshotFreshness.valid,
    ),
    binding: challengeBinding,
    provenance: provenance(
      challengeCandidate.provenance,
      'snapshot.route_challenge.provenance',
      ['route_challenge', 'mycelium_router'],
    ),
  };

  const claimCandidates = exactArray(candidate.claims, 'snapshot.claims');
  const claims: SemanticClaim[] = [];
  const claimIds = new Set<string>();
  const claimsById = new Map<string, SemanticClaim>();
  const claimsBySemanticKey = new Map<string, Set<string>>();
  for (let index = 0; index < claimCandidates.length; index += 1) {
    const claimPath = `snapshot.claims[${index}]`;
    const claimCandidate = exactObject(
      claimCandidates[index],
      ['id', 'scope', 'statement', 'value', 'freshness', 'provenance'],
      claimPath,
    );
    const id = identifier(claimCandidate.id, `${claimPath}.id`);
    if (claimIds.has(id)) {
      return validationError('snapshot claim ids must be unique');
    }
    claimIds.add(id);
    const claimScope = scope(
      claimCandidate.scope,
      `${claimPath}.scope`,
      snapshotBinding,
      requestId,
    );
    const expectedStatement = SCOPE_STATEMENTS[claimScope.kind];
    if (claimCandidate.statement !== expectedStatement) {
      return validationError(`${claimPath}.statement does not match its scope`);
    }
    const claimValue = claimCandidate.value;
    if (claimValue !== 'confirmed' && claimValue !== 'rejected' && claimValue !== 'unknown') {
      return validationError(`${claimPath}.value is unsupported`);
    }
    const parsedClaim: SemanticClaim = {
      id,
      scope: claimScope,
      statement: expectedStatement,
      value: claimValue,
      freshness: boundedFreshness(
        claimCandidate.freshness,
        `${claimPath}.freshness`,
        snapshotFreshness.observed,
        snapshotFreshness.valid,
      ),
      provenance: provenance(
        claimCandidate.provenance,
        `${claimPath}.provenance`,
        SCOPE_PROVENANCE[claimScope.kind],
      ),
    };
    claims.push(parsedClaim);
    claimsById.set(id, parsedClaim);
    const semanticKey = `${claimScope.kind}\0${claimScope.id}\0${expectedStatement}`;
    const groupedClaimIds = claimsBySemanticKey.get(semanticKey) ?? new Set<string>();
    groupedClaimIds.add(id);
    claimsBySemanticKey.set(semanticKey, groupedClaimIds);
  }

  const conflictCandidates = exactArray(candidate.conflicts, 'snapshot.conflicts');
  const conflicts: SemanticConflict[] = [];
  const reportedConflictGroups = new Set<string>();
  for (let index = 0; index < conflictCandidates.length; index += 1) {
    const conflictPath = `snapshot.conflicts[${index}]`;
    const conflictCandidate = exactObject(
      conflictCandidates[index],
      ['claim_ids', 'scope', 'reason'],
      conflictPath,
    );
    const conflictClaimCandidates = exactArray(
      conflictCandidate.claim_ids,
      `${conflictPath}.claim_ids`,
    );
    const conflictClaimIds = conflictClaimCandidates.map((item, claimIndex) =>
      identifier(item, `${conflictPath}.claim_ids[${claimIndex}]`),
    );
    if (
      conflictClaimIds.length < 2 ||
      new Set(conflictClaimIds).size !== conflictClaimIds.length ||
      conflictClaimIds.some((id) => !claimIds.has(id))
    ) {
      return validationError(
        `${conflictPath}.claim_ids must reference at least two unique claims`,
      );
    }
    const reason = conflictCandidate.reason;
    if (typeof reason !== 'string' || !CONFLICT_REASONS.has(reason as ConflictReason)) {
      return validationError(`${conflictPath}.reason is unsupported`);
    }
    const conflictScope = scope(
      conflictCandidate.scope,
      `${conflictPath}.scope`,
      snapshotBinding,
      requestId,
    );
    if (
      conflictClaimIds.some((id) => {
        const claim = claimsById.get(id)!;
        return claim.scope.kind !== conflictScope.kind || claim.scope.id !== conflictScope.id;
      })
    ) {
      return validationError(
        `${conflictPath}.claim_ids must all match the exact conflict scope`,
      );
    }
    reportedConflictGroups.add([...conflictClaimIds].sort().join('\0'));
    conflicts.push({
      claim_ids: conflictClaimIds,
      scope: conflictScope,
      reason: reason as ConflictReason,
    });
  }

  for (const groupedClaimIds of claimsBySemanticKey.values()) {
    if (
      groupedClaimIds.size > 1 &&
      !reportedConflictGroups.has([...groupedClaimIds].sort().join('\0'))
    ) {
      return validationError(
        'duplicate semantic claims must be represented by one exact conflict',
      );
    }
  }

  return {
    protocol: OBSERVATORY_SNAPSHOT_PROTOCOL,
    snapshot_id: snapshotId,
    freshness: snapshotFreshness.value,
    binding: snapshotBinding,
    claims,
    conflicts,
    route_challenge: routeChallenge,
    request_lifecycle: requestLifecycle,
    provenance: topProvenance,
  };
}

/** Decode one exact v1 public projection; unknown fields and protocol majors fail closed. */
export function decodeObservatorySnapshot(value: unknown): ObservatorySemanticSnapshot {
  return deepFreeze(parseSnapshot(value));
}

interface ParsedEventCandidate {
  readonly candidate: Candidate;
  readonly header: ObservatoryEventHeader;
}

function parseEventCandidate(value: unknown): ParsedEventCandidate {
  if (protocolField(value, 'event') !== OBSERVATORY_EVENT_PROTOCOL) {
    throw new UnsupportedObservatoryProtocolError('unsupported Observatory event protocol');
  }
  const candidate = exactObject(value, EVENT_KEYS, 'event');
  return {
    candidate,
    header: {
      protocol: OBSERVATORY_EVENT_PROTOCOL,
      generation: safeInteger(candidate.generation, 'event.generation', 1),
    },
  };
}

/** Parse the event protocol and generation without traversing its snapshot. */
export function parseObservatoryEventHeader(value: unknown): ObservatoryEventHeader {
  return deepFreeze(parseEventCandidate(value).header);
}

/** Decode one complete v1 event containing one complete semantic snapshot. */
export function decodeObservatoryEvent(value: unknown): ObservatorySemanticEvent {
  const parsed = parseEventCandidate(value);
  return deepFreeze({
    ...parsed.header,
    snapshot: decodeObservatorySnapshot(parsed.candidate.snapshot),
  });
}

function epochMicroseconds(value: number | Date): bigint {
  const milliseconds = value instanceof Date ? value.getTime() : value;
  if (typeof milliseconds !== 'number' || !Number.isFinite(milliseconds)) {
    return validationError('now must be a finite Unix timestamp in milliseconds');
  }
  const microseconds = milliseconds * 1000;
  if (!Number.isSafeInteger(microseconds)) {
    return validationError('now must be representable at microsecond precision');
  }
  return BigInt(microseconds);
}

function freshAt(value: SemanticFreshness, now: bigint): boolean {
  const observed = timestamp(value.observed_at, 'freshness.observed_at').epochMicroseconds;
  const valid = timestamp(value.valid_until, 'freshness.valid_until').epochMicroseconds;
  return observed <= now && now < valid;
}

function claimKey(kind: SemanticScopeKind, id: string, statement: SemanticStatement): string {
  return `${kind}\0${id}\0${statement}`;
}

/** Derive the live gate from validated evidence; no producer-supplied live flag is trusted. */
export function qualifySemanticSnapshot(
  snapshot: unknown,
  now: number | Date = Date.now(),
): SemanticQualification {
  const decoded = decodeObservatorySnapshot(snapshot);
  const instant = epochMicroseconds(now);
  const reasons: QualificationReason[] = [];

  if (!freshAt(decoded.freshness, instant)) reasons.push('snapshot_stale');
  if (decoded.conflicts.length > 0) reasons.push('conflicts_present');
  if (decoded.route_challenge.status !== 'succeeded') {
    reasons.push('route_challenge_not_successful');
  }
  if (!freshAt(decoded.route_challenge.freshness, instant)) {
    reasons.push('route_challenge_stale');
  }
  if (decoded.request_lifecycle.state !== 'completed') {
    reasons.push('request_lifecycle_not_completed');
  }
  if (!freshAt(decoded.request_lifecycle.freshness, instant)) {
    reasons.push('request_lifecycle_stale');
  }

  const required = new Set<string>([
    claimKey('deployment', decoded.binding.deployment.id, 'deployment_bound'),
    claimKey('model', decoded.binding.model.id, 'model_bound'),
    claimKey('route', decoded.binding.route.id, 'route_challenge_succeeded'),
    claimKey(
      'request',
      decoded.request_lifecycle.request_id,
      'request_lifecycle_observed',
    ),
    ...decoded.binding.route.assignments.map((assignment) =>
      claimKey('assignment', assignment.id, 'assignment_ready'),
    ),
  ]);
  const indexed = new Map<string, SemanticClaim>();
  for (const claim of decoded.claims) {
    indexed.set(claimKey(claim.scope.kind, claim.scope.id, claim.statement), claim);
  }

  const requiredClaims: SemanticClaim[] = [];
  let missing = false;
  for (const key of required) {
    const claim = indexed.get(key);
    if (claim === undefined) missing = true;
    else requiredClaims.push(claim);
  }
  if (missing) reasons.push('required_claim_missing');
  if (requiredClaims.some((claim) => claim.value !== 'confirmed')) {
    reasons.push('required_claim_not_confirmed');
  }
  if (requiredClaims.some((claim) => !freshAt(claim.freshness, instant))) {
    reasons.push('required_claim_stale');
  }

  return deepFreeze({ live: reasons.length === 0, reasons });
}
