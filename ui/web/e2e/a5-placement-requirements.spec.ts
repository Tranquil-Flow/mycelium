import { expect, test } from '@playwright/test';
import { liveRouteStatusFixture } from '../src/features/liveRoute/routeStatusTestFixture';
import { makeProductBootstrapFixture } from '../src/test/productFixtures';
import { validObservatoryAdapterEvent } from '../src/test/observatoryEventFixture';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Same read-only loader as the adjudicator's unit suite: never execute physical main.
const source = readFileSync(new URL('../scripts/a5-product-browser-e2e.mjs', import.meta.url), 'utf8');
const productSnapshot = JSON.parse(readFileSync(new URL('../../../contracts/compatibility-fixtures/product-snapshot-v1.json', import.meta.url), 'utf8'));
const context = vm.createContext({ process: { env: {} } });
vm.runInContext(source.replace(/^import .*;\n/gm, '').split('\nmain().catch(')[0], context);
const verifyPlacementRequirements = vm.runInContext('verifyPlacementRequirements', context);
const verifyPanel = vm.runInContext('verifyPanel', context);

// A live-profile build with intercepted local observations; never fleet evidence.
test.skip(process.env.MYCELIUM_A5_LOCAL_UI !== '1', 'Requires an explicitly selected live-profile local fixture run');

test('Device Lab reconstructs exact placement bindings without promoting stale evidence', async ({ page }) => {
  const original = liveRouteStatusFixture();
  const status = { ...original, peers: original.peers.map((peer, index) => index === 0
    ? { ...peer, placements: [{ ...original.stages[0],
      placement_id: original.replica_track_qualification[0].placement_id, node_id: peer.node_id }] }
    : peer) };
  const current = { ...status.replica_track_qualification[0], issued_at_unix_ms: Date.now() - 1000,
    expires_at_unix_ms: Date.now() + 300_000 };
  let qualification = current;
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4173') return route.abort('blockedbyclient');
    if (url.pathname === '/api/v1/bootstrap') return route.fulfill({ json: { ...makeProductBootstrapFixture(), source_mode: 'live' } });
    if (url.pathname === '/api/v1/observatory/snapshot') return route.fulfill({ json: validObservatoryAdapterEvent() });
    if (url.pathname === '/api/v1/product/snapshot') return route.fulfill({ json: productSnapshot });
    if (url.pathname === '/__mycelium/live-status') {
      return route.fulfill({ json: { ...status, replica_track_qualification: [qualification] } });
    }
    if (url.pathname.startsWith('/__mycelium/') || url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/')) {
      return route.fulfill({ status: 503, json: { error: 'fixture_endpoint_unavailable' } });
    }
    return route.continue();
  });
  await page.goto('/#lab');
  await verifyPlacementRequirements(page, { qualifications: [qualification], losses: new Set() });
  await page.reload();
  await verifyPlacementRequirements(page, { qualifications: [qualification], losses: new Set() });
  qualification = { ...current, expires_at_unix_ms: Date.now() - 1 };
  const panel = page.getByRole('region', { name: 'Replica placement requirements', exact: true });
  await expect(panel.getByText('Expired', { exact: true })).toBeVisible();
  await verifyPlacementRequirements(page, { qualifications: [qualification], losses: new Set() });
  await expect(panel.getByText('Pass', { exact: true })).toHaveCount(0);
  await expect(panel.getByText('Current qualification', { exact: true })).toHaveCount(0);
  for (const [workspace, view] of [['inference', 'tracks'], ['readiness', 'qualification'], ['incidents', 'loss']]) {
    await page.goto(`/#${workspace}`);
    await verifyPanel(page, workspace, view, { qualifications: [qualification], losses: new Set() });
  }
  qualification = { ...current, issued_at_unix_ms: Date.now() + 60_000 };
  for (const [workspace, view] of [['inference', 'tracks'], ['readiness', 'qualification'], ['incidents', 'loss']]) {
    await page.goto(`/#${workspace}`);
    await verifyPanel(page, workspace, view, { qualifications: [qualification], losses: new Set() });
    await expect(page.getByText('not yet valid', { exact: true })).toBeVisible();
  }
  await page.goto('/#nodes');
  const placementPanel = page.getByRole('region', { name: 'Placement work and qualification', exact: true });
  await expect(placementPanel).toContainText('Not yet valid');
  await expect(placementPanel.getByText(/· Qualified$/)).toHaveCount(0);
  await page.reload();
  await expect(placementPanel).toContainText('Not yet valid');
  await page.screenshot({ path: test.info().outputPath('nodes-future-proof.png'), fullPage: true });
  await page.goto('/#lab');
  // A contradictory public record is rejected at the real HTTP decoder boundary.
  qualification = { ...current, parity_verified: false };
  await expect(page.getByRole('region', { name: 'Concurrent execution and scoped liveness', exact: true }))
    .toContainText('route_ready contradicts its proof results');
  await expect(panel).toHaveCount(0);
});
