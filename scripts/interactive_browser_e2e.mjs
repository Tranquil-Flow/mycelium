#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const DEFAULT_CHROME_CANDIDATES = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
];

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
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  if (!port) fail('free_port_unavailable');
  return port;
}

async function waitFor(description, probe, timeoutMilliseconds = 45_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await probe();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(100);
  }
  const suffix = lastError instanceof Error ? `: ${lastError.message}` : '';
  fail(`${description}_timeout${suffix}`);
}

async function readJson(url, options = {}) {
  const response = await fetch(url, { ...options, cache: 'no-store' });
  if (!response.ok) fail(`http_${response.status}:${url}`);
  return response.json();
}

class CdpClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.exceptions = [];
    this.consoleErrors = [];
  }

  async connect() {
    this.socket = new WebSocket(this.url);
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
        return;
      }
      if (message.method === 'Runtime.exceptionThrown') {
        this.exceptions.push(message.params.exceptionDetails.text ?? 'browser_exception');
      }
      if (
        message.method === 'Runtime.consoleAPICalled'
        && ['error', 'assert'].includes(message.params.type)
      ) {
        this.consoleErrors.push(message.params.args.map((item) => item.value ?? item.description ?? '').join(' '));
      }
    });
    await new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve, { once: true });
      this.socket.addEventListener('error', reject, { once: true });
    });
    await this.send('Runtime.enable');
    await this.send('Page.enable');
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      fail(`browser_evaluation_failed:${result.exceptionDetails.text ?? 'unknown'}`);
    }
    return result.result.value;
  }

  close() {
    this.socket?.close();
  }
}

function chromePath() {
  const configured = process.env.CHROME_PATH;
  if (configured) {
    if (!existsSync(configured)) fail(`chrome_not_found:${configured}`);
    return configured;
  }
  const found = DEFAULT_CHROME_CANDIDATES.find(existsSync);
  if (!found) fail('chrome_not_found:set_CHROME_PATH');
  return found;
}

async function launchChrome({ executable, debugPort, profile, url }) {
  const child = spawn(executable, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-networking',
    '--disable-component-update',
    '--remote-allow-origins=*',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    url,
  ], { cwd: ROOT, stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', (chunk) => {
    stderr += String(chunk);
    if (stderr.length > 32_768) stderr = stderr.slice(-32_768);
  });
  const target = await waitFor('chrome_debug_target', async () => {
    if (child.exitCode !== null) fail(`chrome_exited_${child.exitCode}:${stderr.slice(-500)}`);
    const targets = await readJson(`http://127.0.0.1:${debugPort}/json/list`);
    return targets.find((item) => item.type === 'page' && item.webSocketDebuggerUrl);
  });
  const cdp = new CdpClient(target.webSocketDebuggerUrl);
  await cdp.connect();
  return { child, cdp, stderr: () => stderr };
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

