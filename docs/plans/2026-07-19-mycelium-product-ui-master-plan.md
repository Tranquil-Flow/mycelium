# Mycelium Product UI Master Implementation and Parallel-Session Plan

> **For Hermes:** Load `writing-plans`, `test-driven-development`, `subagent-driven-development`, `systematic-debugging`, and `requesting-code-review`. Execute this plan from an isolated worktree. Use one primary integration driver and only the explicitly independent parallel lanes below.

**Goal:** Turn the existing rich Network Observatory prototype into Mycelium's general product UI: one privacy-preserving interface that observes, configures, and operates multi-device distributed inference while preserving authority boundaries and never requiring Tailscale.

**Architecture:** Keep one React/TypeScript application and one visual language. A browser-facing same-origin product gateway composes read-only Observatory data, a separately authorized inference request gateway, and an explicitly authorized swarm/device coordinator; the Observatory service itself remains read-only. Native Mycelium node agents use authenticated iroh direct/relay transport and qualification-owned readiness. The UI never fabricates route readiness or treats the synthetic browser matrix demo as model inference.

**Tech stack:** React 19, TypeScript, Vite, Vitest, Testing Library, React Flow, D3, ELK, Python 3.14 ASGI services, SSE, existing Router/request/qualification contracts, native iroh sidecar. Add browser automation only through the primary-owned package manifest and lockfile.

---

## 1. Handoff state and source of truth

Planning branch and worktree created for this document:

- Worktree: `/Users/evinova-self/Projects/mycelium-wt-product-ui-plan`
- Branch: `planning/mycelium-product-ui`
- Code base before this planning commit: `b447c9423f45fdcf847332ab32d7d74d1921d6a9`
- Base branch: `integration/mycelium-interactive-ui`

Why this base:

- It contains the rich React Observatory inherited from main.
- It contains the authenticated request gateway and request-to-iroh local qualification.
- It contains the read-only Observatory event adapter.
- It contains the interactive browser-swarm prototype and its tests.
- It contains the current route-qualification and iroh integration work needed by the UI.

Do not silently rebase this effort onto canonical `main` (`f2bc55b62c5e3103eda584c1c68c222244bde489`) or the older manual-driver branch. If a newer integration base is chosen later, the primary driver must first produce a reviewed base-delta report and rerun all gates.

Authoritative product/UI sources:

1. Original rich UI plan:
   `/Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-15_233708-mycelium-ui-display-plan.md`
2. MVP synthesis and claim boundaries:
   `/Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-17_142630-mycelium-ddai-mvp-synthesis-plan.md`
3. This product/UI execution plan:
   `docs/plans/2026-07-19-mycelium-product-ui-master-plan.md`
4. Observatory contract:
   `docs/observatory-gateway-contract.md`
5. Observatory event adapter:
   `docs/observatory-event-adapter.md`
6. Request gateway contract:
   `docs/request-gateway-api-contract.md`
7. Interactive prototype boundary:
   `docs/interactive-browser-swarm.md`
8. Iroh sidecar contract:
   `contracts/iroh-sidecar-v1.md`

When documents disagree, this plan governs product-UI intent, but existing service contracts govern security and authority. Do not weaken a backend contract to make the UI easier.

---

## 2. Current observed status

### Existing rich frontend

`ui/web/` already provides substantial fixture-oriented functionality:

- Network, Plans, Evidence, and Incidents surfaces.
- Pipeline, ring, and elastic-geography layouts.
- Directed physical and logical edges.
- Prefill/decode/closure filters.
- Route tracing and inspectors.
- Device memory/layer glyphs.
- Simulation/current/replay source modes.
- Provenance and readiness semantics.

### Live Observatory gap

The repository contains both snapshot polling and GET/SSE event adapters, but production UI wiring is incomplete:

- `ui/web/src/data/observatorySource.ts`
- `ui/web/src/data/observatoryEventSource.ts`
- `ui/web/src/data/observatoryEventProjection.ts`
- `ui/web/src/model/semanticProjection.ts`

Current live mode renders a compact semantic summary instead of driving the same rich `AppShell` and graph model. `LiveObservatoryEventSource` is tested but not imported by production UI code.

### Inference gap

`mycelium_request_gateway/` already supports authenticated qualification lookup, inference admission, SSE token streams, cancellation, lifecycle cleanup, and privacy-safe metrics. No React product surface calls it.

### Swarm/device gap

`mycelium_interactive/` proves bounded invite/join/poll/result/leave mechanics and a synthetic browser matrix stage. Its separate static operator page and device page are prototypes, not the final product UI. Synthetic matrix success is never model inference, distributed inference, or route qualification.

### Network gap

Tailscale appears only as an old physical test path and handover convenience. It is not an allowed product dependency. Existing non-local iroh mode uses the N0 preset, but membership, EndpointID exchange, production multi-host qualification, and scalable provisioning remain incomplete system gates.

---

## 3. Non-negotiable product and safety invariants

1. **One product UI.** Do not retain a second operator UI as the long-term surface.
2. **Observatory remains read-only.** No prompt, cancel, provisioning, peer mutation, or route mutation endpoint enters `mycelium_gateway`.
3. **Separate control authorities.** Inference actions go through `mycelium_request_gateway`; device actions go through an explicitly authorized coordinator boundary.
4. **No browser-held upstream bearer credential.** Product gateway keeps service credentials server-side. Browser receives only a bounded local product session.
5. **No Tailscale requirement.** No Tailscale address, daemon, CLI, DNS name, or auth state may be required by product code, tests, setup, or success criteria.
6. **Iroh is node data plane.** Native nodes use authenticated Endpoint IDs with direct/relay transport. UI-to-local-agent traffic remains loopback/same-origin.
7. **`route_ready=false` remains false** until qualifier-owned physical evidence produces a current accepted qualification. UI cannot create or reinterpret readiness.
8. **Claim boundaries remain visible.** Fixture, simulated, local-only, synthetic browser, and physical evidence must never collapse into one green state.
9. **No protected tensor or prompt leakage.** No activations, KV data, prompt text, decoded output, tokens, credentials, private endpoint addresses, or local paths enter Observatory logs/metrics.
10. **No Router/runtime/transport semantic edits from UI lanes.** A failing contract may trigger a separately reviewed backend issue, not a convenient UI-lane mutation.
11. **No imports from other distributed-inference repositories.** Mycelium stays isolated.
12. **No canonical-main edits.** Every session uses its own worktree and branch.
13. **No broad staging.** `git add -A` and `git add .` are forbidden. Stage exact owned files only.
14. **No fetch, pull, push, PR, or merge to main** unless Evi separately authorizes it.
15. **Automated browser tests precede physical-device testing.** Physical testing remains a later explicit gate.

---

## 4. Target product information architecture

The final React application contains these first-class workspaces.

### 4.1 Inference

