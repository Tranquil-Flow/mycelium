#!/usr/bin/env node

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const MAX_DEPTH = 128;
const MAX_VISITED_VALUES = 100_000;

function jsonType(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function childPath(parent, key) {
  return /^[A-Za-z_$][A-Za-z0-9_$-]*$/.test(key)
    ? `${parent}.${key}`
    : `${parent}[${JSON.stringify(key)}]`;
}

function addToSetMap(map, key, value) {
  const values = map.get(key) ?? new Set();
  values.add(value);
  map.set(key, values);
}

function collectContract(value) {
  const shape = new Map();
  const protocols = new Map();
  let visited = 0;

  function visit(current, currentPath, depth) {
    visited += 1;
    if (visited > MAX_VISITED_VALUES) {
      throw new Error(`JSON contract exceeds ${MAX_VISITED_VALUES} visited values`);
    }
    if (depth > MAX_DEPTH) {
      throw new Error(`JSON contract exceeds maximum depth ${MAX_DEPTH}`);
    }

    const type = jsonType(current);
    addToSetMap(shape, currentPath, type);

    if (type === 'array') {
      for (const item of current) {
        visit(item, `${currentPath}[]`, depth + 1);
      }
      return;
    }

    if (type !== 'object') return;

    for (const [key, child] of Object.entries(current).sort(([left], [right]) => left.localeCompare(right))) {
      const nextPath = childPath(currentPath, key);
      if (key === 'protocol' && typeof child === 'string') {
        addToSetMap(protocols, nextPath, child);
      }
      visit(child, nextPath, depth + 1);
    }
  }

  visit(value, '$', 0);
  return { shape, protocols };
}

function canonicalSet(values) {
  return [...values].sort().join(' | ');
}

export function compareContracts(baseline, candidate) {
  const left = collectContract(baseline);
  const right = collectContract(candidate);

  const added = [...right.shape.keys()]
    .filter((entry) => !left.shape.has(entry))
    .sort();
  const removed = [...left.shape.keys()]
    .filter((entry) => !right.shape.has(entry))
    .sort();
  const typeChanged = [...left.shape.keys()]
    .filter((entry) => right.shape.has(entry))
    .map((entry) => ({
      path: entry,
      baseline: canonicalSet(left.shape.get(entry)),
      candidate: canonicalSet(right.shape.get(entry)),
    }))
    .filter((entry) => entry.baseline !== entry.candidate)
    .sort((a, b) => a.path.localeCompare(b.path));

  const protocolPaths = new Set([...left.protocols.keys(), ...right.protocols.keys()]);
  const protocolChanged = [...protocolPaths]
    .map((entry) => ({
      path: entry,
      baseline: left.protocols.has(entry) ? canonicalSet(left.protocols.get(entry)) : '<missing>',
      candidate: right.protocols.has(entry) ? canonicalSet(right.protocols.get(entry)) : '<missing>',
    }))
    .filter((entry) => entry.baseline !== entry.candidate)
    .sort((a, b) => a.path.localeCompare(b.path));

  return {
    added,
    removed,
    typeChanged,
    protocolChanged,
    drift:
      added.length > 0 ||
      removed.length > 0 ||
      typeChanged.length > 0 ||
      protocolChanged.length > 0,
  };
}

function usage() {
  return [
    'Usage: node scripts/contract-diff.mjs --baseline FILE --candidate FILE [options]',
    '',
    'Read-only JSON contract comparison. Scalar values and array cardinality are ignored;',
    'field paths, JSON types, and protocol strings are compared.',
    '',
    'Options:',
    '  --json           Emit machine-readable JSON',
    '  --fail-on-drift  Exit 2 when drift is detected (default remains report-only)',
    '  --help           Show this help',
  ].join('\n');
}

function parseArguments(argv) {
  const options = {
    baseline: null,
    candidate: null,
    json: false,
    failOnDrift: false,
    help: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--baseline' || argument === '--candidate') {
      const value = argv[index + 1];
      if (value === undefined || value.startsWith('--')) {
        throw new Error(`${argument} requires a file path`);
      }
      options[argument.slice(2)] = value;
      index += 1;
    } else if (argument === '--json') {
      options.json = true;
    } else if (argument === '--fail-on-drift') {
      options.failOnDrift = true;
    } else if (argument === '--help') {
      options.help = true;
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }

  if (!options.help && (options.baseline === null || options.candidate === null)) {
    throw new Error('--baseline and --candidate are required');
  }
  return options;
}

function readJson(filePath) {
  const resolved = path.resolve(filePath);
  try {
    return JSON.parse(readFileSync(resolved, 'utf8'));
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`Cannot read JSON ${resolved}: ${detail}`);
  }
}

function printHuman(report, baselinePath, candidatePath) {
  console.log(`Baseline:  ${path.resolve(baselinePath)}`);
  console.log(`Candidate: ${path.resolve(candidatePath)}`);
  console.log(`Drift:     ${report.drift ? 'yes' : 'no'}`);

  for (const [label, entries] of [
    ['Added paths', report.added],
    ['Removed paths', report.removed],
  ]) {
    console.log(`\n${label} (${entries.length})`);
    for (const entry of entries) console.log(`  ${entry}`);
  }

  console.log(`\nType changes (${report.typeChanged.length})`);
  for (const entry of report.typeChanged) {
    console.log(`  ${entry.path}: ${entry.baseline} -> ${entry.candidate}`);
  }

  console.log(`\nProtocol changes (${report.protocolChanged.length})`);
  for (const entry of report.protocolChanged) {
    console.log(`  ${entry.path}: ${entry.baseline} -> ${entry.candidate}`);
  }
}

function main(argv) {
  const options = parseArguments(argv);
  if (options.help) {
    console.log(usage());
    return 0;
  }

  const baseline = readJson(options.baseline);
  const candidate = readJson(options.candidate);
  const report = compareContracts(baseline, candidate);

  if (options.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    printHuman(report, options.baseline, options.candidate);
  }
  return options.failOnDrift && report.drift ? 2 : 0;
}

const isMain =
  process.argv[1] !== undefined &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (isMain) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    console.error(`contract-diff: ${detail}`);
    console.error(usage());
    process.exitCode = 1;
  }
}
