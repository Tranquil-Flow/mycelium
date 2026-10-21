// SPDX-License-Identifier: AGPL-3.0-only
// A14 live route-explorer browser gate.
//
// Unlike a14-local-browser.mjs (which spawns a synthetic local-conformance
// server), this gate drives an already running *live* product origin on
// 127.0.0.1 after a forced-relay physical run and checks the ordinary browser
// route explorer against the product's own snapshot:
//
//   * the snapshot and explorer are live, not fixture/replay;
//   * one endpoint shows a retained direct segment followed by a relay
//     segment on a later connection generation (the transition), the direct
//     segment is marked historical and is never rewritten by refresh;
//   * table and logical graph list the same segments, before and after
//     refresh, Back/Forward, all eight workspaces and a clean second session;
//   * optional owner-declared regions render as owner_declared rows while
//     undeclared peers stay unknown, and no coordinate or raw identity appears;
//   * every request is same-origin GET; no third-party request is made;
//   * with A14_LIVE_GLOBE=1 (A14b, opt-in), the schematic region globe is off
//     by default, shows exactly the published per-region counts with unknown
//     peers off-globe, is keyboard-operable, loses no table/graph fact, and
//     leaks no coordinate, identity or canary.
//
// It never authors route evidence and records live_evidence only when every
// check passed. Run one engine per invocation: --engine=chromium|firefox|webkit.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

export const ENGINES = Object.freeze(['chromium', 'firefox', 'webkit']);
export const PROTOCOL = 'mycelium.a14_live_browser_gate.v1';
export const WORKSPACES = Object.freeze(['nodes', 'plans', 'readiness', 'incidents', 'settings', 'lab', 'inference', 'network']);
const REGIONS = new Set(['africa', 'americas_north', 'americas_south', 'asia', 'europe', 'oceania', 'polar', 'unknown']);
const FORBIDDEN_TEXT = ['latitude', 'longitude', 'geohash', 'endpoint_id', 'relay_url', 'ip_address'];
const IPV4 = /\b(?:\d{1,3}\.){3}\d{1,3}\b/;

function fail(code) {
  throw new Error(code);
}

export function exactLoopbackOrigin(value) {
  if (typeof value !== 'string' || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(value)
    || new URL(value).origin !== value || Number(new URL(value).port) > 65535) {
    fail('loopback_live_origin_required');
  }
  return value;
}

// The whole configuration is read and validated before any browser or origin contact.
export function validateLiveConfig(env, engine) {
  if (!ENGINES.includes(engine)) fail('browser_engine_selection_required');
  const origin = exactLoopbackOrigin(env.A14_LIVE_ORIGIN);
  const evidence = env.A14_LIVE_EVIDENCE;
  if (typeof evidence !== 'string' || !path.isAbsolute(evidence)) fail('evidence_directory_required');
  const declared = env.A14_LIVE_DECLARED_REGIONS === undefined ? 0 : Number(env.A14_LIVE_DECLARED_REGIONS);
  if (!Number.isSafeInteger(declared) || declared < 0 || declared > 4096) fail('declared_region_count_invalid');
  const canaries = (env.A14_LIVE_PRIVATE_CANARIES ?? '').split(',').map((item) => item.trim()).filter(Boolean);
  if (canaries.length > 64 || canaries.some((item) => item.length < 3 || item.length > 256)) {
    fail('private_canaries_invalid');
  }
  const globe = env.A14_LIVE_GLOBE === undefined || env.A14_LIVE_GLOBE === '0' ? false
    : env.A14_LIVE_GLOBE === '1' ? true : fail('globe_flag_invalid');
  return Object.freeze({ engine, origin, evidence: path.join(evidence, engine), declared, canaries, globe });
}

export function transportSegments(explorer) {
  return explorer.segments.filter((segment) => segment.edge_kind === 'transport_observation');
}