- Prompt editor with explicit byte/token bounds.
- Model and qualified deployment selector.
- Maximum-new-token control.
- Current qualification identity and readiness reason.
- Start, stream, resume, and cancel request lifecycle.
- Streaming decoded output, terminal state, and stable public error codes.
- Request history limited to privacy-safe local session metadata; prompt/output persistence off by default.
- Disabled submission when qualification is missing, stale, mismatched, or `route_ready=false`.

### 4.2 Network Observatory

Implement all original rich-plan behavior:

- Simulation, Current Snapshot, and Replay.
- Physical topology and logical execution graph as separate layers.
- Pipeline/DAG, ring, SCC expansion, elastic geo, and true-map mode.
- Prefill, decode, closure/control, primary, and alternative flows.
- Route-only default filtering and scalable clustering/edge bundling.
- Stable positions across metric-only updates.
- Node/edge/route inspectors.
- Exact payload and transfer formulas with substituted values and provenance.
- Freeze, replay, change highlighting, timeline/history, export, and pseudonymized screenshots.
- Real traffic overlays only when an authenticated backend trace contract exists.

### 4.3 Nodes and Swarm

- Searchable/sortable node inventory.
- Pair/add device workflow with signed/expiring invite semantics.
- Endpoint identity, trust, direct/relay/unknown connectivity state, and freshness.
- Device class, architecture, backend, precision, memory hierarchy, storage, and power.
- Assigned layers, artifacts, runtime-load state, stage probe, and route membership.
- Browser-worker capability shown separately from native model-stage capability.
- No private address display by default.
- No implication that an invited browser is a model inference peer.

### 4.4 Plans

- Strategy ranking and side-by-side comparison.
- Route/layer allocation, alternatives, pruning trace, bottleneck, memory, and predicted throughput.
- Workload/model assumptions and exact claim boundary.
- No mutable what-if controls until a separately authorized planner control API exists.

### 4.5 Readiness and Evidence

Strict ladder:

`Discovered → Planned → Assigned → Artifacts verified → Runtime loaded → Stage probed → Route challenged → Route ready`

Include:

- Node-by-stage readiness matrix.
- Deployment identity and epoch.
- Model revision, manifest, path, assignment, stage signature, load-proof digest, EndpointID, process, transport, token, and parity bindings.
- Missing-proof explanations.
- Source validation/error drawer.
- Evidence diff and history.
- Verbatim claim boundaries.

### 4.6 Settings and Diagnostics

- Local node identity and product-gateway status.
- Public relay/bootstrap policy without Tailscale.
- Privacy controls and data-retention policy.
- Exportable redacted diagnostics.
- Source and contract versions.
- No raw credentials or private addresses.

---

## 5. Target service architecture

```text
Browser React UI
      |
      | same-origin HTTPS or loopback HTTP
      v
Mycelium Product UI Gateway (new browser-facing BFF)
      |---------------------- GET/SSE ----------------------> Read-only Observatory
      |---------------- POST/GET/SSE/DELETE ---------------> Request Gateway
      |---------------- authorized device actions ---------> Swarm Coordinator
      |
      +-- no upstream bearer token exposed to browser

Local Mycelium node agent
      |
      +-- Router/runtime/qualifier
      +-- native iroh sidecar
             |
             +-- authenticated direct or relay path to other Mycelium nodes
```

The browser-facing gateway is an authority-preserving composition layer, not a replacement Router or qualifier.

Recommended product-gateway API, frozen before parallel work:

- `GET /api/v1/bootstrap`
- `GET /api/v1/observatory/snapshot`
- `GET /api/v1/observatory/events`
- `GET /api/v1/qualification/current`
- `POST /api/v1/inference`
- `GET /api/v1/inference/{request_id}/events`
- `DELETE /api/v1/inference/{request_id}`
- `GET /api/v1/swarm/status`
- `POST /api/v1/swarm/invites`
- `POST /api/v1/swarm/join`
- `POST /api/v1/swarm/leave`

Exact schemas must be checked into `ui/contracts/product/` and tested on Python and TypeScript sides. Endpoint existence does not authorize a route mutation. Swarm actions own device-session state only.

---

## 6. Parallelization decision

Parallel agents make sense only after the primary driver freezes shared contracts and shell boundaries. Six UI/application lanes plus one native-membership systems lane can then proceed concurrently because they own disjoint files. L7 is necessary for a real no-Tailscale native-node onboarding path; it is not cosmetic UI work and must keep its narrower systems claim boundary.

Do **not** start workers from `b447c94` directly. Start them from the primary driver's single reviewed `FOUNDATION_SHA`.

Recommended graph:

```text
P0 Primary: preflight + contracts + shell foundation
  |
  +--> L1 Product gateway backend --------------------+
  +--> L2 Live Observatory controller ----------------+
  +--> L3 Inference workspace ------------------------+
  +--> L4 Swarm/device workspace ---------------------+--> P1 Primary integration
  +--> L5 Rich network graph -------------------------+
  +--> L6 Plans/readiness/evidence -------------------+
  +--> L7 Native iroh membership/enrollment ----------+
                                                        |
                                                        +--> R1 Browser/a11y review
                                                        +--> R2 Security/claim review
                                                        |
                                                        +--> P2 Primary fixes + full gates
```

Tasks that must remain serial and primary-owned:

- Base selection and topology check.
- Shared contract/schema creation.
- `App.tsx`, `main.tsx`, `AppShell.tsx`, global styles, package manifests, and lockfile.
- Package additions.
- Cherry-pick order and conflict resolution.
- Final API composition.
- Full repository gates.
- Physical qualification decision.

---

## 7. Worktree and Git protocol

### Primary integration worktree

After this document is committed, primary session creates:

```bash
git worktree add -b integration/mycelium-product-ui \
  /Users/evinova-self/Projects/mycelium-wt-product-ui-primary \
  planning/mycelium-product-ui
```

Primary makes one foundation commit and reports its exact SHA as `FOUNDATION_SHA`.

### Worker worktrees

Create only after foundation passes focused gates:

```bash
git worktree add -b feature/product-ui-gateway \
  /Users/evinova-self/Projects/mycelium-wt-ui-gateway FOUNDATION_SHA

git worktree add -b feature/product-ui-live-observatory \
  /Users/evinova-self/Projects/mycelium-wt-ui-live FOUNDATION_SHA

git worktree add -b feature/product-ui-inference \
  /Users/evinova-self/Projects/mycelium-wt-ui-inference FOUNDATION_SHA

git worktree add -b feature/product-ui-swarm \
  /Users/evinova-self/Projects/mycelium-wt-ui-swarm FOUNDATION_SHA

git worktree add -b feature/product-ui-network-graph \
  /Users/evinova-self/Projects/mycelium-wt-ui-graph FOUNDATION_SHA

git worktree add -b feature/product-ui-plans-evidence \
  /Users/evinova-self/Projects/mycelium-wt-ui-plans FOUNDATION_SHA

git worktree add -b feature/product-ui-native-membership \
  /Users/evinova-self/Projects/mycelium-wt-ui-membership FOUNDATION_SHA
```

