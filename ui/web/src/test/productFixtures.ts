import {
  MAX_NEW_TOKENS,
  MAX_PROMPT_UTF8_BYTES,
  PRODUCT_API_PATHS,
  PRODUCT_BOOTSTRAP_PROTOCOL,
  PRODUCT_INFERENCE_PROTOCOL,
  PRODUCT_OBSERVATORY_PROTOCOL,
  PRODUCT_QUALIFIER_AUTHORITY,
  PRODUCT_SWARM_PROTOCOL,
  type ProductBootstrap,
  type ProductObservatoryEnvelope,
  type ProductQualification,
  type ProductSwarmStatus,
} from '../app/contracts';

const DEFAULT_DIGEST = `sha256:${'b'.repeat(64)}`;
const DEFAULT_NOW_UNIX_MS = 1_800_000_000_000;
const MAX_FIXTURE_NODES = 10_000;

export class ProductFixturePrivacyError extends TypeError {
  constructor(message: string) {
    super(message);
    this.name = 'ProductFixturePrivacyError';
  }
}

const forbiddenNormalizedFixtureKey = /^(?:prompt|completion|generatedtext|tokens?|tokenids?|inputids?|outputids?|activations?|hiddenstates?|logits?|weights?|statedict|kvcache|apikey|authorization|proxyauthorization|accesstoken|refreshtoken|clientsecret|password|passphrase|credentials?|secrets?|privatekey|authtoken|bearertoken|endpoint|endpointurl|upstreamurl|baseurl|socketaddress|cookie|sessioncookie|sessiontoken|csrftoken)$/;

function normalizedFixtureKey(key: string): string {
  return key.replace(/[^A-Za-z0-9]/g, '').toLowerCase();
}

export function assertProductFixturePrivacy(value: unknown): void {
  const visited = new WeakSet<object>();
  let visitedNodes = 0;

  const visit = (candidate: unknown, path: string, depth: number): void => {
    if (depth > 32) throw new ProductFixturePrivacyError(`${path}: fixture nesting exceeds limit`);
    if (candidate === null || typeof candidate !== 'object') return;
    visitedNodes += 1;
    if (visitedNodes > MAX_FIXTURE_NODES) {
      throw new ProductFixturePrivacyError(`${path}: fixture size exceeds limit`);
    }
    if (visited.has(candidate)) {
      throw new ProductFixturePrivacyError(`${path}: cyclic fixture object`);
    }
    visited.add(candidate);
    if (Array.isArray(candidate)) {
      if (Object.getPrototypeOf(candidate) !== Array.prototype) {
        throw new ProductFixturePrivacyError(`${path}: fixture array prototype prohibited`);
      }
      if (candidate.length > 4_096) {
        throw new ProductFixturePrivacyError(`${path}: fixture array exceeds limit`);
      }
      const ownKeys = Reflect.ownKeys(candidate);
      if (ownKeys.some((key) => typeof key === 'symbol')) {
        throw new ProductFixturePrivacyError(`${path}: symbol fixture keys prohibited`);
      }
      const keys = ownKeys.filter((key): key is string => key !== 'length');
      if (keys.length !== candidate.length) {
        throw new ProductFixturePrivacyError(`${path}: sparse or extended fixture array prohibited`);
      }
      for (let index = 0; index < candidate.length; index += 1) {
        if (keys[index] !== String(index)) {
          throw new ProductFixturePrivacyError(`${path}: sparse or extended fixture array prohibited`);
        }
        const descriptor = Object.getOwnPropertyDescriptor(candidate, String(index));
        if (descriptor === undefined || 'get' in descriptor || 'set' in descriptor) {
          throw new ProductFixturePrivacyError(`${path}[${index}]: fixture accessors prohibited`);
        }
        visit(descriptor.value, `${path}[${index}]`, depth + 1);
      }
      return;
    }
    const prototype = Object.getPrototypeOf(candidate);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new ProductFixturePrivacyError(`${path}: fixture must contain plain objects`);
    }
    for (const rawKey of Reflect.ownKeys(candidate)) {
      if (typeof rawKey === 'symbol') {
        throw new ProductFixturePrivacyError(`${path}: symbol fixture keys prohibited`);
      }
      const key = rawKey;
      const childPath = `${path}.${key}`;
      const descriptor = Object.getOwnPropertyDescriptor(candidate, key);
      if (
        descriptor === undefined ||
        descriptor.enumerable !== true ||
        'get' in descriptor ||
        'set' in descriptor
      ) {
        throw new ProductFixturePrivacyError(`${childPath}: hidden or accessor fixture field prohibited`);
      }
      const item = descriptor.value;
      const normalizedKey = normalizedFixtureKey(key);
      const intentionalCsrfFixture = childPath === 'fixture.session.csrf_token';
      if (forbiddenNormalizedFixtureKey.test(normalizedKey) && !intentionalCsrfFixture) {
        throw new ProductFixturePrivacyError(`${childPath}: private payload field prohibited`);
      }
      if (/^(?:privateaddress|ipaddress|host|hostname)$/.test(normalizedKey) && item !== null) {
        throw new ProductFixturePrivacyError(`${childPath}: private network identity prohibited`);
      }
      visit(item, childPath, depth + 1);
    }
  };

  visit(value, 'fixture', 0);
}

