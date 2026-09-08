import { expect, test } from '@playwright/test';
import { liveRouteStatusFixture } from '../src/features/liveRoute/routeStatusTestFixture';
import { makeProductBootstrapFixture, makeProductObservatoryFixture } from '../src/test/productFixtures';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Same read-only loader as the adjudicator's unit suite: never execute physical main.
const source = readFileSync(new URL('../scripts/a5-product-browser-e2e.mjs', import.meta.url), 'utf8');
const context = vm.createContext({ process: { env: {} } });
vm.runInContext(source.replace(/^import .*;\n/gm, '').split('\nmain().catch(')[0], context);
const verifyPlacementRequirements = vm.runInContext('verifyPlacementRequirements', context);

// A live-profile build with intercepted local observations; never fleet evidence.
test.skip(process.env.MYCELIUM_A5_LOCAL_UI !== '1', 'Requires an explicitly selected live-profile local fixture run');

test('Device Lab reconstructs exact placement bindings without promoting stale evidence', async ({ page }) => {
  const status = liveRouteStatusFixture();
  const current = { ...status.replica_track_qualification[0], issued_at_unix_ms: Date.now() - 1000,
    expires_at_unix_ms: Date.now() + 300_000 };
  let qualification = current;
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4173') return route.abort('blockedbyclient');
    if (url.pathname === '/api/v1/bootstrap') return route.fulfill({ json: { ...makeProductBootstrapFixture(), source_mode: 'live' } });
    if (url.pathname === '/api/v1/observatory/snapshot') return route.fulfill({ json: makeProductObservatoryFixture() });
    if (url.pathname === '/__mycelium/live-status') {
      return route.fulfill({ json: { ...status, replica_track_qualification: [qualification] } });
    }
    if (url.pathname.startsWith('/__mycelium/') || url.pathname.startsWith('/v1/')) {
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
  // A contradictory public record is rejected at the real HTTP decoder boundary.
  qualification = { ...current, parity_verified: false };
  await expect(page.getByRole('region', { name: 'Concurrent execution and scoped liveness', exact: true }))
    .toContainText('route_ready contradicts its proof results');
  await expect(panel).toHaveCount(0);
});