Each worker session creates its own named worktree. If its path or branch already exists, it may proceed only when both resolve to the named branch, exact `FOUNDATION_SHA`, and a clean status; otherwise it must stop and report. Never reset, clean, switch, or reuse another session's worktree.

Worker handover must contain:

- Base SHA.
- Final commit SHA(s).
- Exact owned files changed.
- Commands and exits.
- Test counts.
- Known gaps.
- Claim boundary.
- Confirmation of no push/merge/Tailscale dependency.

---

## 8. File ownership matrix

### Primary only — no worker edits

- `ui/web/src/App.tsx`
- `ui/web/src/App.test.tsx`
- `ui/web/src/main.tsx`
- `ui/web/src/components/AppShell.tsx`
- `ui/web/src/styles.css`
- `ui/web/package.json`
- `ui/web/package-lock.json`
- `ui/web/vite.config.ts`
- `ui/web/tsconfig*.json`
- `ui/contracts/product/**`
- `ui/web/src/app/**`
- Top-level product UI docs and final handover

Workers needing a change to primary-owned files must record a requested patch in their handover; they must not edit the file.

### Lane L1 — product gateway backend

Own only:

- Create `mycelium_ui_gateway/**`
- Create `tests/ui_gateway/**`
- Create `docs/product-ui/product-gateway.md`

May import public interfaces from existing gateway packages. Must not edit:

- `mycelium_gateway/**`
- `mycelium_request_gateway/**`
- Router/runtime/qualifier/iroh code
- React files

### Lane L2 — live Observatory data/controller

Own only:

- `ui/web/src/data/observatorySource.ts`
- `ui/web/src/data/observatorySource.test.ts`
- `ui/web/src/data/observatoryEventSource.ts`
- `ui/web/src/data/observatoryEventSource.test.ts`
- `ui/web/src/data/observatoryEventProjection.ts`
- `ui/web/src/data/observatoryEventProjection.test.ts`
- `ui/web/src/model/semanticProjection.ts`
- `ui/web/src/model/semanticProjection.test.ts`
- Create `ui/web/src/features/observatory/live/**`

No view, shell, graph, package, Python, or global-style edits.

### Lane L3 — inference workspace

Own only:

- Create `ui/web/src/features/inference/**`
- Create `docs/product-ui/inference-workspace.md`

No request-gateway Python edits. Consume frozen product-gateway contract. No shell/global-style/package edits.

### Lane L4 — swarm/device workspace

Own only:

- Create `ui/web/src/features/swarm/**`
- `ui/web/src/interactive/**`
- `mycelium_interactive/**`
- `tests/interactive/**`
- Create `docs/product-ui/swarm-workspace.md`

No AppShell, global-style, product-gateway, Router, qualifier, or iroh edits. Retain synthetic-browser claim boundary.

### Lane L5 — rich network graph

Own only:

- `ui/web/src/graph/**`
- `ui/web/src/components/RouteCanvas.tsx`
- `ui/web/src/components/DeviceNode.tsx`
- `ui/web/src/views/NetworkView.tsx`
- Create `ui/web/src/features/observatory/components/**`
- Create `ui/web/src/features/observatory/styles/**`

No live data source, shared model, shell, global-style, package, or Python edits.

### Lane L6 — plans/readiness/evidence

Own only:

- `ui/web/src/views/PlansView.tsx`
- `ui/web/src/views/EvidenceView.tsx`
- `ui/web/src/views/IncidentsView.tsx`
- Create `ui/web/src/features/plans/**`
- Create `ui/web/src/features/readiness/**`
- Create `ui/web/src/features/nodes/**`
- Create feature-local CSS modules under those directories
- Create `docs/product-ui/plans-readiness.md`

No graph, live data, shell, global-style, package, or Python edits.

### Lane L7 — native iroh membership/enrollment

Own only:

- Create `mycelium_membership/**`
- Create `tests/membership/**`
- Create `docs/product-ui/native-membership.md`

May consume public iroh sidecar, identity, gossip, and qualification contracts. Must not edit:

- `native/iroh_transport/**`
- Router/runtime/qualifier/gossip implementations
- `mycelium_interactive/**`
- `mycelium_ui_gateway/**`
- React files

Primary owns the final adapter from this package into the product gateway and swarm UI.

---

## 9. Phase P0 — primary foundation, serial

### Task P0.1: Verify live topology and base

**Objective:** Prove all sessions start from the intended clean code state.

**Steps:**

1. Run `git worktree list --porcelain` from canonical repository.
2. Verify planning branch contains this document and parent base is `b447c94`.
3. Verify every protected old worktree remains clean or report pre-existing dirt without touching it.
4. Create primary worktree/branch only if path and branch do not exist.
5. Record exact base SHA in `docs/product-ui/current-integration-state.md`.

### Task P0.2: Freeze browser-facing contracts

**Files:**

- Create `ui/contracts/product/product-ui-bootstrap-v1.schema.json`
- Create `ui/contracts/product/product-ui-observatory-v1.schema.json`
- Create `ui/contracts/product/product-ui-inference-v1.schema.json`
- Create `ui/contracts/product/product-ui-swarm-v1.schema.json`
- Create `ui/web/src/app/contracts.ts`
- Create `ui/web/src/app/contracts.test.ts`

**TDD steps:**

1. Add fixture validation tests for valid, unknown-field, oversized, stale, and `route_ready=false` payloads.
2. Run focused tests and verify failure before implementation.
3. Add exact TypeScript discriminated unions and JSON schemas.
4. Verify Python/TypeScript field names agree.
5. Preserve stable public error codes; never expose exception strings.

### Task P0.3: Create feature registry and shell slots

**Files:**

- Create `ui/web/src/app/navigation.ts`
- Create `ui/web/src/app/navigation.test.ts`
- Create `ui/web/src/app/ProductState.ts`
- Create `ui/web/src/app/ProductState.test.ts`
- Modify primary-owned `App.tsx`, `AppShell.tsx`, and `App.test.tsx`

**Required route IDs:**

- `inference`
- `network`
- `nodes`
- `plans`
- `readiness`
- `incidents`
- `settings`

Build stable lazy feature slots so workers can add feature modules without editing shell files.

### Task P0.4: Create test harness and fixture factories

**Files:**

- Create `ui/web/src/test/productFixtures.ts`
- Create `ui/web/src/test/renderProductFeature.tsx`
- Create `ui/web/src/test/networkRecorder.ts`

Tests must prove:

- Fixture/live/replay labels cannot disappear.
- `route_ready=false` disables inference.
- Browser never sends upstream bearer credentials.
- No request reaches a non-same-origin URL.
- Unknown values remain unknown, never zero/healthy/ready.

### Task P0.5: Decide browser automation package once

Primary owns this decision. Prefer pinned Playwright with Chromium, Firefox, and WebKit because multi-browser automation is required before physical trials.

If package installation is not authorized or network is unavailable:

