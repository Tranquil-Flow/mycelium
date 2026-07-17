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
The bundle is loaded through `StaticObservatorySource`; its adapted snapshot, incidents, and provisioning evidence remain the existing UI contract.

## Read-only data-source boundary

`App` accepts an `ObservatoryDataSource`, while `createObservatorySource()` selects static mode by default and rejects unknown source kinds. `LiveObservatorySource` is an injectable transport shell only:

- snapshot and optional event-stream URLs are required constructor/config inputs; no gateway path is assumed
- initial acquisition uses one explicit `GET` with `cache: no-store`
- optional updates use inbound-only server-sent events
- gateway payload decoders are injected, so this UI does not predeclare a future backend schema; each decoder owns complete semantic validation against its published contract
- each decoder must return one atomic Observatory bundle plus a non-negative generation
- event subscription starts before snapshot acquisition, then generation ordering reconciles the two read-only streams without rollback
- stale or duplicate generations are ignored, including malformed envelopes whose valid generation is already stale
- disconnect preserves the last coherent bundle; reconnect becomes current only after a strictly newer generation arrives
- the transport exposes no request-submission or control-message surface

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
  --baseline ../tests/fixtures/source/manual-provisioning-route-v1.json \
  --candidate /absolute/path/to/new-manual-provisioning-route.json
```

Useful flags:

- `--json` emits machine-readable output.
- `--fail-on-drift` exits with status 2 when drift exists.
- Without `--fail-on-drift`, differences are report-only.

The command never copies, edits, or imports backend artifacts.
