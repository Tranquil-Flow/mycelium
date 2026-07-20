import { deepFreeze } from '../model/runtime';

export const OBSERVATORY_STREAM_PROTOCOL = 'mycelium.observatory_stream.v1' as const;
export const OBSERVATORY_EVENT_PROJECTION_PROTOCOL =
  'mycelium.observatory.request_projection.v1' as const;
export const OBSERVATORY_EVENT_STATUS_PROTOCOL =
  'mycelium.observatory.event_adapter_status.v1' as const;
export const ROUTE_QUALIFICATION_PROTOCOL = 'mycelium.route_qualification.v1' as const;
export const REQUEST_EVENT_PROTOCOL = 'mycelium.request_event.v1' as const;

const MAX_SESSIONS = 256;
const MAX_INCIDENTS = 256;
const MAX_REASON_CODES = 64;
const MAX_STAGE_DIGESTS = 256;
const PUBLIC_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$/;
const HOST_PORT = /^(?:[A-Za-z0-9.-]+):[0-9]{1,5}$/;
const CREDENTIAL = /(?:\bbearer\s+|\bsk-[a-z0-9_-]{12,}|\bgh[pousr]_[a-z0-9]{20,}|\bgithub_pat_[a-z0-9_]{20,}|-----BEGIN[ A-Z0-9_-]{0,48}PRIVATE KEY-----)/i;
const SAFE_CODE = /^[a-z][a-z0-9_]{0,63}$/;
const SHA256_REF = /^sha256:[a-f0-9]{64}$/;

export type ObservatoryRequestState =
  | 'accepted'
  | 'streaming'
  | 'completed'
  | 'cancelled'
  | 'failed'
  | 'quarantined';

export interface ObservatoryAdapterBinding {
  readonly qualification_id: string;
  readonly qualification_digest: string;
  readonly deployment_id: string;
  readonly deployment_epoch: number;
  readonly topology_version: number;
  readonly model_id: string;
  readonly resolved_commit: string;
  readonly manifest_digest: string;
  readonly path_manifest_digest: string;
  readonly stage_load_proof_digests: readonly string[];
}

export interface ObservatoryAdapterQualification {
  readonly protocol: typeof ROUTE_QUALIFICATION_PROTOCOL;
  readonly qualification_id: string;
  readonly issued_at_unix_ms: number;
  readonly evidence_class: 'physical_qualification' | 'synthetic_test_fixture';
  readonly route_ready: boolean;
  readonly reason_codes: readonly string[];
  readonly binding: ObservatoryAdapterBinding;
}

export interface ObservatoryRequestSession {
  readonly request_id: string;
  readonly state: ObservatoryRequestState;
  readonly last_sequence: number;
  readonly event_count: number;
  readonly token_count: number;
  readonly terminal: boolean;
  readonly qualification_id: string;
  readonly started_at_unix_ms: number;
  readonly updated_at_unix_ms: number;
  readonly quarantine_reason: string | null;
}

export interface ObservatoryAdapterSnapshot {
  readonly protocol: typeof OBSERVATORY_EVENT_PROJECTION_PROTOCOL;
  readonly source_cursor: number;
  readonly observed_at_unix_ms: number;
  readonly qualification: ObservatoryAdapterQualification | null;
  readonly sessions: readonly ObservatoryRequestSession[];
}

export interface ObservatoryAdapterIncident {
  readonly protocol:
    | typeof ROUTE_QUALIFICATION_PROTOCOL
    | typeof REQUEST_EVENT_PROTOCOL
    | 'unknown';
  readonly source_cursor: number;
  readonly reason: string;
}

export interface ObservatoryAdapterStatus {
  readonly protocol: typeof OBSERVATORY_EVENT_STATUS_PROTOCOL;
  readonly route_ready: boolean;
  readonly source_cursor: number;
  readonly buffered_sessions: number;
  readonly quarantine_capacity: number;
  readonly dropped_quarantine_count: number;
}

export interface ObservatoryAdapterBundle {
  readonly snapshot: ObservatoryAdapterSnapshot;
  readonly incidents: readonly ObservatoryAdapterIncident[];
  readonly provisioning: ObservatoryAdapterStatus;
}

export interface ObservatoryAdapterEventHeader {
  readonly protocol: typeof OBSERVATORY_STREAM_PROTOCOL;
  readonly generation: number;
}

export interface ObservatoryAdapterEvent extends ObservatoryAdapterEventHeader {
  readonly bundle: ObservatoryAdapterBundle;
}

