import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Exercise the shipped adjudicator without running its physical main entrypoint.
const source = readFileSync(new URL('./a5-product-browser-e2e.mjs', import.meta.url), 'utf8');
const context = vm.createContext({ process: { env: {} } });
vm.runInContext(source.replace(/^import .*;\n/gm, '').split('\nmain().catch(')[0], context);

function pageWithStatus(text) {
  const locator = {
    filter() { return this; }, first() { return this; },
    async waitFor() {}, async innerText() { return text; },
  };
  return { locator() { return locator; } };
}

for (const text of ['Cancellation unconfirmed', 'Cancellation pending', 'Completedness']) {
  test(`nonterminal status cannot pass terminal adjudication: ${text}`, async () => {
    context.page = pageWithStatus(text);
    await assert.rejects(vm.runInContext('terminalPhase(page)', context), /terminal_phase_invalid/);
  });
}
for (const label of ['Completed', 'Cancelled', 'Failed']) {
  test(`server terminal label is accepted: ${label}`, async () => {
    context.page = pageWithStatus(`${label}\n64 tokens applied`);
    assert.equal(await vm.runInContext('terminalPhase(page)', context), label.toLowerCase());
  });
}

test('every workspace is checked by direct URL, reload, back and forward', async () => {
  const visits = [];
  context.visits = visits;
  context.replicaState = {};
  context.page = {
    async waitForURL() {},
    async goto(url) { visits.push(['goto', url]); },
    async reload() { visits.push(['reload']); },
    async goBack() { visits.push(['back']); },
    async goForward() { visits.push(['forward']); },
  };
  vm.runInContext('verifyPanel = async (_page, hash) => visits.push(["panel", hash]);', context);
  const results = await vm.runInContext('verifyWorkspaceNavigation(page, replicaState)', context);
  assert.equal(results.length, 8);
  for (const hash of ['inference', 'lab', 'network', 'nodes', 'plans', 'readiness', 'incidents', 'settings']) {
    for (const suffix of ['direct', 'refresh', 'back', 'forward']) {
      assert.ok(visits.some(([kind, name]) => kind === 'panel' && name === `${hash}-${suffix}`));
    }
  }
});

test('clean sessions reject another tab private canary', async () => {
  context.page = { locator() { return { async innerText() { return 'REPLICA-BROWSER-chromium-PRIMARY'; } }; } };
  await assert.rejects(vm.runInContext('verifyPrivateIsolation(page)', context), /private_canary_leaked/);
});

test('clean sessions reject decoded output even without a prompt canary', async () => {
  context.page = {
    locator() { return { async innerText() { return ''; }, async evaluateAll() { return ''; } }; },
    getByRole() { return { async count() { return 1; }, async innerText() { return 'Other tab generated output'; } }; },
  };
  await assert.rejects(vm.runInContext('verifyPrivateIsolation(page)', context), /private_output_leaked/);
});
