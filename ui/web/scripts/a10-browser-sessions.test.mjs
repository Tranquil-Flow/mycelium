// SPDX-License-Identifier: AGPL-3.0-only
// Unit-level checks for the A10 gate-1 two-session browser harness. No browser
// engine, origin or network is contacted here; the end-to-end run against a
// loopback origin lives in tests/a10_browser (pytest drives node).
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  ENGINES,
  PROTOCOL,
  WORKLOAD_PROTOCOL,
  assertConcurrentOverlap,
  assertFrozenSubmission,
  assertLateArrival,
  assertLiveStatus,
  assertSessionPrivacy,
  canonicalJson,
  harnessPlan,
  requestRef,
  sha256Hex,
  validateConfig,
  validateWorkload,
} from './a10-browser-sessions.mjs';

const scriptPath = fileURLToPath(new URL('./a10-browser-sessions.mjs', import.meta.url));

function submission(session, index, maxNewTokens) {
  return {
    session,
    index,
    prompt: `A10 benchmark request ${index}:${' benchmark'.repeat(16)}`,
    max_new_tokens: maxNewTokens,
    qos_class: 'interactive',
    workload_profile_id: 'interactive_chat_v1',
  };
}

function workload() {
  const body = {
    protocol: WORKLOAD_PROTOCOL,
    sessions: ['a', 'b'],
    rounds: [
      { round: 'concurrent', submissions: [submission('a', 0, 32), submission('b', 2, 32)] },
      { round: 'late_arrival', submissions: [submission('a', 4, 32), submission('b', 6, 8)] },
    ],
  };
  return { ...body, workload_digest: `sha256:${sha256Hex(canonicalJson(body))}` };
}

function writeWorkload(document) {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'a10-browser-test-'));
  const file = path.join(directory, 'workload.json');
  writeFileSync(file, JSON.stringify(document));
  return { directory, file };
}

