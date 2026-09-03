export const A5_REPLICA_QUALIFICATION_PROTOCOL =
  'mycelium.replica_qualification.v1' as const;

export interface A5ReplicaTrackQualification {
  readonly protocol: typeof A5_REPLICA_QUALIFICATION_PROTOCOL;
  readonly qualification_id: string;
  readonly qualification_digest: string;
  readonly deployment_id: string;
  readonly deployment_epoch: number;
  readonly replica_group_id: string;
  readonly placement_id: string;
  readonly placement_ids: readonly string[];
  readonly track_id: string;
  readonly traffic_fraction: number;
  readonly qualifier_generation: number;
  readonly issued_at_unix_ms: number;
  readonly expires_at_unix_ms: number;
  readonly evidence_bundle_digest: string;
  readonly load_proof_digest: string;
  readonly assignment_digest: string;
  readonly artifact_verification_digest: string;
  readonly parity_verified: boolean;
  readonly startup_challenge_passed: boolean;
  readonly memory_within_bounds: boolean;
  readonly cleanup_within_bounds: boolean;
  readonly directed_link_qualified: boolean;
  readonly workload_envelope_digest: string;
  readonly rejected_reasons: readonly string[];
  readonly route_ready: boolean;
}

const QUALIFICATION_FIELDS = [
  'protocol',
  'qualification_id',
  'qualification_digest',
  'deployment_id',
  'deployment_epoch',
  'replica_group_id',
  'placement_id',
  'placement_ids',
  'track_id',
  'traffic_fraction',
  'qualifier_generation',
  'issued_at_unix_ms',
  'expires_at_unix_ms',
  'evidence_bundle_digest',
  'load_proof_digest',
  'assignment_digest',
  'artifact_verification_digest',
  'parity_verified',
  'startup_challenge_passed',
  'memory_within_bounds',
  'cleanup_within_bounds',
  'directed_link_qualified',
  'workload_envelope_digest',
  'rejected_reasons',
  'route_ready',
] as const;

function object(
  value: unknown,
  fields: readonly string[],
  path: string,
): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${path} must be an object`);
  }
  const record = value as Record<string, unknown>;
  if (
    Object.keys(record)
      .sort()
      .join('|') !== [...fields].sort().join('|')
  ) {
    throw new TypeError(`${path} has unknown or missing fields`);
  }
  return record;
}

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:~\/-]{0,255}$/;
const encoder = new TextEncoder();

function identifier(value: unknown, path: string): string {
  if (
    typeof value !== 'string' ||
    !IDENTIFIER.test(value) ||
    encoder.encode(value).byteLength > 256
  ) {
    throw new TypeError(`${path} must be a bounded identifier`);
  }
  return value;
}

function digest(value: unknown, path: string): string {
  if (typeof value !== 'string' || !SHA256.test(value)) {
    throw new TypeError(`${path} must be a sha256 digest`);
  }
  return value;
}

function integer(value: unknown, path: string): number {
  if (
    typeof value !== 'number' ||
    !Number.isSafeInteger(value) ||
    value < 0
  ) {
    throw new TypeError(`${path} must be a non-negative integer`);
  }
  return value;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new TypeError(`${path} must be a boolean`);
  }
  return value;
}

function trafficFraction(value: unknown): number {
  if (
    typeof value !== 'number' ||
    !Number.isFinite(value) ||
    value <= 0 ||
    value > 1
  ) {
    throw new TypeError('traffic_fraction must be in (0, 1]');
  }
  return value;
}

function identifierArray(
  value: unknown,
  path: string,
  minimum = 0,
): readonly string[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > 64) {
    throw new TypeError(`${path} must be a bounded array`);
  }
  const decoded = value.map((item, index) =>
    identifier(item, `${path}[${index}]`),
  );
  if (new Set(decoded).size !== decoded.length) {
    throw new TypeError(`${path} must contain unique identifiers`);
  }
  return Object.freeze(decoded);
}

export function decodeA5ReplicaTrackQualification(
  value: unknown,
): A5ReplicaTrackQualification {
  const source = object(value, QUALIFICATION_FIELDS, 'a5_qualification');
  if (source.protocol !== A5_REPLICA_QUALIFICATION_PROTOCOL) {
    throw new TypeError('a5_qualification protocol is invalid');
  }
  const expires = integer(
    source.expires_at_unix_ms,
    'a5_qualification.expires_at_unix_ms',
  );
  const issued = integer(
    source.issued_at_unix_ms,
    'a5_qualification.issued_at_unix_ms',
  );
  if (expires < issued) {
    throw new TypeError('a5_qualification expires before issuance');
  }
  const placementId = identifier(source.placement_id, 'placement_id');
  const placementIds = identifierArray(source.placement_ids, 'placement_ids', 2);
  if (!placementIds.includes(placementId)) {
    throw new TypeError('placement_ids must include placement_id');
  }
  return Object.freeze({
    protocol: A5_REPLICA_QUALIFICATION_PROTOCOL,
    qualification_id: digest(source.qualification_id, 'qualification_id'),
    qualification_digest: digest(
      source.qualification_digest,
      'qualification_digest',
    ),
    deployment_id: identifier(source.deployment_id, 'deployment_id'),
    deployment_epoch: integer(source.deployment_epoch, 'deployment_epoch'),
    replica_group_id: identifier(source.replica_group_id, 'replica_group_id'),
    placement_id: placementId,
    placement_ids: placementIds,
    track_id: identifier(source.track_id, 'track_id'),
    traffic_fraction: trafficFraction(source.traffic_fraction),
    qualifier_generation: integer(
      source.qualifier_generation,
      'qualifier_generation',
    ),
    issued_at_unix_ms: issued,
    expires_at_unix_ms: expires,
    evidence_bundle_digest: digest(
      source.evidence_bundle_digest,
      'evidence_bundle_digest',
    ),
    load_proof_digest: digest(source.load_proof_digest, 'load_proof_digest'),
    assignment_digest: digest(source.assignment_digest, 'assignment_digest'),
    artifact_verification_digest: digest(
      source.artifact_verification_digest,
      'artifact_verification_digest',
    ),
    parity_verified: boolean(source.parity_verified, 'parity_verified'),
    startup_challenge_passed: boolean(
      source.startup_challenge_passed,
      'startup_challenge_passed',
    ),
    memory_within_bounds: boolean(
      source.memory_within_bounds,
      'memory_within_bounds',
    ),
    cleanup_within_bounds: boolean(
      source.cleanup_within_bounds,
      'cleanup_within_bounds',
    ),
    directed_link_qualified: boolean(
      source.directed_link_qualified,
      'directed_link_qualified',
    ),
    workload_envelope_digest: digest(
      source.workload_envelope_digest,
      'workload_envelope_digest',
    ),
    rejected_reasons: identifierArray(source.rejected_reasons, 'rejected_reasons'),
    route_ready: boolean(source.route_ready, 'route_ready'),
  });
}

export function decodeA5ReplicaTrackQualifications(
  value: unknown,
): readonly A5ReplicaTrackQualification[] {
  if (!Array.isArray(value) || value.length > 64) {
    throw new TypeError('a5_qualifications must be a bounded array');
  }
  return Object.freeze(
    value.map((item, index) =>
      decodeA5ReplicaTrackQualification(item),
    ),
  );
}
