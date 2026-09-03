#!/usr/bin/env node
import { chromium, firefox, webkit } from '@playwright/test';
import { mkdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const origin = (process.env.MYCELIUM_A5_PRODUCT_ORIGIN ?? '').replace(/\/$/, '');
const outputPath = process.env.MYCELIUM_A5_BROWSER_EVIDENCE ?? '';
const browserPhase = process.env.MYCELIUM_A5_BROWSER_PHASE ?? '';
const closureWords = /(?:^|\s)(?:Completed|Cancelled|Failed|Cancellation unconfirmed)/;

const OPTIONAL_CAPABILITY_PATHS = Object.freeze([
  '/__mycelium/models/operation',
  '/__mycelium/model-capacity-refresh',
  '/__mycelium/model-preparation',
  '/__mycelium/deployment-activation',
  '/__mycelium/planning/workload-comparison',
  '/__mycelium/governance-readiness',
]);

const WORKSPACES = Object.freeze([
  ['Inference', 'inference', 'tracks'],
  ['Device Lab', 'lab', 'tracks'],
  ['Network', 'network', 'tracks'],
  ['Nodes', 'nodes', 'tracks'],
  ['Plans', 'plans', 'tracks'],
  ['Readiness', 'readiness', 'qualification'],
  ['Incidents', 'incidents', 'loss'],
  ['Settings', 'settings', 'qualification'],
]);

function fail(code) {
  throw new Error(code);
}

async function atomicWrite(target, value) {
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: 'utf8',
    mode: 0o600,
  });
  await rename(temporary, target);
}

function observePage(page, engine, failures, offlineWindow = null) {
  page.on('pageerror', () => failures.push(`${engine}:pageerror`));
  page.on('console', (message) => {
    if (!['error', 'assert'].includes(message.type())) return;
    const locationUrl = message.location().url;
    const optional404 = message.text().includes('404')
      && locationUrl.startsWith(origin)
      && OPTIONAL_CAPABILITY_PATHS.some((suffix) => {
        const target = new URL(locationUrl);
        return target.pathname === suffix;
      });
    if (optional404) return;
    const expectedOfflineFailure = offlineWindow?.active === true
      && locationUrl.startsWith(origin)
      && /(?:ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|WebKit encountered an internal error)/.test(
        message.text(),
      );
    if (expectedOfflineFailure) return;
    const location = locationUrl.startsWith(origin)
      ? new URL(locationUrl).pathname
      : 'unknown';
    const detail = message.text().replace(/[^A-Za-z0-9_.:-]+/g, '_').slice(0, 120);
    failures.push(`${engine}:console_error:${location}:${detail}`);
  });
  page.on('request', (request) => {
    const target = new URL(request.url());
    if (target.origin !== origin) failures.push(`${engine}:cross_origin_request`);
    const headers = request.headers();
    if (
      headers.authorization !== undefined
      || headers['proxy-authorization'] !== undefined
    ) {
      failures.push(`${engine}:ambient_authorization_header`);
    }
  });
}

