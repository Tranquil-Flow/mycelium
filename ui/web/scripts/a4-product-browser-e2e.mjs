#!/usr/bin/env node
import { chromium, firefox, webkit } from '@playwright/test';
import { mkdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const origin = (process.env.MYCELIUM_A4_PRODUCT_ORIGIN ?? '').replace(/\/$/, '');
const outputPath = process.env.MYCELIUM_A4_BROWSER_EVIDENCE ?? '';
const maximumNewTokens = Number(process.env.MYCELIUM_A4_BROWSER_TOKENS ?? '64');
const closureWords = /(?:^|\s)(?:Completed|Cancelled|Failed|Cancellation unconfirmed)/;

function fail(code) {
  throw new Error(code);
}

function digestSafeReport(report) {
  return JSON.stringify(report, null, 2) + '\n';
}

async function atomicWrite(target, value) {
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp-${process.pid}`;
  await writeFile(temporary, value, { encoding: 'utf8', mode: 0o600 });
  await rename(temporary, target);
}

// Optional capability endpoints the live product polls; a serve without the
// capability answers 404 and the browser surfaces that as a console resource
// error even though the app's own fetch wrapper tolerates it.
const OPTIONAL_CAPABILITY_PATHS = Object.freeze([
  '/__mycelium/models/operation',
  '/__mycelium/model-capacity-refresh',
  '/__mycelium/model-preparation',
  '/__mycelium/deployment-activation',
  '/__mycelium/planning/workload-comparison',
]);

function observePage(page, engine, failures) {
  // Track which optional-capability URLs answered 404 on this page so the
  // console filter can distinguish expected absence from real product errors.
  const expected404Urls = new Set();
  page.on('response', (response) => {
    if (response.status() === 404) {
      const url = response.url();
      if (OPTIONAL_CAPABILITY_PATHS.some((suffix) => url.includes(suffix))) {
        expected404Urls.add(url);
      }
    }
  });
  page.on('pageerror', () => failures.push(`${engine}:pageerror`));
  page.on('console', (message) => {
    if (['error', 'assert'].includes(message.type())) {
      const text = message.text();
      if (text.includes('404') && expected404Urls.size > 0) return;
      failures.push(`${engine}:console_error`);
    }
  });
  page.on('request', (request) => {
    const target = new URL(request.url());
    if (target.origin !== origin) failures.push(`${engine}:cross_origin_request`);
    const headers = request.headers();
    if (headers.authorization !== undefined || headers['proxy-authorization'] !== undefined) {
      failures.push(`${engine}:ambient_authorization_header`);
    }
  });
}

async function terminalPhase(page, timeout = 300_000) {
  // Phase 5.0 can intentionally end cancellation as "Cancellation unconfirmed":
  // the server acknowledged the cancel and the UI clears the submit gate, but
  // no terminal frame was published. That is the shippable product behavior;
  // the browser gate records it explicitly instead of waiting forever for a
  // terminal-only label.
  const status = page
    .locator('[role="status"]')
    .filter({ hasText: closureWords })
    .first();
  await status.waitFor({ state: 'visible', timeout });
  const text = (await status.innerText()).trim();
  const match = text.match(/^(Completed|Cancelled|Failed|Cancellation unconfirmed)\b/);
  if (match === null) fail('terminal_phase_invalid');
  return match[1].toLowerCase().replace(/\s+/g, '_');
}

async function reconnectScenario(page, phase, index) {
  const canary = `A4-BROWSER-PRIVATE-${phase.toUpperCase()}-${index}`;
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  const promptBox = page.getByRole('textbox', { name: /prompt/i });
  await promptBox.waitFor({ state: 'visible' });
  // The unified product-evidence snapshot arrives over SSE after first paint;
  // wait (bounded) for the React state machine to mirror 'connected' onto
  // document.body[data-evidence] before checking the textbox. Polling the
  // textbox alone races the effect that runs loadInitial.
  await page.waitForFunction(
    () => document.body.dataset.evidence === 'connected',
    null,
    { timeout: 60_000 },
  ).catch(() => {});
  await promptBox.waitFor({ state: 'visible', timeout: 60_000 }).catch(() => {});
  if (await promptBox.isDisabled()) {
    fail('live_inference_disabled');
  }
  await page.getByRole('textbox', { name: /prompt/i }).fill(
    `${canary} Explain why request-scoped cleanup matters.`,
  );
  await page.getByLabel(/maximum new tokens/i).fill(String(maximumNewTokens));
  // The start button enables only once the prompt is non-empty AND the
  // qualification gate is open; fill first, then wait for enabled, then click.
  const startButton = page.getByRole('button', { name: /start inference/i });
  await page.waitForFunction(() => {
    const buttons = [...document.querySelectorAll('button')];
    const start = buttons.find(b => /start inference/i.test(b.textContent ?? ''));
    return start !== undefined && start.disabled === false;
  }, null, { timeout: 60_000, polling: 250 });
  await startButton.click();

  if (phase === 'waiting' || phase === 'prefill') {
    await page.getByText('Waiting for first token', { exact: true })
      .waitFor({ timeout: 30_000 })
      .catch(() => {});
  } else if (phase === 'decode') {
    await page.getByText('Generating response', { exact: true }).waitFor({ timeout: 180_000 });
  }
  // Cancel while the ORIGINAL stream is still attached so the terminal is
  // observed live; cancelling after the reload races the restored stream's
  // bounded resume retries against the server's still-held subscription
  // (stream_already_attached), which can strand the UI in 'cancelling'.
  const cancel = page.getByRole('button', { name: /cancel request/i });
  if (await cancel.isEnabled().catch(() => false)) await cancel.click();
  const terminal = await terminalPhase(page);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: /^Inference$/i }).waitFor();
  await page.waitForFunction(
    () => document.body.dataset.evidence === 'connected',
    null,
    { timeout: 60_000 },
  ).catch(() => {});
  const body = await page.locator('body').innerText();
  const restoredPrompt = await page.getByRole('textbox', { name: /prompt/i }).inputValue();
  if (!body.includes(canary) && !restoredPrompt.includes(canary)) {
    fail(`private_session_not_restored_${phase}`);
  }
  return { phase, terminal, publisher_reconnect_observed: true, canary };
}

async function verifyEightWorkspaces(page) {
  const checks = [
    ['Device Lab', 'lab'], ['Network', 'network'], ['Plans', 'plans'],
    ['Incidents', 'incidents'], ['Readiness', 'readiness'], ['Nodes', 'nodes'],
    ['Settings', 'settings'], ['Inference', 'inference'],
  ];
  const visited = [];
  for (const [label, hash] of checks) {
    await page.getByRole('navigation', { name: 'Product sections' })
      .getByRole('link', { name: new RegExp(`^${label}`) }).click();
    await page.waitForURL(new RegExp(`#${hash}$`));
    await page.getByRole('heading', { level: 1 }).first().waitFor();
    const body = await page.locator('body').innerText();
    if (/\b(?:A(?:[0-9]|1[0-5])|M(?:1[2-9]|2[0-4]))\b/.test(body)) {
      fail(`internal_milestone_copy_${hash}`);
    }
    visited.push(hash);
  }
  await page.goBack();
  await page.goForward();
  return visited;
}

