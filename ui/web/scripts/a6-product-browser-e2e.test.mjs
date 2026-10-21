// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import test from 'node:test';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as faultHarness from './a6-product-browser-e2e.mjs';
const {
  assertPrivacyClean,
  atomicPrivateWrite,
  awaitLiveInferenceEnabled,
  exerciseReconnect,
  expectedWorkspaceFields,
  validateRecoveryHistory,
  validateFaultControl,
  sendControlledFault,
  verifyInferenceOutcome,
} = faultHarness;

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

test('live CLI refuses absent, invalid, repeated or ambiguous engine before any contact', () => {
  // Valid local grant and configuration make this a real CLI selection boundary,
  // yet none of these probes may import Playwright or touch either endpoint.
  const env = {
    MYCELIUM_A6_FLEET_GRANT: 'FLEET GRANTED TO A6',
    MYCELIUM_A6_PRODUCT_ORIGIN: 'http://127.0.0.1:9',
    MYCELIUM_A6_BROWSER_EVIDENCE: '/tmp/forbidden-a6-browser-evidence.json',
  };
  for (const args of [[], ['--engine='], ['--engine=all'],
    ['--engine=Chromium'], ['--engine=chromium', '--engine=firefox'],
    ['--engine=webkit', '--engine=webkit'], ['--engine=chromium', '--unknown']]) {
    const result = run(args, env);
    assert.equal(result.status, 1, `${args}: ${result.stderr}`);
    assert.match(result.stderr, /browser_engine_selection_required/);
    assert.doesNotMatch(result.stderr, /FLEET GRANTED|browserType|ECONNREFUSED|Cannot find package/);
  }
});

test('single-engine receipt binds only the selected engine and is explicitly non-aggregate', () => {
  for (const engine of ['chromium', 'firefox', 'webkit']) {
    const positive = { scenario: 'positive', source_failure_observed: true };
    const negative = { scenario: 'negative', source_failure_observed: true };
    const report = faultHarness.singleEngineReceipt(engine, positive, negative);
    assert.equal(report.engine, engine);
    assert.deepEqual(report.engines, [{ engine, positive, negative }]);
    assert.equal(report.aggregation_pending, true);
    assert.equal(report.qualification_claim, false);
    assert.equal(report.promotion_authorized, false);
    assert.equal(report.second_session_private_content_visible, false);
    assert.doesNotMatch(JSON.stringify(report), /FLEET GRANTED|A6-BROWSER-PRIVATE/);
  }
});