function qualificationBinding() {
  return {
    qualification_id: 'fixture-qualification-1',
    qualification_digest: DEFAULT_DIGEST,
    deployment_id: 'fixture-deployment',
    deployment_epoch: 1,
    topology_version: 1,
    model_id: 'fixture-model',
    resolved_commit: 'fixture-commit',
    manifest_digest: DEFAULT_DIGEST,
    path_manifest_digest: DEFAULT_DIGEST,
    stage_load_proof_digests: [DEFAULT_DIGEST],
  } as const;
}

export function makeProductBootstrapFixture(): ProductBootstrap {
  const fixture: ProductBootstrap = {
    protocol: PRODUCT_BOOTSTRAP_PROTOCOL,
    source_mode: 'fixture',
    session: {
      csrf_header: 'X-Mycelium-CSRF',
      csrf_token: 'fixture-csrf-token-not-a-secret',
      expires_at_unix_ms: DEFAULT_NOW_UNIX_MS + 60_000,
    },
    api: PRODUCT_API_PATHS,
    limits: {
      max_prompt_utf8_bytes: MAX_PROMPT_UTF8_BYTES,
      max_new_tokens: MAX_NEW_TOKENS,
    },
    qualification_authority: PRODUCT_QUALIFIER_AUTHORITY,
  };
  assertProductFixturePrivacy(fixture);
  return fixture;
}

export function makeProductObservatoryFixture(): ProductObservatoryEnvelope {
  const fixture: ProductObservatoryEnvelope = {
    protocol: PRODUCT_OBSERVATORY_PROTOCOL,
    generation: 1,
    status: 'connected',
    source: {
      mode: 'fixture',
      freshness: 'fixture',
      observed_at_unix_ms: null,
      replay_of_generation: null,
    },
    metrics: {
      native_node_count: null,
      browser_worker_count: null,
      incident_count: null,
    },
  };
  assertProductFixturePrivacy(fixture);
  return fixture;
}

export function makeProductQualificationFixture(
  options: { readonly issued_at_unix_ms?: number } = {},
): ProductQualification {
  const fixture: ProductQualification = {
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    issued_at_unix_ms: options.issued_at_unix_ms ?? DEFAULT_NOW_UNIX_MS,
    evidence_class: 'synthetic_test_fixture',
    route_ready: false,
    reason_codes: ['physical_qualification_missing'],
    binding: qualificationBinding(),
  };
  assertProductFixturePrivacy(fixture);
  return fixture;
}

export function makeAcceptedQualificationContractFixture(
  options: { readonly issued_at_unix_ms?: number } = {},
): ProductQualification {
  const fixture: ProductQualification = {
    protocol: PRODUCT_INFERENCE_PROTOCOL,
    issued_at_unix_ms: options.issued_at_unix_ms ?? DEFAULT_NOW_UNIX_MS,
    evidence_class: 'physical_qualification',
    route_ready: true,
    reason_codes: [],
    binding: qualificationBinding(),
  };
  assertProductFixturePrivacy(fixture);
  return fixture;
}

export function makeProductSwarmFixture(): ProductSwarmStatus {
  const fixture: ProductSwarmStatus = {
    protocol: PRODUCT_SWARM_PROTOCOL,
    native_nodes: [
      {
        member_id: 'fixture-native-node-1',
        capability: 'native_inference_node',
        membership_state: 'reachable',
        connectivity: 'local',
        endpoint_id: 'fixture-endpoint-1',
      },
    ],
    browser_workers: [
      {
        peer_id: 'fixture-browser-worker-1',
        capability: 'synthetic_browser_probe',
        state: 'ready',
        expires_at_unix_ms: DEFAULT_NOW_UNIX_MS + 60_000,
      },
    ],
  };
  assertProductFixturePrivacy(fixture);
  return fixture;
}
