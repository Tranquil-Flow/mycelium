# A14a — local production implementation (Session 8C)

## Scope and authority

Implemented on an isolated branch from `abe291c3ae856bef60394f528f1f86bddf78b2ad`.
The Wave 8 goal, not the historical inventory's `design_only` marker, authorizes
this implementation. The frozen acceptance inventory and shared integration/ledger
remain unchanged. This is a local candidate, not integration, release, physical
qualification, completion of all A14, or permission to begin A14b.

Production path:

`membership / live-route / A8 internet projection -> ProductProjector -> validated
product snapshot + route_explorer -> ProductEvidenceApplication -> ProductGateway
snapshot/events -> decodeProductSnapshot -> ProductEvidenceContext -> Network
RouteExplorerWorkspace`.

No new planner, membership, readiness, incident or region authority. Endpoint and
membership pseudonyms remain separate. A8 knows a remote endpoint but does not bind
that observation to both members, a request, track or inference phase. The view
therefore MUST NOT paint its transport class onto selected logical links. The
server recomputes canonical projection/source equivalence; the browser validates
closed public shapes. Digests are integrity references, not signatures.

## Implemented behavior

- Closed `mycelium.route_explorer.v1`, optional in legacy product-v1 reads. Registry,
  fixture, manifest and Python/TypeScript validators are updated together.
- Selected directed logical links and A8 direct/relay observation history are
  separate records. Unknown is explicit, not a zero metric. Stable observation IDs
  exclude changing freshness; connection generations/issued times retain transitions.
- Normative semantic table, compact expandable metrics/bindings, synchronized
  static directed graph, independent path/transport/phase/role/provenance/freshness
  filters, retained selection detail and deterministic pagination.
- Unknown-region peer tray remains available even when all route rows are filtered
  out. Up to 4096 peers/segments are accepted, 32 segments and 64 peers rendered per
  page. Roster shrink clamps pagination. Upstream observation history is bounded
  to 64 records: this is NOT a lifetime traffic archive or a new historical authority.
- Keyboard-operable controls, explicit table headers/caption, named regions,
  pressed-state selection, focus indicators, live status, forced-colors support,
  200% text scaling, horizontally scrollable table, responsive static graph.
- Pause retains a historical view; low-power freshness refresh is bounded at 30s
  instead of 1s. There are no graph animations, map assets, coordinates or pointer-
  only semantics. Route filters/selection are not persisted as private session data.
- Product subscription now recovers from initial snapshot failure and transport
  disconnect rather than permanently stopping after the first load failure.
  Expired/future observations render stale/unknown respectively.

## Acceptance / implementation / check matrix

Tests named below are local conformance, never physical evidence. The final external
`final-results.json`, browser reports, source/build hashes and self-excluding index
bind actual outcomes to the committed candidate; do not substitute this document
for those receipts.

| Inventory case / requirement | Implementation and local check | Remaining boundary |
|---|---|---|
| `a14a_without_geographic_region` | Production Network route table/graph/tray; browser IDs agree; all peers remain in bounded tray, including 256 synthetic members | Real fleet/assistive-technology qualification not run |
| `a14b_before_a14a_rejected` | Existing inventory test retained; no globe/WebGL/map assets introduced | A14b remains unstarted; coordinator acceptance required |
| `authorized_region_normalization` | Existing frozen inventory coverage retained; current projection permits only unknown/unavailable | Signed region declaration/reviewed metadata producer-consumer is absent from this A8 source; no authority invented |
| `unknown_conflicting_or_stale_region` | Closed projection and nested hostile-field tests; no supplied region authority means unknown; peer not hidden | Conflict/withdrawal of a future signed region authority is not claimed implemented |
| `network_derived_location_rejected` | Python and TypeScript reject coordinate/address/hostname/private fields; real browser canary and same-origin request checks | No location inference or regional placement provided |
| `direct_relay_transition_history` | Production projector with labelled direct then relay conformance; table/graph keep both IDs and generations; refresh/reconnect/second browser context reconstruct | Inventory physical-positive gate remains unrun |
| `forged_or_stale_transition_rejected` | Existing A8 validation plus source-equivalence/rehashed-tamper tests; browser expiry/disconnect does not create new authority | Physical forged/stale negative gate remains unrun |
| `path_transport_filter_independence` | Distinct closed dimensions and composed filter tests; browser selected+relay produces no inferred row, clear restores same IDs | A8 supplies selected logical and observed transport facts, not proposed/failed/recovered path authority or request-phase bindings |
| `sealed_or_historical_not_live` | Explicit fixture/replay/degraded labels, per-record provenance and history; no animation or qualification minting | Current source publishes one provenance mode per snapshot; mixed signed archives are not fabricated |
| `accessible_presentation_equivalence` | Real Chrome: semantic table/graph IDs, keyboard selection/focus, all eight workspace routes, reload, Back/Forward, reconnect, clean second context; DOM/AX and screenshots | Not VoiceOver/NVDA user qualification or a Firefox/WebKit execution claim |
| `reduced_motion_and_low_power` | Browser reduced-motion/forced-colors/low-power/pause; static markers, bounded refresh, history preserved | No performance/energy benchmark claim |
| `projection_mutation_attempt` | All browser-observed requests are same-origin GET; view controls only update local state; nested contract rejection/source equivalence | Separate region mutation workflow intentionally absent |

