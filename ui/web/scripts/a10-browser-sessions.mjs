// SPDX-License-Identifier: AGPL-3.0-only
// A10 gate 1: two clean browser sessions submit the frozen workload.
//
// Specification §10, physical positive gate 1: "At least two clean browser
// sessions submit the frozen concurrent and late-arrival workload through the
// ordinary gateway on one qualified multi-stage route."  This harness is the
// automated stand-in for the owner driving two browsers by hand.  It drives an
// already running loopback product origin (the ordinary `mycelium_demo serve
// --mode live` of the A10 run day) through the real operator UI:
//
//   * two independent browser contexts, each a fresh profile (no cookie, no
//     storage) that bootstraps its own product-gateway session;
//   * every request is typed into the Inference workspace and submitted with
//     the "Start inference" button, and the POST /api/v1/inference body the UI
//     sends is checked against the frozen workload (prompt, token bound, QoS);
//   * round "concurrent": both sessions submit together and both requests are
//     observed active at the same time;
//   * round "late_arrival": session a submits, and session b submits only once
//     session a is decoding (at least one token applied) and is still active;
//   * every request must reach "Completed" in its own tab;
//   * neither session, nor a third clean session, may show the other session's
//     prompt or decoded output; all requests are same-origin, the only
//     mutating request is POST /api/v1/inference, and no page error occurs.
//
// The report records request pseudonyms (`mycelium.a10.request:` SHA-256, the
// same rule as mycelium_router.a10_live_publication.pseudonym) so the gate can
// find the runtime's own stage-compute and batch records for these requests in
// the live status.  No prompt, decoded text, token, cookie or request ID is
// recorded.  It never authors route evidence; `live_evidence` is true only in
// live mode with every check passed, and it never makes a qualification claim.
// Run one engine per invocation: --engine=chromium|firefox|webkit.
// `--preflight` checks configuration and the engine executable without any
// origin or browser contact; `--plan` prints the contact-free plan.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

export const ENGINES = Object.freeze(['chromium', 'firefox', 'webkit']);
export const PROTOCOL = 'mycelium.a10_browser_sessions.v1';
export const WORKLOAD_PROTOCOL = 'mycelium.a10_browser_workload.v1';
export const MODES = Object.freeze(['live', 'local_conformance']);
export const REQUIRED_CHECKS = Object.freeze([
  'live_status_preflight',
  'fresh_independent_sessions',
  'concurrent_round_submitted_through_ui',
  'concurrent_overlap_observed',
  'late_arrival_round_submitted_through_ui',
  'late_arrival_observed',
  'all_requests_completed',
  'cross_session_privacy',
  'clean_third_session_privacy',
  'same_origin_inference_only_mutations',
]);
const SESSIONS = Object.freeze(['a', 'b']);
const SUBMISSION_FIELDS = Object.freeze(['index', 'max_new_tokens', 'prompt', 'qos_class', 'session',
  'workload_profile_id']);
const TERMINAL = /^(Completed|Cancelled|Failed)$/;
const SUBMIT_TIMEOUT_MS = 120_000;
const FIRST_TOKEN_TIMEOUT_MS = 300_000;
const TERMINAL_TIMEOUT_MS = 600_000;

function fail(code) {
  throw new Error(code);
}

export function sha256Hex(text) {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

// json.dumps(value, sort_keys=True, separators=(",", ":")) for plain JSON values.
export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

// mycelium_router.a10_live_publication.pseudonym("request", request_id)
export function requestRef(requestId) {
  return `sha256:${sha256Hex(`mycelium.a10.request:${requestId}`)}`;
}

export function exactLoopbackOrigin(value) {
  if (typeof value !== 'string' || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(value)
    || Number(value.slice(value.lastIndexOf(':') + 1)) > 65535 || new URL(value).origin !== value) {
    fail('loopback_product_origin_required');
  }
  return value;
}

function exactKeys(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort());
}