// A live transition: one endpoint's direct segment retained as history and a
// relay segment on a strictly later connection generation as the latest.
export function assertLiveTransitionHistory(explorer) {
  if (explorer?.source_mode !== 'live') fail('route_explorer_not_live');
  const byEndpoint = new Map();
  for (const segment of transportSegments(explorer)) {
    if (segment.provenance !== 'live') fail('transport_segment_not_live');
    if (segment.path_status !== 'observed' || !['direct', 'relay'].includes(segment.transport)) continue;
    if (!Number.isSafeInteger(segment.connection_generation)) fail('transition_generation_missing');
    const list = byEndpoint.get(segment.destination) ?? [];
    list.push(segment);
    byEndpoint.set(segment.destination, list);
  }
  const transitions = [];
  for (const [endpoint, list] of byEndpoint) {
    list.sort((a, b) => a.connection_generation - b.connection_generation || a.issued_at - b.issued_at);
    const latest = list[list.length - 1];
    if (latest.history !== false) fail('latest_observation_marked_historical');
    for (let i = 0; i + 1 < list.length; i += 1) {
      if (list[i].history !== true) fail('superseded_observation_not_historical');
      if (list[i].transport === 'direct' && list[i + 1].transport === 'relay') {
        if (!(list[i + 1].connection_generation > list[i].connection_generation)) {
          fail('transition_generation_not_advanced');
        }
        transitions.push({ endpoint, from: list[i].id, to: list[i + 1].id,
          from_generation: list[i].connection_generation, to_generation: list[i + 1].connection_generation });
      }
    }
  }
  if (transitions.length === 0) fail('direct_relay_transition_unobserved');
  return transitions;
}

function immutable(segment) {
  const { history: _history, freshness: _freshness, ...rest } = segment;
  return JSON.stringify(rest, Object.keys(rest).sort());
}

// Refresh, reconnect and a second session may add segments and flip history,
// never rewrite or drop an observed segment.
export function assertSegmentsRetained(before, after) {
  const later = new Map(after.segments.map((segment) => [segment.id, segment]));
  for (const segment of transportSegments(before)) {
    const kept = later.get(segment.id);
    if (!kept) fail('observed_segment_dropped');
    if (immutable(kept) !== immutable(segment)) fail('observed_segment_rewritten');
  }
  return true;
}

export function assertRegionRows(explorer, declared) {
  if (declared === 0) {
    if (Object.hasOwn(explorer, 'regions')) {
      for (const row of explorer.regions) if (row.region_source === 'owner_declared') fail('undeclared_region_published');
    }
    return 0;
  }
  if (!Array.isArray(explorer.regions)) fail('declared_regions_unpublished');
  const nodes = new Map(explorer.nodes.map((node) => [node.id, node]));
  let owner = 0;
  for (const row of explorer.regions) {
    if (!REGIONS.has(row.region)) fail('region_outside_vocabulary');
    const node = nodes.get(row.node);
    if (!node || node.region !== row.region || node.region_source !== row.region_source) fail('region_row_unbound');
    if (row.region_source === 'owner_declared') owner += 1;
    if (row.region_source === 'unavailable' && row.region !== 'unknown') fail('unknown_peer_assigned_region');
  }
  if (owner !== declared) fail('declared_region_count_mismatch');
  return owner;
}

const GLOBE_REGIONS = Object.freeze(['africa', 'americas_north', 'americas_south', 'asia', 'europe', 'oceania', 'polar']);

// A14b: the globe's per-region counts must equal the published node regions,
// with every unknown-region peer off-globe and no cell for `unknown`.
export function assertGlobeCounts(explorer, shown) {
  const expected = { unknown: 0 };
  for (const region of GLOBE_REGIONS) expected[region] = 0;
  for (const node of explorer.nodes) {
    if (!REGIONS.has(node.region)) fail('region_outside_vocabulary');
    expected[node.region] += 1;
  }
  if (Object.hasOwn(shown, 'unknown_cell')) fail('unknown_region_on_globe');
  for (const key of [...GLOBE_REGIONS, 'unknown']) {
    if (shown[key] !== expected[key]) fail(`globe_count_mismatch:${key}`);
  }
  return expected;
}

