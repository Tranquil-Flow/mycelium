import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import test from 'node:test';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  assertPrivacyClean,
  atomicPrivateWrite,
  exerciseReconnect,
  expectedWorkspaceFields,
  validateRecoveryHistory,
  verifyInferenceOutcome,
} from './a6-product-browser-e2e.mjs';

const scriptPath = fileURLToPath(new URL('./a6-product-browser-e2e.mjs', import.meta.url));
const packagePath = fileURLToPath(new URL('../package.json', import.meta.url));

function run(args, env = {}) {
  return spawnSync(process.execPath, [scriptPath, ...args], {
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
}

test('plan mode is network-free and declares the complete future live qualification', () => {
  const result = run(['--plan']);
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  assert.equal(report.protocol, 'mycelium.a6_product_browser_plan.v1');
  assert.equal(report.mode, 'plan');
  assert.equal(report.live_evidence, false);
  assert.equal(report.fixture_or_unit_evidence, false);
  assert.equal(report.serve_contact_attempted, false);
  assert.equal(report.fleet_execution_attempted, false);
  assert.deepEqual(report.engines, ['chromium', 'firefox', 'webkit']);
  assert.deepEqual(report.workspaces, [
    'lab', 'network', 'plans', 'incidents',
    'readiness', 'nodes', 'settings', 'inference',
  ]);
  assert.deepEqual(report.required_scenarios, [
    'positive_source_failure_recovery',
    'truthful_no_successor_abort',
    'privacy_canaries',
    'reconnect_before_cutover',
    'reconnect_after_cutover',
    'reload_during_recovery',
    'back_forward',
    'workspace_switching',
    'clean_second_session',
    'no_stale_source',
    'no_duplicate_terminal',
    'console_page_cross_origin_errors',
  ]);
});

test('fixture mode is explicitly local preparation and never live evidence', () => {
  const result = run(['--fixture']);
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout);
  assert.equal(report.protocol, 'mycelium.a6_product_browser_fixture_observation.v1');
  assert.equal(report.mode, 'fixture');
  assert.equal(report.live_evidence, false);
  assert.equal(report.fixture_or_unit_evidence, true);
  assert.equal(report.execution_scope, 'fixture_only');
  assert.equal(report.live_browser_executed, false);
  assert.equal(report.qualification_claim, false);
  assert.equal(report.promotion_authorized, false);
  assert.equal(report.serve_contact_attempted, false);
  assert.match(report.claim_boundary, /not live browser evidence/i);
});

test('live mode fails closed before contacting a serve without the exact fleet grant', () => {
  const result = run([], {
    MYCELIUM_A6_PRODUCT_ORIGIN: 'http://127.0.0.1:9',
    MYCELIUM_A6_BROWSER_EVIDENCE: '/tmp/forbidden-a6-browser-evidence.json',
    MYCELIUM_A6_FLEET_GRANT: 'not granted',
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /explicit_fleet_grant_required/);
  assert.doesNotMatch(result.stderr, /fetch|ECONNREFUSED/i);
});

test('browser outcome proof requires completed UI stream or truthful failed UI', async () => {
  const observed = [];
  const page = {
    getByRole: (role, options = {}) => ({
      waitFor: async () => observed.push([role, options.name ?? null]),
      innerText: async () => role === 'status' ? 'Completed' : 'bounded recovered output',
    }),
  };
  await verifyInferenceOutcome(page, 'positive');
  assert.deepEqual(observed, [
    ['status', null],
    ['log', 'Decoded output'],
  ]);
  const failedPage = {
    getByRole: () => ({
      waitFor: async () => undefined,
      innerText: async () => 'Failed',
    }),
  };
  await verifyInferenceOutcome(failedPage, 'negative');
});

test('live workspace proof requires route-specific bounded recovery fields', () => {
  assert.deepEqual(expectedWorkspaceFields('inference'), ['Request count', 'Recovering count', 'Terminal count']);
  assert.deepEqual(expectedWorkspaceFields('lab'), ['Maximum recovery attempt', 'Cleanup complete count', 'Breaker state']);
  assert.deepEqual(expectedWorkspaceFields('network'), ['Recovery source status', 'Snapshot cursor', 'Recovering count']);
  assert.deepEqual(expectedWorkspaceFields('nodes'), ['Request count', 'Cleanup complete count', 'Reconciled count']);
  assert.deepEqual(expectedWorkspaceFields('plans'), ['Maximum recovery attempt', 'Recovering count', 'Terminal blocked count']);
  assert.deepEqual(expectedWorkspaceFields('readiness'), ['Recovery source status', 'Cleanup complete count', 'Reconciled count']);
  assert.deepEqual(expectedWorkspaceFields('incidents'), ['Terminal count', 'Terminal blocked count', 'Breaker state']);
  assert.deepEqual(expectedWorkspaceFields('settings'), ['Maximum recovery attempt', 'Recovery source status', 'Retained snapshot count']);
});

test('browser evidence writer requires owner-private existing root and writes mode 0600', async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'a6-browser-evidence-'));
  const output = path.join(root, 'browser.json');
  try {
    chmodSync(root, 0o700);
    await atomicPrivateWrite(output, '{}\n');
    assert.equal(statSync(output).mode & 0o777, 0o600);
    chmodSync(root, 0o755);
    await assert.rejects(
      () => atomicPrivateWrite(path.join(root, 'rejected.json'), '{}\n'),
      /browser_evidence_root_mode_invalid/,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('reconnect helper proves a bounded offline-to-online transition', async () => {
  const transitions = [];
  const page = {
    context: () => ({
      setOffline: async (offline) => transitions.push(offline),
    }),
    waitForTimeout: async () => undefined,
  };
  let observed = false;
  await exerciseReconnect(page, async () => {
    observed = true;
  });
  assert.deepEqual(transitions, [true, false]);
  assert.equal(observed, true);
});

test('fixture history validator accepts bounded aggregate recovery progression', () => {
  const observations = validateRecoveryHistory([
    { cursor: 10, request_count: 1, recovering_count: 0, terminal_count: 0, terminal_blocked_count: 0, max_recovery_attempt: 0 },
    { cursor: 11, request_count: 1, recovering_count: 1, terminal_count: 0, terminal_blocked_count: 0, max_recovery_attempt: 1 },
    { cursor: 12, request_count: 1, recovering_count: 0, terminal_count: 1, terminal_blocked_count: 0, max_recovery_attempt: 1 },
  ]);
  assert.equal(observations.source_failure_observed, true);
  assert.equal(observations.recovery_observed, true);
  assert.equal(observations.terminal_count, 1);
  assert.equal(observations.stale_source_after_cutover, false);
});

test('fixture history validator rejects stale aggregate cursors and impossible terminals', () => {
  assert.throws(() => validateRecoveryHistory([
    { cursor: 12, request_count: 1, recovering_count: 1, terminal_count: 0, terminal_blocked_count: 0, max_recovery_attempt: 1 },
    { cursor: 11, request_count: 1, recovering_count: 0, terminal_count: 1, terminal_blocked_count: 0, max_recovery_attempt: 1 },
  ]), /stale_source_after_cutover/);
  assert.throws(() => validateRecoveryHistory([
    { cursor: 12, request_count: 1, recovering_count: 0, terminal_count: 2, terminal_blocked_count: 0, max_recovery_attempt: 1 },
  ]), /duplicate_terminal/);
});

test('browser harness observes only the shared privacy-reduced product snapshot', () => {
  const source = readFileSync(scriptPath, 'utf8');
  assert.doesNotMatch(source, /fetch\(['"]\/api\/v1\/recovery\/status/);
  assert.match(source, /fetch\(['"]\/api\/v1\/product\/snapshot/);
  assert.doesNotMatch(source, /request\.(request_id|path_id)/);
});

test('privacy scanner rejects prompt/output canaries and private recovery material', () => {
  assert.doesNotThrow(() => assertPrivacyClean('Full-context replay recovery · 12 committed tokens'));
  for (const leaked of [
    'A6-BROWSER-PRIVATE-PROMPT-123',
    'A6-BROWSER-PRIVATE-OUTPUT-123',
    'encoded_prompt_token_ids',
    'committed_prefix_tokens',
    'checkpoint_path',
  ]) {
    assert.throws(() => assertPrivacyClean(`public ${leaked}`), /privacy_canary_visible/);
  }
});

test('package scripts separate local harness validation from granted live execution', () => {
  const pkg = JSON.parse(readFileSync(packagePath, 'utf8'));
  assert.equal(pkg.scripts['test:a6-browser-harness'], 'node --test scripts/a6-product-browser-e2e.test.mjs');
  assert.equal(pkg.scripts['test:a6-live-browser'], 'node scripts/a6-product-browser-e2e.mjs');
});


test('browser product has no divergent raw recovery runtime decoder', () => {
  for (const relative of [
    '../src/features/recovery/recoveryProjection.ts',
    '../src/features/recovery/recoveryProjection.test.ts',
    '../src/features/recovery/recoveryFixtures.ts',
  ]) {
    assert.equal(existsSync(fileURLToPath(new URL(relative, import.meta.url))), false, relative);
  }
});
