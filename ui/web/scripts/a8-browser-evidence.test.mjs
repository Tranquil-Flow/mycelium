import assert from 'node:assert/strict';
import { createHash, createPublicKey, randomBytes, verify } from 'node:crypto';
import { mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
  browserEvidenceAuthority,
  browserStatementBytes,
  canonicalJsonBytes,
  loadBrowserEvidenceAuthority,
  loadBrowserEvidenceSigner,
  signBrowserObservation,
} from './a8-browser-evidence.mjs';

const SPKI_ED25519_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');

function signaturePayload(envelope) {
  const signature = envelope.signature;
  return canonicalJsonBytes({
    algorithm: 'ed25519',
    protocol: 'mycelium.ed25519_evidence_signature.v1',
    signed_statement_digest: signature.signed_statement_digest,
    signer_endpoint_id: signature.signer_endpoint_id,
    verification_key_digest: signature.verification_key_digest,
  });
}

test('signs canonical browser observations with an owner-private key', async () => {
  const root = await mkdtemp(join(tmpdir(), 'a8-browser-evidence-'));
  try {
    const keyPath = join(root, 'browser.key');
    await writeFile(keyPath, randomBytes(32), { mode: 0o600 });
    const signer = await loadBrowserEvidenceSigner(keyPath);
    const now = Date.now();
    const binding = {
      challenge_id: `sha256:${'2'.repeat(64)}`,
      case_id: 'direct_path_qualified_browser_inference',
      origin: 'https://a8.example.test',
      deployment_id: 'deployment-a8',
      spec_digest: `sha256:${'3'.repeat(64)}`,
      source_digest: `sha256:${'4'.repeat(64)}`,
      request_count: 1,
      issued_at_unix_ms: now,
      expires_at_unix_ms: now + 300_000,
    };
    const observation = {
      protocol: 'mycelium.a8_product_browser_observation.v2',
      origin: 'https://a8.example.test',
      values: [true, 0, 1e-7, 'moon'],
    };
    const envelope = signBrowserObservation(observation, signer);
    const authority = browserEvidenceAuthority(signer, binding);
    const authorityPath = join(root, 'browser-authority.json');
    await writeFile(authorityPath, JSON.stringify(authority), { mode: 0o600 });
    assert.deepEqual(await loadBrowserEvidenceAuthority(authorityPath, signer), authority);
    assert.equal(envelope.protocol, 'mycelium.a8_product_browser_observation_envelope.v2');
    const rawPublicKey = Buffer.from(
      authority.verification_keys[0].verification_key,
      'base64',
    );
    const publicKey = createPublicKey({
      key: Buffer.concat([SPKI_ED25519_PREFIX, rawPublicKey]),
      format: 'der',
      type: 'spki',
    });
    assert.equal(
      verify(
        null,
        signaturePayload(envelope),
        publicKey,
        Buffer.from(envelope.signature.signature, 'base64'),
      ),
      true,
    );
    envelope.observation.origin = 'https://attacker.invalid';
    const tamperedDigest = `sha256:${createHash('sha256')
      .update(browserStatementBytes(envelope.observation))
      .digest('hex')}`;
    assert.notEqual(envelope.signature.signed_statement_digest, tamperedDigest);
    assert.throws(
      () => signBrowserObservation({ value: 9_007_199_254_740_992 }, signer),
      /browser_observation_not_canonical/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test('rejects loose-mode and symlink signing keys', async () => {
  const root = await mkdtemp(join(tmpdir(), 'a8-browser-evidence-'));
  try {
    const keyPath = join(root, 'browser.key');
    await writeFile(keyPath, randomBytes(32), { mode: 0o644 });
    await assert.rejects(
      loadBrowserEvidenceSigner(keyPath),
      /browser_evidence_signer_invalid/,
    );
    await writeFile(keyPath, randomBytes(32), { mode: 0o600 });
    const linkPath = join(root, 'browser-link.key');
    await symlink(keyPath, linkPath);
    await assert.rejects(
      loadBrowserEvidenceSigner(linkPath),
      /browser_evidence_signer_invalid/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
