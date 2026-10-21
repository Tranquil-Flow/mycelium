// SPDX-License-Identifier: AGPL-3.0-only
import { expect, test } from '@playwright/test';

const PRODUCT_ORIGIN = 'http://127.0.0.1:4173';
const PRODUCT_HTTP_ORIGIN = new URL(PRODUCT_ORIGIN);

test('product shell navigation stays truthful, local, and inference-disabled', async ({ page }) => {
  const inferenceRequests: string[] = [];
  const requestViolations: string[] = [];
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];

  await page.route('**/*', async (route) => {
    const request = route.request();
    const target = new URL(request.url());
    const headers = await request.allHeaders();
    if (target.origin !== PRODUCT_ORIGIN || target.username !== '' || target.password !== '') {
      requestViolations.push(`cross-origin request: ${target.origin}${target.pathname}`);
      await route.abort('blockedbyclient');
      return;
    }
    if (headers.authorization !== undefined || headers['proxy-authorization'] !== undefined) {
      requestViolations.push(`authorization header: ${request.method()} ${target.pathname}`);
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });

  await page.routeWebSocket('**', (socket) => {
    const target = new URL(socket.url());
    const expectedProtocol = PRODUCT_HTTP_ORIGIN.protocol === 'https:' ? 'wss:' : 'ws:';
    if (
      target.protocol !== expectedProtocol ||
      target.host !== PRODUCT_HTTP_ORIGIN.host ||
      target.username !== '' ||
      target.password !== ''
    ) {
      requestViolations.push(`cross-origin websocket: ${target.origin}${target.pathname}`);
      void socket.close({ code: 1008, reason: 'blocked by product network policy' });
      return;
    }
    socket.connectToServer();
  });

  page.on('request', (request) => {
    const target = new URL(request.url());
    if (/\/api\/v1\/inference(?:\/|$)/.test(target.pathname)) {
      inferenceRequests.push(`${request.method()} ${target.pathname}`);
    }
  });
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => pageErrors.push(error.name));

  await page.goto('/#inference');

  await expect(page.getByText('Mycelium', { exact: true })).toBeVisible();
  await expect(page.getByText('FIXTURE DATA · NOT LIVE')).toBeVisible();
  await expect(page.getByText(/route readiness unknown/i).first()).toBeVisible();
  await expect(page.getByRole('textbox', { name: /prompt/i })).toBeDisabled();
  await expect(page.getByRole('button', { name: /start inference/i })).toBeDisabled();
  await expect(page.getByText(/no model request was made/i)).toBeVisible();

  const navigation = page.getByRole('navigation', { name: 'Product sections' });
  await expect(navigation.getByRole('link')).toHaveCount(8);

  const routeChecks = [
    ['Device Lab', 'lab', /^device lab$/i],
    ['Network', 'network', /network topology/i],
    ['Plans', 'plans', /strategy comparison/i],
    ['Incidents', 'incidents', /failover replay/i],
    ['Readiness', 'readiness', /proof matrix/i],
    ['Nodes', 'nodes', /^nodes$/i],
    ['Settings', 'settings', /^settings$/i],
    ['Inference', 'inference', /^inference$/i],
  ] as const;

  const routeByHash = new Map(routeChecks.map((entry) => [entry[1], entry]));

  for (const [label, hash, heading] of routeChecks) {
    const otherRoute = routeByHash.get(hash === 'settings' ? 'inference' : 'settings') ?? routeChecks[0];
    await navigation.getByRole('link', { name: new RegExp(`^${label}`) }).click();
    await expect(page).toHaveURL(new RegExp(`#${hash}$`));
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    // Exercise the real history router, not just navigation-link presence.
    await page.goto(`/#${hash}`);
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    await page.goto(`/#${otherRoute[1]}`);
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`#${hash}$`));
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    // Load-based back/forward pair, on the canonical /operator path.
    //
    // The two entries are created by real document loads and the canonical path is asserted
    // before traversing, so this stays a strict check in every engine. Measured on WebKit
    // (Playwright 1.61.1): a `history.replaceState` that rewrites a session-history entry
    // (here the legacy `/#<hash>` -> `/operator#<hash>` canonicalisation the shell performs on
    // load) makes WebKit drop that entry's forward list, so `history.forward()` from it is a
    // silent no-op - `goBack()` reached `/operator#settings` and `goForward()` stayed on
    // `/operator#settings`, with no navigation event; the identical sequence over plain,
    // un-rewritten entries traverses correctly and chromium/firefox keep the entry. Driving
    // the pair through entries the shell canonicalises on load would therefore assert WebKit's
    // dropped forward list instead of this router's behaviour. If the shell ever stops holding
    // the canonical path on a loaded entry, the assertion below goes red.
    await page.goto(`/operator#${otherRoute[1]}`);
    await page.goto(`/operator#${hash}`);
    await expect(page).toHaveURL(new RegExp(`/operator#${hash}$`));
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/operator#${otherRoute[1]}$`));
    await page.goForward();
    await expect(page).toHaveURL(new RegExp(`/operator#${hash}$`));
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    // In-app router back/forward pair: two pushState entries created by the real navigation
    // links (section -> section -> Back -> Forward). A router that stops pushing history
    // entries, or stops restoring the hash route on a history traversal, fails here in every
    // engine.
    await navigation.getByRole('link', { name: new RegExp(`^${otherRoute[0]}`) }).click();
    await expect(page).toHaveURL(new RegExp(`/operator#${otherRoute[1]}$`));
    await expect(
      page.getByRole('heading', { level: 1, name: otherRoute[2] }),
    ).toBeVisible();
    await navigation.getByRole('link', { name: new RegExp(`^${label}`) }).click();
    await expect(page).toHaveURL(new RegExp(`/operator#${hash}$`));
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/operator#${otherRoute[1]}$`));
    await expect(
      page.getByRole('heading', { level: 1, name: otherRoute[2] }),
    ).toBeVisible();
    await page.goForward();
    await expect(page).toHaveURL(new RegExp(`/operator#${hash}$`));
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
  }

  await navigation.getByRole('link', { name: /^Readiness/ }).click();
  const lifecyclePanel = page.getByRole('region', {
    name: /recorded lifecycle projection coverage/i,
  });
  await expect(lifecyclePanel).toBeVisible();
  const lifecycleLabels = [
    'Preparing qualifier evidence',
    'Loading evidence snapshot',
    'Qualifier-owned not accepted',
    'Qualified distributed execution',
    'Generating accepted request',
    'Cancellation pending',
    'Peer lost',
    'Recovering route binding',
    'Stale qualification',
    'Revoked swarm member',
    'Cleanup complete',
  ] as const;
  for (const label of lifecycleLabels) {
    await expect(lifecyclePanel.getByText(label, { exact: true })).toBeVisible();
  }
  await expect(lifecyclePanel.getByText('recorded_event_projection_only').first()).toBeVisible();
  await expect(lifecyclePanel.getByText('real_device=false').first()).toBeVisible();
  await expect(lifecyclePanel.getByText('physical_devices_present=0').first()).toBeVisible();

  const hasHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(hasHorizontalOverflow).toBe(false);
  expect(requestViolations).toEqual([]);
  expect(inferenceRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
});
