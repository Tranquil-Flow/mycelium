export const PRODUCT_BOOTSTRAP_PROTOCOL = 'mycelium.product_ui.bootstrap.v1' as const;
export const PRODUCT_OBSERVATORY_PROTOCOL = 'mycelium.product_ui.observatory.v1' as const;
export const PRODUCT_INFERENCE_PROTOCOL = 'mycelium.request_gateway.v1' as const;
export const PRODUCT_INFERENCE_SUBMISSION_PROTOCOL = 'mycelium.request_gateway.v2' as const;
export const PRODUCT_INFERENCE_EVENT_PROTOCOL = 'mycelium.request_event.v1' as const;
export const PRODUCT_INFERENCE_EVENT_PROTOCOL_V2 = 'mycelium.request_event.v2' as const;
export const PRODUCT_SWARM_PROTOCOL = 'mycelium.product_ui.swarm.v1' as const;
export const PRODUCT_ERROR_PROTOCOL = 'mycelium.product_ui.error.v1' as const;
export const PRODUCT_QUALIFIER_AUTHORITY =
  'mycelium_qualification.qualifier:RouteQualificationV1' as const;
export const MAX_PROMPT_UTF8_BYTES = 131_072;
export const MAX_NEW_TOKENS = 4_096;
export const QUALIFICATION_MAX_AGE_MS = 3_600_000;
export const MAX_COLLECTION_ITEMS = 4_096;

export const PRODUCT_API_PATHS = Object.freeze({
  observatory_snapshot: '/api/v1/observatory/snapshot',
  observatory_events: '/api/v1/observatory/events',
  qualification_current: '/api/v1/qualification/current',
  inference_submit: '/api/v1/inference',
  swarm_status: '/api/v1/swarm/status',
  swarm_invites: '/api/v1/swarm/invites',
  swarm_join: '/api/v1/swarm/join',
  swarm_leave: '/api/v1/swarm/leave',
  product_snapshot: '/api/v1/product/snapshot',
  product_events: '/api/v1/product/events',
  product_export: '/api/v1/product/export',
} as const);

const SHA256_REF = /^sha256:[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const REQUEST_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/;
const SAFE_ERROR_CODE = /^[a-z][a-z0-9_]{0,63}$/;
const FORBIDDEN_OBJECT_KEYS = new Set(['__proto__', 'prototype', 'constructor']);

export type ProductSourceMode = 'fixture' | 'live' | 'replay';
export type ProductFreshness = 'fixture' | 'current' | 'stale' | 'replay' | 'unknown';

export class ProductContractError extends Error {
  readonly code = 'invalid_product_contract' as const;

  constructor(readonly field: string, detail: string) {
    super(`${field}: ${detail}`);
    this.name = 'ProductContractError';
  }
}

function fail(field: string, detail: string): never {
  throw new ProductContractError(field, detail);
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return fail(field, 'expected object');
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    return fail(field, 'expected plain object');
  }
  return value as Record<string, unknown>;
}

function exactRecord(
  value: unknown,
  expected: readonly string[],
  field: string,
): Record<string, unknown> {
  const candidate = record(value, field);
  let keys: string[];
  try {
    keys = Object.keys(candidate);
  } catch {
    return fail(field, 'unreadable object');
  }
  if (keys.some((key) => FORBIDDEN_OBJECT_KEYS.has(key))) {
    return fail(field, 'forbidden property name');
  }
  if (
    keys.length !== expected.length ||
    keys.some((key) => !expected.includes(key)) ||
    expected.some((key) => !Object.prototype.hasOwnProperty.call(candidate, key))
  ) {
    return fail(field, 'unexpected fields');
  }
  return candidate;
}

function readOwn(candidate: Record<string, unknown>, key: string, field: string): unknown {
  try {
    return candidate[key];
  } catch {
    return fail(field, 'unreadable field');
  }
}

function stringValue(
  value: unknown,
  field: string,
  options: { readonly min?: number; readonly max?: number } = {},
): string {
  const min = options.min ?? 1;
  const max = options.max ?? 256;
  if (typeof value !== 'string' || value.length < min || value.length > max) {
    return fail(field, 'invalid string');
  }
  return value;
}

function identifier(value: unknown, field: string): string {
  const decoded = stringValue(value, field);
  if (!IDENTIFIER.test(decoded)) return fail(field, 'invalid identifier');
  return decoded;
}

function requestIdentifier(value: unknown, field: string): string {
  if (typeof value !== 'string' || !REQUEST_IDENTIFIER.test(value)) {
    return fail(field, 'invalid request identifier');
  }
  return value;
}

function safeCode(value: unknown, field: string): string {
  const decoded = stringValue(value, field, { max: 64 });
  if (!SAFE_ERROR_CODE.test(decoded)) return fail(field, 'invalid public error code');
  return decoded;
}

function safeInteger(value: unknown, field: string, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    return fail(field, 'invalid safe integer');
  }
  return value as number;
}