async function liveReplicaState() {
  const response = await fetch(`${origin}/__mycelium/live-status`, {
    headers: { Accept: 'application/json' },
    redirect: 'error',
  });
  if (!response.ok) fail(`live_status_${response.status}`);
  const status = await response.json();
  if (status.route_alive !== true) fail('route_not_alive');
  if (status.simulated !== false) fail('physical_route_required');
  if (typeof status.deployment_id !== 'string' || status.deployment_id.length === 0) {
    fail('deployment_binding_missing');
  }
  const qualifications = status.replica_track_qualification;
  const losses = status.replica_loss_placement_ids;
  if (!Array.isArray(qualifications) || qualifications.length === 0) {
    fail('replica_qualification_missing');
  }
  if (!Array.isArray(losses)) fail('replica_loss_projection_invalid');
  if (browserPhase === 'positive' && losses.length !== 0) {
    fail('positive_phase_requires_intact_replica');
  }
  if (browserPhase === 'degraded' && losses.length === 0) {
    fail('executed_replica_loss_required');
  }
  const nowUnixMs = Date.now();
  const qualificationIds = new Set();
  for (const qualification of qualifications) {
    if (qualification.deployment_id !== status.deployment_id) {
      fail('replica_deployment_binding_invalid');
    }
    if (qualification.route_ready !== true) fail('replica_not_route_ready');
    if (qualification.expires_at_unix_ms <= nowUnixMs) {
      fail('replica_qualification_expired');
    }
    if (!Array.isArray(qualification.placement_ids)
      || qualification.placement_ids.length < 2
      || !qualification.placement_ids.includes(qualification.placement_id)) {
      fail('replica_complete_track_invalid');
    }
    if (qualificationIds.has(qualification.qualification_id)) {
      fail('duplicate_replica_qualification');
    }
    qualificationIds.add(qualification.qualification_id);
  }
  const lossSet = new Set(losses);
  const degraded = qualifications.filter((qualification) =>
    qualification.placement_ids.some((placementId) => lossSet.has(placementId))
  );
  if (browserPhase === 'degraded' && degraded.length === 0) {
    fail('replica_degradation_not_observed');
  }
  return {
    qualifications,
    losses: lossSet,
    nowUnixMs,
    deploymentId: status.deployment_id,
    topologyVersion: status.topology_version,
    routeIdentityDigest: status.route_identity_digest,
  };
}

function expectedTrackState(qualification, losses) {
  if (qualification.placement_ids.some((placementId) => losses.has(placementId))) {
    return 'placement lost';
  }
  if (qualification.route_ready === true) return 'qualified';
  return qualification.rejected_reasons.join(' ').replaceAll('_', ' ');
}

async function verifyPanel(page, hash, view, replicaState) {
  const panel = page.locator(
    `section[aria-label="${view} request-level stage replication"]`,
  );
  await panel.waitFor({ state: 'visible', timeout: 60_000 });
  const text = await panel.innerText();
  const normalizedText = text.toLowerCase();
  if (!normalizedText.includes('request-level stage replication')) {
    fail(`replica_title_missing_${hash}`);
  }
  if (!normalizedText.includes('data parallel')) fail(`data_parallel_missing_${hash}`);
  if (!normalizedText.includes('exactly one complete legal track')) {
    fail(`request_level_boundary_missing_${hash}`);
  }

  const rows = panel.locator('tbody tr');
  if (await rows.count() !== replicaState.qualifications.length) {
    fail(`replica_row_count_invalid_${hash}`);
  }

  for (const qualification of replicaState.qualifications) {
    if (!text.includes(qualification.placement_id)) {
      fail(`placement_missing_${hash}`);
    }
    const row = panel.locator(
      `tbody tr[data-qualification-id="${qualification.qualification_id}"]`,
    );
    if (await row.count() !== 1) fail(`replica_row_identity_invalid_${hash}`);
    await row.waitFor({ state: 'visible', timeout: 30_000 });
    const rowText = await row.innerText();
    const rowHeader = (await row.locator('th').innerText()).trim();
    const cells = (await row.locator('td').allTextContents()).map((value) => value.trim());
    if (view === 'tracks') {
      const expectedTrackId = qualification.track_id.startsWith('sha256:')
        ? `${qualification.track_id.slice(0, 15)}…`
        : qualification.track_id;
      if (rowHeader !== expectedTrackId) fail(`track_identity_invalid_${hash}`);
      const expectedCells = [
        qualification.placement_id,
        qualification.replica_group_id,
        String(qualification.qualifier_generation),
        expectedTrackState(qualification, replicaState.losses),
      ];
      if (JSON.stringify(cells) !== JSON.stringify(expectedCells)) {
        fail(`track_fields_invalid_${hash}`);
      }
    } else if (view === 'qualification') {
      if (rowHeader !== qualification.placement_id) {
        fail(`qualification_identity_invalid_${hash}`);
      }
      const expectedCells = [
        qualification.parity_verified,
        qualification.startup_challenge_passed,
        qualification.memory_within_bounds,
        qualification.cleanup_within_bounds,
        qualification.directed_link_qualified,
      ].map((value) => value ? 'pass' : 'fail');
      expectedCells.push(new Date(qualification.expires_at_unix_ms).toISOString());
      if (JSON.stringify(cells) !== JSON.stringify(expectedCells)) {
        fail(`qualification_fields_invalid_${hash}`);
      }
    } else {
      if (rowHeader !== qualification.placement_id) {
        fail(`loss_identity_invalid_${hash}`);
      }
      const expected = qualification.placement_ids.some(
        (placementId) => replicaState.losses.has(placementId),
      )
        ? 'lost — new admission blocked'
        : 'surviving';
      if (cells.length !== 1 || cells[0] !== expected) {
        fail(`loss_state_invalid_${hash}`);
      }
    }
    if (rowText.length === 0) fail(`replica_row_empty_${hash}`);
  }
}