function validSubmission(item, session) {
  return exactKeys(item, SUBMISSION_FIELDS) && item.session === session
    && Number.isSafeInteger(item.index) && item.index >= 0
    && typeof item.prompt === 'string' && item.prompt.startsWith(`A10 benchmark request ${item.index}:`)
    && Number.isSafeInteger(item.max_new_tokens) && item.max_new_tokens >= 1 && item.max_new_tokens <= 256
    && item.qos_class === 'interactive' && item.workload_profile_id === 'interactive_chat_v1';
}

// The frozen workload is authored by the Python gate (single source of truth);
// the harness accepts only its exact closed shape and verifies its digest.
export function validateWorkload(document) {
  if (!exactKeys(document, ['protocol', 'rounds', 'sessions', 'workload_digest'])
    || document.protocol !== WORKLOAD_PROTOCOL
    || JSON.stringify(document.sessions) !== JSON.stringify(SESSIONS)
    || !Array.isArray(document.rounds) || document.rounds.length !== 2
    || document.rounds[0]?.round !== 'concurrent' || document.rounds[1]?.round !== 'late_arrival'
    || !document.rounds.every((round) => exactKeys(round, ['round', 'submissions'])
      && Array.isArray(round.submissions) && round.submissions.length === 2
      && validSubmission(round.submissions[0], 'a') && validSubmission(round.submissions[1], 'b'))) {
    fail('frozen_workload_shape_invalid');
  }
  const indexes = document.rounds.flatMap((round) => round.submissions.map((item) => item.index));
  if (new Set(indexes).size !== indexes.length) fail('frozen_workload_shape_invalid');
  const { workload_digest: digest, ...body } = document;
  if (digest !== `sha256:${sha256Hex(canonicalJson(body))}`) fail('frozen_workload_digest_mismatch');
  return document;
}

// The whole configuration is read and validated before any browser or origin contact.
export function validateConfig(env, engine) {
  if (!ENGINES.includes(engine)) fail('browser_engine_selection_required');
  const origin = exactLoopbackOrigin(env.A10_BROWSER_ORIGIN);
  const evidence = env.A10_BROWSER_EVIDENCE;
  if (typeof evidence !== 'string' || !path.isAbsolute(evidence)) fail('evidence_directory_required');
  if (existsSync(evidence)) fail('evidence_directory_exists');
  const mode = env.A10_BROWSER_MODE === undefined ? 'live' : env.A10_BROWSER_MODE;
  if (!MODES.includes(mode)) fail('browser_mode_invalid');
  const workloadPath = env.A10_BROWSER_WORKLOAD;
  if (typeof workloadPath !== 'string' || !path.isAbsolute(workloadPath) || !existsSync(workloadPath)) {
    fail('frozen_workload_required');
  }
  let workload;
  try {
    workload = JSON.parse(readFileSync(workloadPath, 'utf8'));
  } catch {
    fail('frozen_workload_shape_invalid');
  }
  return Object.freeze({ engine, origin, evidence, mode, workload: validateWorkload(workload) });
}

export function assertLiveStatus(status, mode) {
  if (status === null || typeof status !== 'object') fail('live_status_invalid');
  if (status.route_alive !== true) fail('route_not_alive');
  if (mode === 'live' && status.simulated !== false) fail('physical_route_required');
  if (mode === 'local_conformance' && status.simulated !== true) fail('local_conformance_must_be_simulated');
  if (typeof status.deployment_id !== 'string' || status.deployment_id.length === 0) {
    fail('deployment_binding_missing');
  }
  return status;
}

export function assertFrozenSubmission(body, frozen) {
  if (body === null || typeof body !== 'object' || body.prompt !== frozen.prompt
    || body.max_new_tokens !== frozen.max_new_tokens || body.qos_class !== frozen.qos_class
    || body.workload_profile_id !== frozen.workload_profile_id) {
    fail('browser_submission_not_frozen_workload');
  }
  return true;
}

// Both requests were accepted and, after both acceptances, neither tab is terminal.
export function assertConcurrentOverlap(phases) {
  if (Object.values(phases).some((phase) => typeof phase !== 'string' || TERMINAL.test(phase))) {
    fail('concurrent_overlap_not_observed');
  }
  return true;
}