function booleanValue(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') return fail(field, 'invalid boolean');
  return value;
}

function enumValue<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  field: string,
): T[number] {
  if (typeof value !== 'string' || !allowed.includes(value)) {
    return fail(field, 'invalid enum value');
  }
  return value as T[number];
}

function nullableInteger(value: unknown, field: string): number | null {
  return value === null ? null : safeInteger(value, field);
}

function sha256(value: unknown, field: string): string {
  if (typeof value !== 'string' || !SHA256_REF.test(value)) {
    return fail(field, 'invalid sha256 reference');
  }
  return value;
}

function uniqueStringArray(
  value: unknown,
  field: string,
  decode: (item: unknown, itemField: string) => string,
  maximum = 128,
): readonly string[] {
  if (!Array.isArray(value) || value.length > maximum) return fail(field, 'invalid array');
  const decoded = value.map((item, index) => decode(item, `${field}[${index}]`));
  if (new Set(decoded).size !== decoded.length) return fail(field, 'duplicate values');
  return Object.freeze(decoded);
}

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

export interface ProductBootstrap {
  readonly protocol: typeof PRODUCT_BOOTSTRAP_PROTOCOL;
  readonly source_mode: ProductSourceMode;
  readonly session: {
    readonly csrf_header: 'X-Mycelium-CSRF';
    readonly csrf_token: string;
    readonly expires_at_unix_ms: number;
  };
  readonly api: typeof PRODUCT_API_PATHS;
  readonly limits: {
    readonly max_prompt_utf8_bytes: typeof MAX_PROMPT_UTF8_BYTES;
    readonly max_new_tokens: typeof MAX_NEW_TOKENS;
  };
  readonly qualification_authority: typeof PRODUCT_QUALIFIER_AUTHORITY;
}

export interface ProductObservatorySource {
  readonly mode: ProductSourceMode;
  readonly freshness: ProductFreshness;
  readonly observed_at_unix_ms: number | null;
  readonly replay_of_generation: number | null;
}

export interface ProductObservatoryEnvelope {
  readonly protocol: typeof PRODUCT_OBSERVATORY_PROTOCOL;
  readonly generation: number;
  readonly status: 'connecting' | 'connected' | 'disconnected';
  readonly source: ProductObservatorySource;
  readonly metrics: {
    readonly native_node_count: number | null;
    readonly browser_worker_count: number | null;
    readonly incident_count: number | null;
  };
}