async function terminalPhase(page, timeout = 300_000) {
  const status = page
    .locator('section[aria-labelledby="output-title"] [role="status"]')
    .filter({ hasText: closureWords })
    .first();
  await status.waitFor({ state: 'visible', timeout });
  const text = (await status.innerText()).trim();
  const match = text.match(/^(Completed|Cancelled|Failed|Cancellation unconfirmed)\b/);
  if (match === null) fail('terminal_phase_invalid');
  return match[1].toLowerCase().replace(/\s+/g, '_');
}

async function prepareInference(page, canary) {
  await page.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('navigation', { name: 'Product sections' }).waitFor({ timeout: 60_000 });
  await page.waitForFunction(
    () => document.body.dataset.evidence === 'connected',
    null,
    { timeout: 60_000 },
  );
  const prompt = page.getByRole('textbox', { name: /prompt/i });
  await prompt.fill(`${canary} Explain request-scoped cleanup in one sentence.`);
  await page.getByLabel(/maximum new tokens/i).fill('64');
  const start = page.getByRole('button', { name: /start inference/i });
  await page.waitForFunction(() => {
    const button = [...document.querySelectorAll('button')]
      .find((item) => /start inference/i.test(item.textContent ?? ''));
    return button !== undefined && button.disabled === false;
  }, null, { timeout: 60_000, polling: 250 });
  return start;
}

async function submitInference(page, start, label) {
  let requestObserved = false;
  const observeSubmission = (request) => {
    if (request.method() === 'POST'
      && new URL(request.url()).pathname === '/api/v1/inference') {
      requestObserved = true;
    }
  };
  page.on('request', observeSubmission);
  const acceptedResponse = page.waitForResponse((response) =>
    response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/inference'
  );
  try {
    await start.click();
    const response = await acceptedResponse;
    if (!response.ok()) fail(`browser_inference_submit_${label}_${response.status()}`);
    const accepted = await response.json();
    if (typeof accepted.request_id !== 'string' || accepted.request_id.length === 0) {
      fail(`browser_request_id_missing_${label}`);
    }
    return accepted;
  } catch (error) {
    if (error instanceof Error && /browser_/.test(error.message)) throw error;
    fail(
      `browser_inference_response_missing_${label}_${requestObserved ? 'request_observed' : 'request_not_observed'}_${await start.isEnabled() ? 'button_enabled' : 'button_disabled'}`,
    );
  } finally {
    page.off('request', observeSubmission);
  }
}