- Do not let workers alter package files.
- Complete Vitest/Testing Library coverage.
- Record Playwright as a blocking pre-physical gate.

If authorized:

- Modify only `package.json` and lockfile in primary.
- Add `ui/web/playwright.config.ts` and `ui/web/e2e/**` as primary-owned paths.
- Run one smoke test in all three browser engines before parallel fan-out.

### Task P0.6: Foundation verification and commit

Run:

```bash
cd ui/web && npm run check
python3.14 -m pytest -q test_observatory_gateway.py tests/observatory_events tests/request_gateway tests/request_conformance tests/interactive
git diff --check
```

Commit exact foundation files. Report `FOUNDATION_SHA`. Only then create worker worktrees.

---

## 10. Lane L1 — browser-facing product gateway

### Objective

Build a same-origin BFF that composes existing services without moving their authority into the browser.

### Required behavior

1. Loopback-only safe default.
2. Explicit non-loopback mode requires TLS/auth configuration and fails closed when absent.
3. Per-launch bounded product session using HttpOnly, SameSite=Strict cookie.
4. CSRF defense for state-changing endpoints.
5. No upstream bearer credential in HTML, JavaScript, URL, browser storage, response, log, or exception.
6. Exact body and response bounds.
7. `Cache-Control: no-store` on private/control responses.
8. Same-origin only; no permissive CORS.
9. Observable source status with no private endpoints.
10. Proxy/adapt existing request and Observatory APIs; never reconstruct `route_ready=true`.

### TDD slices

1. Session/bootstrap and CSRF contract.
2. Read-only Observatory snapshot proxy.
3. SSE Observatory event forwarding with disconnect/backpressure bounds.
4. Qualification projection.
5. Inference submit, stream, resume, and cancel forwarding.
6. Swarm status/action adapter over injected coordinator interface.
7. Privacy/logging/redaction adversarial tests.
8. Static asset/fallback handling only if the primary contract explicitly delegates it.

### Focused gates

```bash
python3.14 -m pytest -q tests/ui_gateway
python3.14 -m compileall -q mycelium_ui_gateway tests/ui_gateway
git diff --check
```

### Claim boundary

Local browser-facing composition proof only. Does not prove public deployment, physical inference, multi-host iroh routing, or route readiness.

---

## 11. Lane L2 — live Observatory controller

### Objective

Drive the rich product UI from validated GET/SSE state while preserving fixture and replay behavior.

### Required behavior

1. One `ObservatoryController` owns bootstrap snapshot, event stream, reconnect, resume cursor, staleness, freeze, and replay.
2. Event and snapshot payloads normalize to the frozen product Observatory contract.
3. Rich graphs consume the same normalized view for simulation/current/replay.
4. Monotonic generation and source cursor enforcement.
5. Invalid/newer payload fails closed while preserving clearly stale last-known state.
6. Freeze stops visible mutation without losing event-source status.
7. Replay never opens network connections.
8. Change set identifies added/removed/changed nodes, edges, routes, readiness, and evidence.
9. `route_ready` remains literal from qualified source.
10. Production code actually imports the event source.

### TDD slices

1. Controller state machine.
2. Initial snapshot and event transition.
3. Resume and generation mismatch.
4. Freeze/unfreeze.
5. Replay isolation.
6. Staleness timers.
7. Change-set calculation.
8. Privacy and unknown-value preservation.

### Focused gates

```bash
cd ui/web
npm test -- --run src/data src/model/semanticProjection.test.ts src/features/observatory/live
npm run typecheck
npm run build
git diff --check
```

### Claim boundary

Validated browser projection and source lifecycle only; no physical route or inference claim.

---

## 12. Lane L3 — inference workspace

### Objective

Build the product prompt/stream/cancel UX against the frozen product-gateway contract.

### Required behavior

1. Qualification loads before submission.
2. Submit disabled with an exact reason when unavailable, stale, changed, or false.
3. Request captures exact qualification binding and cannot silently retry against a new route.
4. Prompt byte bound and token bound validated client-side and server-side.
5. Accepted request opens SSE stream and applies contiguous sequence IDs only.
6. Resume uses last fully applied event ID.
7. Duplicate events are idempotent; gaps/future events fail closed.
8. Cancellation is idempotent and terminal state is immutable.
9. Output is not persisted by default.
10. Prompt/output never enters logs, telemetry, URL, localStorage, or screenshots from automated tests.
11. Accessible streaming region, keyboard operation, reduced motion, and clear terminal states.
12. UI distinguishes local test backend from qualified distributed execution.

### Suggested files

- `ui/web/src/features/inference/types.ts`
- `.../requestClient.ts`
- `.../requestClient.test.ts`
- `.../useInferenceSession.ts`
- `.../useInferenceSession.test.tsx`
- `.../InferenceWorkspace.tsx`
- `.../InferenceWorkspace.test.tsx`
- `.../InferenceWorkspace.module.css`

### Focused gates

```bash
cd ui/web
npm test -- --run src/features/inference
npm run typecheck
npm run build
git diff --check
```

### Claim boundary

UI request lifecycle proof against mocked/local gateway only unless separately backed by accepted physical qualification.

---

## 13. Lane L4 — swarm and device workspace

### Objective

Replace the separate operator demo with product-native device enrollment/status while preserving optional browser-worker mechanics and their narrow claim boundary.

### Required behavior

1. Device inventory and pairing UI under the main product application.
2. Expiring single-use invite with origin and session bounds.
3. Peer join, heartbeat/poll, result, leave, and idle expiry remain bounded.
4. Native node capability and browser-worker capability are distinct types.
5. Synthetic matrix jobs appear only under a developer/probe label.
6. No synthetic job can set route readiness, model readiness, stage readiness, or inference success.
7. No Tailscale address generation, checking, documentation requirement, or setup branch.
8. Product-facing connectivity states: direct, relayed, local, disconnected, stale, unknown.
9. Private IP/address data redacted from default UI.
10. Device remove/leave action requires explicit confirmation and does not mutate Router state.
11. Keep phone/browser page responsive and accessible.
12. Retire or redirect old `/ui` operator page after primary integration; retain `/device` only if useful as a peer entry surface.

### TDD slices

1. Typed swarm status client.
2. Product device inventory.
3. Invite creation/copy/QR payload without secrets in server logs.
4. Join and session lifecycle.
5. Browser worker capability classification.
6. Synthetic job developer panel.
7. Idle/expiry/replay/cancel adversarial tests.
8. No-Tailscale contract scan.

### Focused gates

```bash
python3.14 -m pytest -q tests/interactive
python3.14 -m compileall -q mycelium_interactive tests/interactive
cd ui/web
npm test -- --run src/interactive src/features/swarm
npm run typecheck
npm run build
git diff --check
```

### Claim boundary

Device-session and synthetic browser-stage proof only. No model tensor execution or distributed inference claim.

---

## 14. Lane L5 — rich Network Observatory graph

### Objective

