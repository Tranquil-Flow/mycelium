import {
  createHash,
  createPrivateKey,
  createPublicKey,
  sign as signBytes,
} from 'node:crypto';
import { constants as fsConstants } from 'node:fs';
import { open } from 'node:fs/promises';

const PKCS8_ED25519_SEED_PREFIX = Buffer.from('302e020100300506032b657004220420', 'hex');
const SIGNATURE_PROTOCOL = 'mycelium.ed25519_evidence_signature.v1';
const BROWSER_CASES = new Set([
  'direct_path_qualified_browser_inference',
  'forced_relay_privacy_reduced_browser_inference',
  'observed_path_transition_and_reconnect',
]);
const AUTHORITY_FIELDS = new Set([
  'protocol', 'signer_id', 'verification_keys', 'challenge_id', 'case_id',
  'origin', 'deployment_id', 'spec_digest', 'source_digest', 'request_count',
  'issued_at_unix_ms', 'expires_at_unix_ms',
]);

function fail(code) {
  throw new Error(code);
}

function canonicalJson(value) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    return JSON.stringify(value);
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) {
      fail('browser_observation_not_canonical');
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  }
  if (typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
    ).join(',')}}`;
  }
  fail('browser_observation_not_canonical');
}

export function canonicalJsonBytes(value) {
  return Buffer.from(canonicalJson(value), 'utf8');
}

export function canonicalTransportReportDigest(rawReport) {
  if (typeof rawReport !== 'string') fail('transport_report_not_canonical');
  const canonical = rawReport.trimEnd();
  if (!canonical || JSON.stringify(JSON.parse(canonical)) === undefined) {
    fail('transport_report_not_canonical');
  }
  return `sha256:${createHash('sha256').update(canonical, 'utf8').digest('hex')}`;
}

function browserSignatureValue(value) {
  if (value === null) return ['null'];
  if (typeof value === 'boolean') return ['boolean', value];
  if (typeof value === 'string') return ['string', value];
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('browser_observation_not_canonical');
    if (Number.isSafeInteger(value)) return ['integer', String(value)];
    if (Number.isInteger(value)) fail('browser_observation_not_canonical');
    const encoded = Buffer.allocUnsafe(8);
    encoded.writeDoubleBE(value);
    return ['float64', encoded.toString('hex')];
  }
  if (Array.isArray(value)) {
    return ['array', value.map((item) => browserSignatureValue(item))];
  }
  if (typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    return [
      'object',
      Object.keys(value).sort().map((key) => [key, browserSignatureValue(value[key])]),
    ];
  }
  fail('browser_observation_not_canonical');
}

export function browserStatementBytes(value) {
  return canonicalJsonBytes({
    protocol: 'mycelium.a8_browser_signature_statement.v1',
    value: browserSignatureValue(value),
  });
}

function sha256(bytes) {
  return `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
}

function sameFileMetadata(before, after) {
  return before.dev === after.dev
    && before.ino === after.ino
    && before.mode === after.mode
    && before.uid === after.uid
    && before.nlink === after.nlink
    && before.size === after.size
    && before.mtimeNs === after.mtimeNs
    && before.ctimeNs === after.ctimeNs;
}

export async function loadBrowserEvidenceSigner(keyPath, signerId = 'a8-browser-collector') {
  if (typeof keyPath !== 'string' || !keyPath || typeof signerId !== 'string' || !signerId) {
    fail('browser_evidence_signer_invalid');
  }
  const flags = fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW;
  let handle;
  try {
    handle = await open(keyPath, flags);
    const metadata = await handle.stat({ bigint: true });
    const expectedUid = typeof process.geteuid === 'function'
      ? BigInt(process.geteuid())
      : metadata.uid;
    if (
      !metadata.isFile()
      || metadata.uid !== expectedUid
      || (metadata.mode & 0o777n) !== 0o600n
      || metadata.nlink !== 1n
      || metadata.size !== 32n
    ) {
      fail('browser_evidence_signer_invalid');
    }
    const seed = await handle.readFile();
    const metadataAfter = await handle.stat({ bigint: true });
    if (seed.length !== 32 || !sameFileMetadata(metadata, metadataAfter)) {
      fail('browser_evidence_signer_invalid');
    }
    const privateKey = createPrivateKey({
      key: Buffer.concat([PKCS8_ED25519_SEED_PREFIX, seed]),
      format: 'der',
      type: 'pkcs8',
    });
    const publicDer = createPublicKey(privateKey).export({ format: 'der', type: 'spki' });
    const publicKey = Buffer.from(publicDer).subarray(-32);
    const verificationKeyDigest = sha256(publicKey);
    return Object.freeze({
      signerId,
      privateKey,
      verificationKeyDigest,
      publicKeyRecord: Object.freeze({
        algorithm: 'ed25519',
        encoding: 'base64',
        verification_key: publicKey.toString('base64'),
        verification_key_digest: verificationKeyDigest,
      }),
    });
  } catch (error) {
    if (error instanceof Error && error.message === 'browser_evidence_signer_invalid') throw error;
    fail('browser_evidence_signer_invalid');
  } finally {
    await handle?.close();
  }
}

