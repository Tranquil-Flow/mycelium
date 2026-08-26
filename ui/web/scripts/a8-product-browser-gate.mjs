#!/usr/bin/env node
import { writeFile } from 'node:fs/promises';
import process from 'node:process';
import readline from 'node:readline/promises';

import { chromium } from '@playwright/test';
import {
  canonicalTransportReportDigest,
  loadBrowserEvidenceAuthority,
  loadBrowserEvidenceSigner,
  signBrowserObservation,
} from './a8-browser-evidence.mjs';

const ROUTES = Object.freeze([
  ['inference', 'Inference path'],
  ['lab', 'Internet-native bootstrap'],
  ['network', 'Internet activation path (Network)'],
  ['nodes', 'Internet member state (Nodes)'],
  ['plans', 'Internet-native plan path costs'],
  ['readiness', 'Internet-native readiness'],
  ['incidents', 'Internet-native incidents'],
  ['settings', 'Internet-native settings'],
]);

function fail(code) {
  throw new Error(code);
}

function browserContextOptions() {
  const username = process.env.A8_BROWSER_HTTP_USERNAME;
  const password = process.env.A8_BROWSER_HTTP_PASSWORD;
  if (Boolean(username) !== Boolean(password)) fail('browser_http_credentials_incomplete');
  if (!username) return {};
  return { httpCredentials: { username, password } };
}

function usage() {
  process.stdout.write(`Usage: node scripts/a8-product-browser-gate.mjs \\
  --origin https://product.example \\
  --output /private/evidence/browser-report.json \\
  --evidence-signing-key /private/browser-ed25519.key \\
  --browser-authority /private/browser-authority.json \\
  --transport-output /private/evidence/signed-transport.json [...] \\
  [--prompt "A8 browser qualification"] \\
  [--forbidden value ...] [--headed]\n\nThe runner-issued authority fixes request_count. When it is greater than one, the\ncollector pauses on stdin after each request except the last. Change the physical\npath, wait for fresh signed sidecar evidence, then press Enter. Supply exactly one\n--transport-output per authorized request.\n`);
}

function parseArgs(argv) {
  const result = {
    origin: null,
    output: null,
    evidenceSigningKey: null,
    browserAuthority: null,
    transportOutputs: [],
    prompt: 'A8 ordinary browser qualification request',
    forbidden: [],
    headed: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--help' || argument === '-h') return { help: true };
    if (argument === '--headed') {
      result.headed = true;
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined) fail(`missing_value:${argument}`);
    index += 1;
    if (argument === '--origin') result.origin = value;
    else if (argument === '--output') result.output = value;
    else if (argument === '--evidence-signing-key') result.evidenceSigningKey = value;
    else if (argument === '--browser-authority') result.browserAuthority = value;
    else if (argument === '--transport-output') result.transportOutputs.push(value);
    else if (argument === '--prompt') result.prompt = value;
    else if (argument === '--forbidden') result.forbidden.push(value);
    else fail(`unknown_argument:${argument}`);
  }
  if (result.origin === null || result.output === null) fail('origin_and_output_required');
  if (result.evidenceSigningKey === null) fail('evidence_signing_key_required');
  if (result.browserAuthority === null) fail('browser_authority_required');
  const parsed = new URL(result.origin);
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    fail('canonical_https_product_origin_required');
  }
  result.origin = parsed.origin;

  const outputPaths = [result.output, ...result.transportOutputs];
  if (new Set(outputPaths).size !== outputPaths.length) fail('output_paths_not_unique');
  if (!result.prompt) fail('prompt_invalid');
  return result;
}