Finish the original graph vision against frozen normalized fixtures, independent of live transport.

### Required behavior

1. Pipeline layout ordered by exact half-open layer boundaries.
2. Ring layout with distinct decode closure.
3. SCC condensation and expandable mini-rings for arbitrary cycles.
4. Elastic geo preserving bearing with persistent distortion warning.
5. True map toggle for uncompressed coordinates.
6. Unknown-location side tray; no fabricated coordinates.
7. Route-only default; physical all-pairs only on demand.
8. Stable node positions across metric updates.
9. Clustering and edge bundling at large network sizes.
10. Physical and logical edges independently toggleable.
11. Prefill/decode/closure/alternative styles use redundant non-color cues.
12. Node glyph supports unified/discrete memory, layers, backend, precision, storage, and proof state.
13. Edge inspector shows direction, role, payload, RTT/jitter/loss/bandwidth, formula, substituted values, distance, provenance, and missing evidence.
14. Accessible node/edge/stage tables equivalent to canvas.
15. Reduced-motion mode and animation off by default.
16. Flow animation labeled `ILLUSTRATIVE MODELED FLOW` unless a real trace contract exists.
17. 24-node detailed and 100-node compact performance fixtures.

### TDD slices

1. Pipeline/ring/SCC layout properties.
2. Geo distortion and unknown location.
3. Node glyph truthfulness.
4. Directed edge semantics.
5. Exact formula rendering.
6. Selection/path tracing/filtering.
7. Clustering and performance budget.
8. Accessibility/reduced motion.

### Focused gates

```bash
cd ui/web
npm test -- --run src/graph src/components/RouteCanvas.tsx src/components/DeviceNode.tsx src/views/NetworkView.tsx src/features/observatory/components
npm run typecheck
npm run build
git diff --check
```

### Claim boundary

Renderer/layout proof against normalized evidence; visuals do not elevate source truth.

---

## 15. Lane L6 — Plans, Nodes, Readiness, Evidence, and timeline

### Objective

Complete non-canvas operational surfaces from the original rich plan.

### Required behavior

1. Strategy ranking with combined/prefill/decode/single-request metrics.
2. Two-strategy synchronized comparison and explicit deltas.
3. Route/layer allocation, alternatives, pruning trace, bottleneck, and assumptions.
4. No editable planner controls without an authorized API.
5. Searchable/sortable node inventory.
6. Node detail with identity, hardware, memory, runtime, assignment, location precision, and raw redacted source.
7. Strict readiness ladder and node-by-stage matrix.
8. Artifact verification never becomes runtime loaded or route ready.
9. Source/evidence drawer with protocol, digest, produced/acquired time, validation, claim boundary, and missing artifacts.
10. Evidence diff/history and timeline playback.
11. Incidents generated only from evidence/status transitions, never invented severity.
12. Pseudonymized export/share views.
13. Full keyboard navigation, semantic tables, and non-color state cues.

### TDD slices

1. Strategy ranking and comparison.
2. Node inventory and detail.
3. Readiness matrix and missing-proof explanations.
4. Evidence/source drawer.
5. Timeline/replay model.
6. Incidents truthfulness.
7. Pseudonymized export.
8. Accessibility.

### Focused gates

```bash
cd ui/web
npm test -- --run src/views/PlansView.tsx src/views/EvidenceView.tsx src/views/IncidentsView.tsx src/features/plans src/features/readiness src/features/nodes
npm run typecheck
npm run build
git diff --check
```

### Claim boundary

Read-only interpretation of supplied plans/evidence; no planner, provisioner, Router, or qualifier authority.

---

## 15A. Lane L7 — native iroh membership and enrollment

### Objective

Provide the no-Tailscale native-node onboarding seam required by the product UI without changing Router, runtime, gossip, qualifier, or iroh transport semantics.

### Required behavior

1. Signed, expiring, single-use enrollment invitation binds issuer identity, intended swarm/deployment scope, protocol version, and approved discovery/relay policy.
2. Invitation carries authenticated iroh EndpointID/bootstrap material, never a required Tailscale/IP/DNS address.
3. Join verifies signature, expiry, audience/scope, replay status, and explicit operator trust before persisting membership.
4. Endpoint updates are signed and monotonic; stale/replayed updates fail closed.
5. Membership states remain distinct: `invited`, `trusted`, `reachable`, `assigned`, `qualified`, and `revoked`.
6. Reachability never implies assignment, qualification, inference capability, or `route_ready=true`.
7. Public status projection redacts private addresses, credentials, relay tokens, and local paths.
8. Revocation is idempotent and immediately prevents future enrollment use; it does not mutate an active Router route without Router authority.
9. Service exposes a small injected coordinator protocol for L1/primary integration; no UI or transport implementation edits.
10. Two-process local tests exercise invite, join, reconnect, endpoint rotation, expiry, replay, and revoke through fake/injected transport state.
11. Existing iroh direct/relay status may be observed, but L7 cannot claim internet scale or physical multi-host qualification.
12. No prompt, output token, activation, KV state, tensor, protected edit, or model artifact enters membership records or gossip.

### Suggested files

- `mycelium_membership/contracts.py`
- `mycelium_membership/invitations.py`
- `mycelium_membership/registry.py`
- `mycelium_membership/service.py`
- `mycelium_membership/public_projection.py`
- `tests/membership/test_invitations.py`
- `tests/membership/test_registry.py`
- `tests/membership/test_service.py`
- `tests/membership/test_privacy.py`
- `docs/product-ui/native-membership.md`

### TDD slices

1. Canonical invitation signing and verification.
2. Expiry, audience, scope, and replay rejection.
3. Explicit trust and idempotent join.
4. Signed monotonic EndpointID update.
5. Reachability versus qualification separation.
6. Revocation and reconnect races.
7. Redacted public projection.
8. Coordinator protocol and two-process local harness.
9. No-Tailscale and no-protected-payload scans.

### Focused gates

```bash
python3.14 -m pytest -q tests/membership
python3.14 -m compileall -q mycelium_membership tests/membership
git diff --check
```

### Claim boundary

Local authenticated membership/enrollment contract proof only. Does not prove public internet scale, NAT traversal on arbitrary networks, physical inference, Router admission, or route readiness.

---

## 16. Phase P1 — primary integration, serial

Primary integrates worker commits one at a time. Do not merge branches wholesale; cherry-pick reviewed commits.

Recommended order:

1. L7 native membership/enrollment.
2. L1 product gateway.
3. L2 live Observatory controller.
4. L5 graph renderer.
5. L6 plans/readiness/evidence.
6. L3 inference workspace.
7. L4 swarm/device workspace.

After every cherry-pick:

1. Inspect `git show --stat --oneline HEAD`.
2. Verify only lane-owned files changed.
3. Run lane-focused gates.
4. Run `git diff --check`.
5. If a worker edited a primary-owned/shared file, stop and manually extract only owned hunks rather than accepting the commit.

Then primary alone:

