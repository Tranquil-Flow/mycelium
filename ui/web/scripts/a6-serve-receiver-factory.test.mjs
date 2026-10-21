// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { candidateBindingFromManifest, createServeReceiverFactory } from './a6-serve-receiver-factory.mjs';
import { sourceBinding } from './a6-product-browser-matrix.mjs';

const script = fileURLToPath(new URL('./a6-serve-receiver-factory.mjs', import.meta.url));
const root = fileURLToPath(new URL('../../../', import.meta.url));
const lifetime = 'c'.repeat(64);
const cases = ['ordinary_browser_source_failure_recovery', 'no_successor'];

// A stand-in serve process: prints the live startup line, then on SIGTERM the
// receiver teardown witness, shaped by FAKE_* variables.
const fake = path.join(mkdtempSync(path.join(tmpdir(), 'a6-fake-serve-')), 'serve.mjs');
writeFileSync(fake, `
const env = process.env;
const started = {protocol:'mycelium.live_server_started.v1', url:env.FAKE_URL ?? 'http://127.0.0.1:18787/',
  route_ready: env.FAKE_READY !== 'false', simulated: env.FAKE_SIMULATED === 'true',
  a6_fault_control: {protocol:'mycelium.a6_product_fault_control_status.v1',
    mounted: env.FAKE_MOUNTED !== 'false', url: env.FAKE_CONTROL ?? 'http://127.0.0.1:18788/a6/controlled-fault',
    lifetime_id: env.FAKE_LIFETIME ?? '${lifetime}', seam_ids: [], executed_case_ids: []}};
console.log('serve diagnostics that are not a protocol line');
process.on('SIGTERM', () => {
  if (env.FAKE_NO_TEARDOWN !== 'true') console.log(JSON.stringify({
    protocol:'mycelium.live_server_a6_fault_control_stopped.v1',
    lifetime_id: env.FAKE_TEARDOWN_LIFETIME ?? '${lifetime}',
    executed_case_ids: JSON.parse(env.FAKE_EXECUTED ?? '${JSON.stringify(cases).replace(/"/g, '\\"')}'),
    torn_down: env.FAKE_TORN_DOWN !== 'false'}));
  process.exit(Number(env.FAKE_EXIT ?? '0'));
});
// Like the real serve, the stop handler is installed before startup is reported.
if (env.FAKE_SILENT !== 'true') console.log(JSON.stringify(started));
setInterval(() => {}, 1000);
`);

function factory(env = {}, options = {}) {
  return createServeReceiverFactory({
    command: process.execPath, args: [fake], cwd: root,
    env: { ...process.env, ...env }, readyTimeoutMs: 5000, stopTimeoutMs: 5000, ...options,
  });
}

test('starts one serve per engine and returns a receiver-derived teardown witness', async () => {
  const f = factory();
  const runtime = await f.start('chromium');
  assert.equal(runtime.lifetime_id, lifetime);
  assert.equal(runtime.origin, 'http://127.0.0.1:18787');
  assert.equal(runtime.positiveUrl, 'http://127.0.0.1:18788/a6/controlled-fault');
  assert.equal(runtime.negativeUrl, runtime.positiveUrl);
  assert.equal(runtime.maximumNewTokens, 64);
  const witness = await runtime.close();
  assert.deepEqual(witness, { lifetime_id: lifetime, source_binding: sourceBinding(),
    case_ids: cases, statuses: [200, 200], torn_down: true });
  const again = await f.start('firefox');
  assert.equal(again.lifetime_id, lifetime);
  await again.close();
});

test('refuses a startup that is not a ready, real, mounted loopback receiver', async () => {
  for (const env of [
    { FAKE_READY: 'false' }, { FAKE_SIMULATED: 'true' }, { FAKE_MOUNTED: 'false' },
    { FAKE_URL: 'http://0.0.0.0:18787/' }, { FAKE_URL: 'https://mycelium.now/' },
    { FAKE_CONTROL: 'http://10.0.0.1:18788/a6/controlled-fault' },
    { FAKE_LIFETIME: 'not-a-lifetime' },
  ]) {
    await assert.rejects(factory(env).start('chromium'), /serve_receiver_startup_invalid/, JSON.stringify(env));
  }
});

test('refuses a serve that never reports startup within the bound', async () => {
  await assert.rejects(factory({ FAKE_SILENT: 'true' }, { readyTimeoutMs: 300 }).start('webkit'),
    /serve_receiver_startup_timeout/);
});

test('teardown refuses a foreign, partial, missing or unclean witness', async () => {
  for (const env of [
    { FAKE_TEARDOWN_LIFETIME: 'd'.repeat(64) }, { FAKE_TORN_DOWN: 'false' },
    { FAKE_NO_TEARDOWN: 'true' }, { FAKE_EXIT: '3' },
  ]) {
    const runtime = await factory(env).start('chromium');
    await assert.rejects(runtime.close(), /serve_receiver_teardown_invalid/, JSON.stringify(env));
  }
});

test('reports only the cases the receiver itself completed', async () => {
  const runtime = await factory({ FAKE_EXECUTED: JSON.stringify([cases[0]]) }).start('chromium');
  const witness = await runtime.close();
  assert.deepEqual(witness.case_ids, [cases[0]]);
  assert.deepEqual(witness.statuses, [200]);
});

test('refuses an invalid engine or token bound before any serve is spawned', async () => {
  await assert.rejects(factory().start('edge'), /browser_engine_selection_required/);
  assert.throws(() => factory({}, { maximumNewTokens: 4 }), /browser_token_bound_invalid/);
  assert.throws(() => createServeReceiverFactory({ command: '', args: [] }), /serve_command_required/);
});

test('the candidate binding is the digest of the pinned A6 source manifest bytes', () => {
  const bytes = readFileSync(path.join(root, 'artifacts/a6/source-manifest-v1.json'));
  assert.equal(candidateBindingFromManifest(root),
    'sha256:' + createHash('sha256').update(bytes).digest('hex'));
});

test('the live CLI refuses without the grant, evidence path or serve command', () => {
  const run = (args, env) => spawnSync(process.execPath, [script, ...args],
    { encoding: 'utf8', env: { ...process.env, MYCELIUM_A6_FLEET_GRANT: '', ...env } });
  let p = run(['--evidence=/tmp/x.json', '--', process.execPath, fake], {});
  assert.equal(p.status, 1); assert.match(p.stderr, /explicit_fleet_grant_required/);
  const granted = { MYCELIUM_A6_FLEET_GRANT: 'FLEET GRANTED TO A6' };
  p = run(['--evidence=relative.json', '--', process.execPath, fake], granted);
  assert.equal(p.status, 1); assert.match(p.stderr, /browser_evidence_path_required/);
  p = run(['--evidence=/tmp/x.json'], granted);
  assert.equal(p.status, 1); assert.match(p.stderr, /serve_command_required/);
  assert.doesNotMatch(p.stderr, /FLEET GRANTED/);
});
