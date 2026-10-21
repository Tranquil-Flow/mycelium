// SPDX-License-Identifier: AGPL-3.0-only
// Unit-level checks for the A14 live route-explorer gate. No browser engine,
// origin or network is contacted here.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  ENGINES,
  PROTOCOL,
  assertGlobeCounts,
  assertLiveTransitionHistory,
  assertPrivacy,
  assertRegionRows,
  assertSegmentsRetained,
  createPageDiagnostics,
  harnessPlan,
  validateLiveConfig,
} from './a14-live-browser.mjs';

const scriptPath = fileURLToPath(new URL('./a14-live-browser.mjs', import.meta.url));
const hash = (c) => `sha256:${c.repeat(64)}`;

function segment(id, transport, generation, history, extra = {}) {
  return {
    id: hash(id), source: null, destination: hash('e'), edge_kind: 'transport_observation',
    path_status: 'observed', transport, connection_generation: generation, history,
    provenance: 'live', freshness: 'current', issued_at: 1000 + generation, expires_at: 9000,
    phase: 'unknown', role: 'unknown', authority_digest: hash(id), metrics: {},
    binding: { deployment: null, request: null, attempt: null, track: null, path_generation: null },
    ...extra,
  };
}

function explorer(segments, extra = {}) {
  return { protocol: 'mycelium.route_explorer.v1', source_mode: 'live', segments,
    nodes: [{ id: hash('m'), kind: 'member', region: 'unknown', region_source: 'unavailable' }],
    ...extra };
}

const transition = () => explorer([segment('1', 'direct', 1, true), segment('2', 'relay', 2, false)]);