// The late request was submitted after the leader applied a token and was
// accepted while the leader was still active.
export function assertLateArrival({ leaderTokensBefore, leaderPhaseAfterAccept }) {
  if (!(Number.isSafeInteger(leaderTokensBefore) && leaderTokensBefore >= 1)
    || typeof leaderPhaseAfterAccept !== 'string' || TERMINAL.test(leaderPhaseAfterAccept)) {
    fail('late_arrival_not_observed');
  }
  return true;
}

// Text this session typed or decoded itself is never attributed to the other
// session (a model may decode identical text for two frozen prompts).
export function assertSessionPrivacy(text, own, other) {
  if (typeof text !== 'string') fail('privacy_input_invalid');
  for (const prompt of other.prompts) {
    if (!own.prompts.includes(prompt) && text.includes(prompt)) fail('cross_session_prompt_visible');
  }
  const mine = own.outputs.map((item) => item.trim());
  for (const output of other.outputs) {
    const value = typeof output === 'string' ? output.trim() : '';
    if (value.length >= 8 && !mine.some((item) => item.includes(value)) && text.includes(value)) {
      fail('cross_session_output_visible');
    }
  }
  return true;
}

export function harnessPlan() {
  return {
    protocol: PROTOCOL,
    mode: 'plan',
    engines: [...ENGINES],
    independent_sessions: SESSIONS.length,
    rounds: ['concurrent', 'late_arrival'],
    required_checks: [...REQUIRED_CHECKS],
    required_environment: ['A10_BROWSER_ORIGIN', 'A10_BROWSER_EVIDENCE', 'A10_BROWSER_WORKLOAD'],
    optional_environment: ['A10_BROWSER_MODE'],
    browser_contact_attempted: false,
    origin_contact_attempted: false,
    live_evidence: false,
    qualification_claim: false,
  };
}

async function liveStatus(origin) {
  const response = await fetch(`${origin}/__mycelium/live-status`, {
    headers: { Accept: 'application/json' }, redirect: 'error', cache: 'no-store',
  });
  if (!response.ok) fail(`live_status_${response.status}`);
  return response.json();
}

// Visible per-tab inference state: phase label and applied token count.
async function tabState(page) {
  const status = page.locator('section[aria-labelledby="output-title"] [role="status"]')
    .filter({ hasText: /tokens applied/ }).first();
  const phase = (await status.locator('strong').innerText()).trim();
  const tokens = Number((await status.locator('span').innerText()).replace(/[^0-9]/g, ''));
  return { phase, tokens: Number.isSafeInteger(tokens) ? tokens : -1 };
}