- Wires feature registry into `App.tsx` and `AppShell.tsx`.
- Connects product gateway bootstrap to app startup.
- Makes rich `AppShell` render for live, fixture, and replay.
- Adds navigation entries and feature-level error boundaries.
- Consolidates global tokens/layout and preserves feature-local CSS.
- Retires the separate operator page or redirects it into the product UI.
- Updates package scripts and lockfile.
- Adds browser E2E flows.

Required integrated browser flows:

1. Fixture Observatory navigation and graph interactions.
2. Live snapshot + event update + freeze + replay.
3. Qualification false disables inference with exact reason.
4. Mock qualified request submit + token stream + resume + completion.
5. Request cancellation.
6. Native-node invite + injected EndpointID join + status + revoke with no Tailscale material.
7. Browser-worker invite + second browser join + status + leave.
8. Synthetic browser job visibly labeled non-inference.
9. Plans comparison and readiness matrix.
10. Stale/disconnected event stream retains labeled last-known state.
11. No browser request leaves same origin; no bearer/private endpoint leaks.

---

## 17. Phase R — independent review sessions

These reviews begin only after primary integration commit `INTEGRATED_SHA` exists. Review sessions should be read-only unless the primary explicitly creates fix branches.

### R1 Browser, accessibility, and performance review

- Run automated Chromium, Firefox, and WebKit flows when available.
- Test narrow phone, tablet, laptop, and large desktop viewports.
- Keyboard-only navigation.
- Reduced-motion and high-contrast behavior.
- 24-node and 100-node fixtures.
- Record screenshots only with synthetic/pseudonymized data.
- Report exact failures; do not edit shared source.

### R2 Security, privacy, contracts, and claim-boundary review

- Browser credential and CSRF posture.
- Same-origin and no-direct-upstream proof.
- Prompt/output/log persistence scan.
- Tailscale dependency scan.
- Route-readiness authority scan.
- Observatory mutation scan.
- Synthetic-browser claim scan.
- Contract drift and unknown-value handling.
- Report critical/important/minor findings; do not edit shared source.

Primary fixes all critical and important findings, then reruns both reviews or reproduces every finding locally.

---

## 18. Full verification bundle

Run from integrated product-UI worktree.

```bash
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
python3.14 -m compileall -q .
git diff --check

cd native/iroh_transport
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test

cd ../../ui/web
npm run check
```

If Playwright is authorized and configured:

```bash
npm run test:e2e
```

Also run explicit policy scans:

```bash
# Must return no product-code requirement. Historical evidence/docs may match.
git grep -n -i tailscale -- \
  ':!*.md' ':!docs/automation/**' ':!.hermes/evidence/**'

# Inspect all readiness assignment points.
git grep -n 'route_ready' -- ui mycelium_ui_gateway mycelium_interactive

# Inspect forbidden browser credential/storage usage.
git grep -n -E 'Authorization|localStorage|sessionStorage|Bearer ' -- ui/web/src
```

A grep match is a review trigger, not automatic failure; fixtures and negative tests may legitimately contain forbidden terms.

Document exact command exits and counts. Never summarize a partial bundle as full success.

---

## 19. Definition of done

The product UI is implementation-complete only when:

1. One React application exposes Inference, Network, Nodes, Plans, Readiness, Incidents, and Settings.
2. Live Observatory data drives the same rich views as fixture/replay.
3. All original rich graph, inspector, provenance, readiness, comparison, timeline, clustering, export, accessibility, and performance requirements have tests.
4. Prompt submit/stream/resume/cancel works through the product gateway against a locally qualified/mocked authority.
5. Browser receives no upstream bearer credential.
6. Native-node signed invite/join/status/revoke works through an injected iroh membership coordinator without Tailscale material.
7. Browser-worker invite/join/status/leave works in automated two-browser tests.
8. Browser synthetic work remains clearly non-inference.
9. No product path requires Tailscale.
10. Observatory remains read-only.
11. `route_ready=false` blocks inference and remains visibly false.
12. Full Python, Rust, TypeScript, contract, and browser gates pass.
13. Review sessions have no unresolved critical/important findings.
14. Branch is clean, commits are lane-scoped, and nothing was pushed or merged to main.

Product UI completion does **not** by itself prove:

- physical multi-host distributed inference;
- internet-scale authenticated membership;
- acceptable model quality or performance;
- mobile native model-stage execution;
- production deployment security;
- route readiness.

Those claims require separate physical and semantic qualification.

---

## 20. Primary integration-driver prompt

Copy the following into the new primary session after replacing `<PLAN_COMMIT>` if desired:

```text
You are the primary integration driver for the Mycelium product UI.

Repository: /Users/evinova-self/Projects/mycelium
Planning worktree: /Users/evinova-self/Projects/mycelium-wt-product-ui-plan
Planning branch: planning/mycelium-product-ui
Planning commit: <PLAN_COMMIT>
Code base under the plan: b447c9423f45fdcf847332ab32d7d74d1921d6a9
Master plan: /Users/evinova-self/Projects/mycelium-wt-product-ui-plan/docs/plans/2026-07-19-mycelium-product-ui-master-plan.md
Original rich UI plan: /Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-15_233708-mycelium-ui-display-plan.md
MVP synthesis/claim plan: /Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-17_142630-mycelium-ddai-mvp-synthesis-plan.md

Read all three plans completely before acting. Load writing-plans, test-driven-development, subagent-driven-development, systematic-debugging, and requesting-code-review.

Goal: build Mycelium's one general product UI, including the complete rich Network Observatory, live/replay data, inference prompt/stream/cancel, no-Tailscale native iroh enrollment, swarm/device management, plans, nodes, readiness, evidence, timeline, export, accessibility, and automated browser qualification.

Non-negotiable:
- No Tailscale requirement anywhere in product code or acceptance.
- Preserve Observatory as read-only.
- Inference actions use the separate request gateway through a browser-safe same-origin product gateway.
- Keep all upstream bearer credentials out of the browser.
- Keep route_ready=false until qualifier-owned accepted evidence says otherwise.
- Synthetic browser matrix work is never model inference.
- Do not edit canonical main or any existing source worktree.
- Create/use /Users/evinova-self/Projects/mycelium-wt-product-ui-primary on branch integration/mycelium-product-ui from the planning commit.
- No fetch/pull/push/PR/merge to main.
- No imports from other distributed-inference repositories.
- Stage explicit files only; never git add -A or git add .

Execution:
1. Re-probe all worktrees, SHAs, and statuses. Stop only for a material conflict.
2. Execute Phase P0 serially with TDD.
3. Run foundation gates and make one clean foundation commit.
4. Report FOUNDATION_SHA and exact creation commands for the seven isolated worker worktrees/branches. Do not create worker worktrees unless Evi explicitly asks; each spawned worker normally creates and owns its own worktree.
5. Enforce the file ownership matrix. Do not allow parallel workers to edit shared shell/contracts/package files.
6. Integrate completed worker commits one at a time in the documented order; verify scope and focused gates after each.
7. Wire shared shell and end-to-end flows yourself.
8. Run independent browser/accessibility and security/claim reviews after integration.
9. Fix all critical/important findings.
10. Run every full verification gate and leave a precise handover.

Deliver:
- clean local integration commits;
- exact worker commits integrated/rejected and why;
- test counts and command exits;
- remaining physical/semantic/network gaps;
- claim boundary: local evidence only unless separately qualified;
- route_ready value and authority source;
- confirmation: no Tailscale dependency, no main merge, no push.
```