export interface QualificationBinding {
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

export type ProductEvidenceClass = 'physical_qualification' | 'synthetic_test_fixture';

export interface ProductQualification {
  readonly protocol: typeof PRODUCT_INFERENCE_PROTOCOL;
  readonly issued_at_unix_ms: number;
  readonly evidence_class: ProductEvidenceClass;
  readonly route_ready: boolean;
  readonly reason_codes: readonly string[];
  readonly binding: QualificationBinding;
}

export interface InferenceSubmission {
  readonly protocol: typeof PRODUCT_INFERENCE_SUBMISSION_PROTOCOL;
  readonly prompt: string;
  readonly max_new_tokens: number;
  readonly qualification: QualificationBinding;
  readonly workload_profile_id: string;
  readonly qos_class: 'interactive' | 'batch';
}

export interface InferenceWorkloadBinding {
  readonly profile_id: string;
  readonly qos_class: 'interactive' | 'batch';
}

export interface InferenceAcceptedResponse {
  readonly protocol: typeof PRODUCT_INFERENCE_PROTOCOL;
  readonly request_id: string;
  readonly accepted: true;
  readonly event_path: string;
  readonly cancel_path: string;
}

export interface InferenceCancelResponse {
  readonly protocol: typeof PRODUCT_INFERENCE_PROTOCOL;
  readonly request_id: string;
  readonly cancelled: boolean;
}

interface InferenceEventBase {
  readonly protocol: typeof PRODUCT_INFERENCE_EVENT_PROTOCOL | typeof PRODUCT_INFERENCE_EVENT_PROTOCOL_V2;
  readonly request_id: string;
  readonly sequence: number;
  readonly publisher_generation: number;
}

export type InferenceEvent =
  | (InferenceEventBase & { readonly type: 'accepted' | 'completed' | 'cancelled' })
  | (InferenceEventBase & {
      readonly type: 'token';
      readonly token_index: number;
      readonly text: string;
    })
  | (InferenceEventBase & { readonly type: 'failed'; readonly code: string })
  | (InferenceEventBase & {
      readonly type: 'lifecycle';
      readonly phase: 'admission' | 'queue' | 'prefill' | 'first_token' | 'decode' | 'completion';
    });

export interface ProductErrorResponse {
  readonly protocol: typeof PRODUCT_ERROR_PROTOCOL;
  readonly code: string;
  readonly retryable: boolean;
}

export interface ProductNativeNode {
  readonly member_id: string;
  readonly capability: 'native_inference_node';
  readonly membership_state:
    | 'invited'
    | 'trusted'
    | 'reachable'
    | 'assigned'
    | 'qualified'
    | 'revoked';
  readonly connectivity: 'direct' | 'relayed' | 'local' | 'disconnected' | 'stale' | 'unknown';
  readonly endpoint_id: string | null;
}

export interface ProductBrowserWorker {
  readonly peer_id: string;
  readonly capability: 'synthetic_browser_probe';
  readonly state: 'invited' | 'connecting' | 'ready' | 'busy' | 'disconnected' | 'stale' | 'revoked';
  readonly expires_at_unix_ms: number;
}

export interface ProductSwarmStatus {
  readonly protocol: typeof PRODUCT_SWARM_PROTOCOL;
  readonly native_nodes: readonly ProductNativeNode[];
  readonly browser_workers: readonly ProductBrowserWorker[];
}

export interface SwarmInviteRequest {
  readonly protocol: typeof PRODUCT_SWARM_PROTOCOL;
  readonly action: 'create_invite';
  readonly capability: 'native_inference_node' | 'synthetic_browser_probe';
  readonly expires_in_seconds: number;
}

export interface SwarmInviteResponse {
  readonly protocol: typeof PRODUCT_SWARM_PROTOCOL;
  readonly invite_id: string;
  readonly invite_code: string;
  readonly capability: 'native_inference_node' | 'synthetic_browser_probe';
  readonly expires_at_unix_ms: number;
}

export interface SwarmJoinRequest {
  readonly protocol: typeof PRODUCT_SWARM_PROTOCOL;
  readonly action: 'join';
  readonly invite_code: string;
  readonly display_name: string;
}

export interface SwarmJoinResponse {
  readonly protocol: typeof PRODUCT_SWARM_PROTOCOL;
  readonly joined: true;
  readonly member_id: string;
  readonly capability: 'native_inference_node' | 'synthetic_browser_probe';
}

export interface SwarmLeaveRequest {
  readonly protocol: typeof PRODUCT_SWARM_PROTOCOL;
  readonly action: 'leave';
  readonly member_id: string;
}

export interface SwarmLeaveResponse {
  readonly protocol: typeof PRODUCT_SWARM_PROTOCOL;
  readonly member_id: string;
  readonly left: boolean;
}

function decodeBinding(value: unknown, field = 'qualification.binding'): QualificationBinding {
  const candidate = exactRecord(
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
    field,
  );
  const stageDigests = uniqueStringArray(
    readOwn(candidate, 'stage_load_proof_digests', `${field}.stage_load_proof_digests`),
    `${field}.stage_load_proof_digests`,
    sha256,
    MAX_COLLECTION_ITEMS,
  );
  return Object.freeze({
    qualification_id: stringValue(
      readOwn(candidate, 'qualification_id', field),
      `${field}.qualification_id`,
      { max: 4096 },
    ),
    qualification_digest: sha256(
      readOwn(candidate, 'qualification_digest', field),
      `${field}.qualification_digest`,
    ),
    deployment_id: stringValue(
      readOwn(candidate, 'deployment_id', field),
      `${field}.deployment_id`,
      { max: 4096 },
    ),
    deployment_epoch: safeInteger(
      readOwn(candidate, 'deployment_epoch', field),
      `${field}.deployment_epoch`,
    ),
    topology_version: safeInteger(
      readOwn(candidate, 'topology_version', field),
      `${field}.topology_version`,
    ),
    model_id: stringValue(
      readOwn(candidate, 'model_id', field),
      `${field}.model_id`,
      { max: 4096 },
    ),
    resolved_commit: stringValue(
      readOwn(candidate, 'resolved_commit', field),
      `${field}.resolved_commit`,
      { max: 4096 },
    ),
    manifest_digest: sha256(readOwn(candidate, 'manifest_digest', field), `${field}.manifest_digest`),
    path_manifest_digest: sha256(
      readOwn(candidate, 'path_manifest_digest', field),
      `${field}.path_manifest_digest`,
    ),
    stage_load_proof_digests: stageDigests,
  });
}

export function decodeProductBootstrap(value: unknown): ProductBootstrap {
  const candidate = exactRecord(
    value,
    ['protocol', 'source_mode', 'session', 'api', 'limits', 'qualification_authority'],
    'bootstrap',
  );
  if (readOwn(candidate, 'protocol', 'bootstrap.protocol') !== PRODUCT_BOOTSTRAP_PROTOCOL) {
    return fail('bootstrap.protocol', 'unsupported protocol');
  }
  const session = exactRecord(
    readOwn(candidate, 'session', 'bootstrap.session'),
    ['csrf_header', 'csrf_token', 'expires_at_unix_ms'],
    'bootstrap.session',
  );
  if (readOwn(session, 'csrf_header', 'bootstrap.session.csrf_header') !== 'X-Mycelium-CSRF') {
    return fail('bootstrap.session.csrf_header', 'unsupported CSRF header');
  }
  const api = exactRecord(
    readOwn(candidate, 'api', 'bootstrap.api'),
    Object.keys(PRODUCT_API_PATHS),
    'bootstrap.api',
  );
  for (const [key, path] of Object.entries(PRODUCT_API_PATHS)) {
    if (readOwn(api, key, `bootstrap.api.${key}`) !== path) {
      return fail(`bootstrap.api.${key}`, 'unsupported same-origin path');
    }
  }
  const limits = exactRecord(
    readOwn(candidate, 'limits', 'bootstrap.limits'),
    ['max_prompt_utf8_bytes', 'max_new_tokens'],
    'bootstrap.limits',
  );
  if (
    readOwn(limits, 'max_prompt_utf8_bytes', 'bootstrap.limits.max_prompt_utf8_bytes') !==
      MAX_PROMPT_UTF8_BYTES ||
    readOwn(limits, 'max_new_tokens', 'bootstrap.limits.max_new_tokens') !== MAX_NEW_TOKENS
  ) {
    return fail('bootstrap.limits', 'unsupported bounds');
  }
  if (
    readOwn(candidate, 'qualification_authority', 'bootstrap.qualification_authority') !==
    PRODUCT_QUALIFIER_AUTHORITY
  ) {
    return fail('bootstrap.qualification_authority', 'unsupported authority');
  }
  return Object.freeze({
    protocol: PRODUCT_BOOTSTRAP_PROTOCOL,
    source_mode: enumValue(
      readOwn(candidate, 'source_mode', 'bootstrap.source_mode'),
      ['fixture', 'live', 'replay'] as const,
      'bootstrap.source_mode',
    ),
    session: Object.freeze({
      csrf_header: 'X-Mycelium-CSRF' as const,
      csrf_token: stringValue(
        readOwn(session, 'csrf_token', 'bootstrap.session.csrf_token'),
        'bootstrap.session.csrf_token',
        { min: 16, max: 512 },
      ),
      expires_at_unix_ms: safeInteger(
        readOwn(session, 'expires_at_unix_ms', 'bootstrap.session.expires_at_unix_ms'),
        'bootstrap.session.expires_at_unix_ms',
      ),
    }),
    api: PRODUCT_API_PATHS,
    limits: Object.freeze({
      max_prompt_utf8_bytes: MAX_PROMPT_UTF8_BYTES,
      max_new_tokens: MAX_NEW_TOKENS,
    }),
    qualification_authority: PRODUCT_QUALIFIER_AUTHORITY,
  });
}

export function decodeProductObservatory(value: unknown): ProductObservatoryEnvelope {
  const candidate = exactRecord(
    value,
    ['protocol', 'generation', 'status', 'source', 'metrics'],
    'observatory',
  );
  if (readOwn(candidate, 'protocol', 'observatory.protocol') !== PRODUCT_OBSERVATORY_PROTOCOL) {
    return fail('observatory.protocol', 'unsupported protocol');
  }
  const source = exactRecord(
    readOwn(candidate, 'source', 'observatory.source'),
    ['mode', 'freshness', 'observed_at_unix_ms', 'replay_of_generation'],
    'observatory.source',
  );
  const mode = enumValue(
    readOwn(source, 'mode', 'observatory.source.mode'),
    ['fixture', 'live', 'replay'] as const,
    'observatory.source.mode',
  );
  const freshness = enumValue(
    readOwn(source, 'freshness', 'observatory.source.freshness'),
    ['fixture', 'current', 'stale', 'replay', 'unknown'] as const,
    'observatory.source.freshness',
  );
  const replayOfGeneration = nullableInteger(
    readOwn(source, 'replay_of_generation', 'observatory.source.replay_of_generation'),
    'observatory.source.replay_of_generation',
  );
  if (mode === 'fixture' && (freshness !== 'fixture' || replayOfGeneration !== null)) {
    return fail('observatory.source', 'inconsistent fixture provenance');
  }
  if (
    mode === 'live' &&
    (!(['current', 'stale', 'unknown'] as const).includes(freshness as 'current') ||
      replayOfGeneration !== null)
  ) {
    return fail('observatory.source', 'inconsistent live provenance');
  }
  if (mode === 'replay' && (freshness !== 'replay' || replayOfGeneration === null)) {
    return fail('observatory.source', 'inconsistent replay provenance');
  }
  const metrics = exactRecord(
    readOwn(candidate, 'metrics', 'observatory.metrics'),
    ['native_node_count', 'browser_worker_count', 'incident_count'],
    'observatory.metrics',
  );
  return Object.freeze({
    protocol: PRODUCT_OBSERVATORY_PROTOCOL,
    generation: safeInteger(readOwn(candidate, 'generation', 'observatory.generation'), 'observatory.generation'),
    status: enumValue(
      readOwn(candidate, 'status', 'observatory.status'),
      ['connecting', 'connected', 'disconnected'] as const,
      'observatory.status',
    ),
    source: Object.freeze({
      mode,
      freshness,
      observed_at_unix_ms: nullableInteger(
        readOwn(source, 'observed_at_unix_ms', 'observatory.source.observed_at_unix_ms'),
        'observatory.source.observed_at_unix_ms',
      ),
      replay_of_generation: replayOfGeneration,
    }),
    metrics: Object.freeze({
      native_node_count: nullableInteger(
        readOwn(metrics, 'native_node_count', 'observatory.metrics.native_node_count'),
        'observatory.metrics.native_node_count',
      ),
      browser_worker_count: nullableInteger(
        readOwn(metrics, 'browser_worker_count', 'observatory.metrics.browser_worker_count'),
        'observatory.metrics.browser_worker_count',
      ),
      incident_count: nullableInteger(
        readOwn(metrics, 'incident_count', 'observatory.metrics.incident_count'),
        'observatory.metrics.incident_count',
      ),
    }),
  });
}

function assertQualificationInvariants(qualification: ProductQualification): void {
  if (qualification.route_ready) {
    if (
      qualification.evidence_class !== 'physical_qualification' ||
      qualification.reason_codes.length !== 0 ||
      qualification.binding.stage_load_proof_digests.length === 0
    ) {
      fail('qualification', 'inconsistent accepted qualification');
    }
  } else if (qualification.reason_codes.length === 0) {
    fail('qualification', 'inconsistent rejected qualification');
  }
}

export function decodeProductQualification(value: unknown): ProductQualification {
  const candidate = exactRecord(
    value,
    ['protocol', 'issued_at_unix_ms', 'evidence_class', 'route_ready', 'reason_codes', 'binding'],
    'qualification',
  );
  if (readOwn(candidate, 'protocol', 'qualification.protocol') !== PRODUCT_INFERENCE_PROTOCOL) {
    return fail('qualification.protocol', 'unsupported protocol');
  }
  const qualification: ProductQualification = Object.freeze({
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    issued_at_unix_ms: safeInteger(
      readOwn(candidate, 'issued_at_unix_ms', 'qualification.issued_at_unix_ms'),
      'qualification.issued_at_unix_ms',
    ),
    evidence_class: enumValue(
      readOwn(candidate, 'evidence_class', 'qualification.evidence_class'),
      ['physical_qualification', 'synthetic_test_fixture'] as const,
      'qualification.evidence_class',
    ),
    route_ready: booleanValue(
      readOwn(candidate, 'route_ready', 'qualification.route_ready'),
      'qualification.route_ready',
    ),
    reason_codes: uniqueStringArray(
      readOwn(candidate, 'reason_codes', 'qualification.reason_codes'),
      'qualification.reason_codes',
      safeCode,
    ),
    binding: decodeBinding(readOwn(candidate, 'binding', 'qualification.binding')),
  });
  assertQualificationInvariants(qualification);
  return qualification;
}

export function inferenceBlockReason(
  qualification: ProductQualification | null,
  nowUnixMs: number,
): string | null {
  if (qualification === null) return 'Qualification unavailable';
  try {
    assertQualificationInvariants(qualification);
  } catch {
    return 'Qualification is invalid';
  }
  if (!qualification.route_ready) {
    return `Route is not ready: ${qualification.reason_codes.join(', ')}`;
  }
  if (!Number.isSafeInteger(nowUnixMs) || nowUnixMs < qualification.issued_at_unix_ms) {
    return 'Qualification timestamp is in the future';
  }
  if (nowUnixMs - qualification.issued_at_unix_ms > QUALIFICATION_MAX_AGE_MS) {
    return 'Qualification is stale';
  }
  return null;
}

export function buildInferenceSubmission(
  prompt: string,
  maxNewTokens: number,
  qualification: ProductQualification | null,
  nowUnixMs: number,
  workload: InferenceWorkloadBinding = {
    profile_id: 'interactive_chat_v1',
    qos_class: 'interactive',
  },
): InferenceSubmission {
  const blocked = inferenceBlockReason(qualification, nowUnixMs);
  if (blocked !== null) throw new ProductContractError('submission.qualification', blocked);
  if (typeof prompt !== 'string' || prompt.length === 0) {
    return fail('submission.prompt', 'Prompt is required');
  }
  if (utf8Length(prompt) > MAX_PROMPT_UTF8_BYTES) {
    return fail('submission.prompt', `Prompt exceeds ${MAX_PROMPT_UTF8_BYTES} UTF-8 bytes`);
  }
  const tokenCount = safeInteger(maxNewTokens, 'submission.max_new_tokens', 1, MAX_NEW_TOKENS);
  const workloadProfileId = identifier(workload.profile_id, 'submission.workload_profile_id');
  const qosClass = enumValue(
    workload.qos_class,
    ['interactive', 'batch'] as const,
    'submission.qos_class',
  );
  return Object.freeze({
    protocol: PRODUCT_INFERENCE_SUBMISSION_PROTOCOL,
    prompt,
    max_new_tokens: tokenCount,
    qualification: qualification!.binding,
    workload_profile_id: workloadProfileId,
    qos_class: qosClass,
  });
}

export function decodeInferenceEvent(value: unknown): InferenceEvent {
  const baseCandidate = record(value, 'inference_event');
  const type = enumValue(
    readOwn(baseCandidate, 'type', 'inference_event.type'),
    ['accepted', 'token', 'completed', 'cancelled', 'failed', 'lifecycle'] as const,
    'inference_event.type',
  );
  const expected =
    type === 'token'
      ? ['protocol', 'request_id', 'sequence', 'publisher_generation', 'type', 'token_index', 'text']
      : type === 'failed'
        ? ['protocol', 'request_id', 'sequence', 'publisher_generation', 'type', 'code']
        : type === 'lifecycle'
          ? ['protocol', 'request_id', 'sequence', 'publisher_generation', 'type', 'phase']
        : ['protocol', 'request_id', 'sequence', 'publisher_generation', 'type'];
  const candidate = exactRecord(value, expected, 'inference_event');
  const protocol = readOwn(candidate, 'protocol', 'inference_event.protocol');
  if (protocol !== PRODUCT_INFERENCE_EVENT_PROTOCOL && protocol !== PRODUCT_INFERENCE_EVENT_PROTOCOL_V2) {
    return fail('inference_event.protocol', 'unsupported protocol');
  }
  if (type === 'lifecycle' && protocol !== PRODUCT_INFERENCE_EVENT_PROTOCOL_V2) {
    return fail('inference_event.protocol', 'lifecycle requires v2');
  }
  const publisherGeneration = safeInteger(
    readOwn(candidate, 'publisher_generation', 'inference_event.publisher_generation'),
    'inference_event.publisher_generation',
  );
  if (publisherGeneration < 1) {
    return fail('inference_event.publisher_generation', 'must be positive');
  }
  const base = {
    protocol,
    request_id: requestIdentifier(
      readOwn(candidate, 'request_id', 'inference_event.request_id'),
      'inference_event.request_id',
    ),
    sequence: safeInteger(readOwn(candidate, 'sequence', 'inference_event.sequence'), 'inference_event.sequence'),
    publisher_generation: publisherGeneration,
  };
  if (type === 'token') {
    const textValue = readOwn(candidate, 'text', 'inference_event.text');
    if (
      typeof textValue !== 'string' ||
      Array.from(textValue).length > 65_536 ||
      utf8Length(textValue) > 262_144
    ) {
      return fail('inference_event.text', 'text too large');
    }
    return Object.freeze({
      ...base,
      type,
      token_index: safeInteger(
        readOwn(candidate, 'token_index', 'inference_event.token_index'),
        'inference_event.token_index',
      ),
      text: textValue,
    });
  }
  if (type === 'failed') {
    return Object.freeze({
      ...base,
      type,
      code: safeCode(readOwn(candidate, 'code', 'inference_event.code'), 'inference_event.code'),
    });
  }
  if (type === 'lifecycle') {
    return Object.freeze({
      ...base,
      type,
      phase: enumValue(
        readOwn(candidate, 'phase', 'inference_event.phase'),
        ['admission', 'queue', 'prefill', 'first_token', 'decode', 'completion'] as const,
        'inference_event.phase',
      ),
    });
  }
  return Object.freeze({ ...base, type });
}

export function decodeInferenceAccepted(value: unknown): InferenceAcceptedResponse {
  const candidate = exactRecord(
    value,
    ['protocol', 'request_id', 'accepted', 'event_path', 'cancel_path'],
    'inference_accepted',
  );
  if (readOwn(candidate, 'protocol', 'inference_accepted.protocol') !== PRODUCT_INFERENCE_PROTOCOL) {
    return fail('inference_accepted.protocol', 'unsupported protocol');
  }
  if (readOwn(candidate, 'accepted', 'inference_accepted.accepted') !== true) {
    return fail('inference_accepted.accepted', 'request was not accepted');
  }
  const id = requestIdentifier(
    readOwn(candidate, 'request_id', 'inference_accepted.request_id'),
    'inference_accepted.request_id',
  );
  const expectedEventPath = `/api/v1/inference/${id}/events`;
  const expectedCancelPath = `/api/v1/inference/${id}/cancel`;
  if (readOwn(candidate, 'event_path', 'inference_accepted.event_path') !== expectedEventPath) {
    return fail('inference_accepted.event_path', 'request ID mismatch');
  }
  if (readOwn(candidate, 'cancel_path', 'inference_accepted.cancel_path') !== expectedCancelPath) {
    return fail('inference_accepted.cancel_path', 'request ID mismatch');
  }
  return Object.freeze({
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    request_id: id,
    accepted: true,
    event_path: expectedEventPath,
    cancel_path: expectedCancelPath,
  });
}

export function decodeInferenceCancelResponse(value: unknown): InferenceCancelResponse {
  const candidate = exactRecord(
    value,
    ['protocol', 'request_id', 'cancelled'],
    'inference_cancel',
  );
  if (readOwn(candidate, 'protocol', 'inference_cancel.protocol') !== PRODUCT_INFERENCE_PROTOCOL) {
    return fail('inference_cancel.protocol', 'unsupported protocol');
  }
  return Object.freeze({
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    request_id: requestIdentifier(
      readOwn(candidate, 'request_id', 'inference_cancel.request_id'),
      'inference_cancel.request_id',
    ),
    cancelled: booleanValue(
      readOwn(candidate, 'cancelled', 'inference_cancel.cancelled'),
      'inference_cancel.cancelled',
    ),
  });
}

export function decodeProductError(value: unknown): ProductErrorResponse {
  const candidate = exactRecord(value, ['protocol', 'code', 'retryable'], 'product_error');
  if (readOwn(candidate, 'protocol', 'product_error.protocol') !== PRODUCT_ERROR_PROTOCOL) {
    return fail('product_error.protocol', 'unsupported protocol');
  }
  return Object.freeze({
    protocol: PRODUCT_ERROR_PROTOCOL,
    code: safeCode(readOwn(candidate, 'code', 'product_error.code'), 'product_error.code'),
    retryable: booleanValue(
      readOwn(candidate, 'retryable', 'product_error.retryable'),
      'product_error.retryable',
    ),
  });
}

function decodeNativeNode(value: unknown, index: number): ProductNativeNode {
  const field = `swarm.native_nodes[${index}]`;
  const candidate = exactRecord(
    value,
    ['member_id', 'capability', 'membership_state', 'connectivity', 'endpoint_id'],
    field,
  );
  if (readOwn(candidate, 'capability', `${field}.capability`) !== 'native_inference_node') {
    return fail(`${field}.capability`, 'invalid capability');
  }
  const endpointValue = readOwn(candidate, 'endpoint_id', `${field}.endpoint_id`);
  return Object.freeze({
    member_id: identifier(readOwn(candidate, 'member_id', `${field}.member_id`), `${field}.member_id`),
    capability: 'native_inference_node',
    membership_state: enumValue(
      readOwn(candidate, 'membership_state', `${field}.membership_state`),
      ['invited', 'trusted', 'reachable', 'assigned', 'qualified', 'revoked'] as const,
      `${field}.membership_state`,
    ),
    connectivity: enumValue(
      readOwn(candidate, 'connectivity', `${field}.connectivity`),
      ['direct', 'relayed', 'local', 'disconnected', 'stale', 'unknown'] as const,
      `${field}.connectivity`,
    ),
    endpoint_id: endpointValue === null ? null : identifier(endpointValue, `${field}.endpoint_id`),
  });
}

function decodeBrowserWorker(value: unknown, index: number): ProductBrowserWorker {
  const field = `swarm.browser_workers[${index}]`;
  const candidate = exactRecord(
    value,
    ['peer_id', 'capability', 'state', 'expires_at_unix_ms'],
    field,
  );
  if (readOwn(candidate, 'capability', `${field}.capability`) !== 'synthetic_browser_probe') {
    return fail(`${field}.capability`, 'invalid capability');
  }
  return Object.freeze({
    peer_id: identifier(readOwn(candidate, 'peer_id', `${field}.peer_id`), `${field}.peer_id`),
    capability: 'synthetic_browser_probe',
    state: enumValue(
      readOwn(candidate, 'state', `${field}.state`),
      ['invited', 'connecting', 'ready', 'busy', 'disconnected', 'stale', 'revoked'] as const,
      `${field}.state`,
    ),
    expires_at_unix_ms: safeInteger(
      readOwn(candidate, 'expires_at_unix_ms', `${field}.expires_at_unix_ms`),
      `${field}.expires_at_unix_ms`,
    ),
  });
}

export function decodeProductSwarmStatus(value: unknown): ProductSwarmStatus {
  const candidate = exactRecord(value, ['protocol', 'native_nodes', 'browser_workers'], 'swarm');
  if (readOwn(candidate, 'protocol', 'swarm.protocol') !== PRODUCT_SWARM_PROTOCOL) {
    return fail('swarm.protocol', 'unsupported protocol');
  }
  const nativeNodesValue = readOwn(candidate, 'native_nodes', 'swarm.native_nodes');
  const browserWorkersValue = readOwn(candidate, 'browser_workers', 'swarm.browser_workers');
  if (!Array.isArray(nativeNodesValue) || nativeNodesValue.length > MAX_COLLECTION_ITEMS) {
    return fail('swarm.native_nodes', 'invalid array');
  }
  if (!Array.isArray(browserWorkersValue) || browserWorkersValue.length > MAX_COLLECTION_ITEMS) {
    return fail('swarm.browser_workers', 'invalid array');
  }
  return Object.freeze({
    protocol: PRODUCT_SWARM_PROTOCOL,
    native_nodes: Object.freeze(nativeNodesValue.map(decodeNativeNode)),
    browser_workers: Object.freeze(browserWorkersValue.map(decodeBrowserWorker)),
  });
}

export function buildSwarmInviteRequest(
  capability: 'native_inference_node' | 'synthetic_browser_probe',
  expiresInSeconds: number,
): SwarmInviteRequest {
  return Object.freeze({
    protocol: PRODUCT_SWARM_PROTOCOL,
    action: 'create_invite',
    capability,
    expires_in_seconds: safeInteger(expiresInSeconds, 'swarm_invite.expires_in_seconds', 30, 86_400),
  });
}

export function decodeSwarmInviteResponse(value: unknown): SwarmInviteResponse {
  const candidate = exactRecord(
    value,
    ['protocol', 'invite_id', 'invite_code', 'capability', 'expires_at_unix_ms'],
    'swarm_invite',
  );
  if (readOwn(candidate, 'protocol', 'swarm_invite.protocol') !== PRODUCT_SWARM_PROTOCOL) {
    return fail('swarm_invite.protocol', 'unsupported protocol');
  }
  return Object.freeze({
    protocol: PRODUCT_SWARM_PROTOCOL,
    invite_id: identifier(readOwn(candidate, 'invite_id', 'swarm_invite.invite_id'), 'swarm_invite.invite_id'),
    invite_code: stringValue(
      readOwn(candidate, 'invite_code', 'swarm_invite.invite_code'),
      'swarm_invite.invite_code',
      { min: 16, max: 2_048 },
    ),
    capability: enumValue(
      readOwn(candidate, 'capability', 'swarm_invite.capability'),
      ['native_inference_node', 'synthetic_browser_probe'] as const,
      'swarm_invite.capability',
    ),
    expires_at_unix_ms: safeInteger(
      readOwn(candidate, 'expires_at_unix_ms', 'swarm_invite.expires_at_unix_ms'),
      'swarm_invite.expires_at_unix_ms',
    ),
  });
}

export function buildSwarmJoinRequest(inviteCode: string, displayName: string): SwarmJoinRequest {
  return Object.freeze({
    protocol: PRODUCT_SWARM_PROTOCOL,
    action: 'join',
    invite_code: stringValue(inviteCode, 'swarm_join.invite_code', { min: 16, max: 2_048 }),
    display_name: stringValue(displayName, 'swarm_join.display_name', { max: 128 }),
  });
}

export function decodeSwarmJoinResponse(value: unknown): SwarmJoinResponse {
  const candidate = exactRecord(
    value,
    ['protocol', 'joined', 'member_id', 'capability'],
    'swarm_join',
  );
  if (readOwn(candidate, 'protocol', 'swarm_join.protocol') !== PRODUCT_SWARM_PROTOCOL) {
    return fail('swarm_join.protocol', 'unsupported protocol');
  }
  if (readOwn(candidate, 'joined', 'swarm_join.joined') !== true) {
    return fail('swarm_join.joined', 'join was not accepted');
  }
  return Object.freeze({
    protocol: PRODUCT_SWARM_PROTOCOL,
    joined: true,
    member_id: identifier(readOwn(candidate, 'member_id', 'swarm_join.member_id'), 'swarm_join.member_id'),
    capability: enumValue(
      readOwn(candidate, 'capability', 'swarm_join.capability'),
      ['native_inference_node', 'synthetic_browser_probe'] as const,
      'swarm_join.capability',
    ),
  });
}

export function buildSwarmLeaveRequest(memberId: string): SwarmLeaveRequest {
  return Object.freeze({
    protocol: PRODUCT_SWARM_PROTOCOL,
    action: 'leave',
    member_id: identifier(memberId, 'swarm_leave.member_id'),
  });
}

export function decodeSwarmLeaveResponse(value: unknown): SwarmLeaveResponse {
  const candidate = exactRecord(value, ['protocol', 'member_id', 'left'], 'swarm_leave');
  if (readOwn(candidate, 'protocol', 'swarm_leave.protocol') !== PRODUCT_SWARM_PROTOCOL) {
    return fail('swarm_leave.protocol', 'unsupported protocol');
  }
  return Object.freeze({
    protocol: PRODUCT_SWARM_PROTOCOL,
    member_id: identifier(readOwn(candidate, 'member_id', 'swarm_leave.member_id'), 'swarm_leave.member_id'),
    left: booleanValue(readOwn(candidate, 'left', 'swarm_leave.left'), 'swarm_leave.left'),
  });
}

export function sourceTruthLabel(mode: ProductSourceMode): string {
  if (mode === 'fixture') return 'Fixture data';
  if (mode === 'live') return 'Live evidence';
  return 'Replay evidence';
}