async function main() {
  const executable = chromePath();
  const serverPort = await freePort();
  const hostDebugPort = await freePort();
  const peerOneDebugPort = await freePort();
  const peerTwoDebugPort = await freePort();
  const stateRoot = await mkdtemp(path.join(os.tmpdir(), 'mycelium-browser-e2e-state-'));
  const hostProfile = await mkdtemp(path.join(os.tmpdir(), 'mycelium-browser-e2e-host-'));
  const peerOneProfile = await mkdtemp(path.join(os.tmpdir(), 'mycelium-browser-e2e-peer-one-'));
  const peerTwoProfile = await mkdtemp(path.join(os.tmpdir(), 'mycelium-browser-e2e-peer-two-'));
  const origin = `http://127.0.0.1:${serverPort}`;
  const children = [];
  let host = null;
  const peers = [];

  const server = spawn('python3.14', [
    'scripts/interactive_swarm_server.py',
    '--host', '127.0.0.1',
    '--port', String(serverPort),
    '--state-root', stateRoot,
  ], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  children.push(server);
  let serverStdout = '';
  let serverStderr = '';
  server.stdout.on('data', (chunk) => { serverStdout += String(chunk); });
  server.stderr.on('data', (chunk) => { serverStderr += String(chunk); });

  try {
    const serverStart = await waitFor('interactive_server', async () => {
      if (server.exitCode !== null) fail(`server_exited_${server.exitCode}:${serverStderr.slice(-1000)}`);
      for (const line of serverStdout.split('\n')) {
        if (!line.trim()) continue;
        try {
          const value = JSON.parse(line);
          if (value.protocol === 'mycelium.interactive_server_started.v1') return value;
        } catch {
          // Wait for a complete startup line.
        }
      }
      return null;
    }, 60_000);
    if (serverStart.route_ready !== false || serverStart.local_evidence_only !== true) {
      fail('server_start_claim_boundary_invalid');
    }
    const operatorUrl = new URL(serverStart.operator_url);
    if (operatorUrl.origin !== origin || !operatorUrl.hash.startsWith('#operator/')) {
      fail('operator_url_invalid');
    }
    const operatorToken = operatorUrl.hash.slice('#operator/'.length);
    if (!operatorToken) fail('operator_capability_missing');
    const operatorOptions = { headers: { authorization: `Bearer ${operatorToken}` } };
    const statusAtStart = await readJson(`${origin}/api/interactive/status`, operatorOptions);
    if (statusAtStart.status.route_ready !== false) fail('interactive_server_not_ready');

    host = await launchChrome({
      executable,
      debugPort: hostDebugPort,
      profile: hostProfile,
      url: operatorUrl.href,
    });
    children.push(host.child);
    await waitFor('host_console', async () => (await host.cdp.evaluate(
      "document.body.innerText.includes('Mycelium browser swarm')",
    )) === true);
    const hostLocation = await host.cdp.evaluate("({href: location.href, hash: location.hash})");
    if (hostLocation.hash !== '' || hostLocation.href !== `${origin}/`) {
      fail('consumed_operator_fragment_not_cleared');
    }

    await host.cdp.evaluate("document.querySelector('#create-invite').click(); true");
    await waitFor('first_invite_url', async () => host.cdp.evaluate(
      "document.querySelectorAll('[data-invite-url]').length === 1",
    ), 5_000);
    await host.cdp.evaluate("document.querySelector('#create-invite').click(); true");
    const inviteUrls = await waitFor('second_invite_url', async () => {
      const values = await host.cdp.evaluate(
        "Array.from(document.querySelectorAll('[data-invite-url]'), (element) => element.value)",
      );
      return values.length === 2 ? values : null;
    });
    if (new Set(inviteUrls).size !== 2) fail('invite_urls_not_unique');
    if (!inviteUrls.every((value) => value.startsWith(`${origin}/#join/`))) {
      fail('invite_origin_invalid');
    }
    await host.cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 390,
      height: 844,
      deviceScaleFactor: 1,
      mobile: true,
    });
    const hostMobileLayout = await host.cdp.evaluate(`({
      innerWidth: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      inviteCount: document.querySelectorAll('[data-invite-url]').length,
      createVisible: !!document.querySelector('#create-invite')?.offsetParent,
    })`);
    if (
      hostMobileLayout.innerWidth !== 390
      || hostMobileLayout.scrollWidth > hostMobileLayout.innerWidth
      || hostMobileLayout.inviteCount !== 2
      || hostMobileLayout.createVisible !== true
    ) {
      fail(`host_mobile_layout_invalid:${JSON.stringify(hostMobileLayout)}`);
    }

    await host.cdp.evaluate(`(() => {
      const prompt = document.querySelector('#prompt');
      const tokens = document.querySelector('#max-new');
      prompt.value = 'two browser moon swarm';
      prompt.dispatchEvent(new Event('input', { bubbles: true }));
      tokens.value = '2';
      tokens.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`);
    await sleep(3_250);
    const preservedDraft = await host.cdp.evaluate(
      "document.querySelector('#prompt')?.value === 'two browser moon swarm'",
    );
    if (!preservedDraft) fail('host_draft_lost_during_status_refresh');

    const peerSpecs = [
      { debugPort: peerOneDebugPort, profile: peerOneProfile, url: inviteUrls[0] },
      { debugPort: peerTwoDebugPort, profile: peerTwoProfile, url: inviteUrls[1] },
    ];
    for (const [index, spec] of peerSpecs.entries()) {
      const peer = await launchChrome({ executable, ...spec });
      peers.push(peer);
      children.push(peer.child);
      await waitFor(`peer_${index + 1}_join`, async () => (await peer.cdp.evaluate(
        "document.body.innerText.includes('State\\nrunning')",
      )) === true);
      const peerLocation = await peer.cdp.evaluate(
        "({href: location.href, hash: location.hash, subtle: !!crypto.subtle})",
      );
      if (peerLocation.hash !== '' || peerLocation.href !== `${origin}/`) {
        fail(`peer_${index + 1}_consumed_invite_fragment_not_cleared`);
      }
      if (peerLocation.subtle !== true) fail(`peer_${index + 1}_browser_crypto_unavailable`);
      await peer.cdp.send('Emulation.setDeviceMetricsOverride', {
        width: 390,
        height: 844,
        deviceScaleFactor: 1,
        mobile: true,
      });
      const peerMobileLayout = await peer.cdp.evaluate(`({
        innerWidth: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
        stopVisible: !!document.querySelector('#stop-peer')?.offsetParent,
      })`);
      if (
        peerMobileLayout.innerWidth !== 390
        || peerMobileLayout.scrollWidth > peerMobileLayout.innerWidth
        || peerMobileLayout.stopVisible !== true
      ) {
        fail(`peer_${index + 1}_mobile_layout_invalid:${JSON.stringify(peerMobileLayout)}`);
      }
    }

    await waitFor('host_peer_status', async () => {
      const value = await readJson(`${origin}/api/interactive/status`, operatorOptions);
      return value.status.peer_count === 2 && value.status.ready_peer_count === 2;
    });
    await waitFor('host_request_enabled', async () => (await host.cdp.evaluate(
      "document.querySelector('#request-form button')?.disabled === false",
    )) === true);
    await host.cdp.evaluate("document.querySelector('#request-form button').click(); true");

    const status = await waitFor('browser_inference', async () => {
      const value = await readJson(`${origin}/api/interactive/status`, operatorOptions);
      return value.status.completed_request_count === 1 ? value.status : null;
    }, 60_000);
    const record = status.recent_requests.at(-1);
    if (!record) fail('inference_record_missing');
    if (record.route_ready !== false || record.local_evidence_only !== true) {
      fail('claim_boundary_invalid');
    }
    const peerJobCounts = status.peers.map((item) => item.completed_jobs).sort((a, b) => a - b);
    if (
      record.generated_tokens.length !== 2
      || new Set(record.peer_ids).size !== 2
      || peerJobCounts.length !== 2
      || peerJobCounts[0] !== 1
      || peerJobCounts[1] !== 1
    ) {
      fail(`browser_stage_distribution_invalid:tokens=${record.generated_tokens.length}:record=${JSON.stringify(record.peer_ids)}:status=${JSON.stringify(status.peers)}`);
    }
    if (record.max_intermediate_error >= 1e-6 || record.max_logit_error >= 2e-6) {
      fail('browser_parity_tolerance_exceeded');
    }
    await waitFor('host_evidence_render', async () => (await host.cdp.evaluate(
      "document.body.innerText.includes('Inference completed with local browser-stage evidence.')",
    )) === true);
    for (const [index, peer] of peers.entries()) {
      await waitFor(`peer_${index + 1}_job_render`, async () => (await peer.cdp.evaluate(
        "document.body.innerText.includes('Completed jobs\\n1')",
      )) === true);
      await peer.cdp.evaluate("document.querySelector('#stop-peer').click(); true");
      await waitFor(`peer_${index + 1}_clean_stop`, async () => (await peer.cdp.evaluate(
        "document.body.innerText.includes('State\\nstopped') && !document.querySelector('[role=alert]')",
      )) === true, 5_000);
    }
    await peers[0].cdp.send('Page.navigate', { url: 'about:blank' });
    await waitFor('peer_blank_before_reuse', async () => (await peers[0].cdp.evaluate(
      "location.href === 'about:blank'",
    )) === true, 5_000);
    await peers[0].cdp.send('Page.navigate', { url: inviteUrls[0] });
    await waitFor('consumed_invite_reuse_failure', async () => (await peers[0].cdp.evaluate(
      "document.body.innerText.includes('State\\nfailed') && document.querySelector('[role=alert]')?.textContent.includes('invite_invalid_or_consumed')",
    )) === true, 5_000);
    const reusedInviteLocation = await peers[0].cdp.evaluate(
      "({ href: location.href, hash: location.hash })",
    );
    if (reusedInviteLocation.href !== `${origin}/` || reusedInviteLocation.hash !== '') {
      fail('reused_invite_fragment_not_cleared');
    }

    if (host.cdp.exceptions.length || host.cdp.consoleErrors.length) fail('host_browser_console_error');
    for (const [index, peer] of peers.entries()) {
      if (peer.cdp.exceptions.length || peer.cdp.consoleErrors.length) {
        fail(`peer_${index + 1}_browser_console_error`);
      }
    }

    console.log(JSON.stringify({
      protocol: 'mycelium.interactive_browser_e2e.v2',
      host_browser_process: true,
      peer_browser_processes: peers.length,
      independent_browser_profiles: 1 + peers.length,
      operator_capability_required: true,
      consumed_operator_fragment_cleared: true,
      one_use_weblinks_joined: inviteUrls.length,
      consumed_peer_fragments_cleared: true,
      mobile_viewport_checks: { host: true, peers: peers.length },
      draft_survived_status_refresh: true,
      peer_completed_jobs: peerJobCounts,
      clean_peer_stops: peers.length,
      consumed_invite_reuse_rejected: true,
      request_id: record.request_id,
      generated_labels: record.generated_labels,
      max_intermediate_error: record.max_intermediate_error,
      max_logit_error: record.max_logit_error,
      browser_console_errors: 0,
      route_ready: false,
      local_evidence_only: true,
    }, null, 2));
  } finally {
    host?.cdp.close();
    for (const peer of peers) peer.cdp.close();
    for (const child of children.reverse()) await stopChild(child);
    await Promise.all([
      rm(stateRoot, { recursive: true, force: true }),
      rm(hostProfile, { recursive: true, force: true }),
      rm(peerOneProfile, { recursive: true, force: true }),
      rm(peerTwoProfile, { recursive: true, force: true }),
    ]);
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack : String(error));
  process.exitCode = 1;
});