---

## 21. Parallel worker prompts

Replace `<FOUNDATION_SHA>` with the exact SHA produced by the primary session. Each session creates only its named worktree/branch; an existing path/branch is reusable only when it is the named clean worktree at that exact SHA.

### L1 prompt — product gateway backend

```text
Implement Lane L1 from the Mycelium Product UI Master Plan.

Repository: /Users/evinova-self/Projects/mycelium
Base SHA: <FOUNDATION_SHA>
Worktree: /Users/evinova-self/Projects/mycelium-wt-ui-gateway
Branch: feature/product-ui-gateway
Plan: docs/plans/2026-07-19-mycelium-product-ui-master-plan.md, Sections 3, 5, 7, 8, and 10.

Create the worktree from exactly <FOUNDATION_SHA>. If path/branch exists, use it only when it is the named clean worktree at that exact SHA; otherwise stop and report. Stop for any material topology difference. Follow TDD.

You own only:
- mycelium_ui_gateway/**
- tests/ui_gateway/**
- docs/product-ui/product-gateway.md

Do not edit existing Observatory/request-gateway/Router/runtime/qualifier/iroh/React files. Use their public interfaces only.

Build the browser-safe same-origin BFF: bounded local session, HttpOnly SameSite=Strict cookie, CSRF defense, no-store, body/response bounds, Observatory GET/SSE composition, qualification projection, inference submit/stream/resume/cancel forwarding, and injected swarm coordinator adapter. No upstream bearer credential may reach browser, URL, HTML, JS, logs, or errors. No Tailscale logic. Never reconstruct route_ready=true.

Run focused tests, compileall, and git diff --check. Stage exact owned files only. Commit locally. Do not push or merge.

Handover: base SHA, commit SHA(s), files, tests/counts/exits, gaps, and local-only claim boundary.
```

### L2 prompt — live Observatory controller

```text
Implement Lane L2 from the Mycelium Product UI Master Plan.

Repository: /Users/evinova-self/Projects/mycelium
Base SHA: <FOUNDATION_SHA>
Worktree: /Users/evinova-self/Projects/mycelium-wt-ui-live
Branch: feature/product-ui-live-observatory
Plan: docs/plans/2026-07-19-mycelium-product-ui-master-plan.md, Sections 3, 4.2, 7, 8, and 11.

Create the worktree from exactly <FOUNDATION_SHA>. If path/branch exists, use it only when it is the named clean worktree at that exact SHA; otherwise stop and report. Stop for any material topology difference. Follow TDD.

Own only the existing Observatory TS data/projection files listed in Section 8 plus new ui/web/src/features/observatory/live/**. Do not edit views, graph files, shell, shared contracts, global styles, package files, or Python.

Build one production ObservatoryController for snapshot bootstrap, SSE updates, cursor/generation monotonicity, reconnect, staleness, freeze, replay, and change sets. Ensure production code imports the event source. Preserve fixture/live/replay labels, unknown values, privacy reduction, and literal route_ready. Replay must make zero network calls.

Run focused Vitest, typecheck, build, and git diff --check. Stage exact files only. Commit locally. No push/merge.

Handover exact SHA/files/tests and local browser-projection claim boundary.
```

### L3 prompt — inference workspace

```text
Implement Lane L3 from the Mycelium Product UI Master Plan.

Repository: /Users/evinova-self/Projects/mycelium
Base SHA: <FOUNDATION_SHA>
Worktree: /Users/evinova-self/Projects/mycelium-wt-ui-inference
Branch: feature/product-ui-inference
Plan: docs/plans/2026-07-19-mycelium-product-ui-master-plan.md, Sections 3, 4.1, 5, 7, 8, and 12.

Create the worktree from exactly <FOUNDATION_SHA>. If path/branch exists, use it only when it is the named clean worktree at that exact SHA; otherwise stop and report. Stop for any material topology difference. Follow TDD.

Own only ui/web/src/features/inference/** and docs/product-ui/inference-workspace.md. Do not edit shell, global styles, package/lock files, shared contracts, or Python.

Build qualification-gated prompt submission, exact binding capture, SSE token stream/resume, contiguous sequence validation, idempotent duplicate handling, cancellation, terminal states, privacy-safe defaults, accessibility, and reduced motion. Use the frozen same-origin product-gateway contract. Never put prompt/output in logs, URL, localStorage, sessionStorage, or metrics. route_ready=false must disable submit with exact reason.

Run focused Vitest, typecheck, build, and git diff --check. Stage exact owned files only. Commit locally. No push/merge.

Handover exact SHA/files/tests and local/mock request-lifecycle claim boundary.
```

### L4 prompt — swarm/device workspace

```text
Implement Lane L4 from the Mycelium Product UI Master Plan.

Repository: /Users/evinova-self/Projects/mycelium
Base SHA: <FOUNDATION_SHA>
Worktree: /Users/evinova-self/Projects/mycelium-wt-ui-swarm
Branch: feature/product-ui-swarm
Plan: docs/plans/2026-07-19-mycelium-product-ui-master-plan.md, Sections 3, 4.3, 7, 8, and 13.

Create the worktree from exactly <FOUNDATION_SHA>. If path/branch exists, use it only when it is the named clean worktree at that exact SHA; otherwise stop and report. Stop for any material topology difference. Follow TDD.

Own only:
- ui/web/src/features/swarm/**
- ui/web/src/interactive/**
- mycelium_interactive/**
- tests/interactive/**
- docs/product-ui/swarm-workspace.md

Do not edit App/AppShell/global styles/package files/product gateway/Router/runtime/qualifier/iroh.

Build product-native device inventory and invite/join/status/leave UX. Keep native-node and browser-worker capabilities distinct. Preserve bounded sessions and expiry. Keep synthetic matrix jobs behind an explicit developer/probe label and route_ready=false. Remove every product assumption that Tailscale exists; connectivity labels are direct/relayed/local/disconnected/stale/unknown. Redact private addresses by default.

Run Python interactive tests/compileall plus focused TS tests/typecheck/build and git diff --check. Stage exact owned files only. Commit locally. No push/merge.

Handover exact SHA/files/tests and synthetic-browser-only claim boundary.
```

### L5 prompt — rich Network Observatory graph

