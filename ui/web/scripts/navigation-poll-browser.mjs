#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Navigation x background-poll browser test (local conformance host only).
//
// Serves the BUILT production app (`npm run build:live`) through
// tests/a14_acceptance/serve_navigation_polls.py, which delays the app shell
// document and every /__mycelium poll response. Each iteration reloads and
// traverses Back/Forward across workspaces while polls run, exactly the
// navigation pattern of scripts/a14-live-browser.mjs, and fails on any
// `pageerror`. WebKit reports a fetch that a page starts after a navigation
// began ("Fetch API cannot load <url> due to access control checks.") as a
// pageerror; so does an unhandled poll rejection in every engine.
//
// A second check keeps the product honest while the page is live and visible:
// a failing poll (the host answers 404) must still surface as the existing
// degraded state ("Artifact acquisition status is unavailable").
//
//   node scripts/navigation-poll-browser.mjs --engine webkit [--iterations 20]
//
// Writes one JSON line per iteration and a final summary to stdout; exit 1 on
// any failure. Local conformance input only: no physical or qualification claim.
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const ENGINES = new Set(['chromium', 'firefox', 'webkit']);
const WORKSPACES = Object.freeze(['network', 'inference', 'nodes']);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

export function parseArgs(argv) {
  const options = { engine: null, iterations: 1, documentDelayMs: 1_500, pollDelayMs: 600 };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index + 1];
    if (argv[index] === '--engine') options.engine = value;
    else if (argv[index] === '--iterations') options.iterations = Number(value);
    else if (argv[index] === '--document-delay-ms') options.documentDelayMs = Number(value);
    else if (argv[index] === '--poll-delay-ms') options.pollDelayMs = Number(value);
    else throw new Error(`unknown_argument:${argv[index]}`);
    index += 1;
  }
  if (!ENGINES.has(options.engine)) throw new Error('engine_required');
  for (const key of ['iterations', 'documentDelayMs', 'pollDelayMs']) {
    if (!Number.isSafeInteger(options[key]) || options[key] < (key === 'iterations' ? 1 : 0)) {
      throw new Error(`invalid_${key}`);
    }
  }
  return options;
}

async function startHost({ documentDelayMs, pollDelayMs }) {
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1',
    A14_NAV_DOCUMENT_DELAY_MS: String(documentDelayMs), A14_NAV_POLL_DELAY_MS: String(pollDelayMs) };
  for (const key of ['PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV']) delete env[key];
  const python = process.env.NAV_POLL_PYTHON ?? '/opt/homebrew/bin/python3.14';
  const host = spawn(python, ['-B', 'tests/a14_acceptance/serve_navigation_polls.py'],
    { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  host.stderr.on('data', (chunk) => { stderr = (stderr + chunk.toString()).slice(-4_000); });
  const lines = createInterface({ input: host.stdout });
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error(`host_start_timeout:${stderr}`)), 20_000).unref());
  const [line] = await Promise.race([once(lines, 'line'), timeout]);
  const { origin } = JSON.parse(line);
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) throw new Error('host_origin_invalid');
  return { origin, close: () => host.kill('SIGTERM') };
}

async function iteration(playwright, browser, origin, index) {
  const { expect } = playwright;
  const started = Date.now();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 },
    reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage();
  let step = 'start';
  const pageErrors = [];
  const consoleMessages = [];
  const failedRequests = [];
  const at = () => Date.now() - started;
  page.on('pageerror', (error) => pageErrors.push({ t_ms: at(), step, message: error.message }));
  page.on('console', (message) => {
    if (['error', 'warning'].includes(message.type()) && consoleMessages.length < 50) {
      consoleMessages.push({ t_ms: at(), step, type: message.type(), text: message.text().slice(0, 240) });
    }
  });
  page.on('requestfailed', (request) => {
    if (failedRequests.length < 100) {
      failedRequests.push({ t_ms: at(), step, path: new URL(request.url()).pathname,
        failure: request.failure()?.errorText ?? null });
    }
  });
  // The A14 harness intercepts every request; keep the same composition.
  await page.route('**/*', (route) => (new URL(route.request().url()).origin === origin
    ? route.continue() : route.abort()));
  const active = (route) => page.locator(`nav a[href="#${route}"][aria-current="page"]`);
  let degradedVisible = false;
  try {
    for (const route of WORKSPACES) {
      step = `goto:${route}`;
      await page.goto(`${origin}/#${route}`);
      await expect(active(route)).toBeVisible({ timeout: 20_000 });
      await page.waitForTimeout(1_200); // let every background poll start
      step = `reload:${route}`;
      await page.reload();
      await expect(active(route)).toBeVisible({ timeout: 20_000 });
      if (route === 'inference') {
        step = 'live_degraded_state';
        await expect(page.getByRole('alert').filter({ hasText: 'Artifact acquisition status is unavailable' }))
          .toBeVisible({ timeout: 20_000 });
        degradedVisible = true;
      }
      const away = route === 'network' ? 'nodes' : 'network';
      step = `goto_away:${route}->${away}`;
      await page.goto(`${origin}/?nav=${index}#${away}`);
      await expect(active(away)).toBeVisible({ timeout: 20_000 });
      await page.waitForTimeout(600);
      step = `back:${route}`;
      await page.goBack();
      await expect(active(route)).toBeVisible({ timeout: 20_000 });
      await page.waitForTimeout(600);
      step = `forward:${route}`;
      await page.goForward();
      await expect(active(away)).toBeVisible({ timeout: 20_000 });
    }
    step = 'settle';
    await page.waitForTimeout(500);
  } finally {
    step = 'close';
    await context.close();
  }
  return { iteration: index, duration_ms: Date.now() - started, page_errors: pageErrors,
    degraded_state_visible: degradedVisible, console: consoleMessages, request_failures: failedRequests };
}

export async function main(argv) {
  const options = parseArgs(argv);
  const playwright = await import('@playwright/test');
  const host = await startHost(options);
  const browser = await playwright[options.engine].launch({ headless: true });
  const results = [];
  let failures = 0;
  try {
    for (let index = 1; index <= options.iterations; index += 1) {
      let result;
      try {
        result = await iteration(playwright, browser, host.origin, index);
      } catch (error) {
        result = { iteration: index, error: error instanceof Error ? error.message.slice(0, 600) : 'iteration_failed',
          page_errors: [] };
      }
      const failed = result.error !== undefined || result.page_errors.length > 0 || result.degraded_state_visible !== true;
      if (failed) failures += 1;
      results.push(result);
      process.stdout.write(`${JSON.stringify({ engine: options.engine, passed: !failed, ...result })}\n`);
    }
  } finally {
    await browser.close();
    host.close();
  }
  const summary = { protocol: 'mycelium.navigation_poll_browser.v1', engine: options.engine,
    iterations: options.iterations, failures, passed: failures === 0,
    page_error_count: results.reduce((total, result) => total + result.page_errors.length, 0),
    document_delay_ms: options.documentDelayMs, poll_delay_ms: options.pollDelayMs,
    evidence_class: 'local_conformance', physical_claim: false };
  process.stdout.write(`${JSON.stringify(summary)}\n`);
  return summary.passed ? 0 : 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).then((code) => { process.exitCode = code; }, (error) => {
    process.stderr.write(`${error instanceof Error ? error.message : 'navigation_poll_browser_failed'}\n`);
    process.exitCode = 2;
  });
}
