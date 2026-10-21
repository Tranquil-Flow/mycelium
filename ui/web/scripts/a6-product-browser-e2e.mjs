#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
import { readFileSync } from 'node:fs';
import { rename, stat, unlink, writeFile } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const EXACT_FLEET_GRANT = 'FLEET GRANTED TO A6';
// The control answers only after the product seam has measured the fault. The
// two browser cases each wait up to two seam bounds of 30 s (source failure
// observed, then the recovery transition or the terminal), so the transport
// must outlast 60 s; 10 s abandoned a case the product was still measuring.
export const CONTROLLED_FAULT_TIMEOUT_MS = 75_000;
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
          if (response.statusCode === 200) resolve(200);
          else rejectSafe(`controlled_fault_rejected_${Number.isInteger(response.statusCode)
            ? response.statusCode : 'invalid_status'}`);
        });
        response.resume();
      });
      request.on('error', () => rejectSafe('controlled_fault_transport_failed'));
      request.setTimeout(CONTROLLED_FAULT_TIMEOUT_MS, () => request.destroy(new Error('controlled_fault_timeout')));
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
    const recovery = snapshot.entities?.find((entity) => (
      entity.kind === 'recovery' && entity.entity_id === 'recovery-runtime'
    ));
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

// Polls the product snapshot until ``predicate`` holds (bounded by
// ``timeout``). ``abort`` (one entry or a list) names states that can never
// reach the predicate, e.g. a positive request that ended without a cutover:
// the wait then fails at once with that entry's ``code`` instead of waiting
// out the whole bound. Either failure reports the wait's ``label`` and the
// last observation on stderr.
export async function waitForObservation(page, predicate, options = {}) {
  const {
    timeout = 300_000,
    label = 'unlabelled',
    abort = null,
    observe = productRecoveryStatus,
    report = (line) => process.stderr.write(`${line}\n`),
  } = typeof options === 'number' ? { timeout: options } : options;
  const deadline = Date.now() + timeout;
  const aborts = abort === null ? [] : [].concat(abort);
  let previousSignature = '';
  let observation = null;
  const history = [];
  const failWith = (code) => {
    report(JSON.stringify({ event: 'a6_browser_wait_failed', code, wait: label, last: observation }));
    fail(code);
  };
  while (Date.now() < deadline) {
    observation = await observe(page);
    const signature = JSON.stringify(observation);
    if (signature !== previousSignature) {
      history.push(observation);
      previousSignature = signature;
    }
    if (predicate(observation)) return { observation, history };
    for (const entry of aborts) {
      if (entry.when(observation)) failWith(entry.code);
    }
    await page.waitForTimeout(200);
  }
  failWith('recovery_transition_timeout');
}


// The prompt renders disabled at first paint: the unified product evidence
// arrives over SSE afterwards and only then can enable live inference. The
// product mirrors its evidence status onto document.body[data-evidence] for
// exactly this wait (the A4 harness uses it too); checking the textbox as soon
// as it is visible races that effect and refuses every fresh serve.
export async function awaitLiveInferenceEnabled(page, prompt, timeout = 60_000) {
  await prompt.waitFor({ state: 'visible', timeout });
  await page.waitForFunction(
    () => document.body.dataset.evidence === 'connected',
    null,
    { timeout },
  ).catch(() => {});
  // Connected evidence can still be catching up to the qualified route; give
  // the same bounded budget for the prompt itself to become enabled.
  const deadline = Date.now() + timeout;
  while (await prompt.isDisabled()) {
    if (Date.now() >= deadline) fail('live_inference_disabled');
    await page.waitForTimeout(250);
  }
}