export class ObservatoryAdapterProjectionError extends TypeError {
  constructor(message: string) {
    super(message);
    this.name = 'ObservatoryAdapterProjectionError';
  }
}

export class UnsupportedObservatoryAdapterProtocolError extends ObservatoryAdapterProjectionError {
  constructor(message: string) {
    super(message);
    this.name = 'UnsupportedObservatoryAdapterProtocolError';
  }
}

type Candidate = Record<string, unknown>;

function invalid(message: string): never {
  throw new ObservatoryAdapterProjectionError(message);
}

function exactObject(value: unknown, keys: readonly string[], path: string): Candidate {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return invalid(`${path} must be an object`);
  }
  let ownKeys: readonly PropertyKey[];
  let prototype: object | null;
  try {
    ownKeys = Reflect.ownKeys(value);
    prototype = Object.getPrototypeOf(value) as object | null;
  } catch {
    return invalid(`${path} cannot be inspected`);
  }
  if (prototype !== Object.prototype && prototype !== null) {
    return invalid(`${path} must be a plain object`);
  }
  if (
    ownKeys.length !== keys.length ||
    ownKeys.some((key) => typeof key !== 'string' || !keys.includes(key)) ||
    keys.some((key) => !ownKeys.includes(key))
  ) {
    return invalid(`${path} has unknown or missing fields`);
  }
  const copied: Candidate = {};
  for (const key of keys) {
    let descriptor: PropertyDescriptor | undefined;
    try {
      descriptor = Object.getOwnPropertyDescriptor(value, key);
    } catch {
      return invalid(`${path} fields cannot be inspected`);
    }
    if (descriptor === undefined || !('value' in descriptor) || !descriptor.enumerable) {
      return invalid(`${path} fields must be enumerable data fields`);
    }
    copied[key] = descriptor.value as unknown;
  }
  return copied;
}

function exactArray(value: unknown, path: string, maximum: number): unknown[] {
  if (!Array.isArray(value)) return invalid(`${path} must be an array`);
  let keys: readonly PropertyKey[];
  let length: number;
  try {
    keys = Reflect.ownKeys(value);
    const descriptor = Object.getOwnPropertyDescriptor(value, 'length');
    if (descriptor === undefined || !('value' in descriptor)) {
      return invalid(`${path} must be a dense array`);
    }
    length = descriptor.value as number;
  } catch {
    return invalid(`${path} cannot be inspected`);
  }
  if (
    !Number.isSafeInteger(length) ||
    length < 0 ||
    length > maximum ||
    keys.length !== length + 1
  ) {
    return invalid(`${path} exceeds its bound or is not a dense array`);
  }
  const copied: unknown[] = [];
  for (let index = 0; index < length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
    if (descriptor === undefined || !('value' in descriptor) || !descriptor.enumerable) {
      return invalid(`${path} must contain data elements only`);
    }
    copied.push(descriptor.value as unknown);
  }
  if (
    keys.some(
      (key) => key !== 'length' && (typeof key !== 'string' || !/^\d+$/.test(key)),
    )
  ) {
    return invalid(`${path} has extra fields`);
  }
  return copied;
}

function safeInteger(value: unknown, path: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    return invalid(`${path} must be a safe integer at least ${minimum}`);
  }
  return value as number;
}