test('plan mode is contact-free and makes no claim', () => {
  const result = spawnSync(process.execPath, [scriptPath, '--plan'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const plan = JSON.parse(result.stdout);
  assert.deepEqual(plan, harnessPlan());
  assert.equal(plan.protocol, PROTOCOL);
  assert.equal(plan.browser_contact_attempted, false);
  assert.equal(plan.origin_contact_attempted, false);
  assert.equal(plan.live_evidence, false);
  assert.equal(plan.qualification_claim, false);
  assert.equal(plan.independent_sessions, 2);
  assert.deepEqual(plan.engines, [...ENGINES]);
});

test('canonical JSON matches the Python gate digest rule (sorted keys, no spaces)', () => {
  assert.equal(canonicalJson({ b: 1, a: [true, null, 'x'], c: { z: 1, y: 2 } }),
    '{"a":[true,null,"x"],"b":1,"c":{"y":2,"z":1}}');
  assert.equal(requestRef('request-1'),
    `sha256:${sha256Hex('mycelium.a10.request:request-1')}`);
});

test('the harness refuses before any contact without engine, loopback origin, evidence or workload', () => {
  const noEngine = spawnSync(process.execPath, [scriptPath], { encoding: 'utf8' });
  assert.notEqual(noEngine.status, 0);
  assert.match(noEngine.stderr, /browser_engine_selection_required/);
  const { directory, file } = writeWorkload(workload());
  const ok = { A10_BROWSER_ORIGIN: 'http://127.0.0.1:8795', A10_BROWSER_EVIDENCE: path.join(directory, 'ev'),
    A10_BROWSER_WORKLOAD: file };
  assert.equal(validateConfig(ok, 'chromium').mode, 'live');
  assert.throws(() => validateConfig(ok, 'opera'), /browser_engine_selection_required/);
  for (const origin of ['https://mycelium.now', 'http://localhost:8795', 'http://127.0.0.1:8795/',
    'http://127.0.0.1:0', 'http://127.0.0.1:99999', 'http://user@127.0.0.1:8795', undefined]) {
    assert.throws(() => validateConfig({ ...ok, A10_BROWSER_ORIGIN: origin }, 'chromium'),
      /loopback_product_origin_required/);
  }
  assert.throws(() => validateConfig({ ...ok, A10_BROWSER_EVIDENCE: 'relative' }, 'chromium'),
    /evidence_directory_required/);
  assert.throws(() => validateConfig({ ...ok, A10_BROWSER_EVIDENCE: directory }, 'chromium'),
    /evidence_directory_exists/);
  assert.throws(() => validateConfig({ ...ok, A10_BROWSER_WORKLOAD: undefined }, 'chromium'),
    /frozen_workload_required/);
  assert.throws(() => validateConfig({ ...ok, A10_BROWSER_MODE: 'fixture' }, 'chromium'),
    /browser_mode_invalid/);
  assert.equal(validateConfig({ ...ok, A10_BROWSER_MODE: 'local_conformance' }, 'webkit').mode,
    'local_conformance');
});

test('only the exact frozen two-session workload is accepted', () => {
  const good = workload();
  assert.equal(validateWorkload(good).workload_digest, good.workload_digest);
  assert.throws(() => validateWorkload({ ...good, workload_digest: `sha256:${'0'.repeat(64)}` }),
    /frozen_workload_digest_mismatch/);
  assert.throws(() => validateWorkload({ ...good, extra: 1 }), /frozen_workload_shape_invalid/);
  const oneSession = structuredClone(good);
  oneSession.rounds[0].submissions[1].session = 'a';
  assert.throws(() => validateWorkload(oneSession), /frozen_workload_shape_invalid/);
  const noLate = structuredClone(good);
  noLate.rounds = [noLate.rounds[0], noLate.rounds[0]];
  assert.throws(() => validateWorkload(noLate), /frozen_workload_shape_invalid/);
  const batchQos = structuredClone(good);
  batchQos.rounds[0].submissions[0].qos_class = 'batch';
  assert.throws(() => validateWorkload(batchQos), /frozen_workload_shape_invalid/);
});

test('a browser submission must be exactly the frozen request', () => {
  const frozen = submission('a', 0, 32);
  const body = { protocol: 'mycelium.request_gateway.v2', prompt: frozen.prompt, max_new_tokens: 32,
    qos_class: 'interactive', workload_profile_id: 'interactive_chat_v1', qualification: {} };
  assert.equal(assertFrozenSubmission(body, frozen), true);
  assert.throws(() => assertFrozenSubmission({ ...body, prompt: `${frozen.prompt} ` }, frozen),
    /browser_submission_not_frozen_workload/);
  assert.throws(() => assertFrozenSubmission({ ...body, max_new_tokens: 64 }, frozen),
    /browser_submission_not_frozen_workload/);
  assert.throws(() => assertFrozenSubmission({ ...body, qos_class: 'batch' }, frozen),
    /browser_submission_not_frozen_workload/);
  assert.throws(() => assertFrozenSubmission(null, frozen), /browser_submission_not_frozen_workload/);
});

test('live status must be a live physical route; local conformance must say it is simulated', () => {
  const live = { route_alive: true, simulated: false, deployment_id: 'dep-1', peers: [] };
  assert.equal(assertLiveStatus(live, 'live').deployment_id, 'dep-1');
  assert.throws(() => assertLiveStatus({ ...live, simulated: true }, 'live'), /physical_route_required/);
  assert.throws(() => assertLiveStatus({ ...live, route_alive: false }, 'live'), /route_not_alive/);
  assert.throws(() => assertLiveStatus({ ...live, deployment_id: '' }, 'live'), /deployment_binding_missing/);
  assert.throws(() => assertLiveStatus(live, 'local_conformance'), /local_conformance_must_be_simulated/);
  assert.equal(assertLiveStatus({ ...live, simulated: true }, 'local_conformance').simulated, true);
});

test('concurrency needs both requests active together; a late arrival must land mid-decode', () => {
  assert.equal(assertConcurrentOverlap({ a: 'Streaming', b: 'Queued' }), true);
  assert.throws(() => assertConcurrentOverlap({ a: 'Completed', b: 'Streaming' }),
    /concurrent_overlap_not_observed/);
  assert.equal(assertLateArrival({ leaderTokensBefore: 1, leaderPhaseAfterAccept: 'Streaming' }), true);
  assert.throws(() => assertLateArrival({ leaderTokensBefore: 0, leaderPhaseAfterAccept: 'Streaming' }),
    /late_arrival_not_observed/);
  assert.throws(() => assertLateArrival({ leaderTokensBefore: 3, leaderPhaseAfterAccept: 'Completed' }),
    /late_arrival_not_observed/);
});

test('a session must not see the other session prompts or decoded output', () => {
  const own = { prompts: ['A10 benchmark request 0:'], outputs: ['my own decoded words'] };
  const other = { prompts: ['A10 benchmark request 2:'], outputs: ['decoded words from b'] };
  assert.equal(assertSessionPrivacy('A10 benchmark request 0: benchmark · Completed', own, other), true);
  assert.throws(() => assertSessionPrivacy('… A10 benchmark request 2: …', own, other),
    /cross_session_prompt_visible/);
  assert.throws(() => assertSessionPrivacy('… decoded words from b …', own, other),
    /cross_session_output_visible/);
  // Output shorter than 8 characters is too weak to attribute and is not used.
  assert.equal(assertSessionPrivacy('ok', own, { prompts: [], outputs: ['ok'] }), true);
  // A model may decode the same text for both frozen prompts; text this session
  // decoded itself is not attributable to the other session.
  const same = { prompts: [], outputs: ['my own decoded words'] };
  assert.equal(assertSessionPrivacy('… my own decoded words …', own, same), true);
  assert.throws(() => assertSessionPrivacy('… my own decoded words …', { prompts: [], outputs: [] }, same),
    /cross_session_output_visible/);
});
