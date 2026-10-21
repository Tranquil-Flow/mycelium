# A8 Product-Spine Wiring Plan — Shared-Runtime Seam (Handover)

**Status:** implemented and physically qualified. The coordinated shared
runtime tranche is included in the frozen A8 source manifest and exercised by
the ordinary-browser direct, forced-relay, and path-transition gates.

## Implemented product-spine composition

- `mycelium_internet/contracts.py` — the four closed shapes (authoritative
  field vocabulary).
- `mycelium_internet/enrollment.py::PublicBootstrapClient.bootstrap_status(now)`
  — emits a validated `mycelium.internet_bootstrap_status.v1` document
  (counters, freshness, tls/pin/route/invitation states; privacy-clean by
  construction).
- `mycelium_internet/activation.py::ActivationObservations` —
  `current_projection()` / `history()` emit
  `mycelium.internet_activation_observation.v1` documents; `RelayProjector`
  emits the HMAC reference + reviewed region.
- `ui/web/src/features/internetNative/` — typed projections and the eight
  workspace modules with vitest coverage. Each module accepts the
  projection documents directly as props.

## Landed seam

1. **Spine projector input** (`mycelium_product_spine/projector.py`) now accepts:
   - a `bootstrap_status` input sourced from the seed-side adapter (live
     mode) or a privacy-clean fixture (fixture mode);
   - an `activation_observation` input sourced from the bound
     `ActivationObservations` ledger of the member's own sidecar (live
     mode) or fixture;
   - a `relay_projection` input sourced from `RelayProjector` with the
     owner-private projection key (never the key itself).
2. **Closed sub-shape** in `product_event.v1`'s `projection`:
   `internet_native: {bootstrap_status | null, activation_observation |
   null, relay_projection | null}` — each sub-object validated with the
   corresponding `mycelium_internet.contracts` validator. Null = unknown
   (unknown-not-zero holds end to end).
3. **SSE/event publication**: the sub-shape rides the existing
   publication cadence; no new endpoints.
4. **UI wiring** (`ui/web/src/App.tsx` + sources) is mounted across:
   - Device Lab: `InternetBootstrapPanel` fed by `bootstrap_status`;
   - Network: `NetworkPathPanel` fed by `activation_observation` +
     `relay_projection`;
   - Nodes: `NodesInternetPanel` fed by member projection fields
     (pseudonym only — never raw endpoint ids);
   - Plans: `planRequiresPathCosts(observation)` gate before any objective
     needing path costs;
   - Readiness: `buildInternetReadinessChecks(...)`;
   - Incidents: `describeInternetIncident` vocabulary for the seven bounded
     codes;
   - Settings: `SettingsInternetPanel`;
   - Inference: `InferencePathBadge` (path class + "membership alone does
     not make inference available" copy when unqualified).
5. **Fixture bundle**: product event/snapshot fixtures contain the three
   privacy-clean documents and pass generated-fixture drift checks.

## Contract-pipeline impact

The `product_event.v1` and `product_snapshot.v1` closed shapes, fixture
generator, compatibility fixtures, Python validators, and TypeScript parser
were updated together. Legacy persisted snapshots receive an exact-field-set,
claim-free migration to `unknown`; malformed or extra-field legacy state
continues to fail closed.

## Executed verification

- Every `internet_native` sub-object passes its contract validator AND
  `ensure_privacy_clean` with the operator needles (invite token, raw
  EndpointID, hostname, relay URL).
- UI vitest: the eight workspace tests in `ui/web/src/features/internetNative/`
  stay green against live-shaped fixtures.
- Browser gates (spec §11) passed against the public live origin with direct
  and forced-relay signed transport evidence. The collector rendered all
  eight workspaces, reconstructed through back/forward/reload, verified a
  clean second session, and completed two transition requests.
- Privacy-reduced retained proof:
  `docs/handover/a8-physical-qualification-summary.v1.json`.

## Physical-era checklist for this seam

- [x] Integration owner reserved the spine composition.
- [x] Spine projection sub-shape landed in the coordinated runtime tranche.
- [x] Fixture bundle gained the three documents.
- [x] `App.tsx` wired the eight modules through the unified product snapshot.
- [x] Focused Python, UI, contract, and native Rust gates passed before
  physical qualification.
- [x] Final post-document regressions passed: 4,592 Python tests plus 121
  subtests, 581 UI unit tests, three-browser Playwright E2E, Rust format/tests/
  Clippy/release build, and changed-file Ruff.
- [x] Contract, governance, claim-boundary, release-security, and completion
  audits passed on the final worktree.
