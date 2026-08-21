# A8 Product-Spine Wiring Plan — Shared-Runtime Seam (Handover)

**Status:** seam specification only. No shared-runtime bytes were touched in
the A8 parallel lane. Spec §10: *"Implementation cannot begin shared wiring
until the current integration owner reserves membership, supervisor,
activation, contracts, and cross-workspace source composition."* The product
spine is shared with the A4 closure lane; wiring it requires coordination
with that integration owner and A1 product authority.

## What already exists in this lane (no wiring needed)

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

## The seam (what the shared runtime must add)

1. **Spine projector input** (`mycelium_product_spine/projector.py`):
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
3. **SSE/event publication**: the new sub-shape rides the existing
   publication cadence; no new endpoints.
4. **UI wiring** (`ui/web/src/App.tsx` + sources):
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
5. **Fixture bundle**: extend the fixture product snapshot with the three
   privacy-clean documents (mirror `compatibility_fixtures()`), so the
   browser gates have fixture-mode rendering before live observations
   exist.

## Contract-pipeline impact

The `product_event.v1` projection change is a closed-shape change to a
shared contract: registry entry, fixture regeneration, manifest, and
`EXPECTED_PROTOCOLS` updates, plus A1 authority review. Do this as part of
the coordinated shared-runtime tranche, not in the A8 parallel lane.

## Verification when wired

- Every `internet_native` sub-object passes its contract validator AND
  `ensure_privacy_clean` with the operator needles (invite token, raw
  EndpointID, hostname, relay URL).
- UI vitest: the eight workspace tests in `ui/web/src/features/internetNative/`
  stay green against live-shaped fixtures.
- Browser gates (spec §11) run against the live origin with the wired UI.

## Physical-era checklist for this seam

- [ ] Integration owner reserves the spine composition (per spec §10).
- [ ] Spine projection sub-shape lands with A1 authority.
- [ ] Fixture bundle gains the three documents.
- [ ] App.tsx wires the eight modules (additive, per-workspace).
- [ ] Full Python suite + UI suite + contract audit green.
