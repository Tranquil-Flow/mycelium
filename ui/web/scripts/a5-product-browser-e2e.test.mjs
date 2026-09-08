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
