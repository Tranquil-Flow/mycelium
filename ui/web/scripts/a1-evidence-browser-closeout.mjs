#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A1 browser acceptance. Local conformance exercises only negative UI contracts, and those
// contracts live behind live-only surfaces, so this arm requires the live-observatory bundle
// (`npm run build:live` in ui/web), not the plain `npm run build` fixture output that the
// loopback host would otherwise serve from ui/web/dist.
// Positive mode is gated by a live, qualified physical route and real completion, so it
// needs an explicit --origin and is refused with --local-conformance.
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const args = process.argv.slice(2);
function option(name) { const at = args.indexOf(name); return at < 0 ? null : args[at + 1]; }
const mode = option('--mode');
const reportPath = option('--report');
const local = args.includes('--local-conformance');
const suppliedOrigin = option('--origin');
const report = { protocol: 'mycelium.a1_browser_closeout.v1', mode, passed: false,
  physical_positive: false, evidence_class: local ? 'local_conformance_fixture' : 'unverified',
  checks: [], failure: null, browser: 'chromium' };
let server, browser, origin;
function check(condition, name) { if (!condition) throw Error(name); report.checks.push(name); }
function counters(e) {
  check(e?.protocol === 'mycelium.evidence_projection.v1' && e.source_kind === 'live_runtime', 'runtime_envelope_required');
  check(e.payload?.protocol === 'mycelium.live_route_status.v1', 'live_route_status_required');
  const sent = e.payload.counters?.frames_sent, received = e.payload.counters?.frames_received;
  check(Number.isSafeInteger(sent) && Number.isSafeInteger(received) && sent >= 0 && received >= 0, 'physical_frame_counters_required');
  return { sent, received };
}
async function read(page, endpoint) {
  return page.evaluate(async (p) => { const r = await fetch(p, { cache: 'no-store', credentials: 'same-origin' });
    return { status: r.status, body: r.ok ? await r.json() : null }; }, endpoint);
}
async function launchLocal() {
  const repo = path.resolve('../..');
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1' };
  for (const key of ['GIT_DIR','GIT_WORK_TREE','GIT_COMMON_DIR','GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES','PYTHONPATH','PYTHONHOME','VIRTUAL_ENV']) delete env[key];
  server = spawn('/opt/homebrew/bin/python3.14', ['scripts/serve_a14_conformance.py'], { cwd: repo, env, stdio: ['ignore','pipe','pipe'] });
  const lines = createInterface({ input: server.stdout });
  const first = await Promise.race([once(lines, 'line').then(([line]) => line), new Promise((_, reject) => setTimeout(() => reject(Error('local_conformance_start_timeout')), 15000))]);
  const started = JSON.parse(first);
  check(started.evidence_class === 'local_conformance' && started.physical_devices === 0, 'local_conformance_not_physical');
  return started.origin;
}
// The negative contracts below live behind live-only surfaces: App.tsx mounts the Evidence
// provenance panel only for a live Observatory source, and the AppShell badge names the mode
// actually served. Refuse a fixture/replay bundle by name here, so an impossible composition
// fails as a named precondition instead of a locator timeout inside an assertion.
async function requireLiveObservatoryBundle(page) {
  const shell = page.locator('[aria-label="Live semantic evidence state"], [aria-label="Fixture data, not live"], [aria-label="Replay evidence, not live"]');
  await shell.first().waitFor({ state: 'visible', timeout: 30000 }).catch(() => { throw Error('app_shell_not_rendered'); });
  check(await page.locator('[aria-label="Live semantic evidence state"]').count() > 0, 'live_observatory_bundle_required_build_with_npm_run_build_live');
}
async function checkEightWorkspaces(page) {
  for (const route of ['lab','network','plans','incidents','readiness','nodes','settings','inference']) {
    await page.locator(`nav[aria-label="Product sections"] a[href="#${route}"]`).click();
    await page.waitForURL(new RegExp(`#${route}$`));
    await expect(page.getByRole('region', { name: 'Unified product evidence source' })).toBeVisible({ timeout: 12000 });
    await expect(page.getByRole('region', { name: 'Evidence provenance' })).toBeVisible();
  }
  report.checks.push('all_eight_workspaces_share_product_and_provenance_sources');
}
async function negative(page) {
  // Only local test inputs are injected. Never replay these envelopes on a live origin.
  const payload = { protocol: 'mycelium.live_route_status.v1', route_alive: false, counters: { frames_sent: 0, frames_received: 0 }, recent_inferences: [] };
  const base = { protocol: 'mycelium.evidence_projection.v1', record_id: 'local-conformance-record', capability: 'route_execution',
    source_kind: 'live_runtime', authority: 'local_conformance_fixture', generation: 1,
    captured_at_unix_ms: 1000, observed_at_unix_ms: 1000, valid_until_unix_ms: 1001,
    freshness: 'stale', payload_protocol: payload.protocol, payload };
  const sealed = { ...base, record_id: 'local-conformance-sealed', capability: 'replicated_serving', source_kind: 'sealed_historical',
    freshness: 'historical', valid_until_unix_ms: null };
  await page.route('**/__mycelium/evidence/runtime', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(base) }));
  await page.route('**/__mycelium/evidence/history', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ protocol: 'mycelium.evidence_history.v1', records: [sealed] }) }));
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await requireLiveObservatoryBundle(page);
  const panel = page.getByRole('region', { name: 'Evidence provenance' });
  await expect(panel.getByRole('heading', { name: 'Stale', exact: true })).toBeVisible({ timeout: 20000 });
  report.checks.push('expired_runtime_displayed_stale_in_production_bundle');
  await panel.getByText('Recorded evidence (1)').click();
  await expect(panel.getByText(/local_conformance_fixture/)).toBeVisible();
  await expect(panel.getByText(/do not describe current runtime readiness/i)).toBeVisible();
  report.checks.push('sealed_record_displayed_only_as_historical');
  await checkEightWorkspaces(page);
  await page.unroute('**/__mycelium/evidence/history');
  let privateResponses = 0;
  await page.route('**/__mycelium/evidence/history', route => { privateResponses += 1; return route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ protocol: 'mycelium.evidence_history.v1', records: [{ ...sealed, authority: 'https://canary.invalid/private' }] }) }); });
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await expect(panel.getByRole('heading', { name: 'Stale', exact: true })).toBeVisible({ timeout: 12000 });
  check(privateResponses > 0, 'private_history_response_reached_browser');
  await expect(panel.getByText('Recorded evidence (1)')).toHaveCount(0, { timeout: 12000 });
  await expect(panel.getByText('https://canary.invalid/private')).toHaveCount(0);
  report.checks.push('private_history_envelope_rejected_before_render');
  await page.unroute('**/__mycelium/evidence/history');
  let restoredResponses = 0;
  await page.route('**/__mycelium/evidence/history', route => { restoredResponses += 1; return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ protocol: 'mycelium.evidence_history.v1', records: [sealed] }) }); });
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await expect(panel.getByText('Recorded evidence (1)')).toBeVisible({ timeout: 12000 });
  check(restoredResponses > 0, 'public_history_restored_after_private_refusal');
  const unknown = await read(page, '/__mycelium/evidence/not-a-capability');
  check(unknown.status === 404, 'unknown_evidence_path_rejected_by_real_host');
  await page.unroute('**/__mycelium/evidence/runtime');
  await page.route('**/__mycelium/evidence/runtime', route => route.abort());
  await expect(panel.getByRole('heading', { name: 'Live source unavailable' })).toBeVisible({ timeout: 12000 });
  await expect(panel.getByText(/No historical or demo fallback is being used/)).toBeVisible();
  report.checks.push('runtime_loss_does_not_promote_sealed_or_fixture');
  const snapshot = await read(page, '/api/v1/product/snapshot');
  check(snapshot.status === 200 && snapshot.body?.publication?.source_mode === 'fixture', 'nonphysical_product_publication_confirmed');
  check(await page.getByRole('button', { name: 'Start inference' }).isDisabled(), 'fixture_cannot_submit_inference');
}
async function positive(page) {
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('body')).toHaveAttribute('data-evidence', 'connected', { timeout: 30000 });
  const publication = await read(page, '/api/v1/product/snapshot');
  check(publication.status === 200 && publication.body?.publication?.source_mode === 'live'
    && Number.isSafeInteger(publication.body?.publication?.published_at_unix_ms)
    && publication.body.source_states?.some(s => s.source_id === 'qualification-source' && s.status === 'current'), 'live_current_product_publication');
  check(publication.body?.readiness?.some(r => r.dimension === 'qualification' && r.state === 'ready'), 'qualified_product_readiness');
  await expect(page.getByText('Qualified distributed execution · qualifier-owned current binding')).toBeVisible();
  await checkEightWorkspaces(page);
  const before = await read(page, '/__mycelium/evidence/runtime');
  check(before.status === 200 && before.body?.freshness === 'current' && before.body?.payload?.route_alive === true && before.body.valid_until_unix_ms >= Date.now(), 'current_live_runtime_evidence');
  const beforeFrames = counters(before.body);
  const historical = await read(page, '/__mycelium/evidence/history');
  check(historical.status === 200 && historical.body?.protocol === 'mycelium.evidence_history.v1' && historical.body.records?.length > 0, 'real_historical_record_required');
  const recorded = historical.body.records[0];
  check(recorded.source_kind === 'sealed_historical' && recorded.freshness === 'historical' && Number.isSafeInteger(recorded.observed_at_unix_ms), 'sealed_observation_time_required');
  const start = page.getByRole('button', { name: 'Start inference' });
  await page.getByRole('textbox', { name: /prompt/i }).fill('Reply with one short word.');
  await page.getByLabel('Maximum new tokens').fill('8');
  await expect(start).toBeEnabled({ timeout: 30000 });
  await start.click();
  await expect(page.locator('[role="status"]').filter({ hasText: /Completed/ }).first()).toBeVisible({ timeout: 240000 });
  report.checks.push('ordinary_browser_request_completed');
  const after = await read(page, '/__mycelium/evidence/runtime');
  check(after.status === 200 && after.body?.record_id === before.body.record_id && after.body.generation > before.body.generation && after.body.observed_at_unix_ms > before.body.observed_at_unix_ms, 'same_incarnation_generation_observation_increased');
  const afterFrames = counters(after.body);
  check(afterFrames.sent > beforeFrames.sent && afterFrames.received > beforeFrames.received, 'physical_frames_increased_both_directions');
  const priorHistory = before.body.payload.recent_inferences ?? [];
  const terminal = after.body.payload.recent_inferences;
  check(Array.isArray(terminal) && terminal.length > 0 && JSON.stringify(terminal.at(-1)) !== JSON.stringify(priorHistory.at(-1)) && terminal.at(-1)?.output_tokens > 0, 'server_retained_new_completed_inference');
  report.deltas = { generation: after.body.generation - before.body.generation, frames_sent: afterFrames.sent - beforeFrames.sent, frames_received: afterFrames.received - beforeFrames.received };
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('body')).toHaveAttribute('data-evidence', 'connected', { timeout: 30000 });
  await expect(page.getByRole('heading', { name: 'Request history' })).toBeVisible({ timeout: 30000 });
  const refreshed = await read(page, '/__mycelium/evidence/runtime');
  check(refreshed.status === 200 && refreshed.body?.record_id === after.body.record_id && JSON.stringify(refreshed.body.payload.recent_inferences).includes(JSON.stringify(terminal.at(-1))), 'server_terminal_history_survives_refresh');
  const historyAfter = await read(page, '/__mycelium/evidence/history');
  const same = historyAfter.body?.records?.find(r => r.record_id === recorded.record_id);
  check(same?.observed_at_unix_ms === recorded.observed_at_unix_ms && same?.freshness === 'historical', 'historical_observation_time_unchanged');
  const panel = page.getByRole('region', { name: 'Evidence provenance' });
  await panel.getByText(/^Recorded evidence \(/).click();
  await expect(panel.getByText(new RegExp(recorded.authority.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).toBeVisible();
  report.checks.push('recorded_item_visible_after_refresh');
  report.physical_positive = true;
  report.evidence_class = 'browser_observed_live_physical_request';
}
try {
  check(mode === 'negative' || mode === 'positive', 'mode_required');
  check(reportPath && path.isAbsolute(reportPath), 'absolute_report_path_required');
  check((local && !suppliedOrigin) || (!local && suppliedOrigin), 'explicit_origin_or_local_conformance_required');
  // The loopback conformance host publishes a deliberately `fixture` product snapshot and
  // has no physical route, so positive mode can never reach live_current_product_publication
  // there. Refuse the combination by name instead of dying later inside a DOM assertion.
  check(!(local && mode === 'positive'), 'positive_mode_requires_a_live_origin_not_local_conformance');
  origin = local ? await launchLocal() : suppliedOrigin?.replace(/\/$/, '');
  check(/^https?:\/\/127\.0\.0\.1:\d+$/.test(origin) && !origin.endsWith(':14351'), 'allowed_loopback_origin_required');
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ serviceWorkers: 'block' });
  const page = await context.newPage();
  await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  if (mode === 'negative') { check(local, 'negative_injection_only_on_local_conformance'); await negative(page); }
  else await positive(page);
  report.passed = true;
} catch (error) {
  report.failure = error instanceof Error ? error.message.slice(0, 220) : 'browser_harness_failure';
  process.exitCode = 1;
} finally {
  try { await browser?.close(); } catch { report.failure ??= 'browser_close_failed'; process.exitCode = 1; }
  if (server) {
    server.kill('SIGTERM');
    await Promise.race([once(server, 'exit'), new Promise(resolve => setTimeout(resolve, 3000))]);
    if (server.exitCode === null && server.signalCode === null) server.kill('SIGKILL');
    report.local_server_stopped = server.exitCode !== null || server.signalCode !== null;
    if (!report.local_server_stopped) { report.passed = false; process.exitCode = 1; }
  }
  if (reportPath && path.isAbsolute(reportPath)) {
    await mkdir(path.dirname(reportPath), { recursive: true });
    await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  console.log(JSON.stringify(report));
}
