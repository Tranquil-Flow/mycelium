#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A12 M5: ONE real browser request through the live loopback product route on
// which the phone is a middle stage (scripts/run_a12_phone_route.py
// browser-request).  Chromium only, one page, one request, max_new_tokens <= 16.
// Writes {terminal} to MYCELIUM_A12_BROWSER_EVIDENCE; exit 0 only on Completed.
// The harness, not this script, checks that the phone placement ran it.
import { chromium } from '@playwright/test';
import { mkdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const origin = (process.env.MYCELIUM_A12_PRODUCT_ORIGIN ?? '').replace(/\/$/, '');
const outputPath = process.env.MYCELIUM_A12_BROWSER_EVIDENCE ?? '';
const maximumNewTokens = Number(process.env.MYCELIUM_A12_BROWSER_TOKENS ?? '12');
const prompt = process.env.MYCELIUM_A12_BROWSER_PROMPT ?? 'Name one colour of the sky.';
const closureWords = /(?:^|\s)(?:Completed|Cancelled|Failed|Cancellation unconfirmed)/;

function fail(code) {
  throw new Error(code);
}

async function atomicWrite(target, value) {
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  await rename(temporary, target);
}

async function oneRequest(page) {
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.body.dataset.evidence === 'connected', null, { timeout: 60_000 })
    .catch(() => {});
  const promptBox = page.getByRole('textbox', { name: /prompt/i });
  await promptBox.waitFor({ state: 'visible', timeout: 60_000 });
  if (await promptBox.isDisabled()) fail('live_inference_disabled');
  await promptBox.fill(prompt);
  await page.getByLabel(/maximum new tokens/i).fill(String(maximumNewTokens));
  await page.waitForFunction(() => {
    const start = [...document.querySelectorAll('button')].find((b) => /start inference/i.test(b.textContent ?? ''));
    return start !== undefined && start.disabled === false;
  }, null, { timeout: 60_000, polling: 250 });
  const started = Date.now();
  await page.getByRole('button', { name: /start inference/i }).click();
  const status = page.locator('[role="status"]').filter({ hasText: closureWords }).first();
  await status.waitFor({ state: 'visible', timeout: 600_000 });
  const text = (await status.innerText()).trim();
  const match = text.match(/^(Completed|Cancelled|Failed|Cancellation unconfirmed)\b/);
  if (match === null) fail('terminal_phase_invalid');
  return { terminal: match[1].toLowerCase().replace(/\s+/g, '_'), elapsed_ms: Date.now() - started };
}

async function main() {
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) fail('loopback_product_origin_required');
  if (!outputPath) fail('browser_evidence_path_required');
  if (!Number.isSafeInteger(maximumNewTokens) || maximumNewTokens < 1 || maximumNewTokens > 16) {
    fail('browser_token_bound_invalid');
  }
  const failures = [];
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    page.on('pageerror', () => failures.push('pageerror'));
    page.on('request', (request) => {
      if (new URL(request.url()).origin !== origin) failures.push('cross_origin_request');
    });
    const result = await oneRequest(page);
    const report = {
      protocol: 'mycelium.a12_phone_route_browser_request.v1',
      engine: 'chromium',
      maximum_new_tokens: maximumNewTokens,
      ...result,
      browser_failures: [...new Set(failures)],
    };
    await atomicWrite(path.resolve(outputPath), report);
    process.stdout.write(`${JSON.stringify(report)}\n`);
    if (report.terminal !== 'completed' || failures.length) process.exitCode = 1;
  } finally {
    await browser.close().catch(() => undefined);
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : 'a12_browser_request_failed'}\n`);
  process.exitCode = 1;
});
