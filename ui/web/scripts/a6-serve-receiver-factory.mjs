#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A6 live receiver runtime factory: one fresh product serve per browser engine.
// The serve owns the grant-gated A6 receiver; this factory only reads what that
// serve reports about itself (startup line, then its post-teardown witness).
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { closeSync, openSync, readFileSync } from 'node:fs';
import { rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { runLiveMatrix, sourceBinding } from './a6-product-browser-matrix.mjs';

const EXACT_FLEET_GRANT = 'FLEET GRANTED TO A6';
const ENGINES = Object.freeze(['chromium', 'firefox', 'webkit']);
const STARTED_PROTOCOL = 'mycelium.live_server_started.v1';
const TEARDOWN_PROTOCOL = 'mycelium.live_server_a6_fault_control_stopped.v1';
const root = fileURLToPath(new URL('../../../', import.meta.url));

function fail(code) {
  throw new Error(code);
}

function hex64(value) {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
}

function loopbackOrigin(raw) {
  try {
    const url = new URL(raw);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port) return null;
    return url.origin;
  } catch {
    return null;
  }
}

export function candidateBindingFromManifest(repoRoot = root) {
  const bytes = readFileSync(path.join(repoRoot, 'artifacts/a6/source-manifest-v1.json'));
  return `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
}

// Protocol lines only; other serve output is ignored and never echoed.
function protocolLines(child) {
  const reader = createInterface({ input: child.stdout, crlfDelay: Infinity });
  const queue = [];
  let waiter = null;
  let ended = false;
  reader.on('line', (line) => {
    if (!line.startsWith('{')) return;
    let value;
    try { value = JSON.parse(line); } catch { return; }
    if (!value || typeof value.protocol !== 'string') return;
    queue.push(value);
    if (waiter) waiter();
  });
  reader.on('close', () => { ended = true; if (waiter) waiter(); });
  async function next(protocol, timeoutMs, timeoutCode) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const index = queue.findIndex((item) => item.protocol === protocol);
      if (index >= 0) return queue.splice(index, 1)[0];
      if (ended) return null;
      const remaining = deadline - Date.now();
      if (remaining <= 0) fail(timeoutCode);
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, remaining);
        waiter = () => { clearTimeout(timer); waiter = null; resolve(); };
      });
    }
  }
  return { next, close: () => reader.close() };
}

// command/args start `mycelium_demo serve --mode live ... --a6-fault-control-port N`.
export function createServeReceiverFactory({ command, args = [], cwd = root, env = process.env,
  readyTimeoutMs = 600_000, stopTimeoutMs = 120_000, maximumNewTokens = 64,
  stderr = () => 'ignore', spawnImpl = spawn } = {}) {
  if (typeof command !== 'string' || !command || !Array.isArray(args)) fail('serve_command_required');
  if (!Number.isSafeInteger(maximumNewTokens) || maximumNewTokens < 8 || maximumNewTokens > 128) {
    fail('browser_token_bound_invalid');
  }
  async function start(engine) {
    if (!ENGINES.includes(engine)) fail('browser_engine_selection_required');
    const sink = stderr(engine);
    const child = spawnImpl(command, args, { cwd, env, stdio: ['ignore', 'pipe', sink] });
    if (typeof sink === 'number') closeSync(sink); // the child holds its own copy
    const exited = new Promise((resolve) => child.once('close', (code) => resolve(code)));
    const lines = protocolLines(child);
    let started;
    try {
      started = await lines.next(STARTED_PROTOCOL, readyTimeoutMs, 'serve_receiver_startup_timeout');
      const control = started?.a6_fault_control;
      const origin = loopbackOrigin(started?.url);
      if (!started || started.route_ready !== true || started.simulated !== false || !origin
        || !control || control.mounted !== true || !hex64(control.lifetime_id)
        || loopbackOrigin(control.url) === null || loopbackOrigin(control.url) === origin) {
        fail('serve_receiver_startup_invalid');
      }
      started = { origin, url: control.url, lifetime_id: control.lifetime_id };
    } catch (error) {
      child.kill('SIGKILL');
      lines.close();
      await exited;
      throw error;
    }
    let closed = false;
    async function close() {
      if (closed) fail('serve_receiver_already_closed');
      closed = true;
      child.kill('SIGTERM');
      let teardown = null;
      try {
        teardown = await lines.next(TEARDOWN_PROTOCOL, stopTimeoutMs, 'serve_receiver_teardown_invalid');
      } catch { teardown = null; }
      const timer = setTimeout(() => child.kill('SIGKILL'), stopTimeoutMs);
      const code = await exited;
      clearTimeout(timer);
      lines.close();
      if (!teardown || code !== 0 || teardown.lifetime_id !== started.lifetime_id
        || teardown.torn_down !== true || !Array.isArray(teardown.executed_case_ids)) {
        fail('serve_receiver_teardown_invalid');
      }
      // executed_case_ids holds only the receiver's 200-completed executions.
      const caseIds = [...teardown.executed_case_ids];
      return { lifetime_id: teardown.lifetime_id, source_binding: sourceBinding(),
        case_ids: caseIds, statuses: caseIds.map(() => 200), torn_down: true };
    }
    return { lifetime_id: started.lifetime_id, origin: started.origin,
      positiveUrl: started.url, negativeUrl: started.url, maximumNewTokens, close };
  }
  return Object.freeze({ start });
}

async function atomicPrivateWrite(target, text) {
  const temporary = `${target}.${process.pid}.tmp`;
  await writeFile(temporary, text, { mode: 0o600, flag: 'wx' });
  await rename(temporary, target);
}

async function main() {
  const argv = process.argv.slice(2);
  const split = argv.indexOf('--');
  const options = split < 0 ? argv : argv.slice(0, split);
  const serve = split < 0 ? [] : argv.slice(split + 1);
  // Authorization stays first; nothing is spawned or contacted without it.
  if (process.env.MYCELIUM_A6_FLEET_GRANT !== EXACT_FLEET_GRANT) fail('explicit_fleet_grant_required');
  const option = (name) => options.find((item) => item.startsWith(`--${name}=`))?.slice(name.length + 3);
  const evidence = option('evidence');
  if (!evidence || !path.isAbsolute(evidence)) fail('browser_evidence_path_required');
  const logDir = option('serve-log-dir');
  if (logDir !== undefined && !path.isAbsolute(logDir)) fail('serve_log_dir_invalid');
  if (serve.length === 0) fail('serve_command_required');
  const factory = createServeReceiverFactory({
    command: serve[0], args: serve.slice(1),
    maximumNewTokens: Number(process.env.MYCELIUM_A6_BROWSER_TOKENS ?? '64'),
    stderr: logDir === undefined ? () => 'ignore'
      : (engine) => openSync(path.join(logDir, `a6-serve-${engine}.log`), 'a', 0o600),
  });
  const result = await runLiveMatrix({ factory,
    candidate_binding: option('candidate-binding') ?? candidateBindingFromManifest() });
  await atomicPrivateWrite(evidence, `${JSON.stringify(result, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify({ passed: result.passed, evidence })}\n`);
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : 'a6_serve_matrix_failed'}\n`);
    process.exitCode = 1;
  });
}