function validateBrowserEvidenceAuthority(authority, signer, nowUnixMs = Date.now()) {
  if (
    typeof authority !== 'object'
    || authority === null
    || Object.getPrototypeOf(authority) !== Object.prototype
    || Object.keys(authority).length !== AUTHORITY_FIELDS.size
    || Object.keys(authority).some((field) => !AUTHORITY_FIELDS.has(field))
    || authority.protocol !== 'mycelium.a8_browser_observation_authority.v2'
    || authority.signer_id !== signer.signerId
    || canonicalJson(authority.verification_keys) !== canonicalJson([signer.publicKeyRecord])
    || !/^sha256:[0-9a-f]{64}$/.test(authority.challenge_id ?? '')
    || authority.challenge_id === `sha256:${'0'.repeat(64)}`
    || !BROWSER_CASES.has(authority.case_id)
    || typeof authority.origin !== 'string'
    || new URL(authority.origin).origin !== authority.origin
    || !authority.origin.startsWith('https://')
    || typeof authority.deployment_id !== 'string'
    || !/^[!-~]{1,256}$/.test(authority.deployment_id)
    || !/^sha256:[0-9a-f]{64}$/.test(authority.spec_digest ?? '')
    || authority.spec_digest === `sha256:${'0'.repeat(64)}`
    || !/^sha256:[0-9a-f]{64}$/.test(authority.source_digest ?? '')
    || authority.source_digest === `sha256:${'0'.repeat(64)}`
    || !Number.isSafeInteger(authority.request_count)
    || authority.request_count < 1
    || authority.request_count > 8
    || (authority.case_id === 'observed_path_transition_and_reconnect'
      ? authority.request_count !== 2
      : authority.request_count !== 1)
    || !Number.isSafeInteger(authority.issued_at_unix_ms)
    || !Number.isSafeInteger(authority.expires_at_unix_ms)
    || authority.expires_at_unix_ms <= authority.issued_at_unix_ms
    || authority.expires_at_unix_ms - authority.issued_at_unix_ms > 300_000
    || authority.issued_at_unix_ms > nowUnixMs + 30_000
    || authority.expires_at_unix_ms < nowUnixMs
  ) {
    fail('browser_authority_invalid');
  }
  return Object.freeze(structuredClone(authority));
}

export function browserEvidenceAuthority(signer, binding) {
  return validateBrowserEvidenceAuthority({
    protocol: 'mycelium.a8_browser_observation_authority.v2',
    signer_id: signer.signerId,
    verification_keys: [signer.publicKeyRecord],
    ...binding,
  }, signer);
}

export async function loadBrowserEvidenceAuthority(authorityPath, signer) {
  if (typeof authorityPath !== 'string' || !authorityPath) fail('browser_authority_invalid');
  const flags = fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW;
  let handle;
  try {
    handle = await open(authorityPath, flags);
    const metadata = await handle.stat({ bigint: true });
    const expectedUid = typeof process.geteuid === 'function'
      ? BigInt(process.geteuid())
      : metadata.uid;
    if (
      !metadata.isFile()
      || metadata.uid !== expectedUid
      || (metadata.mode & 0o777n) !== 0o600n
      || metadata.nlink !== 1n
      || metadata.size < 1n
      || metadata.size > 1_048_576n
    ) {
      fail('browser_authority_invalid');
    }
    const raw = await handle.readFile();
    const metadataAfter = await handle.stat({ bigint: true });
    if (!sameFileMetadata(metadata, metadataAfter)) fail('browser_authority_invalid');
    return validateBrowserEvidenceAuthority(JSON.parse(raw.toString('utf8')), signer);
  } catch (error) {
    if (error instanceof Error && error.message === 'browser_authority_invalid') throw error;
    fail('browser_authority_invalid');
  } finally {
    await handle?.close();
  }
}

export function signBrowserObservation(observation, signer) {
  const statementBytes = browserStatementBytes(observation);
  const signedStatementDigest = sha256(statementBytes);
  const signaturePayload = canonicalJsonBytes({
    algorithm: 'ed25519',
    protocol: SIGNATURE_PROTOCOL,
    signed_statement_digest: signedStatementDigest,
    signer_endpoint_id: signer.signerId,
    verification_key_digest: signer.verificationKeyDigest,
  });
  return {
    protocol: 'mycelium.a8_product_browser_observation_envelope.v2',
    observation,
    signature: {
      algorithm: 'ed25519',
      signer_endpoint_id: signer.signerId,
      verification_key_digest: signer.verificationKeyDigest,
      signed_statement_digest: signedStatementDigest,
      signature: signBytes(null, signaturePayload, signer.privateKey).toString('base64'),
    },
  };
}