async function waitForState(page, predicate, timeout, code) {
  const deadline = Date.now() + timeout;
  for (;;) {
    let state = null;
    try {
      state = await tabState(page);
    } catch {
      state = null;
    }
    if (state !== null && predicate(state)) return state;
    if (Date.now() > deadline) fail(code);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

async function openInference(session, origin) {
  const { page } = session;
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('navigation', { name: 'Product sections' }).waitFor({ timeout: 60_000 });
  await page.waitForFunction(() => document.body.dataset.evidence === 'connected', null, { timeout: 60_000 });
}

async function prepare(session, frozen) {
  const { page } = session;
  const prompt = page.getByRole('textbox', { name: /prompt/i });
  await prompt.waitFor({ state: 'visible', timeout: 60_000 });
  await page.waitForFunction(() => {
    const area = document.querySelector('textarea');
    return area !== null && area.disabled === false;
  }, null, { timeout: SUBMIT_TIMEOUT_MS, polling: 100 });
  await prompt.fill(frozen.prompt);
  await page.getByLabel(/maximum new tokens/i).fill(String(frozen.max_new_tokens));
  const policy = page.getByLabel('Workload and QoS policy', { exact: true });
  if (await policy.count() === 1 && await policy.isEnabled()) {
    const values = await policy.locator('option').evaluateAll((items) => items.map((item) => item.value));
    if (values.includes(frozen.workload_profile_id)) await policy.selectOption(frozen.workload_profile_id);
  }
  const start = page.getByRole('button', { name: /start inference/i });
  await page.waitForFunction(() => {
    const button = [...document.querySelectorAll('button')]
      .find((item) => /start inference/i.test(item.textContent ?? ''));
    return button !== undefined && button.disabled === false;
  }, null, { timeout: SUBMIT_TIMEOUT_MS, polling: 100 });
  // Settle actionability now, so the concurrent clicks below start together.
  await start.click({ trial: true });
  return start;
}

// Click "Start inference" and capture the UI's own POST and the gateway's acceptance.
async function submit(session, start, frozen) {
  const { page } = session;
  const isSubmit = (request) => request.method() === 'POST'
    && new URL(request.url()).pathname === '/api/v1/inference';
  const posted = page.waitForRequest(isSubmit, { timeout: SUBMIT_TIMEOUT_MS });
  const answered = page.waitForResponse((response) => isSubmit(response.request()), { timeout: SUBMIT_TIMEOUT_MS });
  await start.click();
  const request = await posted;
  let body = null;
  try {
    body = request.postDataJSON();
  } catch {
    body = null;
  }
  assertFrozenSubmission(body, frozen);
  const response = await answered;
  if (!response.ok()) fail(`browser_inference_submit_${response.status()}`);
  const accepted = await response.json();
  if (typeof accepted.request_id !== 'string' || accepted.request_id.length === 0) {
    fail('browser_request_id_missing');
  }
  return { request_ref: requestRef(accepted.request_id), accepted_at_ms: Date.now() };
}

async function terminal(session, frozen, record) {
  const state = await waitForState(session.page, (item) => TERMINAL.test(item.phase), TERMINAL_TIMEOUT_MS,
    'browser_terminal_not_observed');
  record.terminal = state.phase.toLowerCase();
  record.tokens = state.tokens;
  record.terminal_observed_at_ms = Date.now();
  if (state.phase !== 'Completed') fail(`browser_request_not_completed:${record.terminal}`);
  if (!(state.tokens >= 1 && state.tokens <= frozen.max_new_tokens)) fail('browser_token_count_invalid');
  const output = session.page.getByRole('log', { name: 'Decoded output' });
  session.outputs.push((await output.innerText()).trim());
  return record;
}

function requestRecord(round, frozen, accepted) {
  return { session: frozen.session, round, index: frozen.index, request_ref: accepted.request_ref,
    max_new_tokens: frozen.max_new_tokens, qos_class: frozen.qos_class,
    accepted_at_ms: accepted.accepted_at_ms, submitted_through_ui: true, frozen_submission_verified: true };
}

async function runConcurrent(sessions, round, records) {
  const [a, b] = round.submissions;
  const [startA, startB] = await Promise.all([prepare(sessions.a, a), prepare(sessions.b, b)]);
  const [acceptedA, acceptedB] = await Promise.all([submit(sessions.a, startA, a), submit(sessions.b, startB, b)]);
  const phases = { a: (await tabState(sessions.a.page)).phase, b: (await tabState(sessions.b.page)).phase };
  assertConcurrentOverlap(phases);
  const recordA = requestRecord('concurrent', a, acceptedA);
  const recordB = requestRecord('concurrent', b, acceptedB);
  records.push(recordA, recordB);
  await Promise.all([terminal(sessions.a, a, recordA), terminal(sessions.b, b, recordB)]);
  // The acceptance skew bounds how close together the two arrivals were; a
  // runtime can batch them only if its collection window covers it.
  return { phases_after_both_accepted: phases,
    acceptance_skew_ms: Math.abs(acceptedA.accepted_at_ms - acceptedB.accepted_at_ms) };
}

async function runLateArrival(sessions, round, records) {
  const [leader, late] = round.submissions;
  const startLeader = await prepare(sessions.a, leader);
  const startLate = await prepare(sessions.b, late);
  const acceptedLeader = await submit(sessions.a, startLeader, leader);
  const decoding = await waitForState(sessions.a.page, (state) => state.tokens >= 1 || TERMINAL.test(state.phase),
    FIRST_TOKEN_TIMEOUT_MS, 'leader_first_token_not_observed');
  const firstTokenAt = Date.now();
  const acceptedLate = await submit(sessions.b, startLate, late);
  const leaderAfter = await tabState(sessions.a.page);
  assertLateArrival({ leaderTokensBefore: TERMINAL.test(decoding.phase) ? 0 : decoding.tokens,
    leaderPhaseAfterAccept: leaderAfter.phase });
  const recordLeader = { ...requestRecord('late_arrival', leader, acceptedLeader), first_token_observed_at_ms: firstTokenAt };
  const recordLate = { ...requestRecord('late_arrival', late, acceptedLate), arrived_while_leader_decoding: true };
  records.push(recordLeader, recordLate);
  await Promise.all([terminal(sessions.a, leader, recordLeader), terminal(sessions.b, late, recordLate)]);
  return { leader_tokens_before_late_submit: decoding.tokens, leader_phase_after_late_accept: leaderAfter.phase };
}

async function pageText(page) {
  const body = await page.locator('body').innerText();
  const inputs = await page.locator('textarea, input').evaluateAll((items) => items.map((item) => item.value).join('\n'));
  return `${body}\n${inputs}`;
}

export async function runSessions(config) {
  const playwright = await import('@playwright/test');
  await mkdir(config.evidence, { recursive: true, mode: 0o700 });
  const checks = [];
  const failures = [];
  const requests = [];
  const records = [];
  const rounds = {};
  let deploymentId = null;
  let browser;
  const prompts = (session) => config.workload.rounds.flatMap((round) => round.submissions)
    .filter((item) => item.session === session).map((item) => item.prompt.slice(0, item.prompt.indexOf(':') + 1));
  try {
    const status = assertLiveStatus(await liveStatus(config.origin), config.mode);
    deploymentId = status.deployment_id;
    checks.push('live_status_preflight');
    browser = await playwright[config.engine].launch({ headless: true });
    const instrument = async (page, label) => {
      page.on('pageerror', () => failures.push(`${label}:pageerror`));
      page.on('request', (request) => {
        const url = new URL(request.url());
        requests.push({ session: label, method: request.method(), path: url.pathname,
          same_origin: url.origin === config.origin });
      });
      await page.route('**/*', (route) => (new URL(route.request().url()).origin === config.origin
        ? route.continue() : route.abort()));
    };
    const sessions = {};
    for (const label of SESSIONS) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 },
        reducedMotion: 'reduce', serviceWorkers: 'block' });
      const fresh = (await context.cookies()).length === 0
        && JSON.stringify(await context.storageState()) === JSON.stringify({ cookies: [], origins: [] });
      if (!fresh) fail('browser_profile_not_fresh');
      const page = await context.newPage();
      await instrument(page, label);
      sessions[label] = { label, context, page, outputs: [] };
    }
    await Promise.all(SESSIONS.map((label) => openInference(sessions[label], config.origin)));
    const cookieDigests = [];
    for (const label of SESSIONS) {
      const cookies = await sessions[label].context.cookies(config.origin);
      if (cookies.length === 0) fail('browser_session_not_bootstrapped');
      cookieDigests.push(sha256Hex(canonicalJson(cookies.map((cookie) => [cookie.name, cookie.value]).sort())));
    }
    if (new Set(cookieDigests).size !== SESSIONS.length) fail('browser_sessions_not_independent');
    checks.push('fresh_independent_sessions');
    rounds.concurrent = await runConcurrent(sessions, config.workload.rounds[0], records);
    checks.push('concurrent_round_submitted_through_ui', 'concurrent_overlap_observed');
    rounds.late_arrival = await runLateArrival(sessions, config.workload.rounds[1], records);
    checks.push('late_arrival_round_submitted_through_ui', 'late_arrival_observed');
    if (records.length !== 4 || !records.every((record) => record.terminal === 'completed')) {
      fail('browser_requests_incomplete');
    }
    if (new Set(records.map((record) => record.request_ref)).size !== 4) fail('browser_request_refs_not_distinct');
    checks.push('all_requests_completed');
    for (const label of SESSIONS) {
      const other = SESSIONS.find((item) => item !== label);
      assertSessionPrivacy(await pageText(sessions[label].page),
        { prompts: prompts(label), outputs: sessions[label].outputs },
        { prompts: prompts(other), outputs: sessions[other].outputs });
      await sessions[label].page.screenshot({ path: path.join(config.evidence, `session-${label}.png`), fullPage: true });
    }
    checks.push('cross_session_privacy');
    const third = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce',
      serviceWorkers: 'block' });
    const clean = { label: 'clean', context: third, page: await third.newPage(), outputs: [] };
    await instrument(clean.page, 'clean');
    await openInference(clean, config.origin);
    const cleanText = await pageText(clean.page);
    for (const label of SESSIONS) {
      assertSessionPrivacy(cleanText, { prompts: [], outputs: [] },
        { prompts: prompts(label), outputs: sessions[label].outputs });
    }
    await third.close();
    checks.push('clean_third_session_privacy');
    for (const label of SESSIONS) await sessions[label].context.close();
    const mutating = requests.filter((request) => !['GET', 'HEAD'].includes(request.method));
    if (!requests.every((request) => request.same_origin)) fail('cross_origin_request');
    if (!mutating.every((request) => request.method === 'POST' && request.path === '/api/v1/inference')) {
      fail('unexpected_mutating_request');
    }
    if (mutating.length !== 4) fail('browser_submission_count_invalid');
    checks.push('same_origin_inference_only_mutations');
  } catch (error) {
    failures.push(error instanceof Error ? error.message.split('\n')[0].slice(0, 160) : 'a10_browser_sessions_failed');
  } finally {
    await browser?.close();
  }
  const passed = failures.length === 0
    && JSON.stringify(checks) === JSON.stringify(REQUIRED_CHECKS);
  const report = {
    protocol: PROTOCOL,
    mode: config.mode,
    engine: config.engine,
    origin: config.origin,
    deployment_id: deploymentId,
    workload_digest: config.workload.workload_digest,
    independent_sessions: SESSIONS.length,
    checks,
    failures,
    rounds,
    requests: records,
    request_count: requests.length,
    passed,
    live_evidence: passed && config.mode === 'live',
    qualification_claim: false,
    physical_claim: false,
  };
  await writeFile(path.join(config.evidence, 'browser-report.json'), `${JSON.stringify(report, null, 2)}\n`,
    { mode: 0o600 });
  return report;
}