async function concurrentInferenceScenario(browser, primaryPage, engine, failures) {
  const secondaryContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const secondaryPage = await secondaryContext.newPage();
  const offlineWindow = { active: false };
  observePage(secondaryPage, `${engine}-concurrent`, failures, offlineWindow);
  try {
    const [primaryStart, secondaryStart] = await Promise.all([
      prepareInference(primaryPage, `REPLICA-BROWSER-${engine}-PRIMARY`),
      prepareInference(secondaryPage, `REPLICA-BROWSER-${engine}-SECONDARY`),
    ]);
    const [primaryAccepted, secondaryAccepted] = await Promise.all([
      submitInference(primaryPage, primaryStart, `${engine}_primary`),
      submitInference(secondaryPage, secondaryStart, `${engine}_secondary`),
    ]);
    const requestIds = [primaryAccepted.request_id, secondaryAccepted.request_id];
    let locked = null;
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      const response = await fetch(`${origin}/__mycelium/runtime/admission-status`, {
        headers: { Accept: 'application/json' },
        redirect: 'error',
      });
      if (!response.ok) fail(`runtime_status_${response.status}`);
      const status = await response.json();
      const requests = requestIds.map((requestId) =>
        status.requests.find((item) => item.request_id === requestId)
      );
      if (requests.every((item) =>
        item !== undefined
          && item.path_state === 'locked'
          && Array.isArray(item.placement_ids)
          && item.placement_ids.length >= 2
      )) {
        locked = requests;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (locked === null) fail('browser_locked_tracks_missing');
    const trackKeys = locked.map((item) => JSON.stringify(item.placement_ids));
    // Positive browser gate requires distinct complete tracks for overlapping requests.
    if (new Set(trackKeys).size !== 2) fail('browser_distinct_complete_tracks_missing');

    // Force a real transport interruption while the unaffected request is
    // active. The page must reconnect to the same server-owned event history.
    offlineWindow.active = true;
    await secondaryContext.setOffline(true);
    await new Promise((resolve) => setTimeout(resolve, 250));
    await secondaryContext.setOffline(false);
    await new Promise((resolve) => setTimeout(resolve, 500));
    offlineWindow.active = false;

    const cancel = primaryPage.getByRole('button', { name: /cancel request/i });
    await cancel.waitFor({ state: 'visible', timeout: 30_000 });
    if (!(await cancel.isEnabled())) fail('browser_cancel_not_enabled');
    await cancel.click();
    const [cancelledTerminal, completedTerminal] = await Promise.all([
      terminalPhase(primaryPage),
      terminalPhase(secondaryPage),
    ]);
    if (!['cancelled', 'failed', 'cancellation_unconfirmed'].includes(cancelledTerminal)) {
      fail('browser_cancel_terminal_invalid');
    }
    if (completedTerminal !== 'completed') fail('browser_unaffected_request_not_completed');
    await secondaryPage.reload({ waitUntil: 'domcontentloaded' });
    const reconstructedTerminal = await terminalPhase(secondaryPage, 60_000);
    if (reconstructedTerminal !== 'completed') {
      fail('browser_terminal_history_not_reconstructed');
    }
    return {
      request_ids: requestIds,
      placement_ids: locked.map((item) => item.placement_ids),
      concurrent_requests_observed: true,
      distinct_complete_tracks_observed: true,
      cancellation_terminal: cancelledTerminal,
      unaffected_terminal: completedTerminal,
      reconnect_verified: true,
      terminal_history_reconstructed: true,
    };
  } finally {
    await secondaryContext.close();
  }
}

async function degradedInferenceScenario(page, engine) {
  const start = await prepareInference(page, `REPLICA-DEGRADED-${engine}`);
  const accepted = await submitInference(page, start, `${engine}_degraded`);
  const terminal = await terminalPhase(page);
  if (terminal !== 'completed') fail('browser_degraded_survivor_not_completed');
  return {
    request_ids: [accepted.request_id],
    surviving_track_completed: true,
    terminal,
  };
}

async function verifyEngine(name, engine, replicaState, failures) {
  const browser = await engine.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    observePage(page, name, failures);
    const inference = browserPhase === 'positive'
      ? await concurrentInferenceScenario(browser, page, name, failures)
      : await degradedInferenceScenario(page, name);
    const visited = [];
    for (const [label, hash, view] of WORKSPACES) {
      await page.getByRole('navigation', { name: 'Product sections' }).waitFor({
        timeout: 60_000,
      });
      const current = page.getByRole('link', { name: new RegExp(`^${label}`) });
      if (new URL(page.url()).hash !== `#${hash}`) {
        await current.click();
        await page.waitForURL(`${origin}/#${hash}`, { timeout: 60_000 });
      }
      if (await current.getAttribute('aria-current') !== 'page') {
        fail(`workspace_navigation_invalid_${hash}`);
      }
      const body = await page.locator('body').innerText();
      if (body.includes('FIXTURE DATA · NOT LIVE')) fail(`fixture_source_${hash}`);
      if (/\bA5\b/.test(body)) fail(`internal_milestone_copy_${hash}`);
      await verifyPanel(page, hash, view, replicaState);
      visited.push({ workspace: hash, replica_view: view, fields_verified: true });
    }

    await page.goBack({ waitUntil: 'domcontentloaded' });
    await verifyPanel(page, 'incidents-back', 'loss', replicaState);
    await page.goForward({ waitUntil: 'domcontentloaded' });
    await verifyPanel(page, 'settings-forward', 'qualification', replicaState);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await verifyPanel(page, 'settings-refresh', 'qualification', replicaState);

    const cleanContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const cleanPage = await cleanContext.newPage();
    observePage(cleanPage, `${name}-clean-session`, failures);
    await cleanPage.goto(`${origin}/#inference`, { waitUntil: 'domcontentloaded' });
    await verifyPanel(cleanPage, 'inference-clean-session', 'tracks', replicaState);
    await cleanContext.close();
    await context.close();
    return {
      engine: name,
      workspaces: visited,
      refresh_verified: true,
      back_forward_verified: true,
      clean_second_session_reconstructed: true,
      inference,
    };
  } finally {
    await browser.close();
  }
}