```text
Implement Lane L5 from the Mycelium Product UI Master Plan.

Repository: /Users/evinova-self/Projects/mycelium
Base SHA: <FOUNDATION_SHA>
Worktree: /Users/evinova-self/Projects/mycelium-wt-ui-graph
Branch: feature/product-ui-network-graph
Plan: original /Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-15_233708-mycelium-ui-display-plan.md plus master plan Sections 3, 4.2, 7, 8, and 14.

Read both plan sections completely. Create the worktree from exactly <FOUNDATION_SHA>. If path/branch exists, use it only when it is the named clean worktree at that exact SHA; otherwise stop and report. Stop for any material topology difference. Follow TDD.

Own only graph files, RouteCanvas, DeviceNode, NetworkView, and new observatory component/style directories listed in Section 8. Do not edit data sources, shared model/contracts, shell, global styles, packages, or Python.

Finish pipeline/ring/SCC/elastic-geo/true-map layouts, stable positions, clustering, edge bundling, physical/logical toggles, prefill/decode/closure/alternatives, node memory/layer glyphs, exact edge formulas/provenance, inspectors, accessible tables, reduced motion, and 24/100-node performance fixtures. Never invent coordinates, bandwidth, readiness, or telemetry. Animation defaults off and remains labeled modeled unless source is a real authenticated trace.

Run focused tests, typecheck, build, and git diff --check. Stage exact owned files only. Commit locally. No push/merge.

Handover exact SHA/files/tests/performance observations and renderer-only claim boundary.
```

### L6 prompt — Plans, Nodes, Readiness, Evidence

```text
Implement Lane L6 from the Mycelium Product UI Master Plan.

Repository: /Users/evinova-self/Projects/mycelium
Base SHA: <FOUNDATION_SHA>
Worktree: /Users/evinova-self/Projects/mycelium-wt-ui-plans
Branch: feature/product-ui-plans-evidence
Plan: original /Users/evinova-self/Projects/mycelium/.hermes/plans/2026-07-15_233708-mycelium-ui-display-plan.md plus master plan Sections 3, 4.3-4.6, 7, 8, and 15.

Read both plan sections completely. Create the worktree from exactly <FOUNDATION_SHA>. If path/branch exists, use it only when it is the named clean worktree at that exact SHA; otherwise stop and report. Stop for any material topology difference. Follow TDD.

Own only PlansView, EvidenceView, IncidentsView, new features/plans, features/readiness, features/nodes, feature-local CSS modules, and docs/product-ui/plans-readiness.md. Do not edit graph, data sources, shell, global styles, packages, or Python.

Build strategy ranking/comparison, route/layer alternatives and assumptions, node inventory/detail, strict readiness ladder/matrix, source/evidence drawer, evidence diff/history, timeline/replay, truthful incidents, pseudonymized export, and accessibility. No mutable planner controls. Artifact verification must never become runtime loaded or route ready.

Run focused tests, typecheck, build, and git diff --check. Stage exact owned files only. Commit locally. No push/merge.

Handover exact SHA/files/tests and read-only interpretation claim boundary.
```

### L7 prompt — native iroh membership/enrollment

```text
Implement Lane L7 from the Mycelium Product UI Master Plan.

Repository: /Users/evinova-self/Projects/mycelium
Base SHA: <FOUNDATION_SHA>
Worktree: /Users/evinova-self/Projects/mycelium-wt-ui-membership
Branch: feature/product-ui-native-membership
Plan: docs/plans/2026-07-19-mycelium-product-ui-master-plan.md, Sections 3, 4.3, 5, 7, 8, and 15A.

Create the worktree from exactly <FOUNDATION_SHA>. If path/branch exists, use it only when it is the named clean worktree at that exact SHA; otherwise stop and report. Stop for any material topology difference. Follow TDD.

Own only mycelium_membership/**, tests/membership/**, and docs/product-ui/native-membership.md. Do not edit native/iroh_transport, Router, runtime, qualifier, gossip, interactive, product-gateway, React, package, or shared contract files. Consume public contracts only.

Build signed expiring single-use native-node invitations, explicit operator trust, authenticated EndpointID/bootstrap exchange, replay/expiry/audience/scope checks, signed monotonic endpoint updates, redacted public status, idempotent revoke, injected coordinator protocol, and a two-process local harness. Never require or encode Tailscale/IP/DNS material. Keep invited/trusted/reachable/assigned/qualified/revoked distinct; reachability must never imply route_ready or inference capability. Membership/gossip carries no prompt, token, activation, KV state, tensor, protected edit, or model artifact.

Run membership pytest, compileall, policy scans, and git diff --check. Stage exact owned files only. Commit locally. No push/merge.

Handover exact SHA/files/tests and local authenticated-membership-only claim boundary; explicitly list unproven internet-scale/NAT/physical gaps.
```

---

## 22. Reviewer prompts

### Browser/accessibility reviewer

```text
Read-only review of integrated Mycelium product UI at <INTEGRATED_WORKTREE> / <INTEGRATED_SHA>. Do not edit files.

Read master plan Sections 4, 16, 17, 18, and 19. Run available automated Chromium, Firefox, and WebKit flows; responsive phone/tablet/laptop/desktop viewports; keyboard-only; reduced motion; high contrast; 24-node and 100-node fixtures. Verify all original rich UI surfaces and product inference/swarm flows. Use only synthetic/pseudonymized data.

Report Critical, Important, Minor findings with exact reproduction, expected/observed result, file/route, and evidence. Distinguish missing package/browser infrastructure from product failures. No physical device claims.
```

### Security/claim-boundary reviewer

```text
Read-only security/privacy/contract review of integrated Mycelium product UI at <INTEGRATED_WORKTREE> / <INTEGRATED_SHA>. Do not edit files.

Audit: browser credential exposure, CSRF, same-origin, direct-upstream requests, prompt/output persistence, private address/path leakage, Tailscale dependencies, Observatory mutation, route_ready authority, qualification binding, SSE sequence/resume, synthetic-browser claims, unknown-to-zero coercion, source provenance, and contract drift.

Run focused tests and static searches. Report Critical, Important, Minor findings with exact evidence. Explicitly state whether any product path requires Tailscale and whether any UI path can fabricate or bypass route readiness.
```

---

## 23. Final handover template

```text
Mycelium Product UI handover

Branch/worktree:
Base SHA:
Foundation SHA:
Integrated worker commits:
Rejected/reworked commits and why:
Final HEAD:
Working tree status:

Implemented surfaces:
- Inference:
- Network:
- Native membership/enrollment:
- Nodes/Swarm:
- Plans:
- Readiness/Evidence:
- Settings/Diagnostics:

Verification:
- Python pytest:
- Contract audit:
- Compileall:
- Rust fmt/clippy/test:
- UI unit/contract/typecheck/build:
- Multi-browser E2E:
- git diff --check:
- Policy scans:

Authority and claim boundary:
- Observatory read-only:
- Request authority:
- Qualification source:
- route_ready:
- Synthetic browser boundary:
- Tailscale dependency:
- Physical multi-host proof:

Remaining gaps:

No merge to main: confirmed
No push/PR: confirmed
```