## Real local application exercise

`serve_a14_conformance.py` is a finite loopback-only HTTP adapter around the ordinary
ASGI ProductGateway and ProductEvidenceApplication. It injects synthetic authority
inputs, not pre-rendered HTML or a mocked browser response. The subclass fixes only
`source_mode='fixture'`; the real projector performs reduction. The standard live
frontend build is served unchanged. Browser session credentials are random,
memory-only, HttpOnly, and never printed. No model, fleet, SSH, geolocation or
third-party service is contacted. Absent qualifier/legacy-observatory endpoints
remain explicit 404/unavailable in their existing panels, not forged green badges.
At 390px, the pre-existing logical-execution pipeline table below the explorer can
extend the page width. The new explorer itself remains bounded and its table is
keyboard-scrollable; no claim of a full-app mobile-layout cleanup is made.

Run from `ui/web`, with an existing supported Chrome binary:

```sh
npm run build:live
A14_EVIDENCE=/absolute/private/output node scripts/a14-local-browser.mjs
A14_CONFORMANCE_MEMBERS=256 A14_EVIDENCE=/absolute/private/large-output node scripts/a14-local-browser.mjs
```

Optional `A14_CHROMIUM` selects an already installed binary. No browser download is
performed. The script uses separate isolated browser contexts and GET fault
injection to test reconnect/initial retry. It owns and terminates its child server;
the server also expires after ten minutes. HTTP lifecycle receipts are recorded.
Do not use the conformance server as a production deployment.

Python: pinned `/opt/homebrew/bin/python3.14` (3.14.4), scrub `PYTHONPATH`,
`PYTHONHOME`, `VIRTUAL_ENV`, set `PYTHONDONTWRITEBYTECODE=1` and disable pytest cache.
Node/npm: pinned Node 22.22.2. Run suites sequentially:

```text
python -m pytest -q -p no:cacheprovider tests/a14_acceptance tests/product_spine tests/contracts
python -m pytest -q -p no:cacheprovider tests/ui_gateway
python -m pytest -q -p no:cacheprovider tests/live/test_internet_projection.py
python scripts/generate_a14_conformance.py --check
python scripts/contract_audit.py --json
npm test
npm run typecheck
npm run build:live
```

Do not combine gateway and live test directories: their legacy bare `conftest`
imports collide during collection. The retained collection error is a verifier
composition failure, not a passing suite. Use separate invocations without
rewriting unrelated tests.

## Review and failed hypotheses retained

- Initial missing-module/stub REDs; initial-reconnect RED; peer-page shrink RED;
  future-issued-time RED; immutable-aging/type-boundary RED; graph-generation RED.
- Loopback bridge initially emitted duplicate Content-Length and the real browser
  refused its payload. Fixed the adapter, not the product decoder.
- Browser gate initially queried a navigation button rather than its actual link.
- Reconnect used a substring check that could match `disconnected`; replaced with
  exact `body[data-evidence=connected]` before accepting browser receipts.
- Screenshot review prompted compact metric disclosures and fixed-width wrapping
  columns; table is no longer several excessively tall metric rows.
- Registry test's explicit protocol set now includes the new registered protocol.
- No test was deleted, physical threshold weakened, or shared authority edited.

## Proposed coordinator ledger update / remaining work

8C: locally implemented and browser-exercised, clean isolated committed candidate;
not shared-integrated. Adopt only after coordinator review and controlled fan-in.
A14 overall remains unqualified. Physical direct/relay positive and forged/stale
negative, and representative assistive-technology/platform qualification remain
external gates. No A5 result, physical source movement, A6 merge or A14b start.

Unknown request/track/phase/role/proposed-path facts need actual bound source
publication before the UI may display them as known. Authorized coarse-region
normalization needs its separate signed authority contract/producer. Optional
A5/A6/A7/A11 overlays remain unavailable until qualified inputs exist. These are
explicit unbuilt/unbound follow-ons, not fabricated by the route renderer.