async function main() {
  if (!/^https?:\/\/127\.0\.0\.1:\d+$/.test(origin)) {
    fail('loopback_product_origin_required');
  }
  if (!outputPath) fail('browser_evidence_path_required');
  if (!['positive', 'degraded'].includes(browserPhase)) {
    fail('browser_phase_required');
  }

  const replicaState = await liveReplicaState();
  const failures = [];
  const engines = [];
  for (const [name, engine] of [
    ['chromium', chromium],
    ['firefox', firefox],
    ['webkit', webkit],
  ]) {
    engines.push(await verifyEngine(name, engine, replicaState, failures));
  }
  if (failures.length > 0) {
    fail(`browser_failures_${[...new Set(failures)].join('_')}`);
  }

  const report = {
    protocol: 'mycelium.a5_product_browser_observation.v1',
    phase: browserPhase,
    qualification_claim: false,
    promotion_authorized: false,
    passed: true,
    browser_failures: 0,
    console_error_count: 0,
    cross_origin_request_count: 0,
    engines,
    independent_browser_engines: engines.length,
    workspaces_per_engine: WORKSPACES.length,
    all_eight_workspaces_replica_fields_verified: true,
    refresh_back_forward_verified: true,
    clean_second_session_reconstructed: true,
    replica_qualification_count: replicaState.qualifications.length,
    replica_loss_count: replicaState.losses.size,
    replica_degradation_observed: browserPhase === 'degraded',
    concurrent_browser_inference_observed: browserPhase === 'positive',
    cancellation_observed: browserPhase === 'positive',
    deployment_id: replicaState.deploymentId,
    topology_version: replicaState.topologyVersion,
    route_identity_digest: replicaState.routeIdentityDigest,
    replica_qualification_ids: replicaState.qualifications.map(
      (qualification) => qualification.qualification_id,
    ).sort(),
    replica_qualification_digests: replicaState.qualifications.map(
      (qualification) => qualification.qualification_digest,
    ).sort(),
  };
  await atomicWrite(path.resolve(outputPath), report);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : 'a5_browser_gate_failed'}\n`);
  process.exitCode = 1;
});