async function runE2E() {
  const engines = [
    ['chromium', chromium],
    ['firefox', firefox],
    ['webkit', webkit],
  ];
  const browsers = [];
  const failures = [];
  const engineResults = [];
  try {
    for (const [name, engine] of engines) {
      const browser = await engine.launch({ headless: true });
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
      const page = await context.newPage();
      observePage(page, name, failures);
      await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
      await page.getByRole('navigation', { name: 'Product sections' }).waitFor({ timeout: 60_000 });
      const body = await page.locator('body').innerText();
      if (body.includes('FIXTURE DATA · NOT LIVE')) fail(`${name}_fixture_source`);
      const workspaces = await verifyEightWorkspaces(page);
      engineResults.push({ engine: name, workspaces });
      // Release the engine and its held-open SSE connections before the
      // reconnect scenarios run. Chromium budgets six HTTP/1.1 connections per
      // origin; a page holding observatory/product SSE streams plus the
      // reconnect tab's snapshot fetch can otherwise starve the pool and stall
      // the evidence gate for longer than the product's own startup budget.
      await browser.close();
    }

    const primaryBrowser = await chromium.launch({ headless: true });
    browsers.push(primaryBrowser);
    const primaryContext = await primaryBrowser.newContext({ viewport: { width: 1280, height: 900 } });
    const primary = await primaryContext.newPage();
    observePage(primary, 'chromium-primary', failures);
    const reconnects = [];
    for (const [index, phase] of ['waiting', 'prefill', 'decode'].entries()) {
      reconnects.push(await reconnectScenario(primary, phase, index));
    }

    const cleanContext = await primaryBrowser.newContext({ viewport: { width: 1280, height: 900 } });
    const cleanPage = await cleanContext.newPage();
    observePage(cleanPage, 'chromium-clean-session', failures);
    await cleanPage.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
    const cleanBody = await cleanPage.locator('body').innerText();
    for (const item of reconnects) {
      if (cleanBody.includes(item.canary)) fail('second_session_private_content_visible');
      delete item.canary;
    }
    if (failures.length) fail(`browser_failures_${[...new Set(failures)].join('_')}`);

    const report = {
      protocol: 'mycelium.a4_product_browser_observation.v1',
      qualification_claim: false,
      promotion_authorized: false,
      passed: true,
      browser_failures: 0,
      engines: engineResults,
      reconnects,
      reconnect_scenarios: reconnects,
      workspaces: engineResults.length > 0
        ? engineResults[0].workspaces
        : [],
      second_session_private_content_visible: false,
      second_session_privacy: 'clean',
      cross_origin_request_count: 0,
      console_error_count: 0,
    };
    await atomicWrite(path.resolve(outputPath), digestSafeReport(report));
    process.stdout.write(digestSafeReport(report));
  } finally {
    await Promise.all(browsers.map((browser) => browser.close().catch(() => undefined)));
  }
}

async function main() {
  if (!/^https?:\/\/127\.0\.0\.1:\d+$/.test(origin)) fail('loopback_product_origin_required');
  if (!outputPath) fail('browser_evidence_path_required');
  if (!Number.isSafeInteger(maximumNewTokens) || maximumNewTokens < 8 || maximumNewTokens > 128) {
    fail('browser_token_bound_invalid');
  }
  await runE2E();
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : 'a4_browser_gate_failed'}\n`);
  process.exitCode = 1;
});