function captureFailures(page, failures, label) {
  page.on('pageerror', (error) => failures.push(`${label}:pageerror:${error.message}`));
  page.on('requestfailed', (request) => {
    const errorText = request.failure()?.errorText ?? 'unknown';
    if (errorText === 'net::ERR_ABORTED') return;
    failures.push(`${label}:requestfailed:${request.url()}:${errorText}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 500) failures.push(`${label}:http_${response.status()}:${response.url()}`);
  });
}

async function expectProjection(page, routeId, projectionName, forbidden) {
  await page.goto(`${page.url().split('/#')[0]}/#${routeId}`, { waitUntil: 'domcontentloaded' });
  const projection = page.getByRole('region', { name: projectionName });
  await projection.waitFor({ state: 'visible', timeout: 60_000 });
  const text = await page.locator('body').innerText();
  for (const needle of forbidden) {
    if (needle && text.includes(needle)) fail(`privacy_needle_rendered:${routeId}`);
  }
}

async function productSnapshot(page) {
  return page.evaluate(async () => {
    const response = await fetch('/api/v1/product/snapshot', {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body?.error ?? `product_snapshot_${response.status}`);
    return body;
  });
}

async function signedTransportReport(page) {
  return page.evaluate(async () => {
    const response = await fetch('/__mycelium/swarm/resource-observations', {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const body = await response.text();
    if (!response.ok) throw new Error(`transport_report_${response.status}`);
    return body;
  });
}

function transportContainsActivation(rawReport, activation) {
  let report;
  try {
    report = JSON.parse(rawReport);
  } catch {
    return false;
  }
  if (report?.protocol !== 'mycelium.live_swarm_resource_observations.v1') return false;
  return (report.signed_snapshots ?? []).some((envelope) =>
    (envelope?.observation?.details?.transport?.transport_path_observations ?? []).some((path) =>
      path.measured_at_unix_ms === activation?.observed_at_unix_ms
      && path.connection_generation === activation?.connection_generation
      && path.path_class === activation?.path_class));
}

async function synchronizedEvidence(page) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const snapshot = await productSnapshot(page);
    const transportReport = await signedTransportReport(page);
    const activation = snapshot?.internet_native?.activation_observation;
    if (transportContainsActivation(transportReport, activation)) {
      return { snapshot, transportReport };
    }
    await page.waitForTimeout(100);
  }
  fail('transport_projection_not_synchronized');
}

async function runInference(page, prompt, sequence) {
  await page.goto(`${page.url().split('/#')[0]}/#inference`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('region', { name: 'Inference path' }).waitFor({ timeout: 60_000 });
  const textarea = page.getByRole('textbox', { name: 'Prompt' });
  await textarea.fill(`${prompt} #${sequence}`);
  const submit = page.getByRole('button', { name: 'Start inference' });
  await submit.waitFor({ state: 'visible', timeout: 60_000 });
  const acceptedResponse = page.waitForResponse((response) => {
    const request = response.request();
    return request.method() === 'POST' && new URL(response.url()).pathname === '/api/v1/inference';
  }, { timeout: 60_000 });
  try {
    await submit.click({ timeout: 60_000 });
  } catch {
    const reason = await page.locator('#submit-reason').innerText();
    fail(`inference_blocked:${reason}`);
  }
  const response = await acceptedResponse;
  if (!response.ok()) fail(`inference_submit_${response.status()}`);
  const accepted = await response.json();
  if (
    typeof accepted?.request_id !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(accepted.request_id)
  ) {
    fail('inference_request_id_invalid');
  }
  await page.getByRole('status').getByText('Completed', { exact: true }).waitFor({ timeout: 300_000 });
  return accepted.request_id;
}

async function assertNavigationReconstruction(page, forbidden) {
  await expectProjection(page, 'network', 'Internet activation path (Network)', forbidden);
  await expectProjection(page, 'settings', 'Internet-native settings', forbidden);
  await page.goBack({ waitUntil: 'domcontentloaded' });
  await page.getByRole('region', { name: 'Internet activation path (Network)' }).waitFor({ timeout: 60_000 });
  await page.goForward({ waitUntil: 'domcontentloaded' });
  await page.getByRole('region', { name: 'Internet-native settings' }).waitFor({ timeout: 60_000 });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByRole('region', { name: 'Internet-native settings' }).waitFor({ timeout: 60_000 });
}

async function assertCleanSecondSession(browser, contextOptions, origin, forbidden, failures) {
  const context = await browser.newContext(contextOptions);
  const page = await context.newPage();
  captureFailures(page, failures, 'second-session');
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('region', { name: 'Inference path' }).waitFor({ timeout: 60_000 });
  const prompt = await page.getByRole('textbox', { name: 'Prompt' }).inputValue();
  if (prompt !== '') fail('second_session_prompt_leaked');
  const storage = await page.evaluate(() => ({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  const rendered = `${await page.locator('body').innerText()}\n${JSON.stringify(storage)}`;
  for (const needle of forbidden) {
    if (needle && rendered.includes(needle)) fail('second_session_privacy_needle_rendered');
  }
  await context.close();
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    usage();
    return;
  }
  const failures = [];
  const evidenceSigner = await loadBrowserEvidenceSigner(options.evidenceSigningKey);
  const authority = await loadBrowserEvidenceAuthority(options.browserAuthority, evidenceSigner);
  if (authority.origin !== options.origin) fail('browser_authority_origin_mismatch');
  if (options.transportOutputs.length !== authority.request_count) {
    fail('transport_output_count_mismatch');
  }
  const contextOptions = browserContextOptions();
  const browser = await chromium.launch({ headless: !options.headed });
  const context = await browser.newContext(contextOptions);
  const page = await context.newPage();
  captureFailures(page, failures, 'primary-session');
  const terminalStates = [];
  const requestIds = [];
  const activationHistoryById = new Map();
  const transportReports = [];
  let snapshot = null;
  const input = readline.createInterface({ input: process.stdin, output: process.stderr });
  try {
    await page.goto(`${options.origin}/#inference`, { waitUntil: 'domcontentloaded' });
    for (const [routeId, projectionName] of ROUTES) {
      await expectProjection(page, routeId, projectionName, options.forbidden);
    }
    await assertNavigationReconstruction(page, options.forbidden);
    for (let sequence = 1; sequence <= authority.request_count; sequence += 1) {
      requestIds.push(await runInference(page, options.prompt, sequence));
      terminalStates.push('completed');
      const evidence = await synchronizedEvidence(page);
      snapshot = evidence.snapshot;
      transportReports.push(evidence.transportReport);
      const observation = snapshot.internet_native?.activation_observation;
      if (!observation?.observation_id) fail('activation_observation_invalid');
      activationHistoryById.delete(observation.observation_id);
      activationHistoryById.set(observation.observation_id, observation);
      if (sequence < authority.request_count) {
        const retainedFailureCount = failures.length;
        process.stdout.write(`${JSON.stringify({ protocol: 'mycelium.a8_browser_transition_pause.v1', completed_requests: sequence })}\n`);
        await input.question('Change physical path, wait for fresh evidence, then press Enter: ');
        failures.splice(retainedFailureCount);
      }
    }
    await assertCleanSecondSession(
      browser, contextOptions, options.origin, options.forbidden, failures,
    );
    if (failures.length > 0) fail(`browser_failures:${failures.join('|')}`);
    if (snapshot?.protocol !== 'mycelium.product_snapshot.v1' || !snapshot.internet_native) {
      fail('product_snapshot_invalid');
    }
    const publicProjection = {
      activation_observation: snapshot.internet_native.activation_observation,
      activation_history: Array.from(activationHistoryById.values()),
      relay_projection: snapshot.internet_native.relay_projection,
    };
    const serialized = JSON.stringify(publicProjection);
    for (const needle of options.forbidden) {
      if (needle && serialized.includes(needle)) fail('privacy_needle_projected');
    }
    const observation = {
      protocol: 'mycelium.a8_product_browser_observation.v2',
      challenge_id: authority.challenge_id,
      case_id: authority.case_id,
      origin: authority.origin,
      deployment_id: authority.deployment_id,
      spec_digest: authority.spec_digest,
      source_digest: authority.source_digest,
      observed_at_unix_ms: Date.now(),
      passed: true,
      browser_failures: 0,
      completed_requests: terminalStates.length,
      request_ids: requestIds,
      terminal_states: terminalStates,
      transport_report_digests: transportReports.map((rawReport) => {
        // The live endpoint emits Python's canonical evidence JSON. Hash those
      // exact bytes: JSON.parse would collapse integral floats such as 0.0
      // to 0 and produce a digest that the Python verifier cannot reproduce.
        return canonicalTransportReportDigest(rawReport);
      }),
      workspaces: ROUTES.map(([routeId]) => routeId),
      public_projection: publicProjection,
    };
    const report = signBrowserObservation(observation, evidenceSigner);
    for (let index = 0; index < options.transportOutputs.length; index += 1) {
      await writeFile(
        options.transportOutputs[index],
        `${transportReports[index].trimEnd()}\n`,
        { mode: 0o600, flag: 'wx' },
      );
    }
    await writeFile(options.output, `${JSON.stringify(report, null, 2)}\n`, {
      mode: 0o600,
      flag: 'wx',
    });
    process.stdout.write(`${JSON.stringify({ protocol: 'mycelium.a8_product_browser_observation_written.v2', challenge_id: authority.challenge_id, output: options.output, completed_requests: terminalStates.length })}\n`);
  } finally {
    input.close();
    await context.close();
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
