#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import https from 'node:https';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { chromium, firefox, webkit } from '@playwright/test';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

function fail(message) {
  throw new Error(message);
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  const port = typeof address === 'object' && address ? address.port : null;
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  if (!port) fail('free_port_unavailable');
  return port;
}

async function waitFor(description, probe, timeoutMilliseconds = 60_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const result = await probe();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await sleep(100);
  }
  const suffix = lastError instanceof Error ? `:${lastError.message}` : '';
  fail(`${description}_timeout${suffix}`);
}

async function readJson(url, options = {}) {
  const response = await fetch(url, { ...options, cache: 'no-store' });
  const document = await response.json().catch(() => null);
  if (!response.ok || document?.ok !== true) {
    fail(document?.error ?? `http_${response.status}:${url}`);
  }
  return document;
}

async function readTrustedJson(url, options = {}, ca = null) {
  if (ca === null) return readJson(url, options);
  return new Promise((resolve, reject) => {
    const request = https.request(url, {
      method: 'GET',
      headers: options.headers ?? {},
      ca,
    }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.once('end', () => {
        const status = response.statusCode ?? 0;
        let document = null;
        try { document = JSON.parse(body); } catch { /* handled below */ }
        if (status < 200 || status >= 300 || document?.ok !== true) {
          reject(new Error(document?.error ?? `http_${status}:${url}`));
          return;
        }
        resolve(document);
      });
    });
    request.once('error', reject);
    request.end();
  });
}

async function serverStart(child) {
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += String(chunk); });
  child.stderr.on('data', (chunk) => { stderr += String(chunk); });
  return waitFor('interactive_server', async () => {
    if (child.exitCode !== null) fail(`server_exited_${child.exitCode}:${stderr.slice(-1000)}`);
    for (const line of stdout.split('\n')) {
      if (!line.trim()) continue;
      try {
        const value = JSON.parse(line);
        if (value.protocol === 'mycelium.interactive_server_started.v1') return value;
      } catch {
        // Wait for a complete startup line.
      }
    }
    return null;
  });
}

