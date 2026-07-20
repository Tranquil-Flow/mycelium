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
  await expect(navigation.getByRole('link')).toHaveCount(7);

  const routeChecks = [
    ['Network', 'network', /network topology/i],
    ['Plans', 'plans', /strategy comparison/i],
    ['Incidents', 'incidents', /failover replay/i],
    ['Readiness', 'readiness', /proof matrix/i],
    ['Nodes', 'nodes', /^nodes$/i],
    ['Settings', 'settings', /^settings$/i],
    ['Inference', 'inference', /^inference$/i],
  ] as const;

  for (const [label, hash, heading] of routeChecks) {
    await navigation.getByRole('link', { name: new RegExp(`^${label}`) }).click();
    await expect(page).toHaveURL(new RegExp(`#${hash}$`));
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
  }

  const hasHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(hasHorizontalOverflow).toBe(false);
  expect(requestViolations).toEqual([]);
  expect(inferenceRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
});
