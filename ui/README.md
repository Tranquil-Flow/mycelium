# Mycelium Network Observatory

Read-only, fixture-first frontend for simulator, route, provisioning, network, and failover evidence.

## Isolation boundary

- All source and generated files stay under `mycelium/ui/`.
- No imports from Mycelium root Python modules.
- No subprocess calls and no mutating HTTP methods.
- Bundled fixtures are immutable copies or explicitly labeled synthetic data.
- Browser payloads use UI-owned allowlisted contracts.
- Live router, allocator, gossip, and provisioning integration remains disabled until those owners publish stable contracts.
- The UI never chooses routes, assigns layers, classifies global peer death, or executes failover.

## Run

```bash
cd ui/web
npm ci
npm run check
npm run dev
```

The default application runs entirely from bundled evidence. “Current” transport is visibly disabled.

Primary views are directly addressable:

- `#network`
- `#plans`
- `#incidents`
- `#evidence`

## Compare an evolving JSON contract

The contract-diff command reads two JSON files and reports normalized field-path, JSON-type, and exact protocol-string changes. Array indices, scalar values, and array cardinality are intentionally ignored so changing deployment IDs or peer counts do not masquerade as schema drift.

```bash
cd ui/web
npm run contracts:diff -- \
  --baseline ../tests/fixtures/source/route-plan-v2.json \
  --candidate /absolute/path/to/new-route-plan.json
```

Useful flags:

- `--json` emits machine-readable output.
- `--fail-on-drift` exits with status 2 when drift exists.
- Without `--fail-on-drift`, differences are report-only.

The command never copies, edits, or imports backend artifacts.