async function stopChild(child) {
  if (!child || child.exitCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([
    new Promise((resolve) => child.once('exit', resolve)),
    sleep(2_000),
  ]);
  if (child.exitCode === null) child.kill('SIGKILL');
}

function captureBrowserFailures(page, name, failures) {
  page.on('pageerror', (error) => failures.push(`${name}:pageerror:${error.message}`));
  page.on('console', (message) => {
    if (['error', 'assert'].includes(message.type())) {
      failures.push(`${name}:console:${message.text()}`);
    }
  });
}

async function mobileMetrics(page) {
  await page.setViewportSize({ width: 390, height: 844 });
  return page.evaluate(() => ({
    innerWidth: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    routeBoundary: document.body.innerText.includes('route_ready=false'),
  }));
}

async function main() {
  const serverPort = await freePort();
  const deviceLabHost = process.env.MYCELIUM_DEVICE_LAB_HOST?.trim() || null;
  const suppliedStateRoot = process.env.MYCELIUM_DEVICE_LAB_STATE_ROOT?.trim() || null;
  const origin = deviceLabHost
    ? `https://${deviceLabHost}:${serverPort}`
    : `http://127.0.0.1:${serverPort}`;
  const stateRoot = suppliedStateRoot
    ? path.resolve(suppliedStateRoot)
    : await mkdtemp(path.join(os.tmpdir(), 'mycelium-multibrowser-state-'));
  const removeStateRoot = suppliedStateRoot === null;
  const downloadRoot = await mkdtemp(path.join(os.tmpdir(), 'mycelium-multibrowser-download-'));
  const serverArguments = deviceLabHost
    ? [
      '-m', 'mycelium_demo',
      'device-lab',
      '--advertise-host', deviceLabHost,
      '--port', String(serverPort),
      '--state-root', stateRoot,
      '--static-root', path.join(ROOT, 'ui', 'web', 'dist'),
      '--worker-static-root', path.join(ROOT, 'mycelium_interactive', 'static'),
    ]
    : [
      '-m', 'mycelium_demo',
      'serve',
      '--mode', 'live',
      '--host', '127.0.0.1',
      '--port', String(serverPort),
      '--state-root', stateRoot,
      '--static-root', path.join(ROOT, 'ui', 'web', 'dist'),
      '--worker-static-root', path.join(ROOT, 'mycelium_interactive', 'static'),
    ];
  const server = spawn('python3.14', serverArguments, {
    cwd: ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const browsers = [];
  const failures = [];

  try {
    const started = await serverStart(server);
    const ca = deviceLabHost
      ? await readFile(path.join(stateRoot, 'tls', 'mycelium-device-lab-ca.crt'))
      : null;
    const readApiJson = (url, options) => readTrustedJson(url, options, ca);
    if (started.route_ready !== false || started.local_evidence_only !== true) {
      fail('server_claim_boundary_invalid');
    }
    const operatorUrl = new URL(started.operator_url);
    const operatorPrefix = '#lab/operator/';
    const operatorToken = operatorUrl.hash.startsWith(operatorPrefix)
      ? operatorUrl.hash.slice(operatorPrefix.length)
      : '';
    if (operatorUrl.origin !== origin || !operatorToken) fail('operator_capability_invalid');
    const operatorOptions = { headers: { authorization: `Bearer ${operatorToken}` } };

    const engineSpecs = [
      { name: 'chromium', engine: chromium, role: 'operator' },
      { name: 'firefox', engine: firefox, role: 'peer-1' },
      { name: 'webkit', engine: webkit, role: 'peer-2' },
    ];
    const pages = {};
    const userAgents = {};
    for (const spec of engineSpecs) {
      const browser = await spec.engine.launch({ headless: true });
      browsers.push(browser);
      const context = await browser.newContext({
        viewport: { width: 1280, height: 900 },
        ignoreHTTPSErrors: deviceLabHost !== null,
      });
      const page = await context.newPage();
      captureBrowserFailures(page, spec.name, failures);
      pages[spec.role] = page;
      userAgents[spec.name] = await page.evaluate(() => navigator.userAgent);
    }

    const operator = pages.operator;
    await operator.goto(operatorUrl.href, { waitUntil: 'domcontentloaded' });
    await operator.getByRole('heading', { name: 'Device Lab', exact: true }).waitFor();
    const operatorLocation = await operator.evaluate(() => ({ href: location.href, hash: location.hash }));
    if (
      operatorLocation.href !== `${origin}/#lab`
      || operatorLocation.hash !== '#lab'
      || operatorLocation.href.includes(operatorToken)
    ) {
      fail('operator_fragment_not_cleared');
    }
    await operator.getByLabel('Invite count').fill('2');
    await operator.getByRole('button', { name: 'Create 2 one-use links' }).click();
    try {
      await operator.locator('[data-invite-url]').nth(1).waitFor();
    } catch (error) {
      const diagnostic = (await operator.locator('body').innerText()).slice(-2_000);
      fail(`invite_render_failed:${error instanceof Error ? error.message : String(error)}:${diagnostic}:browser_failures=${failures.join('|')}`);
    }
    const inviteUrls = await operator.locator('[data-invite-url]').evaluateAll(
      (elements) => elements.map((element) => element.getAttribute('data-invite-url')),
    );
    if (
      inviteUrls.length !== 2
      || inviteUrls.some((value) => typeof value !== 'string')
      || new Set(inviteUrls).size !== 2
    ) fail('invite_urls_invalid');

    await Promise.all([
      pages['peer-1'].goto(inviteUrls[0], { waitUntil: 'domcontentloaded' }),
      pages['peer-2'].goto(inviteUrls[1], { waitUntil: 'domcontentloaded' }),
    ]);
    await Promise.all([
      pages['peer-1'].waitForFunction(() => document.body.innerText.includes('State\nrunning')),
      pages['peer-2'].waitForFunction(() => document.body.innerText.includes('State\nrunning')),
    ]);
    for (const role of ['peer-1', 'peer-2']) {
      const location = await pages[role].evaluate(() => ({
        href: window.location.href,
        hash: window.location.hash,
        preflightPassCount: document.querySelectorAll('[data-device-check][data-check-state=pass]').length,
      }));
      if (location.href !== `${origin}/device` || location.hash !== '') fail(`${role}_fragment_not_cleared`);
      if (location.preflightPassCount !== 5) fail(`${role}_device_preflight_incomplete`);
    }

    await waitFor('two_cross_engine_peers_ready', async () => {
      const document = await readApiJson(`${origin}/api/interactive/status`, operatorOptions);
      return document.status.ready_peer_count === 2 ? document.status : null;
    });
    await operator.getByText('Minimum 2 distinct peer sessions met', { exact: true }).waitFor({ timeout: 30_000 });
    await operator.waitForFunction(
      () => Array.from(document.querySelectorAll('button')).some((button) => (
        button.textContent?.trim() === 'Run local evidence request' && !button.disabled
      )),
    );
    await operator.getByLabel('Prompt seed').fill('cross engine moon swarm');
    await operator.getByLabel('Maximum fixture tokens').fill('2');
    await operator.getByLabel('Minimum distinct peer sessions').fill('2');
    await operator.getByRole('button', { name: 'Run local evidence request' }).click();
    await operator.waitForFunction(
      () => document.querySelectorAll('[data-token-evidence]').length === 2,
      null,
      { timeout: 60_000 },
    );

    const status = await waitFor('cross_engine_inference', async () => {
      const document = await readApiJson(`${origin}/api/interactive/status`, operatorOptions);
      return document.status.completed_request_count === 1 ? document.status : null;
    });
    const record = status.recent_requests.at(-1);
    if (!record) fail('inference_record_missing');
    const evidenceText = await operator.locator('body').innerText();
    if (!evidenceText.includes('2 / 2 exact peer sessions')) fail('rendered_peer_session_proof_missing');
    const completedJobs = status.peers.map((peer) => peer.completed_jobs).sort((left, right) => left - right);
    if (
      record.route_ready !== false
      || record.local_evidence_only !== true
      || record.generated_tokens.length !== 2
      || record.required_distinct_peers !== 2
      || record.observed_distinct_peers !== 2
      || new Set(record.peer_ids).size !== 2
      || completedJobs.length !== 2
      || completedJobs[0] !== 1
      || completedJobs[1] !== 1
    ) {
      fail('cross_engine_distribution_invalid');
    }
    if (record.max_intermediate_error >= 1e-6 || record.max_logit_error >= 2e-6) {
      fail('cross_engine_parity_tolerance_exceeded');
    }

    const downloadPromise = operator.waitForEvent('download');
    await operator.getByRole('button', { name: 'Download local evidence JSON' }).click();
    const download = await downloadPromise;
    const evidencePath = path.join(downloadRoot, download.suggestedFilename());
    await download.saveAs(evidencePath);
    const downloadedEvidence = JSON.parse(await readFile(evidencePath, 'utf8'));
    if (
      downloadedEvidence.request_id !== record.request_id
      || downloadedEvidence.stage_pack_digest !== record.stage_pack_digest
      || downloadedEvidence.route_ready !== false
      || downloadedEvidence.local_evidence_only !== true
      || downloadedEvidence.required_distinct_peers !== 2
      || downloadedEvidence.observed_distinct_peers !== 2
    ) {
      fail('cross_engine_downloaded_evidence_invalid');
    }

    const responsive = {
      operator: await mobileMetrics(operator),
      firefoxPeer: await mobileMetrics(pages['peer-1']),
      webkitPeer: await mobileMetrics(pages['peer-2']),
    };
    if (Object.values(responsive).some((item) => (
      item.innerWidth !== 390 || item.scrollWidth > item.innerWidth || item.routeBoundary !== true
    ))) {
      fail(`cross_engine_mobile_layout_invalid:${JSON.stringify(responsive)}`);
    }

    await Promise.all([
      pages['peer-1'].locator('#stop-peer').click(),
      pages['peer-2'].locator('#stop-peer').click(),
    ]);
    await Promise.all([
      pages['peer-1'].waitForFunction(() => document.body.innerText.includes('State\nstopped')),
      pages['peer-2'].waitForFunction(() => document.body.innerText.includes('State\nstopped')),
    ]);
    if (failures.length) fail(`browser_failures:${failures.join('|')}`);

    console.log(JSON.stringify({
      protocol: 'mycelium.interactive_multibrowser_e2e.v1',
      exact_launch_entrypoint: deviceLabHost
        ? 'python3.14 -m mycelium_demo device-lab --advertise-host <LAN_IP>'
        : 'python3.14 -m mycelium_demo serve --mode live',
      network_origin: deviceLabHost ? 'lan_address_https' : 'loopback_http',
      node_https_api_verified: deviceLabHost !== null,
      browser_certificate_errors_ignored: deviceLabHost !== null,
      engines: ['chromium', 'firefox', 'webkit'],
      roles: { operator: 'chromium', peer_1: 'firefox', peer_2: 'webkit' },
      independent_browser_contexts: 3,
      real_browser_stage_jobs: completedJobs,
      evidence_json_downloaded_and_parsed: true,
      responsive_viewport_checks: responsive,
      browser_failures: failures.length,
      physical_devices: 0,
      request_id: record.request_id,
      generated_labels: record.generated_labels,
      max_intermediate_error: record.max_intermediate_error,
      max_logit_error: record.max_logit_error,
      route_ready: false,
      local_evidence_only: true,
      evidence: downloadedEvidence,
      user_agent_families: Object.fromEntries(
        Object.entries(userAgents).map(([name, value]) => [name, value.split(' ').slice(0, 3).join(' ')]),
      ),
    }, null, 2));
  } finally {
    await Promise.allSettled(browsers.map((browser) => browser.close()));
    await stopChild(server);
    const cleanup = [rm(downloadRoot, { recursive: true, force: true })];
    if (removeStateRoot) cleanup.push(rm(stateRoot, { recursive: true, force: true }));
    await Promise.all(cleanup);
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack : String(error));
  process.exitCode = 1;
});
