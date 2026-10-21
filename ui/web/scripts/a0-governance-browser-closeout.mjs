#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A0 source-backed browser check. Route/observatory upstreams deliberately refuse:
// this is NOT a model, physical qualification, or a fabricated live route.
import { chromium, firefox, webkit } from '@playwright/test';
import { spawn } from 'node:child_process';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const originPrefix = 'http://127.0.0.1:';
const python = process.env.MYCELIUM_A0_PYTHON ?? 'python3';
const out = process.env.MYCELIUM_A0_BROWSER_EVIDENCE;
const serverCode = `
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import time
from mycelium_evidence import EvidenceProjectionRegistry, sealed_evidence_projection
from mycelium_governance import governance_readiness
from mycelium_gateway.asgi import ObservatoryASGIApplication
from mycelium_gateway.event_adapter import ObservatoryEventAdapter
from mycelium_gateway.observatory import CoherentSnapshotPublisher
from mycelium_live.supervisor import create_server
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application
root = Path.cwd()
async def unavailable(scope, receive, send):
    await send({'type':'http.response.start','status':503,'headers':[(b'content-type',b'application/json')]})
    await send({'type':'http.response.body','body':b'{"error":"upstream_unavailable"}'})
with TemporaryDirectory(prefix='a0-empty-observatory-') as temporary:
    publisher = CoherentSnapshotPublisher(Path(temporary)/'observatory.json')
    adapter = ObservatoryEventAdapter(publisher)
    # The production adapter's empty, unqualified projection; no invented qualification or API response.
    publisher.publish(adapter._bundle())
    observatory = ObservatoryASGIApplication(adapter, read_policy=lambda scope: True)
    app = create_product_gateway_application(config=GatewayConfig(), observatory_app=observatory, request_gateway_app=unavailable, swarm_coordinator=SimpleNamespace(status=lambda: {'protocol':'mycelium.product_ui.swarm.v1','native_nodes':[],'browser_workers':[]}), request_gateway_bearer_token='unavailable-local-only', observatory_bearer_token='unavailable-observatory-only')
    # Controlled local source observation: old at creation, never advanced or republished.
    # Both envelopes come from the production validating builders; no physical qualification.
    now = int(time.time() * 1000)
    observed = now - 120_000
    runtime_status = {'protocol':'mycelium.live_route_status.v1','route_alive':False,'simulated':True,'counters':{'frames_sent':0,'frames_received':0,'fatal':None},'recent_inferences':[]}
    sealed = sealed_evidence_projection(record_id='a0-local-recorded-observation', capability='replicated_serving', authority='local_conformance:sealed_builder', generation=1, observed_at_unix_ms=observed, payload={'protocol':'mycelium.local_conformance_observation.v1','route_ready':False,'physical_devices':0})
    evidence = EvidenceProjectionRegistry(runtime_source=lambda: runtime_status, historical_records=[sealed], clock_unix_ms=lambda: now - 60_000, live_ttl_ms=3_000)
    server = create_server(app=app, route=SimpleNamespace(public_status=lambda: {'protocol':'mycelium.live_status.v1','route_alive':False,'simulated':True}), static_root=root/'ui/web/dist', host='127.0.0.1', port=0, governance_projection=governance_readiness(root), evidence_registry=evidence)
    print('A0_PORT='+str(server.server_port), flush=True)
    try: server.serve_forever()
    finally: server.server_close()
`;
const scrubbed = { ...process.env, PYTHONDONTWRITEBYTECODE: '1' };
for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV']) delete scrubbed[key];
const child = spawn(python, ['-B', '-u', '-c', serverCode], { cwd: root, env: scrubbed, stdio: ['ignore', 'pipe', 'pipe'] });
let stderr = '';
child.stderr.on('data', chunk => { stderr += chunk.toString(); });
let browser;
const receipt = { protocol: 'mycelium.a0_governance_browser_closeout.v2', kind: 'source_backed_local_browser_no_model', evidence_class: 'local_conformance_nonphysical', physical_devices: 0, engines: [], release_ready: false, errors: [], negative_controls: {} };
try {
  const port = await new Promise((resolve, reject) => {
    let text = '';
    const timer = setTimeout(() => reject(new Error(`server_start_timeout:${stderr.slice(0, 300)}`)), 20000);
    child.once('exit', (code) => { clearTimeout(timer); reject(new Error(`server_exit_${code}:${stderr.slice(0, 2000)}`)); });
    child.stdout.on('data', chunk => {
      text += chunk.toString();
      const match = text.match(/A0_PORT=(\d+)/);
      if (match) { clearTimeout(timer); resolve(Number(match[1])); }
    });
  });
  const origin = originPrefix + port;
  const ledger = JSON.parse(await readFile(path.join(root, 'contracts/governance-ledger.v1.json')));
  const digest = (bytes) => 'sha256:' + createHash('sha256').update(bytes).digest('hex');
  const expectedLedger = digest(await readFile(path.join(root, 'contracts/governance-ledger.v1.json')));
  const expectedManifest = digest(await readFile(path.join(root, 'contracts/contract-manifest.v1.json')));
  const response = await fetch(`${origin}/__mycelium/governance-readiness`);
  if (response.status !== 200) throw new Error(`governance_http_${response.status}`);
  const projection = await response.json();
  const mismatches = { ledger: projection.ledger_digest !== expectedLedger, manifest: projection.contract_manifest_digest !== expectedManifest, source: projection.source_kind !== 'source_control', gate: typeof projection.governance_gate_ok !== 'boolean', release: projection.release_ready !== false, exclusions: JSON.stringify(projection.release_exclusions) !== JSON.stringify(ledger.release_exclusions) };
  if (Object.values(mismatches).some(Boolean)) throw new Error(`source_projection_mismatch:${JSON.stringify(mismatches)}`);
  receipt.projection = { source_commit: projection.source_commit, source_worktree_clean: projection.source_worktree_clean, ledger_digest: expectedLedger, contract_manifest_digest: expectedManifest, governance_gate_ok: projection.governance_gate_ok, exclusion_count: projection.release_exclusions.length, release_ready: projection.release_ready };
  if (!projection.governance_gate_ok) throw new Error('governance_gate_red_contract_pin_pending');
  const scan = async (page) => {
    const text = await page.locator('body').innerText();
    const input = await page.locator('input, textarea').evaluateAll(nodes => nodes.map(node => node.value).join(' '));
    // Do not put private matches in the receipt.
    for (const [name, pattern] of Object.entries({ token: /unavailable-local-only|unavailable-observatory-only/i, sentinel: /A0_PRIVATE_SENTINEL/, home_path: new RegExp(os.homedir().replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&') + '/'), bearer: /Bearer\s+[A-Za-z0-9_-]{24,}/i })) {
      if (pattern.test(text + ' ' + input)) throw new Error(`privacy_leak_detected:${name}`);
    }
  };
  const checkPanel = async (page, where) => {
    const panel = page.locator('section[aria-labelledby="governance-readiness-title"]');
    if (where !== 'missing_panel') await panel.getByText(`mycelium.governance_ledger.v1 · ${expectedLedger}`, { exact: true }).waitFor({ timeout: 12000 });
    if (await panel.count() !== 1) throw new Error(`${where}_panel_count_invalid`);
    const text = await panel.innerText();
    if (!text.includes(expectedManifest) || !text.includes(projection.source_commit) || !text.includes('Not release-ready.') || /(?:^|\W)Release-ready\./.test(text.replaceAll('Not release-ready.', ''))) throw new Error(`${where}_projection_not_rendered`);
    const summary = panel.getByText(`${ledger.release_exclusions.length} current release exclusions`, { exact: true });
    await summary.evaluate(node => { node.closest('details').open = false; });
    await summary.focus();
    await page.keyboard.press('Enter');
    const exclusions = await panel.locator('details li').allInnerTexts();
    if (JSON.stringify(exclusions) !== JSON.stringify(ledger.release_exclusions)) throw new Error(`${where}_exclusions_mismatch`);
    await scan(page);
  };
  const evidenceAt = async (page, endpoint) => page.evaluate(async (p) => {
    const response = await fetch(p, { cache: 'no-store', credentials: 'same-origin' });
    return { status: response.status, body: response.ok ? await response.json() : null };
  }, endpoint);
  const checkEvidence = async (page, where, baseline) => {
    const runtime = await evidenceAt(page, '/__mycelium/evidence/runtime');
    const history = await evidenceAt(page, '/__mycelium/evidence/history');
    if (runtime.status !== 200 || history.status !== 200) throw new Error(`${where}_evidence_endpoint_unavailable`);
    const r = runtime.body, records = history.body?.records;
    if (r?.protocol !== 'mycelium.evidence_projection.v1' || r.source_kind !== 'live_runtime' || r.freshness !== 'degraded' || r.payload?.route_alive !== false || r.payload?.simulated !== true || !Number.isSafeInteger(r.valid_until_unix_ms) || r.valid_until_unix_ms >= Date.now() || r.observed_at_unix_ms > r.captured_at_unix_ms) throw new Error(`${where}_age_stale_builder_contract_invalid`);
    if (history.body?.protocol !== 'mycelium.evidence_history.v1' || records?.length !== 1 || records[0].source_kind !== 'sealed_historical' || records[0].freshness !== 'historical' || records[0].authority !== 'local_conformance:sealed_builder' || records[0].payload?.route_ready !== false || records[0].payload?.physical_devices !== 0 || records[0].valid_until_unix_ms !== null || records[0].observed_at_unix_ms !== records[0].captured_at_unix_ms) throw new Error(`${where}_sealed_builder_contract_invalid`);
    if (baseline && (r.observed_at_unix_ms !== baseline.runtime_observed || r.valid_until_unix_ms !== baseline.valid_until || records[0].observed_at_unix_ms !== baseline.sealed_observed || records[0].record_id !== baseline.sealed_id)) throw new Error(`${where}_observation_timestamp_rewritten`);
    const panel = page.getByRole('region', { name: 'Evidence provenance' });
    await panel.getByRole('heading', { name: 'Stale', exact: true }).waitFor({ timeout: 12000 });
    const summary = panel.getByText('Recorded evidence (1)', { exact: true });
    await summary.waitFor({ timeout: 12000 });
    await summary.evaluate(node => { node.closest('details').open = true; });
    const text = await panel.innerText();
    if (!text.includes('These records are historical. They do not describe current runtime readiness.') || !text.includes('local_conformance:sealed_builder') || !text.includes('Replicated serving') || !text.includes('Recorded evidence')) throw new Error(`${where}_stale_or_sealed_promoted`);
    await scan(page);
    return { runtime_observed: r.observed_at_unix_ms, valid_until: r.valid_until_unix_ms, sealed_observed: records[0].observed_at_unix_ms, sealed_id: records[0].record_id };
  };
  for (const [engine, driver] of Object.entries({ chromium, firefox, webkit })) {
    const checks = { engine, direct: false, refresh: false, switching: false, back_forward: false, reconnect: false, degraded: false, stale: false, age_stale: false, sealed_history: false, second_session: false, responsive: false, keyboard: false, reduced_motion: false, privacy: false, accessible_names: false, historical_nonpromotion: false };
    receipt.engines.push(checks);
    browser = await driver.launch({ headless: true });
    const context = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.on('pageerror', e => receipt.errors.push(`${engine}:pageerror:${e.message}`));
    await page.goto(`${origin}/operator#settings`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await checkPanel(page, 'settings_direct');
    const baseline = await checkEvidence(page, 'settings_direct', null);
    if (await page.locator('main[aria-label="Settings workspace"]').count() !== 1) throw new Error('settings_active_route_missing');
    checks.direct = checks.keyboard = checks.privacy = true;
    const secondContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const second = await secondContext.newPage();
    second.on('pageerror', e => receipt.errors.push(`${engine}:second_pageerror:${e.message}`));
    await second.goto(`${origin}/operator#readiness`, { waitUntil: 'domcontentloaded' });
    await checkPanel(second, 'readiness_second_session');
    await checkEvidence(second, 'readiness_second_session', baseline);
    if (await second.locator('main[aria-label="Readiness workspace"]').count() !== 1) throw new Error('readiness_active_route_missing');
    checks.second_session = checks.accessible_names = true;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await checkPanel(page, 'settings_refresh');
    await checkEvidence(page, 'settings_refresh', baseline);
    checks.refresh = true;
    await page.getByRole('link', { name: /^Readiness\b/ }).focus();
    await page.keyboard.press('Enter');
    await page.waitForURL(/#readiness$/);
    await checkPanel(page, 'readiness_switch');
    await checkEvidence(page, 'readiness_switch', baseline);
    checks.switching = true;
    await page.goBack({ waitUntil: 'domcontentloaded' });
    await page.waitForURL(/#settings$/);
    await checkPanel(page, 'settings_back');
    await checkEvidence(page, 'settings_back', baseline);
    await page.goForward({ waitUntil: 'domcontentloaded' });
    await page.waitForURL(/#readiness$/);
    await checkPanel(page, 'readiness_forward');
    await checkEvidence(page, 'readiness_forward', baseline);
    checks.back_forward = true;
    await page.goto(`${origin}/operator#inference`, { waitUntil: 'domcontentloaded' });
    await checkEvidence(page, 'inference_nonpromotion', baseline);
    if (!await page.getByRole('button', { name: 'Start inference' }).isDisabled()) throw new Error('historical_or_stale_inference_promoted');
    await page.goto(`${origin}/operator#readiness`, { waitUntil: 'domcontentloaded' });
    await checkPanel(page, 'readiness_after_inference');
    await checkEvidence(page, 'readiness_after_inference', baseline);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => window.scrollTo(0, 0));
    checks.mobile_nav_accessible_names = await page.getByRole('link', { name: /^Readiness\b/ }).count() === 1;
    checks.mobile_scroll_width = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, viewport: window.innerWidth }));
    if (checks.mobile_scroll_width.scroll > checks.mobile_scroll_width.viewport) throw new Error('responsive_horizontal_overflow');
    checks.responsive = true;
    const motion = await page.locator('section[aria-labelledby="governance-readiness-title"]').evaluate(node => ({ preference: matchMedia('(prefers-reduced-motion: reduce)').matches, animation: getComputedStyle(node).animationDuration, transition: getComputedStyle(node).transitionDuration }));
    if (!motion.preference || parseFloat(motion.animation) > 0.001 || parseFloat(motion.transition) > 0.001) throw new Error('reduced_motion_not_respected');
    checks.reduced_motion = true;
    const text = await page.locator('body').innerText();
    if (/Physical route live|Qualified distributed execution/i.test(text)) throw new Error('unqualified_source_promoted');
    checks.age_stale = checks.sealed_history = true;
    checks.historical_nonpromotion = true;
    checks.historical_record_present = true;
    checks.evidence_observations = baseline;
    if (out) {
      const image = path.join(path.dirname(out), `a0-${engine}-mobile.png`);
      await page.screenshot({ path: image, fullPage: true });
      checks.screenshot = image;
    }
    // Abort actual transport; no invented source payload or qualification.
    await context.route('**/__mycelium/governance-readiness', request => request.abort('failed'));
    await page.reload({ waitUntil: 'domcontentloaded' });
    const degraded = page.locator('section[aria-labelledby="governance-readiness-title"]');
    await degraded.getByRole('alert').waitFor({ timeout: 12000 });
    if (!(await degraded.innerText()).includes('Not release-ready.') || await degraded.locator('dd').count() !== 0) throw new Error('degraded_source_promoted');
    checks.degraded = checks.stale = true; // Load-on-mount A0 source discards old governance bytes after failed reload.
    await checkEvidence(page, 'degraded_governance', baseline);
    await context.unroute('**/__mycelium/governance-readiness');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await checkPanel(page, 'reconnected');
    await checkEvidence(page, 'reconnected', baseline);
    checks.reconnect = true;
    if (engine === 'chromium') {
      const panel = page.locator('section[aria-labelledby="governance-readiness-title"]');
      const failsAs = async (name, action, expected) => {
        await action();
        try { await checkPanel(page, name); } catch (e) { if (String(e).includes(expected)) { receipt.negative_controls[name] = true; return; } throw e; }
        throw new Error(`negative_control_escaped:${name}`);
      };
      await failsAs('missing_panel', () => panel.evaluate(node => node.remove()), 'panel_count_invalid');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await checkPanel(page, 'before_release_mutation');
      await failsAs('release_ready_promotion', () => panel.evaluate(node => { node.innerHTML = node.innerHTML.replaceAll('Not release-ready.', 'Release-ready.'); }), 'projection_not_rendered');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await checkPanel(page, 'before_privacy_mutation');
      await failsAs('privacy_leak', () => page.evaluate(() => document.body.append('A0_PRIVATE_SENTINEL')), 'privacy_leak_detected');
    }
    await secondContext.close();
    await context.close();
    await browser.close();
    browser = null;
  }
  if (receipt.errors.length) throw new Error(`page_errors:${receipt.errors.join(';')}`);
  if (receipt.engines.some(e => !e.mobile_nav_accessible_names)) throw new Error('mobile_nav_accessible_names_missing');
  if (receipt.engines.length !== 3 || receipt.engines.some(e => Object.entries(e).some(([key, value]) => !['engine', 'mobile_scroll_width', 'evidence_observations', 'screenshot'].includes(key) && value !== true))) throw new Error('browser_matrix_incomplete');
  if (Object.keys(receipt.negative_controls).length !== 3 || Object.values(receipt.negative_controls).some(v => !v)) throw new Error('negative_controls_incomplete');
} catch (error) {
  receipt.failure = String(error);
} finally {
  if (browser) await browser.close();
  child.kill('SIGTERM');
  const exit = await new Promise(resolve => { if (child.exitCode !== null || child.signalCode !== null) resolve({ code: child.exitCode, signal: child.signalCode }); else child.once('exit', (code, signal) => resolve({ code, signal })); });
  receipt.server_teardown = exit;
  if (stderr) receipt.server_stderr = stderr.slice(0, 2000);
  if (out) await writeFile(out, JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
  console.log(JSON.stringify(receipt));
}
if (receipt.failure) process.exitCode = 1;
