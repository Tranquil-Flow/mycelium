#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import { rename, stat, unlink, writeFile } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const EXACT_FLEET_GRANT = 'FLEET GRANTED TO A6';
const validatedControls = new WeakSet();
const ENGINES = Object.freeze(['chromium', 'firefox', 'webkit']);
const WORKSPACES = Object.freeze([
  'lab', 'network', 'plans', 'incidents',
  'readiness', 'nodes', 'settings', 'inference',
]);
const WORKSPACE_RECOVERY_FIELDS = Object.freeze(JSON.parse(readFileSync(
  fileURLToPath(new URL('../src/features/recovery/workspaceFields.json', import.meta.url)),
  'utf8',
)));

export function expectedWorkspaceFields(route) {
  const fields = WORKSPACE_RECOVERY_FIELDS[route];
  if (!fields) fail('unknown_workspace');
  return fields;
}

const REQUIRED_SCENARIOS = Object.freeze([
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
const PRIVATE_MARKERS = Object.freeze([
  'A6-BROWSER-PRIVATE-PROMPT-',
  'A6-BROWSER-PRIVATE-OUTPUT-',
  'encoded_prompt_token_ids',
  'committed_prefix_tokens',
  'checkpoint_path',
]);


function fail(code) {
  throw new Error(code);
}

function jsonLine(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function harnessPlan(mode) {
  return {
    protocol: mode === 'fixture'
      ? 'mycelium.a6_product_browser_fixture_observation.v1'
      : 'mycelium.a6_product_browser_plan.v1',
    mode,
    live_evidence: false,
    fixture_or_unit_evidence: mode === 'fixture',
    execution_scope: mode === 'fixture' ? 'fixture_only' : 'preparation_only',
    live_browser_executed: false,
    claim_boundary: mode === 'fixture'
      ? 'Local fixture/unit harness preparation only; not live browser evidence.'
      : 'Network-free preparation plan only; not executed browser or fleet evidence.',
    qualification_claim: false,
    promotion_authorized: false,
    serve_contact_attempted: false,
    fleet_execution_attempted: false,
    engines: [...ENGINES],
    workspaces: [...WORKSPACES],
    required_scenarios: [...REQUIRED_SCENARIOS],
  };
}

export function assertPrivacyClean(text) {
  if (PRIVATE_MARKERS.some((marker) => String(text).includes(marker))) {
    fail('privacy_canary_visible');
  }
}

export function validateRecoveryHistory(history) {
  if (!Array.isArray(history) || history.length === 0) fail('recovery_history_empty');
  let highestCursor = -1;
  let sourceFailureObserved = false;
  let recoveryObserved = false;
  let terminalCount = 0;
  for (const observation of history) {
    if (observation === null || typeof observation !== 'object') fail('recovery_history_invalid');
    const fields = [
      observation.cursor,
      observation.request_count,
      observation.recovering_count,
      observation.terminal_count,
      observation.terminal_blocked_count,
      observation.max_recovery_attempt,
    ];
    if (!fields.every((item) => Number.isSafeInteger(item) && item >= 0)) {
      fail('recovery_history_invalid');
    }
    if (observation.cursor < highestCursor) fail('stale_source_after_cutover');
    if (
      observation.recovering_count > observation.request_count
      || observation.terminal_count > observation.request_count
      || observation.terminal_blocked_count > observation.terminal_count
    ) fail('duplicate_terminal');
    if (observation.max_recovery_attempt > 2) fail('recovery_history_invalid');
    highestCursor = observation.cursor;
    sourceFailureObserved ||= observation.recovering_count > 0 || observation.max_recovery_attempt > 0;
    recoveryObserved ||= observation.max_recovery_attempt > 0;
    terminalCount = Math.max(terminalCount, observation.terminal_count);
  }
  return Object.freeze({
    source_failure_observed: sourceFailureObserved,
    recovery_observed: recoveryObserved,
    terminal_count: terminalCount,
    stale_source_after_cutover: false,
  });
}

export async function atomicPrivateWrite(target, value) {
  const parent = path.dirname(target);
  const parentInfo = await stat(parent);
  if (!parentInfo.isDirectory() || (parentInfo.mode & 0o777) !== 0o700) {
    fail('browser_evidence_root_mode_invalid');
  }
  if (typeof process.getuid === 'function' && parentInfo.uid !== process.getuid()) {
    fail('browser_evidence_root_owner_invalid');
  }
  const temporary = `${target}.tmp-${process.pid}`;
  try {
    await writeFile(temporary, value, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    await rename(temporary, target);
  } finally {
    await unlink(temporary).catch(() => undefined);
  }
  const outputInfo = await stat(target);
  if ((outputInfo.mode & 0o777) !== 0o600) fail('browser_evidence_record_mode_invalid');
}

function exactLoopbackOrigin(raw) {
  const value = String(raw ?? '').replace(/\/$/, '');
  if (!/^https?:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(value)
    || Number(new URL(value).port) > 65535
    || new URL(value).origin !== value) fail('loopback_product_origin_required');
  return value;
}

// Read the frozen gate's actual inventory; do not maintain a second, drifting list.
function frozenFaultCases() {
  const source = readFileSync(fileURLToPath(
    new URL('../../../scripts/run_a6_recovery_gate.py', import.meta.url),
  ), 'utf8');
  const match = /^REQUIRED_CASES = \(\n((?:    "[a-z0-9_]+",\n)+)\)/m.exec(source);
  if (!match) fail('fault_case_inventory_unreadable');
  const cases = match[1].trim().split('\n').map((line) => JSON.parse(line.trim().slice(0, -1)));
  if (cases.length !== 16 || new Set(cases).size !== cases.length) fail('fault_case_inventory_invalid');
  return new Set(cases);
}

function localControlUrl(raw, productOrigin) {
  // The raw spelling is checked BEFORE URL normalization: no aliases, userinfo,
  // fragments, query forwarding, dot segments, escapes, or default-port elision.
  const match = typeof raw === 'string'
    ? /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})(\/[a-zA-Z0-9_/-]+)$/.exec(raw)
    : null;
  if (!match) fail('local_fault_url_invalid');
  const port = Number(match[1]);
  if (port > 65535) fail('local_fault_url_invalid');
  const url = new URL(raw);
  if (url.origin === productOrigin || url.pathname !== match[2] || url.href !== raw) {
    fail('local_fault_url_invalid');
  }
  return Object.freeze({ port, path: match[2], origin: url.origin });
}

export function validateFaultControl({ grant, productOrigin, positiveUrl, negativeUrl,
  positiveCaseId, negativeCaseId }) {
  if (grant !== EXACT_FLEET_GRANT) fail('explicit_fleet_grant_required');
  const origin = exactLoopbackOrigin(productOrigin);
  const cases = frozenFaultCases();
  if (!cases.has(positiveCaseId) || !cases.has(negativeCaseId)
    || positiveCaseId === negativeCaseId) fail('fault_case_id_invalid');
  const positive = localControlUrl(positiveUrl, origin);
  const negative = localControlUrl(negativeUrl, origin);
  if (positive.origin !== negative.origin) fail('fault_control_origin_mismatch');
  const positiveControl = Object.freeze({ ...positive, caseId: positiveCaseId });
  const negativeControl = Object.freeze({ ...negative, caseId: negativeCaseId });
  validatedControls.add(positiveControl);
  validatedControls.add(negativeControl);
  return Object.freeze({ positive: positiveControl, negative: negativeControl });
}

// Native HTTP is deliberately direct: no page/request-context state, proxy,
// ambient cookies/auth, redirect handling, DNS lookup, or forwarded headers.
export async function sendControlledFault(control, usedCases, requestFactory = http.request) {
  if (!validatedControls.has(control) || !(usedCases instanceof Set)) fail('fault_control_unvalidated');
  if (usedCases.has(control.caseId)) fail('fault_case_already_used');
  usedCases.add(control.caseId); // consume before contact, including transport failures
  const body = JSON.stringify({ action: 'execute_controlled_source_failure', case_id: control.caseId });
  return new Promise((resolve, reject) => {
    const rejectSafe = (code) => reject(new Error(code));
    try {
      const request = requestFactory({
        hostname: '127.0.0.1', port: control.port, path: control.path,
        method: 'POST', agent: false,
        headers: {
          accept: 'application/json', 'content-type': 'application/json',
          'content-length': Buffer.byteLength(body),
          'x-mycelium-a6-fleet-grant': EXACT_FLEET_GRANT,
        },
      }, (response) => {
        response.on('error', () => rejectSafe('controlled_fault_transport_failed'));
        response.on('end', () => {
          if (response.statusCode >= 200 && response.statusCode < 300) resolve();
          else rejectSafe(`controlled_fault_rejected_${Number.isInteger(response.statusCode)
            ? response.statusCode : 'invalid_status'}`);
        });
        response.resume();
      });
      request.on('error', () => rejectSafe('controlled_fault_transport_failed'));
      request.setTimeout(10_000, () => request.destroy(new Error('controlled_fault_timeout')));
      request.end(body);
    } catch {
      rejectSafe('controlled_fault_transport_failed');
    }
  });
}

function observePage(page, engine, origin, failures) {
  page.on('pageerror', (error) => failures.push(`${engine}:pageerror:${error.message}`));
  page.on('console', (message) => {
    if (['error', 'assert'].includes(message.type())) {
      failures.push(`${engine}:console:${message.text()}`);
    }
  });
  page.on('request', (request) => {
    const target = new URL(request.url());
    if (target.origin !== origin) failures.push(`${engine}:cross_origin_request:${target.origin}`);
    const headers = request.headers();
    if (headers.authorization !== undefined || headers['proxy-authorization'] !== undefined) {
      failures.push(`${engine}:ambient_authorization_header`);
    }
  });
}

async function productRecoveryStatus(page) {
  return page.evaluate(async () => {
    const response = await fetch('/api/v1/product/snapshot', {
      method: 'GET', credentials: 'same-origin', cache: 'no-store',
      headers: { accept: 'application/json' },
    });
    if (!response.ok) throw new Error(`product_snapshot_${response.status}`);
    const snapshot = await response.json();
    const recovery = snapshot.entities?.find((entity) => entity.kind === 'recovery');
    if (!recovery || !recovery.attributes) throw new Error('product_recovery_aggregate_missing');
    return { cursor: snapshot.publication?.cursor, ...recovery.attributes };
  });
}

export async function exerciseReconnect(page, observe) {
  const context = page.context();
  await context.setOffline(true);
  try {
    await page.waitForTimeout(250);
  } finally {
    await context.setOffline(false);
  }
  return observe();
}

async function waitForObservation(page, predicate, timeout = 300_000) {
  const deadline = Date.now() + timeout;
  let previousSignature = '';
  const history = [];
  while (Date.now() < deadline) {
    const observation = await productRecoveryStatus(page);
    const signature = JSON.stringify(observation);
    if (signature !== previousSignature) {
      history.push(observation);
      previousSignature = signature;
    }
    if (predicate(observation)) return { observation, history };
    await page.waitForTimeout(200);
  }
  fail('recovery_transition_timeout');
}


async function startRequest(page, privateCanaries, maximumNewTokens) {
  await page.goto(`${page.url().split('/#')[0]}/#inference`, { waitUntil: 'domcontentloaded' });
  const baseline = await productRecoveryStatus(page);
  const prompt = page.getByRole('textbox', { name: /prompt/i });
  await prompt.waitFor({ state: 'visible', timeout: 60_000 });
  if (await prompt.isDisabled()) fail('live_inference_disabled');
  await prompt.fill(`${privateCanaries.join(' ')} Explain bounded request recovery.`);
  await page.getByLabel(/maximum new tokens/i).fill(String(maximumNewTokens));
  const start = page.getByRole('button', { name: /start inference/i });
  await start.click();
  const started = await waitForObservation(
    page,
    (observation) => observation.request_count > baseline.request_count,
  );
  return { ...started, baseline };
}

async function verifyWorkspaces(page, privateCanaries) {
  const visited = [];
  for (const hash of WORKSPACES) {
    await page.goto(`${page.url().split('/#')[0]}/#${hash}`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { level: 1 }).first().waitFor({ timeout: 60_000 });
    await page.getByRole('complementary', { name: /full-context replay recovery status/i })
      .waitFor({ timeout: 60_000 });
    const body = await page.locator('body').innerText();
    for (const field of expectedWorkspaceFields(hash)) {
      if (!body.includes(field)) fail(`recovery_field_missing_${hash}:${field}`);
    }
    if (body.includes('FIXTURE DATA · NOT LIVE')) fail(`fixture_source_${hash}`);
    if (/\bA6\b/.test(body)) fail(`internal_milestone_copy_${hash}`);
    if (hash !== 'inference') {
      for (const canary of privateCanaries) {
        if (body.includes(canary)) fail(`private_session_content_visible_${hash}`);
      }
      assertPrivacyClean(body);
    }
    visited.push(hash);
  }
  await page.goBack({ waitUntil: 'domcontentloaded' });
  await page.goForward({ waitUntil: 'domcontentloaded' });
  return visited;
}

export async function verifyInferenceOutcome(page, scenario) {
  const status = page.getByRole('status');
  await status.waitFor({ state: 'visible', timeout: 60_000 });
  const statusText = await status.innerText();
  if (scenario === 'positive') {
    if (!/Completed/i.test(statusText)) fail('positive_ui_terminal_missing');
    const output = page.getByRole('log', { name: 'Decoded output' });
    await output.waitFor({ state: 'visible', timeout: 60_000 });
    if ((await output.innerText()).trim().length === 0) fail('positive_ui_output_missing');
    return;
  }
  if (!/Failed/i.test(statusText)) fail('negative_ui_terminal_missing');
}

async function runScenario(page, scenario, faultControl, usedCases, maximumNewTokens) {
  const promptCanary = `A6-BROWSER-PRIVATE-PROMPT-${scenario}-${Date.now()}`;
  const outputCanary = `A6-BROWSER-PRIVATE-OUTPUT-${scenario}-${Date.now()}`;
  const privateCanaries = [promptCanary, outputCanary];
  const started = await startRequest(page, privateCanaries, maximumNewTokens);
  await sendControlledFault(faultControl, usedCases);
  const failed = await waitForObservation(
    page,
    (observation) => observation.recovering_count > 0
      || observation.terminal_blocked_count > started.baseline.terminal_blocked_count,
  );
  const beforeCutoverReconnect = await exerciseReconnect(
    page,
    () => waitForObservation(page, (observation) => observation.cursor >= failed.observation.cursor),
  );

  await page.reload({ waitUntil: 'domcontentloaded' });
  const afterReload = await waitForObservation(
    page,
    (observation) => observation.max_recovery_attempt > 0
      || observation.terminal_blocked_count > started.baseline.terminal_blocked_count,
  );
  const beforeCutoverHistory = [
    ...started.history,
    ...failed.history,
    ...beforeCutoverReconnect.history,
    ...afterReload.history,
  ];

  await page.reload({ waitUntil: 'domcontentloaded' });
  const finished = await waitForObservation(
    page,
    (observation) => observation.terminal_count > started.baseline.terminal_count,
  );
  await verifyInferenceOutcome(page, scenario);
  const afterCutoverReconnect = await exerciseReconnect(
    page,
    () => waitForObservation(
      page,
      (observation) => observation.cursor >= finished.observation.cursor
        && observation.terminal_count >= finished.observation.terminal_count,
    ),
  );
  const signatures = new Set();
  const history = [
    ...beforeCutoverHistory,
    ...finished.history,
    ...afterCutoverReconnect.history,
  ].filter((item) => {
    const signature = JSON.stringify(item);
    if (signatures.has(signature)) return false;
    signatures.add(signature);
    return true;
  });
  const validation = validateRecoveryHistory(history);
  if (!validation.source_failure_observed) fail(`${scenario}_source_failure_missing`);
  if (scenario === 'positive') {
    if (
      !validation.recovery_observed
      || finished.observation.terminal_blocked_count > started.baseline.terminal_blocked_count
    ) fail('positive_recovery_missing');
  } else if (
    finished.observation.terminal_blocked_count <= started.baseline.terminal_blocked_count
  ) {
    fail('truthful_abort_missing');
  }
  const workspaces = await verifyWorkspaces(page, privateCanaries);
  return {
    scenario,
    private_canaries: privateCanaries,
    workspaces,
    source_failure_observed: true,
    recovery_observed: validation.recovery_observed,
    terminal_count: validation.terminal_count,
    stale_source_after_cutover: false,
    duplicate_terminal: false,
    reload_during_recovery: true,
    reconnect_before_cutover: true,
    reconnect_after_cutover: true,
    back_forward: true,
  };
}

export function singleEngineReceipt(engine, positive, negative) {
  if (!ENGINES.includes(engine)) fail('browser_engine_selection_required');
  return {
    protocol: 'mycelium.a6_product_browser_observation.v1',
    mode: 'live',
    live_evidence: true,
    fixture_or_unit_evidence: false,
    qualification_claim: false,
    promotion_authorized: false,
    aggregation_pending: true,
    claim_boundary: 'One engine and one receiver lifetime only; three-engine and physical qualification pending.',
    fleet_grant_verified: true,
    passed: true,
    engine,
    engines: [{ engine, positive, negative }],
    workspaces: [...WORKSPACES],
    required_scenarios: [...REQUIRED_SCENARIOS],
    source_failure_transition_observed: true,
    stale_source_after_cutover: false,
    duplicate_terminal: false,
    second_session_private_content_visible: false,
    console_error_count: 0,
    page_error_count: 0,
    cross_origin_request_count: 0,
  };
}

async function runLive({ engine, origin, outputPath, faultControl, maximumNewTokens }) {
  const playwright = await import('@playwright/test');
  const failures = [];
  const usedCases = new Set();
  const browser = await playwright[engine].launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    observePage(page, engine, origin, failures);
    await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
    const positive = await runScenario(page, 'positive', faultControl.positive, usedCases, maximumNewTokens);
    const negative = await runScenario(page, 'negative', faultControl.negative, usedCases, maximumNewTokens);
    const canaries = [...positive.private_canaries, ...negative.private_canaries];
    delete positive.private_canaries;
    delete negative.private_canaries;
    await context.close();

    // A separate storage context of the same selected engine, never a second case use.
    const privacyBrowser = browser;
    const cleanContext = await privacyBrowser.newContext({ viewport: { width: 1280, height: 900 } });
    const cleanPage = await cleanContext.newPage();
    observePage(cleanPage, `${engine}-clean-session`, origin, failures);
    await cleanPage.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
    const cleanBody = await cleanPage.locator('body').innerText();
    for (const canary of canaries) {
      if (cleanBody.includes(canary)) fail('second_session_private_content_visible');
    }
    assertPrivacyClean(cleanBody);
    await cleanContext.close();

    // Console/page errors can contain private prompt/output; never echo their payload.
    if (failures.length > 0) fail('browser_failures_observed');
    const report = singleEngineReceipt(engine, positive, negative);
    await atomicPrivateWrite(outputPath, jsonLine(report));
    process.stdout.write(jsonLine(report));
  } finally {
    await browser.close().catch(() => undefined);
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 1 && args[0] === '--plan') {
    process.stdout.write(jsonLine(harnessPlan('plan')));
    return;
  }
  if (args.length === 1 && args[0] === '--fixture') {
    process.stdout.write(jsonLine(harnessPlan('fixture')));
    return;
  }

  // Authorization stays first; selection must precede origin, fault or browser contact.
  if (process.env.MYCELIUM_A6_FLEET_GRANT !== EXACT_FLEET_GRANT) {
    fail('explicit_fleet_grant_required');
  }
  if (args.length !== 1 || !args[0].startsWith('--engine=')) {
    fail('browser_engine_selection_required');
  }
  const engine = args[0].slice('--engine='.length);
  if (!ENGINES.includes(engine)) fail('browser_engine_selection_required');
  const origin = exactLoopbackOrigin(process.env.MYCELIUM_A6_PRODUCT_ORIGIN);
  const outputPath = process.env.MYCELIUM_A6_BROWSER_EVIDENCE;
  if (!outputPath || !path.isAbsolute(outputPath)) fail('browser_evidence_path_required');
  const maximumNewTokens = Number(process.env.MYCELIUM_A6_BROWSER_TOKENS ?? '64');
  if (!Number.isSafeInteger(maximumNewTokens) || maximumNewTokens < 8 || maximumNewTokens > 128) {
    fail('browser_token_bound_invalid');
  }
  const faultControl = validateFaultControl({
    grant: process.env.MYCELIUM_A6_FLEET_GRANT,
    productOrigin: origin,
    positiveUrl: process.env.MYCELIUM_A6_POSITIVE_FAULT_URL,
    negativeUrl: process.env.MYCELIUM_A6_NEGATIVE_FAULT_URL,
    positiveCaseId: process.env.MYCELIUM_A6_POSITIVE_CASE_ID,
    negativeCaseId: process.env.MYCELIUM_A6_NEGATIVE_CASE_ID,
  });
  try {
    await runLive({
      engine,
      origin,
      outputPath: path.resolve(outputPath),
      faultControl,
      maximumNewTokens,
    });
  } catch {
    // Browser/transport exception messages may include private typed content.
    fail('a6_browser_live_failed');
  }
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : 'a6_browser_gate_failed'}\n`);
    process.exitCode = 1;
  });
}