async function preflight(config) {
  const playwright = await import('@playwright/test');
  const executable = playwright[config.engine].executablePath();
  if (typeof executable !== 'string' || !existsSync(executable)) fail('browser_engine_executable_missing');
  return { protocol: PROTOCOL, mode: 'preflight', engine: config.engine, browser_mode: config.mode,
    workload_digest: config.workload.workload_digest, engine_executable_present: true,
    browser_contact_attempted: false, origin_contact_attempted: false, live_evidence: false };
}

async function main(args) {
  if (args.length === 1 && args[0] === '--plan') {
    process.stdout.write(`${JSON.stringify(harnessPlan())}\n`);
    return;
  }
  const engineArg = args.find((item) => item.startsWith('--engine='));
  const rest = args.filter((item) => item !== engineArg);
  if (engineArg === undefined || rest.length > 1 || (rest.length === 1 && rest[0] !== '--preflight')) {
    fail('browser_engine_selection_required');
  }
  const config = validateConfig(process.env, engineArg.slice('--engine='.length));
  if (rest.length === 1) {
    process.stdout.write(`${JSON.stringify(await preflight(config))}\n`);
    return;
  }
  const report = await runSessions(config);
  process.stdout.write(`${JSON.stringify({ engine: report.engine, mode: report.mode, checks: report.checks,
    failures: report.failures, passed: report.passed, live_evidence: report.live_evidence })}\n`);
  if (!report.passed) process.exitCode = 1;
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : 'a10_browser_sessions_failed'}\n`);
    process.exitCode = 1;
  });
}