export function assertPrivacy(text, canaries) {
  if (typeof text !== 'string') fail('privacy_input_invalid');
  if (IPV4.test(text)) fail('raw_address_exposed');
  const lower = text.toLowerCase();
  for (const word of FORBIDDEN_TEXT) if (lower.includes(word)) fail(`forbidden_field_exposed:${word}`);
  for (const canary of canaries) if (text.includes(canary)) fail('private_canary_exposed');
  return true;
}

export function harnessPlan() {
  return {
    protocol: PROTOCOL,
    mode: 'plan',
    engines: [...ENGINES],
    workspaces: [...WORKSPACES],
    required_environment: ['A14_LIVE_ORIGIN', 'A14_LIVE_EVIDENCE'],
    optional_environment: ['A14_LIVE_DECLARED_REGIONS', 'A14_LIVE_PRIVATE_CANARIES', 'A14_LIVE_GLOBE'],
    browser_contact_attempted: false,
    live_evidence: false,
    qualification_claim: false,
  };
}

const DIAGNOSTIC_TEXT_LIMIT = 300;
const DIAGNOSTIC_ENTRY_LIMIT = 200;

// Timing diagnostics for a decisive rerun: every page error is still a
// failure, but it is recorded with its wall-clock time, its offset from the
// gate start and the navigation step that was active, next to the page's
// console errors/warnings and failed requests (path only, no query).
export function createPageDiagnostics({ now = Date.now, failures = [] } = {}) {
  const started = now();
  const entries = { page_errors: [], console: [], request_failures: [] };
  let step = 'start';
  const record = (kind, value) => {
    if (entries[kind].length >= DIAGNOSTIC_ENTRY_LIMIT) return;
    const at = now();
    entries[kind].push({ at: new Date(at).toISOString(), t_ms: at - started, step, ...value });
  };
  const clip = (text) => String(text).slice(0, DIAGNOSTIC_TEXT_LIMIT);
  return {
    failures,
    entries,
    get step() { return step; },
    set step(value) { step = String(value); },
    attach(page, label) {
      page.on('pageerror', (error) => {
        failures.push(`pageerror:${error.message}`);
        record('page_errors', { page: label, message: clip(error.message) });
      });
      page.on('console', (message) => {
        if (['error', 'warning'].includes(message.type())) {
          record('console', { page: label, type: message.type(), text: clip(message.text()) });
        }
      });
      page.on('requestfailed', (request) => {
        let target;
        try {
          target = new URL(request.url()).pathname;
        } catch {
          target = 'invalid_url';
        }
        record('request_failures', { page: label, path: clip(target), failure: clip(request.failure()?.errorText ?? 'unknown') });
      });
    },
  };
}

async function snapshotFrom(page) {
  return page.evaluate(async () => {
    const response = await fetch('/api/v1/product/snapshot', { cache: 'no-store' });
    return response.json();
  });
}

