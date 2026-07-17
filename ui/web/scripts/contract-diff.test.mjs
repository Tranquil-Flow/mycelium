import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { compareContracts } from './contract-diff.mjs';

const scriptPath = fileURLToPath(new URL('./contract-diff.mjs', import.meta.url));

test('ignores scalar values and array cardinality when JSON contract shape is stable', () => {
  const baseline = {
    protocol: 'mycelium.route_plan.v2',
    ok: true,
    route: [{ node_id: 'node-a', range: { start_layer: 0, end_layer_exclusive: 3 } }],
  };
  const candidate = {
    protocol: 'mycelium.route_plan.v2',
    ok: false,
    route: [
      { node_id: 'peer-x', range: { start_layer: 0, end_layer_exclusive: 2 } },
      { node_id: 'peer-y', range: { start_layer: 2, end_layer_exclusive: 5 } },
    ],
  };

  assert.deepEqual(compareContracts(baseline, candidate), {
    added: [],
    removed: [],
    typeChanged: [],
    protocolChanged: [],
    drift: false,
  });
});

test('reports shape, type, and protocol drift with normalized array paths', () => {
  const baseline = {
    protocol: 'mycelium.route_plan.v1',
    route: [{ node_id: 'node-a', ready: true }],
    legacy: { inclusive_end: 3 },
  };
  const candidate = {
    protocol: 'mycelium.route_plan.v2',
    route: [{ node_id: 42, ready: true, range: { end_layer_exclusive: 3 } }],
  };

  assert.deepEqual(compareContracts(baseline, candidate), {
    added: ['$.route[].range', '$.route[].range.end_layer_exclusive'],
    removed: ['$.legacy', '$.legacy.inclusive_end'],
    typeChanged: [{ path: '$.route[].node_id', baseline: 'string', candidate: 'number' }],
    protocolChanged: [
      {
        path: '$.protocol',
        baseline: 'mycelium.route_plan.v1',
        candidate: 'mycelium.route_plan.v2',
      },
    ],
    drift: true,
  });
});

test('CLI emits machine-readable output and fails only when requested', () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'mycelium-contract-diff-'));
  const baselinePath = path.join(directory, 'baseline.json');
  const candidatePath = path.join(directory, 'candidate.json');
  writeFileSync(baselinePath, JSON.stringify({ protocol: 'example.v1', value: 1 }));
  writeFileSync(candidatePath, JSON.stringify({ protocol: 'example.v2', value: '1' }));

  const reportOnly = spawnSync(
    process.execPath,
    [scriptPath, '--baseline', baselinePath, '--candidate', candidatePath, '--json'],
    { encoding: 'utf8' },
  );
  assert.equal(reportOnly.status, 0);
  assert.equal(JSON.parse(reportOnly.stdout).drift, true);

  const gated = spawnSync(
    process.execPath,
    [
      scriptPath,
      '--baseline',
      baselinePath,
      '--candidate',
      candidatePath,
      '--json',
      '--fail-on-drift',
    ],
    { encoding: 'utf8' },
  );
  assert.equal(gated.status, 2);
});