test('plan mode is contact-free and makes no claim', () => {
  const result = spawnSync(process.execPath, [scriptPath, '--plan'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const plan = JSON.parse(result.stdout);
  assert.deepEqual(plan, harnessPlan());
  assert.equal(plan.protocol, PROTOCOL);
  assert.equal(plan.browser_contact_attempted, false);
  assert.equal(plan.live_evidence, false);
  assert.deepEqual(plan.engines, [...ENGINES]);
  assert.equal(plan.workspaces.length, 8);
});

test('the gate refuses before any contact without an engine or a loopback origin', () => {
  const noEngine = spawnSync(process.execPath, [scriptPath], { encoding: 'utf8' });
  assert.notEqual(noEngine.status, 0);
  assert.match(noEngine.stderr, /browser_engine_selection_required/);
  const env = { A14_LIVE_ORIGIN: 'https://mycelium.now', A14_LIVE_EVIDENCE: '/tmp/a14' };
  assert.throws(() => validateLiveConfig(env, 'chromium'), /loopback_live_origin_required/);
  for (const origin of ['http://localhost:8787', 'http://127.0.0.1:8787/', 'http://127.0.0.1:0',
    'http://127.0.0.1:99999', 'http://user@127.0.0.1:8787']) {
    assert.throws(() => validateLiveConfig({ ...env, A14_LIVE_ORIGIN: origin }, 'chromium'));
  }
  const ok = { A14_LIVE_ORIGIN: 'http://127.0.0.1:8787', A14_LIVE_EVIDENCE: '/tmp/a14' };
  assert.throws(() => validateLiveConfig(ok, 'opera'), /browser_engine_selection_required/);
  assert.throws(() => validateLiveConfig({ ...ok, A14_LIVE_EVIDENCE: 'relative' }, 'webkit'),
    /evidence_directory_required/);
  assert.throws(() => validateLiveConfig({ ...ok, A14_LIVE_DECLARED_REGIONS: '-1' }, 'webkit'));
  const config = validateLiveConfig({ ...ok, A14_LIVE_DECLARED_REGIONS: '2',
    A14_LIVE_PRIVATE_CANARIES: 'node-0, node-2' }, 'firefox');
  assert.equal(config.evidence, '/tmp/a14/firefox');
  assert.deepEqual(config.canaries, ['node-0', 'node-2']);
  assert.equal(config.declared, 2);
});

test('a live direct-then-relay transition is accepted', () => {
  const found = assertLiveTransitionHistory(transition());
  assert.deepEqual(found, [{ endpoint: hash('e'), from: hash('1'), to: hash('2'),
    from_generation: 1, to_generation: 2 }]);
});

test('fixture, missing relay, unadvanced generation or wrong history is refused', () => {
  assert.throws(() => assertLiveTransitionHistory({ ...transition(), source_mode: 'fixture' }),
    /route_explorer_not_live/);
  assert.throws(() => assertLiveTransitionHistory(explorer([segment('1', 'direct', 1, false)])),
    /direct_relay_transition_unobserved/);
  assert.throws(() => assertLiveTransitionHistory(explorer([
    segment('1', 'direct', 2, true), segment('2', 'relay', 2, false)])),
  /transition_generation_not_advanced/);
  assert.throws(() => assertLiveTransitionHistory(explorer([
    segment('1', 'direct', 1, false), segment('2', 'relay', 2, false)])),
  /superseded_observation_not_historical/);
  assert.throws(() => assertLiveTransitionHistory(explorer([
    segment('1', 'direct', 1, true), segment('2', 'relay', 2, true)])),
  /latest_observation_marked_historical/);
  assert.throws(() => assertLiveTransitionHistory(explorer([
    segment('1', 'direct', 1, true, { provenance: 'fixture' }), segment('2', 'relay', 2, false)])),
  /transport_segment_not_live/);
});

test('refresh may add history but never drops or rewrites an observed segment', () => {
  const before = transition();
  const later = explorer([...before.segments, segment('3', 'relay', 3, false)]);
  later.segments[1] = { ...later.segments[1], history: true };
  assert.equal(assertSegmentsRetained(before, later), true);
  assert.throws(() => assertSegmentsRetained(before, explorer([before.segments[1]])),
    /observed_segment_dropped/);
  const rewritten = explorer([{ ...before.segments[0], transport: 'relay' }, before.segments[1]]);
  assert.throws(() => assertSegmentsRetained(before, rewritten), /observed_segment_rewritten/);
});

test('declared regions must be bound owner_declared rows; undeclared peers stay unknown', () => {
  const node = (id, region, source) => ({ id: hash(id), kind: 'member', region, region_source: source });
  const row = (id, region, source) => ({ node: hash(id), region, region_source: source,
    region_blocker: source === 'unavailable' ? 'missing' : null, distance_class: null });
  const withRegions = { ...transition(),
    nodes: [node('a', 'europe', 'owner_declared'), node('b', 'unknown', 'unavailable')],
    regions: [row('a', 'europe', 'owner_declared'), row('b', 'unknown', 'unavailable')] };
  assert.equal(assertRegionRows(withRegions, 1), 1);
  assert.equal(assertRegionRows(transition(), 0), 0);
  assert.throws(() => assertRegionRows(transition(), 1), /declared_regions_unpublished/);
  assert.throws(() => assertRegionRows(withRegions, 2), /declared_region_count_mismatch/);
  assert.throws(() => assertRegionRows(withRegions, 0), /undeclared_region_published/);
  const outside = structuredClone(withRegions);
  outside.regions[0].region = 'us-east-1';
  assert.throws(() => assertRegionRows(outside, 1), /region_outside_vocabulary/);
  const unbound = structuredClone(withRegions);
  unbound.regions[1].region = 'asia';
  assert.throws(() => assertRegionRows(unbound, 1));
});

test('privacy refuses raw addresses, coordinates and private canaries', () => {
  assert.equal(assertPrivacy('Route timeline · relay · connection 2', ['node-0']), true);
  assert.throws(() => assertPrivacy('peer 192.0.2.23', []), /raw_address_exposed/);
  assert.throws(() => assertPrivacy('{"latitude": 1}', []), /forbidden_field_exposed:latitude/);
  assert.throws(() => assertPrivacy('member node-0 in table', ['node-0']), /private_canary_exposed/);
});

test('the A14b globe check is opt-in and its counts must equal the published regions', () => {
  const ok = { A14_LIVE_ORIGIN: 'http://127.0.0.1:8080', A14_LIVE_EVIDENCE: '/tmp/a14-live' };
  assert.equal(validateLiveConfig(ok, 'chromium').globe, false);
  assert.equal(validateLiveConfig({ ...ok, A14_LIVE_GLOBE: '0' }, 'chromium').globe, false);
  assert.equal(validateLiveConfig({ ...ok, A14_LIVE_GLOBE: '1' }, 'chromium').globe, true);
  assert.throws(() => validateLiveConfig({ ...ok, A14_LIVE_GLOBE: 'yes' }, 'chromium'), /globe_flag_invalid/);
  const node = (id, region) => ({ id: hash(id), kind: 'member', region,
    region_source: region === 'unknown' ? 'unavailable' : 'owner_declared' });
  const explorer = { nodes: [node('a', 'europe'), node('b', 'europe'), node('c', 'unknown')] };
  const shown = { africa: 0, americas_north: 0, americas_south: 0, asia: 0, europe: 2, oceania: 0, polar: 0, unknown: 1 };
  assert.equal(assertGlobeCounts(explorer, shown).europe, 2);
  assert.throws(() => assertGlobeCounts(explorer, { ...shown, europe: 3 }), /globe_count_mismatch:europe/);
  assert.throws(() => assertGlobeCounts(explorer, { ...shown, unknown: 0, asia: 1 }), /globe_count_mismatch/);
  assert.throws(() => assertGlobeCounts(explorer, { ...shown, unknown_cell: 1 }), /unknown_region_on_globe/);
  assert.throws(() => assertGlobeCounts({ nodes: [node('d', 'us-east-1')] }, shown), /region_outside_vocabulary/);
});

test('every page error stays a failure and is recorded with time, step, console and failed requests', () => {
  let clock = Date.parse('2026-10-14T17:00:00.000Z');
  const failures = [];
  const diagnostics = createPageDiagnostics({ now: () => clock, failures });
  const handlers = {};
  diagnostics.attach({ on: (event, handler) => { handlers[event] = handler; } }, 'main');
  diagnostics.step = 'reload:inference';
  clock += 1_250;
  handlers.console({ type: () => 'log', text: () => 'ignored' });
  handlers.console({ type: () => 'error', text: () => 'Failed to load resource: 404' });
  handlers.pageerror(new Error('/127.0.0.1:8787/__mycelium/live-status due to access control checks.'));
  diagnostics.step = 'back:inference';
  clock += 10;
  handlers.requestfailed({ url: () => 'http://127.0.0.1:8787/__mycelium/models/operation?x=1',
    failure: () => ({ errorText: 'Load request cancelled' }) });
  assert.deepEqual(failures, ['pageerror:/127.0.0.1:8787/__mycelium/live-status due to access control checks.']);
  assert.deepEqual(diagnostics.entries.page_errors, [{ at: '2026-10-14T17:00:01.250Z', t_ms: 1_250,
    step: 'reload:inference', page: 'main',
    message: '/127.0.0.1:8787/__mycelium/live-status due to access control checks.' }]);
  assert.deepEqual(diagnostics.entries.console, [{ at: '2026-10-14T17:00:01.250Z', t_ms: 1_250,
    step: 'reload:inference', page: 'main', type: 'error', text: 'Failed to load resource: 404' }]);
  assert.deepEqual(diagnostics.entries.request_failures, [{ at: '2026-10-14T17:00:01.260Z', t_ms: 1_260,
    step: 'back:inference', page: 'main', path: '/__mycelium/models/operation', failure: 'Load request cancelled' }]);
});