function safeCode(value: unknown, path: string): string {
  if (typeof value !== 'string' || !SAFE_CODE.test(value)) {
    return invalid(`${path} must be a safe code`);
  }
  return value;
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

function isIpv6(value: string): boolean {
  if (!value.includes(':')) return false;
  const candidate = value.split('%', 1)[0];
  if (!/^[0-9a-f:.]+$/i.test(candidate)) return false;
  const compression = candidate.indexOf('::');
  if (compression !== -1 && compression !== candidate.lastIndexOf('::')) return false;
  const sides = compression === -1 ? [candidate] : candidate.split('::');
  const units = sides.flatMap((side) => (side.length === 0 ? [] : side.split(':')));
  let count = 0;
  for (const [index, unit] of units.entries()) {
    if (/^[0-9a-f]{1,4}$/i.test(unit)) count += 1;
    else if (index === units.length - 1 && isIpv4(unit)) count += 2;
    else return false;
  }
  return compression === -1 ? count === 8 : count < 8;
}

function publicIdentifier(value: unknown, path: string): string {
  const lowered = typeof value === 'string' ? value.toLowerCase() : '';
  if (
    typeof value !== 'string' ||
    !PUBLIC_IDENTIFIER.test(value) ||
    value.includes('/') ||
    value.includes('\\') ||
    value.includes('://') ||
    HOST_PORT.test(value) ||
    CREDENTIAL.test(value) ||
    lowered === 'localhost' ||
    ['.localhost', '.local', '.internal', '.lan'].some((suffix) =>
      lowered.endsWith(suffix),
    ) ||
    isIpv4(value) ||
    isIpv6(value)
  ) {
    return invalid(`${path} must be a public identifier, not an endpoint`);
  }
  return value;
}

function digest(value: unknown, path: string): string {
  if (typeof value !== 'string' || !SHA256_REF.test(value)) {
    return invalid(`${path} must be a sha256 reference`);
  }
  return value;
}

function protocol(
  value: unknown,
  expected: string,
  path: string,
): string {
  if (value !== expected) {
    throw new UnsupportedObservatoryAdapterProtocolError(
      `${path} does not use the supported protocol`,
    );
  }
  return expected;
}

function decodeBinding(value: unknown): ObservatoryAdapterBinding {
  const item = exactObject(
    value,
    [
      'qualification_id',
      'qualification_digest',
      'deployment_id',
      'deployment_epoch',
      'topology_version',
      'model_id',
      'resolved_commit',
      'manifest_digest',
      'path_manifest_digest',
      'stage_load_proof_digests',
    ],
    'qualification.binding',
  );
  const stageDigests = exactArray(
    item.stage_load_proof_digests,
    'qualification.binding.stage_load_proof_digests',
    MAX_STAGE_DIGESTS,
  ).map((candidate, index) =>
    digest(candidate, `qualification.binding.stage_load_proof_digests[${index}]`),
  );
  if (stageDigests.some((itemDigest, index) => index > 0 && itemDigest <= stageDigests[index - 1])) {
    return invalid('qualification.binding.stage_load_proof_digests must be ordered and unique');
  }
  return {
    qualification_id: publicIdentifier(
      item.qualification_id,
      'qualification.binding.qualification_id',
    ),
    qualification_digest: digest(
      item.qualification_digest,
      'qualification.binding.qualification_digest',
    ),
    deployment_id: publicIdentifier(item.deployment_id, 'qualification.binding.deployment_id'),
    deployment_epoch: safeInteger(
      item.deployment_epoch,
      'qualification.binding.deployment_epoch',
    ),
    topology_version: safeInteger(
      item.topology_version,
      'qualification.binding.topology_version',
    ),
    model_id: publicIdentifier(item.model_id, 'qualification.binding.model_id'),
    resolved_commit: publicIdentifier(
      item.resolved_commit,
      'qualification.binding.resolved_commit',
    ),
    manifest_digest: digest(item.manifest_digest, 'qualification.binding.manifest_digest'),
    path_manifest_digest: digest(
      item.path_manifest_digest,
      'qualification.binding.path_manifest_digest',
    ),
    stage_load_proof_digests: stageDigests,
  };
}

function decodeQualification(value: unknown): ObservatoryAdapterQualification | null {
  if (value === null) return null;
  const item = exactObject(
    value,
    [
      'protocol',
      'qualification_id',
      'issued_at_unix_ms',
      'evidence_class',
      'route_ready',
      'reason_codes',
      'binding',
    ],
    'qualification',
  );
  protocol(item.protocol, ROUTE_QUALIFICATION_PROTOCOL, 'qualification.protocol');
  if (typeof item.route_ready !== 'boolean') return invalid('qualification.route_ready must be boolean');
  if (
    item.evidence_class !== 'physical_qualification' &&
    item.evidence_class !== 'synthetic_test_fixture'
  ) {
    return invalid('qualification.evidence_class is unsupported');
  }
  const qualificationId = publicIdentifier(item.qualification_id, 'qualification.qualification_id');
  const reasonCodes = exactArray(
    item.reason_codes,
    'qualification.reason_codes',
    MAX_REASON_CODES,
  ).map((candidate, index) => safeCode(candidate, `qualification.reason_codes[${index}]`));
  if (new Set(reasonCodes).size !== reasonCodes.length) {
    return invalid('qualification.reason_codes must be unique');
  }
  if (
    (item.route_ready && (item.evidence_class !== 'physical_qualification' || reasonCodes.length !== 0)) ||
    (!item.route_ready && reasonCodes.length === 0)
  ) {
    return invalid('qualification readiness does not match accepted physical evidence');
  }
  const binding = decodeBinding(item.binding);
  if (binding.qualification_id !== qualificationId) {
    return invalid('qualification identifier does not match binding');
  }
  if (item.route_ready && binding.stage_load_proof_digests.length === 0) {
    return invalid('qualification readiness requires stage load proof digests');
  }
  return {
    protocol: ROUTE_QUALIFICATION_PROTOCOL,
    qualification_id: qualificationId,
    issued_at_unix_ms: safeInteger(
      item.issued_at_unix_ms,
      'qualification.issued_at_unix_ms',
    ),
    evidence_class: item.evidence_class,
    route_ready: item.route_ready,
    reason_codes: reasonCodes,
    binding,
  };
}

function decodeSession(value: unknown, index: number): ObservatoryRequestSession {
  const path = `sessions[${index}]`;
  const item = exactObject(
    value,
    [
      'request_id',
      'state',
      'last_sequence',
      'event_count',
      'token_count',
      'terminal',
      'qualification_id',
      'started_at_unix_ms',
      'updated_at_unix_ms',
      'quarantine_reason',
    ],
    path,
  );
  const states: readonly ObservatoryRequestState[] = [
    'accepted',
    'streaming',
    'completed',
    'cancelled',
    'failed',
    'quarantined',
  ];
  if (typeof item.state !== 'string' || !states.includes(item.state as ObservatoryRequestState)) {
    return invalid(`${path}.state is unsupported`);
  }
  const state = item.state as ObservatoryRequestState;
  const terminal = item.terminal;
  const terminalState = ['completed', 'cancelled', 'failed'].includes(state);
  if (typeof terminal !== 'boolean' || terminal !== terminalState) {
    return invalid(`${path}.terminal does not match state`);
  }
  const lastSequence = safeInteger(item.last_sequence, `${path}.last_sequence`);
  const eventCount = safeInteger(item.event_count, `${path}.event_count`, 1);
  const tokenCount = safeInteger(item.token_count, `${path}.token_count`);
  if (eventCount !== lastSequence + 1 || tokenCount >= eventCount) {
    return invalid(`${path} event counts are incoherent`);
  }
  if (
    (state === 'accepted' && (eventCount !== 1 || tokenCount !== 0)) ||
    (state === 'streaming' && tokenCount < 1)
  ) {
    return invalid(`${path}.state does not match event counts`);
  }
  const started = safeInteger(item.started_at_unix_ms, `${path}.started_at_unix_ms`);
  const updated = safeInteger(item.updated_at_unix_ms, `${path}.updated_at_unix_ms`);
  if (updated < started) return invalid(`${path} timestamps are not monotonic`);
  const quarantineReason =
    item.quarantine_reason === null
      ? null
      : safeCode(item.quarantine_reason, `${path}.quarantine_reason`);
  if ((state === 'quarantined') !== (quarantineReason !== null)) {
    return invalid(`${path}.quarantine_reason does not match state`);
  }
  return {
    request_id: publicIdentifier(item.request_id, `${path}.request_id`),
    state,
    last_sequence: lastSequence,
    event_count: eventCount,
    token_count: tokenCount,
    terminal,
    qualification_id: publicIdentifier(item.qualification_id, `${path}.qualification_id`),
    started_at_unix_ms: started,
    updated_at_unix_ms: updated,
    quarantine_reason: quarantineReason,
  };
}

function decodeIncident(value: unknown, index: number): ObservatoryAdapterIncident {
  const path = `incidents[${index}]`;
  const item = exactObject(value, ['protocol', 'source_cursor', 'reason'], path);
  if (
    item.protocol !== ROUTE_QUALIFICATION_PROTOCOL &&
    item.protocol !== REQUEST_EVENT_PROTOCOL &&
    item.protocol !== 'unknown'
  ) {
    return invalid(`${path}.protocol is unsupported`);
  }
  return {
    protocol: item.protocol,
    source_cursor: safeInteger(item.source_cursor, `${path}.source_cursor`),
    reason: safeCode(item.reason, `${path}.reason`),
  };
}

export function parseObservatoryAdapterEventHeader(
  value: unknown,
): ObservatoryAdapterEventHeader {
  const event = exactObject(value, ['protocol', 'generation', 'bundle'], 'event');
  protocol(event.protocol, OBSERVATORY_STREAM_PROTOCOL, 'event.protocol');
  return deepFreeze({
    protocol: OBSERVATORY_STREAM_PROTOCOL,
    generation: safeInteger(event.generation, 'event.generation', 1),
  });
}

export function decodeObservatoryAdapterEvent(value: unknown): ObservatoryAdapterEvent {
  const header = parseObservatoryAdapterEventHeader(value);
  const event = exactObject(value, ['protocol', 'generation', 'bundle'], 'event');
  const bundleCandidate = exactObject(
    event.bundle,
    ['snapshot', 'incidents', 'provisioning'],
    'bundle',
  );
  const snapshotCandidate = exactObject(
    bundleCandidate.snapshot,
    ['protocol', 'source_cursor', 'observed_at_unix_ms', 'qualification', 'sessions'],
    'snapshot',
  );
  protocol(
    snapshotCandidate.protocol,
    OBSERVATORY_EVENT_PROJECTION_PROTOCOL,
    'snapshot.protocol',
  );
  const sourceCursor = safeInteger(snapshotCandidate.source_cursor, 'snapshot.source_cursor', -1);
  const observedAt = safeInteger(
    snapshotCandidate.observed_at_unix_ms,
    'snapshot.observed_at_unix_ms',
  );
  const qualification = decodeQualification(snapshotCandidate.qualification);
  if (qualification !== null && qualification.issued_at_unix_ms > observedAt) {
    return invalid('qualification issuance cannot exceed observation time');
  }
  const sessions = exactArray(snapshotCandidate.sessions, 'snapshot.sessions', MAX_SESSIONS).map(
    decodeSession,
  );
  for (let index = 1; index < sessions.length; index += 1) {
    if (sessions[index].request_id <= sessions[index - 1].request_id) {
      return invalid('snapshot.sessions must have deterministic order and unique request ids');
    }
  }
  if (sessions.length > 0 && qualification === null) {
    return invalid('snapshot.sessions require qualification metadata');
  }
  if (sessions.some((session) => session.updated_at_unix_ms > observedAt)) {
    return invalid('snapshot session timestamps cannot exceed observation time');
  }
  if (
    qualification !== null &&
    sessions.some(
      (session) =>
        !session.terminal &&
        session.state !== 'quarantined' &&
        session.qualification_id !== qualification.qualification_id,
    )
  ) {
    return invalid('active session qualification does not match current qualification');
  }

  const incidents = exactArray(bundleCandidate.incidents, 'incidents', MAX_INCIDENTS).map(
    decodeIncident,
  );
  const statusCandidate = exactObject(
    bundleCandidate.provisioning,
    [
      'protocol',
      'route_ready',
      'source_cursor',
      'buffered_sessions',
      'quarantine_capacity',
      'dropped_quarantine_count',
    ],
    'provisioning',
  );
  protocol(
    statusCandidate.protocol,
    OBSERVATORY_EVENT_STATUS_PROTOCOL,
    'provisioning.protocol',
  );
  if (typeof statusCandidate.route_ready !== 'boolean') {
    return invalid('provisioning.route_ready must be boolean');
  }
  if (statusCandidate.route_ready !== (qualification?.route_ready ?? false)) {
    return invalid('provisioning.route_ready does not match qualifier-owned evidence');
  }
  const statusCursor = safeInteger(statusCandidate.source_cursor, 'provisioning.source_cursor', -1);
  const bufferedSessions = safeInteger(
    statusCandidate.buffered_sessions,
    'provisioning.buffered_sessions',
  );
  const quarantineCapacity = safeInteger(
    statusCandidate.quarantine_capacity,
    'provisioning.quarantine_capacity',
    1,
  );
  const dropped = safeInteger(
    statusCandidate.dropped_quarantine_count,
    'provisioning.dropped_quarantine_count',
  );
  if (statusCursor !== sourceCursor) return invalid('projection source cursor mismatch');
  if (bufferedSessions !== sessions.length || bufferedSessions > MAX_SESSIONS) {
    return invalid('provisioning.buffered_sessions does not match bounded session state');
  }
  if (quarantineCapacity > MAX_INCIDENTS || incidents.length > quarantineCapacity) {
    return invalid('incidents exceed bounded quarantine capacity');
  }

  return deepFreeze({
    protocol: OBSERVATORY_STREAM_PROTOCOL,
    generation: header.generation,
    bundle: {
      snapshot: {
        protocol: OBSERVATORY_EVENT_PROJECTION_PROTOCOL,
        source_cursor: sourceCursor,
        observed_at_unix_ms: observedAt,
        qualification,
        sessions,
      },
      incidents,
      provisioning: {
        protocol: OBSERVATORY_EVENT_STATUS_PROTOCOL,
        route_ready: statusCandidate.route_ready,
        source_cursor: statusCursor,
        buffered_sessions: bufferedSessions,
        quarantine_capacity: quarantineCapacity,
        dropped_quarantine_count: dropped,
      },
    },
  });
}