export async function runLive(config) {
  const playwright = await import('@playwright/test');
  const { expect } = playwright;
  await mkdir(config.evidence, { recursive: true, mode: 0o700 });
  const checks = [];
  const failures = [];
  const requests = [];
  const diagnostics = createPageDiagnostics({ failures });
  let browser;
  let transitions = [];
  try {
    browser = await playwright[config.engine].launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1100 },
      reducedMotion: 'reduce', serviceWorkers: 'block' });
    const instrument = async (page, label) => {
      diagnostics.attach(page, label);
      page.on('request', (request) => {
        const url = new URL(request.url());
        requests.push({ method: request.method(), path: url.pathname, same_origin: url.origin === config.origin });
      });
      await page.route('**/*', (route) => (new URL(route.request().url()).origin === config.origin
        ? route.continue() : route.abort()));
    };
    const page = await context.newPage();
    await instrument(page, 'main');
    diagnostics.step = 'goto:network';
    await page.goto(`${config.origin}/#network`);
    const table = page.getByRole('table', { name: 'Route timeline', exact: true });
    await expect(table).toBeVisible({ timeout: 20_000 });
    const equivalent = async (target = page) => {
      const rows = await target.locator('[data-route-segment]').evaluateAll((es) => es.map((e) => e.getAttribute('data-route-segment')));
      const graph = await target.locator('[data-graph-segment]').evaluateAll((es) => es.map((e) => e.getAttribute('data-graph-segment')));
      assert.deepEqual(rows, graph);
      return rows;
    };
    const first = await snapshotFrom(page);
    assert.equal(first.publication.source_mode, 'live');
    transitions = assertLiveTransitionHistory(first.route_explorer);
    const initial = await equivalent();
    for (const transition of transitions) {
      assert(initial.includes(transition.from) && initial.includes(transition.to));
    }
    checks.push('live_direct_relay_transition_table_graph');
    assertRegionRows(first.route_explorer, config.declared);
    await expect(page.getByRole('region', { name: 'Peer regions and unknown-region peers', exact: true }))
      .toContainText(`All ${first.route_explorer.nodes.length} public peers`);
    checks.push('regions_owner_declared_or_unknown');
    diagnostics.step = 'reload:network';
    await page.reload();
    await expect(table).toBeVisible();
    const refreshed = await snapshotFrom(page);
    assertSegmentsRetained(first.route_explorer, refreshed.route_explorer);
    await equivalent();
    checks.push('refresh_retains_immutable_history');
    for (const route of WORKSPACES) {
      const active = () => page.locator(`nav a[href="#${route}"][aria-current="page"]`);
      diagnostics.step = `goto:${route}`;
      await page.goto(`${config.origin}/#${route}`);
      await expect(active()).toBeVisible();
      diagnostics.step = `reload:${route}`;
      await page.reload();
      await expect(active()).toBeVisible();
      const away = route === 'network' ? 'nodes' : 'network';
      diagnostics.step = `click:${route}->${away}`;
      await page.locator(`nav a[href="#${away}"]`).click();
      diagnostics.step = `back:${route}`;
      await page.goBack();
      await expect(active()).toBeVisible();
      diagnostics.step = `forward:${route}`;
      await page.goForward();
      diagnostics.step = `back_again:${route}`;
      await page.goBack();
      await expect(active()).toBeVisible();
      diagnostics.step = `privacy:${route}`;
      assertPrivacy(await page.locator('body').innerText(), config.canaries);
    }
    diagnostics.step = 'goto:network_final';
    await page.goto(`${config.origin}/#network`);
    await expect(table).toBeVisible();
    checks.push('eight_workspaces_navigation_refresh_back_forward_privacy');
    const second = await browser.newContext({ serviceWorkers: 'block', reducedMotion: 'reduce' });
    const other = await second.newPage();
    await instrument(other, 'second_session');
    diagnostics.step = 'second_session:goto:network';
    await other.goto(`${config.origin}/#network`);
    await expect(other.getByRole('table', { name: 'Route timeline', exact: true })).toBeVisible({ timeout: 20_000 });
    const clean = await snapshotFrom(other);
    assertSegmentsRetained(first.route_explorer, clean.route_explorer);
    await equivalent(other);
    assertPrivacy(JSON.stringify(clean), config.canaries);
    assertPrivacy(await other.locator('body').innerText(), config.canaries);
    checks.push('second_session_same_history_privacy');
    diagnostics.step = 'second_session:close';
    await second.close();
    diagnostics.step = 'evidence:network';
    await writeFile(path.join(config.evidence, 'network-dom.txt'), await page.locator('body').innerText());
    await page.screenshot({ path: path.join(config.evidence, 'network.png'), fullPage: true });
    if (config.globe) {
      diagnostics.step = 'globe';
      const facts = async () => ({ rows: await equivalent(),
        peers: await page.getByRole('region', { name: 'Peer regions and unknown-region peers', exact: true }).innerText() });
      const toggle = page.getByRole('checkbox', { name: /Show schematic region globe/ });
      const globeRegion = page.getByRole('region', { name: 'Schematic region globe', exact: true });
      await expect(toggle).not.toBeChecked();
      await expect(globeRegion).toHaveCount(0);
      const before = await facts();
      const current = await snapshotFrom(page);
      await toggle.focus();
      await page.keyboard.press('Space');
      await expect(globeRegion).toBeVisible();
      const shown = await globeRegion.evaluate((root) => {
        const out = {};
        for (const cell of root.querySelectorAll('button[data-globe-region]')) {
          const key = cell.getAttribute('data-globe-region');
          out[key === 'unknown' ? 'unknown_cell' : key] = Number(cell.querySelector('[data-globe-count]')?.textContent);
        }
        out.unknown = Number(root.querySelector('[data-globe-row="unknown"] td')?.textContent);
        return out;
      });
      const expected = assertGlobeCounts(current.route_explorer, shown);
      assert.deepEqual(await facts(), before);
      const occupied = GLOBE_REGIONS.find((region) => expected[region] > 0);
      if (occupied !== undefined) {
        const cell = globeRegion.locator(`button[data-globe-region="${occupied}"]`);
        await cell.focus();
        await page.keyboard.press('Enter');
        await expect(cell).toHaveAttribute('aria-pressed', 'true');
        const marked = await page.locator('.route-peers [data-region-marked]').count();
        const pageNodes = current.route_explorer.nodes.slice(0, 64).filter((node) => node.region === occupied).length;
        if (marked !== pageNodes) fail('globe_selection_unsynchronized');
      }
      assertPrivacy(await page.locator('body').innerText(), config.canaries);
      const globeHtml = await globeRegion.evaluate((root) => root.outerHTML);
      assertPrivacy(globeHtml, config.canaries);
      for (const node of current.route_explorer.nodes) if (globeHtml.includes(node.id)) fail('globe_identity_exposed');
      if (/cell_/.test(globeHtml)) fail('globe_cell_id_exposed');
      await page.screenshot({ path: path.join(config.evidence, 'network-globe.png'), fullPage: true });
      await toggle.focus();
      await page.keyboard.press('Space');
      await expect(globeRegion).toHaveCount(0);
      assert.deepEqual(await facts(), before);
      checks.push('a14b_globe_default_off_equivalent_keyboard_privacy');
    }
    diagnostics.step = 'context:close';
    await context.close();
    if (!requests.every((request) => request.same_origin && request.method === 'GET')) fail('non_same_origin_or_mutating_request');
    checks.push('same_origin_read_only');
  } catch (error) {
    failures.push(error instanceof Error ? error.message : 'a14_live_browser_failed');
  } finally {
    diagnostics.step = 'browser:close';
    await browser?.close();
  }
  const passed = failures.length === 0 && checks.length === (config.globe ? 7 : 6);
  const report = { protocol: PROTOCOL, engine: config.engine, origin: config.origin, checks, failures,
    transitions, request_count: requests.length, live_evidence: passed, qualification_claim: false,
    physical_claim: false, diagnostics: diagnostics.entries };
  await writeFile(path.join(config.evidence, 'browser-report.json'), `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  return report;
}

async function main(args) {
  if (args.length === 1 && args[0] === '--plan') {
    process.stdout.write(`${JSON.stringify(harnessPlan())}\n`);
    return;
  }
  if (args.length !== 1 || !args[0].startsWith('--engine=')) fail('browser_engine_selection_required');
  const config = validateLiveConfig(process.env, args[0].slice('--engine='.length));
  const report = await runLive(config);
  process.stdout.write(`${JSON.stringify({ engine: report.engine, checks: report.checks, failures: report.failures,
    page_errors: report.diagnostics.page_errors, live_evidence: report.live_evidence })}\n`);
  if (!report.live_evidence) process.exitCode = 1;
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : 'a14_live_browser_failed'}\n`);
    process.exitCode = 1;
  });
}