test('live source launches and checks only the selected engine, not a three-engine case replay', () => {
  const source = readFileSync(scriptPath, 'utf8');
  assert.doesNotMatch(source, /for\s*\(const name of ENGINES\)/);
  assert.match(source, /playwright\[engine\]\.launch/);
  assert.doesNotMatch(source, /playwright\.chromium\.launch/);
  assert.match(source, /privacyBrowser\.newContext/);
  assert.match(source, /runScenario\(page, 'positive', faultControl\.positive, usedCases/);
  assert.match(source, /runScenario\(page, 'negative', faultControl\.negative, usedCases/);
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

const faultConfig = {
  grant: 'FLEET GRANTED TO A6',
  productOrigin: 'http://127.0.0.1:8787',
  positiveUrl: 'http://127.0.0.1:8788/fault',
  negativeUrl: 'http://127.0.0.1:8788/fault',
  positiveCaseId: 'ordinary_browser_source_failure_recovery',
  negativeCaseId: 'no_successor',
};

test('fault control requires exact separate IPv4 loopback origin and two frozen distinct cases', () => {
  const validated = validateFaultControl(faultConfig);
  assert.equal(validated.positive.caseId, faultConfig.positiveCaseId);
  assert.equal(validated.negative.caseId, faultConfig.negativeCaseId);
  for (const change of [
    { grant: undefined }, { grant: 'wrong' },
    { positiveCaseId: undefined }, { positiveCaseId: 'not_frozen' },
    { negativeCaseId: faultConfig.positiveCaseId },
    { positiveUrl: 'http://127.0.0.1:8787/fault' },
    { positiveUrl: 'http://localhost:8788/fault' },
    { positiveUrl: 'http://127.0.0.2:8788/fault' },
    { positiveUrl: 'http://[::1]:8788/fault' },
    { positiveUrl: 'https://127.0.0.1:8788/fault' },
    { positiveUrl: 'http://127.0.0.1:8788@evil.test/fault' },
    { positiveUrl: 'http://127.0.0.1:8788/fault?forward=https://evil.test' },
    { positiveUrl: 'http://127.0.0.1:8788/fault#fragment' },
    { positiveUrl: 'http://127.0.0.1:0/fault' },
    { positiveUrl: 'http://127.0.0.1:65536/fault' },
    { negativeUrl: 'http://127.0.0.1:8789/fault' },
  ]) {
    assert.throws(() => validateFaultControl({ ...faultConfig, ...change }));
  }
});

test('granted CLI refuses incomplete or remote control before browser launch or receiver contact', () => {
  const env = {
    MYCELIUM_A6_FLEET_GRANT: faultConfig.grant,
    MYCELIUM_A6_PRODUCT_ORIGIN: faultConfig.productOrigin,
    MYCELIUM_A6_BROWSER_EVIDENCE: '/tmp/forbidden-a6-browser-evidence.json',
    MYCELIUM_A6_POSITIVE_FAULT_URL: faultConfig.positiveUrl,
    MYCELIUM_A6_NEGATIVE_FAULT_URL: faultConfig.negativeUrl,
    MYCELIUM_A6_POSITIVE_CASE_ID: faultConfig.positiveCaseId,
    MYCELIUM_A6_NEGATIVE_CASE_ID: faultConfig.negativeCaseId,
  };
  for (const change of [
    { MYCELIUM_A6_NEGATIVE_CASE_ID: '' },
    { MYCELIUM_A6_NEGATIVE_CASE_ID: faultConfig.positiveCaseId },
    { MYCELIUM_A6_POSITIVE_FAULT_URL: 'http://evil.test/fault' },
    { MYCELIUM_A6_POSITIVE_FAULT_URL: 'http://127.0.0.1:8787/fault' },
  ]) {
    const result = run(['--engine=chromium'], { ...env, ...change });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /fault_case_id_invalid|local_fault_url_invalid/);
    assert.doesNotMatch(result.stderr, /FLEET GRANTED|browserType|ECONNREFUSED/);
  }
});

test('isolated direct control request delivers only bounded action, case and grant; redirects and errors disclose nothing', async () => {
  const controls = validateFaultControl(faultConfig);
  const control = controls.positive;
  const used = new Set();
  const requests = [];
  const receiver = (options, onResponse) => {
    const req = new EventEmitter();
    req.end = (body) => {
      requests.push({ options, body: JSON.parse(body) });
      queueMicrotask(() => {
        const res = new EventEmitter();
        res.statusCode = requests.length === 1 ? 200 : 302;
        res.resume = () => queueMicrotask(() => res.emit('end'));
        onResponse(res);
      });
    };
    req.setTimeout = () => req;
    req.destroy = () => req;
    return req;
  };
  await sendControlledFault(control, used, receiver);
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body, {
    action: 'execute_controlled_source_failure', case_id: faultConfig.positiveCaseId,
  });
  assert.equal(requests[0].options.hostname, '127.0.0.1');
  assert.equal(requests[0].options.port, 8788);
  assert.equal(requests[0].options.path, '/fault');
  assert.equal(requests[0].options.agent, false);
  assert.equal(requests[0].options.headers['x-mycelium-a6-fleet-grant'] === faultConfig.grant, true);
  assert.equal(requests[0].options.headers.authorization, undefined);
  assert.equal(requests[0].options.headers.cookie, undefined);
  assert.equal(requests[0].options.headers.host, undefined);
  assert.equal(requests[0].options.headers['x-forwarded-for'], undefined);
  assert.equal(requests[0].options.headers['proxy-authorization'], undefined);
  assert.doesNotMatch(JSON.stringify(requests[0].body), /FLEET GRANTED/);
  await assert.rejects(sendControlledFault(control, used, receiver), /fault_case_already_used/);
  assert.equal(requests.length, 1); // repeated case rejected before contact
  await assert.rejects(sendControlledFault(controls.negative, used, receiver), /controlled_fault_rejected_302/);
  assert.equal(requests.length, 2); // redirect is refused, not followed
  assert.doesNotMatch(JSON.stringify({ ...requests[0].options, headers: {} }), /FLEET GRANTED/);
  await assert.rejects(sendControlledFault({ ...control, port: 9999 }, new Set(), receiver), /fault_control_unvalidated/);
  assert.equal(requests.length, 2); // forged endpoint cannot contact receiver
});

test('browser outcome proof requires completed UI stream or truthful failed UI', async () => {
  const observed = [];
  const page = {
    waitForTimeout: async () => undefined,
    getByRole: (role, options = {}) => ({
      waitFor: async () => observed.push([role, options.name ?? null, options.exact ?? null]),
      innerText: async () => role === 'status' ? 'Completed\n3 tokens applied' : 'bounded recovered output',
    }),
  };
  await verifyInferenceOutcome(page, 'positive');
  assert.deepEqual(observed, [
    ['status', 'Inference outcome', true],
    ['log', 'Decoded output', null],
  ]);
  const failedPage = {
    waitForTimeout: async () => undefined,
    getByRole: () => ({
      waitFor: async () => undefined,
      innerText: async () => 'Failed',
    }),
  };
  await verifyInferenceOutcome(failedPage, 'negative');
});

test('browser outcome proof waits through the restored phase and refuses a different terminal', async () => {
  const sequence = (texts) => {
    let index = 0;
    let waits = 0;
    return {
      get waits() { return waits; },
      page: {
        waitForTimeout: async () => { waits += 1; },
        getByRole: (role) => ({
          waitFor: async () => undefined,
          innerText: async () => role === 'status'
            ? texts[Math.min(index++, texts.length - 1)]
            : 'recovered output',
        }),
      },
    };
  };
  const restored = sequence(['Stream interrupted\n1 tokens applied', 'Streaming\n1 tokens applied',
    'Completed3 tokens applied']);
  await verifyInferenceOutcome(restored.page, 'positive', { interval: 0 });
  assert.equal(restored.waits, 2);
  await assert.rejects(
    verifyInferenceOutcome(sequence(['Stream interrupted', 'Failed']).page, 'positive', { interval: 0 }),
    /positive_ui_terminal_missing/,
  );
  await assert.rejects(
    verifyInferenceOutcome(sequence(['Completed']).page, 'negative', { interval: 0 }),
    /negative_ui_terminal_missing/,
  );
  await assert.rejects(
    verifyInferenceOutcome(sequence(['Cancellation pending', 'Cancelled\n1 tokens applied']).page, 'positive',
      { interval: 0 }),
    /positive_ui_terminal_missing/,
  );
  await assert.rejects(
    verifyInferenceOutcome(sequence(['Stream interrupted']).page, 'positive', { interval: 0, timeout: 0 }),
    /positive_ui_terminal_missing/,
  );
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

function promptPage({ enabledAfterPolls, evidence = 'connected' }) {
  const calls = { evidenceWaits: 0, polls: 0, visible: 0 };
  const prompt = {
    waitFor: async () => { calls.visible += 1; },
    isDisabled: async () => {
      calls.polls += 1;
      return calls.polls <= enabledAfterPolls;
    },
  };
  const page = {
    waitForFunction: async () => {
      calls.evidenceWaits += 1;
      if (evidence !== 'connected') throw new Error('timeout');
    },
    waitForTimeout: async () => undefined,
  };
  return { page, prompt, calls };
}

test('live inference wait outlasts the disabled first paint of a fresh serve', async () => {
  // First paint: evidence still loading, prompt disabled for a few polls.
  const { page, prompt, calls } = promptPage({ enabledAfterPolls: 3 });
  await awaitLiveInferenceEnabled(page, prompt, 60_000);
  assert.equal(calls.visible, 1);
  assert.equal(calls.evidenceWaits, 1, 'waits on body[data-evidence] first');
  assert.equal(calls.polls, 4);
});

test('live inference wait still fails closed when the prompt never enables', async () => {
  const { page, prompt } = promptPage({ enabledAfterPolls: Infinity, evidence: 'error' });
  await assert.rejects(awaitLiveInferenceEnabled(page, prompt, 0), /live_inference_disabled/);
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
  assert.doesNotMatch(source, /page\.request\.post\(/); // control must never use browser/session transport
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

test('controlled fault transport outlasts the product seam bounds', () => {
  // Two 30 s seam waits per browser case plus transport margin.
  assert.ok(faultHarness.CONTROLLED_FAULT_TIMEOUT_MS > 2 * 30_000);
  assert.ok(faultHarness.CONTROLLED_FAULT_TIMEOUT_MS <= 120_000);
});

function scriptedObservations(observations) {
  let index = 0;
  const page = { waitForTimeout: async () => {} };
  const observe = async () => observations[Math.min(index++, observations.length - 1)];
  return { page, observe };
}

test('a positive request that goes terminal-blocked fails at once with its own code', async () => {
  // c0dc4350 browser matrix: the first successor ended terminal-blocked and the
  // harness waited out its 300 s bound before reporting a transition timeout.
  const { page, observe } = scriptedObservations([
    { recovering_count: 0, terminal_blocked_count: 0 },
    { recovering_count: 0, terminal_blocked_count: 1 },
  ]);
  const reports = [];
  await assert.rejects(
    faultHarness.waitForObservation(page, (o) => o.recovering_count > 0, {
      label: 'recovering_or_blocked',
      abort: { code: 'positive_recovery_terminal_blocked', when: (o) => o.terminal_blocked_count > 0 },
      observe,
      report: (line) => reports.push(JSON.parse(line)),
    }),
    /positive_recovery_terminal_blocked/,
  );
  assert.deepEqual(reports, [{
    event: 'a6_browser_wait_failed',
    code: 'positive_recovery_terminal_blocked',
    wait: 'recovering_or_blocked',
    last: { recovering_count: 0, terminal_blocked_count: 1 },
  }]);
});

test('a transition timeout names the wait that expired and its last observation', async () => {
  const { page, observe } = scriptedObservations([{ recovering_count: 0, terminal_count: 0 }]);
  const reports = [];
  await assert.rejects(
    faultHarness.waitForObservation(page, () => false, {
      timeout: 0, label: 'terminal', observe, report: (line) => reports.push(JSON.parse(line)),
    }),
    /^Error: recovery_transition_timeout$/,
  );
  assert.equal(reports.length, 1);
  assert.equal(reports[0].wait, 'terminal');
  assert.equal(reports[0].code, 'recovery_transition_timeout');
});

test('the browser harness keeps its 300 s transition bound by default', () => {
  const source = readFileSync(scriptPath, 'utf8');
  assert.match(source, /timeout = 300_000/);
});

// Recovery aggregates shaped like the production producer, not a hand model:
// mycelium_live/router_port.py recovery_public_projection publishes the M16
// coordinator's terminal state (completed, failed or cancelled; never
// TERMINAL_BLOCKED) and recovery_attempt = request_attempt - 1, which rises
// only at cutover. mycelium_product_spine/projector.py then aggregates them.
const IDLE = Object.freeze({
  breaker_state: 'closed', request_count: 0, recovering_count: 0, terminal_count: 0,
  terminal_blocked_count: 0, cleanup_complete_count: 0, reconciled_count: 0,
  max_recovery_attempt: 0,
});
const AFTER_POSITIVE = Object.freeze({
  ...IDLE, request_count: 1, terminal_count: 1, cleanup_complete_count: 1, max_recovery_attempt: 1,
});

function scenarioPage({ baseline, afterFault, uiStatus, clock }) {
  const state = { clicked: false, faulted: false, index: 0, cursor: 100, url: 'http://127.0.0.1:9/#inference' };
  const body = [...new Set(Object.values(JSON.parse(readFileSync(fileURLToPath(
    new URL('../src/features/recovery/workspaceFields.json', import.meta.url)), 'utf8'))).flat())].join('\n');
  const observe = () => {
    if (!state.clicked) return baseline;
    if (!state.faulted) return { ...baseline, request_count: baseline.request_count + 1 };
    return afterFault[Math.min(state.index++, afterFault.length - 1)];
  };
  const waits = { waitFor: async () => undefined };
  const page = {
    url: () => state.url,
    goto: async (url) => { state.url = url; },
    reload: async () => undefined,
    goBack: async () => undefined,
    goForward: async () => undefined,
    evaluate: async () => ({ cursor: state.cursor++, ...observe() }),
    waitForTimeout: async (ms) => { clock.now += ms; },
    waitForFunction: async () => undefined,
    context: () => ({ setOffline: async () => undefined }),
    getByLabel: () => ({ fill: async () => undefined }),
    locator: () => ({ innerText: async () => body }),
    getByRole: (role) => ({
      ...waits,
      first: () => waits,
      isDisabled: async () => false,
      fill: async () => undefined,
      click: async () => { state.clicked = true; },
      innerText: async () => (role === 'status' ? uiStatus : 'bounded recovered output'),
    }),
  };
  return { page, state };
}

async function runProductionShapedScenario(t, scenario, script) {
  const clock = { now: 1_791_926_092_000 };
  const realNow = Date.now;
  Date.now = () => clock.now;
  t.after(() => { Date.now = realNow; });
  const { page, state } = scenarioPage({ ...script, clock });
  const http = await import('node:http');
  const receiver = http.createServer((request, response) => {
    request.resume();
    request.on('end', () => { state.faulted = true; response.end('{"code":"controlled_fault_executed"}'); });
  });
  await new Promise((resolve) => receiver.listen(0, '127.0.0.1', resolve));
  t.after(() => receiver.close());
  const url = `http://127.0.0.1:${receiver.address().port}/a6/controlled-fault`;
  const controls = validateFaultControl({
    grant: 'FLEET GRANTED TO A6', productOrigin: 'http://127.0.0.1:9',
    positiveUrl: url, negativeUrl: url,
    positiveCaseId: 'ordinary_browser_source_failure_recovery', negativeCaseId: 'no_successor',
  });
  const started = clock.now;
  const outcome = faultHarness.runScenario(
    page, scenario, scenario === 'positive' ? controls.positive : controls.negative, new Set(), 16);
  return { outcome, elapsed: () => clock.now - started };
}

test('production-shaped positive recovery passes and requires its cleanup', async (t) => {
  const recovering = { ...IDLE, request_count: 1, recovering_count: 1 };
  const { outcome } = await runProductionShapedScenario(t, 'positive', {
    baseline: IDLE, uiStatus: 'Completed',
    afterFault: [recovering, recovering, recovering, AFTER_POSITIVE],
  });
  const result = await outcome;
  assert.equal(result.recovery_observed, true);
  assert.equal(result.terminal_count, 1);
});

test('a completed positive whose cleanup is not proven is not a recovery', async (t) => {
  const recovering = { ...IDLE, request_count: 1, recovering_count: 1 };
  const { outcome } = await runProductionShapedScenario(t, 'positive', {
    baseline: IDLE, uiStatus: 'Completed',
    afterFault: [recovering, recovering, { ...AFTER_POSITIVE, cleanup_complete_count: 0 }],
  });
  await assert.rejects(outcome, /^Error: positive_recovery_missing$/);
});

test('a positive request that aborts before cutover fails at once, not after 300 s', async (t) => {
  // 67bb87b7 a2 browser matrix: the successor ran past the 30 s checkpoint
  // expiry and the request ended failed with no cutover. The production
  // aggregate never shows a blocked terminal or a recovery attempt for that,
  // so the harness waited out attempt_or_blocked and reported a timeout.
  const recovering = { ...IDLE, request_count: 1, recovering_count: 1 };
  const aborted = { ...IDLE, request_count: 1, terminal_count: 1, cleanup_complete_count: 1 };
  const { outcome, elapsed } = await runProductionShapedScenario(t, 'positive', {
    baseline: IDLE, uiStatus: 'Failed', afterFault: [recovering, recovering, aborted],
  });
  await assert.rejects(outcome, /^Error: positive_recovery_aborted$/);
  assert.ok(elapsed() < 10_000, `failed after ${elapsed()} ms`);
});

test('production-shaped truthful no-successor abort passes', async (t) => {
  // The abort is a cleaned failed terminal with no cutover; it can finish
  // before the first poll, so no recovering aggregate is ever observed.
  const { outcome, elapsed } = await runProductionShapedScenario(t, 'negative', {
    baseline: AFTER_POSITIVE, uiStatus: 'Failed',
    afterFault: [{ ...AFTER_POSITIVE, request_count: 2, terminal_count: 2, cleanup_complete_count: 2 }],
  });
  const result = await outcome;
  assert.equal(result.scenario, 'negative');
  assert.equal(result.terminal_count, 2);
  assert.ok(elapsed() < 10_000, `passed after ${elapsed()} ms`);
});

test('a negative abort whose cleanup is unproven is not a truthful abort', async (t) => {
  const { outcome } = await runProductionShapedScenario(t, 'negative', {
    baseline: AFTER_POSITIVE, uiStatus: 'Failed',
    afterFault: [{ ...AFTER_POSITIVE, request_count: 2, terminal_count: 2, terminal_blocked_count: 1 }],
  });
  await assert.rejects(outcome, /^Error: negative_abort_cleanup_unproven$/);
});

test('a negative terminal without completed cleanup is not a truthful abort', async (t) => {
  const { outcome } = await runProductionShapedScenario(t, 'negative', {
    baseline: AFTER_POSITIVE, uiStatus: 'Failed',
    afterFault: [{ ...AFTER_POSITIVE, request_count: 2, terminal_count: 2 }],
  });
  await assert.rejects(outcome, /^Error: truthful_abort_missing$/);
});

test('a negative request that recovers is not an abort', async (t) => {
  const { outcome } = await runProductionShapedScenario(t, 'negative', {
    baseline: AFTER_POSITIVE, uiStatus: 'Completed',
    afterFault: [{ ...AFTER_POSITIVE, request_count: 2, recovering_count: 1 },
      { ...AFTER_POSITIVE, request_count: 2, terminal_count: 2, cleanup_complete_count: 2 }],
  });
  await assert.rejects(outcome, /^Error: negative_ui_terminal_missing$/);
});