async function startRequest(page, privateCanaries, maximumNewTokens) {
  await page.goto(`${page.url().split('/#')[0]}/#inference`, { waitUntil: 'domcontentloaded' });
  const baseline = await productRecoveryStatus(page);
  const prompt = page.getByRole('textbox', { name: /prompt/i });
  await awaitLiveInferenceEnabled(page, prompt);
  await prompt.fill(`${privateCanaries.join(' ')} Explain bounded request recovery.`);
  await page.getByLabel(/maximum new tokens/i).fill(String(maximumNewTokens));
  const start = page.getByRole('button', { name: /start inference/i });
  await start.click();
  const started = await waitForObservation(
    page,
    (observation) => observation.request_count > baseline.request_count,
    { label: 'request_started' },
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

// The phase label leads the region text; the token count follows it with or
// without a separator ("Completed\n3 tokens applied" / "Completed3 tokens…").
const UI_TERMINAL_LABEL = /^\s*(Completed|Cancelled|Failed)(?![A-Za-z])/;

// The inference workspace renders several role=status regions (route activity,
// catalog, acquisition, evidence); only the named outcome region is the request
// terminal. After a reload it first shows the restored local phase (e.g.
// "Stream interrupted") and reaches the server terminal once the restored page
// re-attaches and the gateway replays, so wait for a terminal label instead of
// reading the first paint. A different terminal label fails at once.
export async function verifyInferenceOutcome(page, scenario, options = {}) {
  const { timeout = 60_000, interval = 250 } = options;
  if (scenario !== 'positive' && scenario !== 'negative') fail('browser_scenario_invalid');
  const expected = scenario === 'positive' ? 'Completed' : 'Failed';
  const code = scenario === 'positive' ? 'positive_ui_terminal_missing' : 'negative_ui_terminal_missing';
  const status = page.getByRole('status', { name: 'Inference outcome', exact: true });
  await status.waitFor({ state: 'visible', timeout });
  const deadline = Date.now() + timeout;
  for (;;) {
    const terminal = UI_TERMINAL_LABEL.exec(await status.innerText())?.[1] ?? null;
    if (terminal === expected) break;
    if (terminal !== null || Date.now() >= deadline) fail(code);
    await page.waitForTimeout(interval);
  }
  if (scenario === 'positive') {
    const output = page.getByRole('log', { name: 'Decoded output' });
    await output.waitFor({ state: 'visible', timeout });
    if ((await output.innerText()).trim().length === 0) fail('positive_ui_output_missing');
  }
}

// What each scenario must observe, in terms of the production aggregate.
// mycelium_live/router_port.py recovery_public_projection publishes the M16
// coordinator terminal (completed, failed or cancelled), keeps a request whose
// cleanup is unproven nonterminal, and raises the recovery attempt only at
// cutover. So a recovery is a cutover that ends terminal with its cleanup
// complete; a terminal without a cutover is an abort. A truthful abort is a
// cleaned terminal without a cutover (the UI must say Failed); a rise in the
// blocked count means cleanup was not proven and is never a truthful abort.
export function scenarioExpectations(scenario, baseline) {
  const ended = (o) => o.terminal_count > baseline.terminal_count;
  const cutOver = (o) => o.max_recovery_attempt > baseline.max_recovery_attempt;
  const cleaned = (o) => o.cleanup_complete_count > baseline.cleanup_complete_count;
  const blocked = (o) => o.terminal_blocked_count > baseline.terminal_blocked_count;
  if (scenario === 'positive') {
    return Object.freeze({
      failureObserved: (o) => o.recovering_count > 0,
      outcomeReached: cutOver,
      // Either state can never become a recovery: fail at once with its own
      // code rather than as a late recovery_transition_timeout.
      aborts: Object.freeze([
        { code: 'positive_recovery_terminal_blocked', when: blocked },
        { code: 'positive_recovery_aborted', when: (o) => ended(o) && !cutOver(o) },
      ]),
      outcomeFailure: (o) => (blocked(o) || !cutOver(o) || !ended(o) || !cleaned(o)
        ? 'positive_recovery_missing' : null),
    });
  }
  if (scenario !== 'negative') fail('browser_scenario_invalid');
  return Object.freeze({
    // A no-successor abort can end before the first poll after the fault.
    failureObserved: (o) => o.recovering_count > 0 || ended(o),
    outcomeReached: ended,
    aborts: Object.freeze([
      { code: 'negative_abort_cleanup_unproven', when: blocked },
      { code: 'negative_unexpected_recovery', when: cutOver },
    ]),
    outcomeFailure: (o) => {
      if (blocked(o)) return 'negative_abort_cleanup_unproven';
      if (!ended(o) || cutOver(o) || !cleaned(o)) return 'truthful_abort_missing';
      return null;
    },
  });
}

export async function runScenario(page, scenario, faultControl, usedCases, maximumNewTokens) {
  const promptCanary = `A6-BROWSER-PRIVATE-PROMPT-${scenario}-${Date.now()}`;
  const outputCanary = `A6-BROWSER-PRIVATE-OUTPUT-${scenario}-${Date.now()}`;
  const privateCanaries = [promptCanary, outputCanary];
  const started = await startRequest(page, privateCanaries, maximumNewTokens);
  await sendControlledFault(faultControl, usedCases);
  const expect = scenarioExpectations(scenario, started.baseline);
  const failed = await waitForObservation(
    page,
    expect.failureObserved,
    { label: 'recovering_or_blocked', abort: expect.aborts },
  );
  const beforeCutoverReconnect = await exerciseReconnect(
    page,
    () => waitForObservation(
      page,
      (observation) => observation.cursor >= failed.observation.cursor,
      { label: 'cursor_before_cutover' },
    ),
  );

  await page.reload({ waitUntil: 'domcontentloaded' });
  const afterReload = await waitForObservation(
    page,
    expect.outcomeReached,
    { label: 'attempt_or_blocked', abort: expect.aborts },
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
    { label: 'terminal' },
  );
  await verifyInferenceOutcome(page, scenario);
  const afterCutoverReconnect = await exerciseReconnect(
    page,
    () => waitForObservation(
      page,
      (observation) => observation.cursor >= finished.observation.cursor
        && observation.terminal_count >= finished.observation.terminal_count,
      { label: 'cursor_after_terminal' },
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
  const outcome = expect.outcomeFailure(finished.observation);
  if (outcome !== null) fail(outcome);
  if (scenario === 'positive' && !validation.recovery_observed) fail('positive_recovery_missing');
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

export function singleEngineReceipt(engine, positive, negative, caseIds = []) {
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
    case_ids: caseIds,
    scenario_passes: Object.fromEntries(REQUIRED_SCENARIOS.map((scenario) => [scenario, true])),
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

export async function runSingleEngine({ engine, origin, outputPath, faultControl, maximumNewTokens }) {
  if (!ENGINES.includes(engine)) fail('browser_engine_selection_required');
  exactLoopbackOrigin(origin);
  if (!validatedControls.has(faultControl?.positive) || !validatedControls.has(faultControl?.negative)) {
    fail('fault_control_unvalidated');
  }
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
    const report = singleEngineReceipt(engine, positive, negative,
      [faultControl.positive.caseId, faultControl.negative.caseId]);
    if (outputPath) await atomicPrivateWrite(outputPath, jsonLine(report));
    return report;
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
  // No standalone CLI can attest to a receiver it did not construct and tear down.
  // Matrix.runLiveMatrix supplies the receiver-owned runtime instead.
  void faultControl; void maximumNewTokens;
  fail('receiver_runtime_factory_required');
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : 'a6_browser_gate_failed'}\n`);
    process.exitCode = 1;
  });
}
