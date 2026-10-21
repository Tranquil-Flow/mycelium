# Mycelium A14b Coarse Region and Globe Specification

**Status:** `design_only`; A14a integrated, A14b specification and acceptance suite only
**Gate:** A14b, the second slice of A14
**Parent:** `2026-08-18-mycelium-a14-route-explorer.md`
**Predecessor:** A14a accessible route table/logical graph, candidate `f35ab91`, integrated
at `435b857` (ancestor of `b90edaf`)
**Frozen inputs:** `tests/a14_acceptance/inventory.v1.json`
(`mycelium.a14_acceptance_inventory.v1`, state `design_only`): region vocabulary,
`coarse_region_authority`/`unknown_region_handling` decisions, `authorized_region_normalization`,
`unknown_conflicting_or_stale_region`, `network_derived_location_rejected`, and
`a14b_before_a14a_rejected` cases, and the `coarse_globe_region_visualization` presentation
row. This document adds no authority to them.
**Depends on:** A14a acceptance. A8 live/relay observation authority is transitive through
A14a. A14b must not begin execution before A14a is accepted.

## 1. Outcome and claim boundary

A14b adds the optional coarse globe/region presentation over the same read-only
`mycelium.route_explorer.v1` projection A14a already publishes. It freezes four things and
nothing else: the shared macro-region vocabulary, the closed coarse-region evidence record
that feeds it, the pure reduction from reviewed evidence to per-peer region rows, and the
renderer boundary that keeps the A14a table normative.

A14b does not geolocate an IP address, accept GPS, Wi-Fi/cell location, street or city
address, latitude/longitude, geohash, provider region name, hostname, raw EndpointID, relay
URL, or private address. It does not derive or refine a region from RTT, jitter, loss,
goodput, route shape, time zone, locale, or device setting. It does not assert that a
declared region is verified physical location, does not locate a peer from a relay region,
and does not create region, path, transport, planning, qualification, liveness, or evidence
authority. It cannot mutate planner input, evidence, selection, route, membership,
qualification, or incident state.

A declared region is a claim by its authority. A cell on the globe is schematic product
geometry, never a position. `unknown` is a first-class outcome, never a failure to be
papered over with the nearest or default region.

## 2. What A14a already does, and what stays frozen

A14a `435b857` publishes a closed, deterministic projection:

- `mycelium.route_explorer.v1` with the exact field set
  `protocol, generation, source_mode, source_digest, nodes, segments, overlays, digest`
  (`mycelium_product_spine/route_explorer.py:27`), where `overlays` must equal
  `'unavailable'` (`:52`);
- nodes with the exact field set `id, kind, region, region_source` (`:58`), currently frozen
  to `region == 'unknown'` and `region_source == 'unavailable'` (`:61`);
- immutable segments carrying path generation, phase, transport, provenance, freshness and
  history (`:64`-`:91`) and a stable identity that excludes publication freshness (`:118`);
- a projection that is a pure function of the product snapshot, re-derived and compared at
  `mycelium_product_spine/contracts.py:709`-`714` and attached at
  `mycelium_product_spine/projector.py:196`-`198`;
- the normative accessible table/timeline and logical graph, the unknown-region tray, the
  independent path/transport filters, and reduced-motion/low-power behaviour documented in
  `docs/a14a-local-implementation.md`.

A14b **adds** region vocabulary, evidence, reduction, and rendering. It weakens no A14a
invariant: every A14a-era projection (no A14b keys, every region unknown) must remain valid
and byte-identical under the A14b validator, and the A14a suite must stay green unchanged.
A14a's endpoint observation is still not a membership edge: a relay region labels the relay
endpoint only.

## 3. Shared coarse-region vocabulary

`mycelium.coarse_region.v1` is the only geographic vocabulary shared by owner declaration,
reviewed A8 relay metadata, the projection, and A14b rendering. Its value set is exactly
the frozen inventory set:

`africa`, `americas_north`, `americas_south`, `asia`, `europe`, `oceania`, `polar`,
`unknown`.

Normalization is a **closed alias table**, not a parser:

| Input code | Normalized value |
|---|---|
| `africa` | `africa` |
| `americas_north`, `north_america`, `central_america`, `caribbean` | `americas_north` |
| `americas_south`, `south_america` | `americas_south` |
| `asia` | `asia` |
| `europe` | `europe` |
| `oceania` | `oceania` |
| `polar`, `arctic`, `antarctic`, `antarctica` | `polar` |
| `unknown` | `unknown` |
| anything else, including any non-string | `unknown` |

Rules:

1. Central America and the Caribbean normalize to `americas_north`; Antarctica and
   Arctic-only declarations normalize to `polar`.
2. A value that spans more than one vocabulary code is ambiguous and stays `unknown`:
   `americas` is not an alias.
3. Unmapped, ambiguous, conflicting, stale, and unauthorized values normalize to `unknown`,
   never to the nearest, largest, or default region.
4. The vocabulary contains no country, subdivision, city, site, availability zone, provider
   region name, latitude, longitude, geohash, network prefix, or free-form location. A
   provider region name such as `us-east-1` normalizes to `unknown`.
5. No IP address, DNS name, hostname, EndpointID, RTT, jitter, loss, goodput, route shape,
   time zone, locale, or device setting may derive or refine a geographic value.

## 4. Closed coarse-region evidence

`mycelium.coarse_region_evidence.v1` is a closed record with exactly these fields:

| Field | Domain |
|---|---|
| `protocol` | exactly `mycelium.coarse_region_evidence.v1` |
| `subject_ref` | `sha256:` digest of a pseudonymous member or relay reference |
| `subject_kind` | `member` or `relay` |
| `source_kind` | `owner_declared`, `reviewed_relay_metadata`, or `latency_distance_class` |
| `region` | one `mycelium.coarse_region.v1` code |
| `distance_class` | `near`, `mid`, `far`, `unknown`, or `null` |
| `vocabulary_version` | exactly `mycelium.coarse_region.v1` |
| `model_version` | `null`, or a bounded code for the declared latency model |
| `uncertainty` | `declared_unverified`, `reviewed`, or `model_estimate` |
| `issued_at_unix_ms` | non-negative integer |
| `expires_at_unix_ms` | non-negative integer, not before `issued_at_unix_ms` |
| `authority_digest` | `sha256:` digest of the owning authority statement |
| `digest` | canonical digest of this record without `digest` |

Invariants:

1. **Exactly one reviewed source per record.** `uncertainty` is bound to `source_kind`:
   `owner_declared` is `declared_unverified`, `reviewed_relay_metadata` is `reviewed`,
   `latency_distance_class` is `model_estimate`.
2. **A latency class is not geography.** When `source_kind` is `latency_distance_class`,
   `region` must be `unknown`, `distance_class` must be one of the four class codes, and
   `model_version` must be non-null. For the two geographic sources `distance_class` is
   `null` and `model_version` is `null`.
3. **A relay record locates the relay only.** `subject_kind == 'relay'` may bind an endpoint
   node; it may never bind a member node. `subject_kind == 'member'` may bind a member node.
4. **No precise or network identity.** `subject_ref` is a digest, never a raw peer, endpoint,
   address, or URL. A closed shape rejects `latitude`, `longitude`, `geohash`, `ip_address`,
   `address`, `city`, `hostname`, `endpoint_id`, `relay_url`, `private_address`,
   `provider_region`, `free_form_location`, and any other key outright.
5. **Digests are integrity references, not signatures.** Canonical digest is
   `'sha256:' + sha256(utf8(json(record, sort_keys=True, separators=(',', ':'),
   allow_nan=False)))` over the record without its own `digest` key, identical to
   `mycelium_product_spine/route_explorer.py:33`-`34`. Booleans are not integers. Expiry is
   inclusive: a record is current while `now_unix_ms <= expires_at_unix_ms`.

## 5. Read-only reduction

`mycelium_product_spine/coarse_region.py` owns the reduction. For each node of the
projection it produces exactly one region row with the closed shape

`node`, `region`, `region_source`, `region_blocker`, `distance_class`

where `region_source` is one of `unavailable`, `owner_declared`, `reviewed_relay_metadata`,
`latency_distance_class`; `region_blocker` is one of `missing`, `expired`, `conflicting`,
`unauthorized`, or `null`; and `distance_class` is a class code or `null`.

Candidates for a node are the evidence records whose `subject_ref` equals the node `id` and
whose `subject_kind` matches the node `kind` (`member` to `member`, `relay` to `endpoint`).
Then, in this frozen precedence:

1. no candidate exists: `(unknown, unavailable, missing, null)`;
2. candidates exist but none carries an accepted `authority_digest`:
   `(unknown, unavailable, unauthorized, null)`;
3. accepted geographic candidates exist but none is current:
   `(unknown, unavailable, expired, null)` — a lapsed geographic statement is never replaced
   by a latency class;
4. current geographic candidates disagree on the normalized region:
   `(unknown, unavailable, conflicting, null)`;
5. current geographic candidates agree: `(code, that source kind, null, null)` — including an
   explicit `unknown` statement, which renders off-globe with its source visible;
6. no accepted geographic candidate exists and no accepted latency class is current:
   `(unknown, unavailable, expired, null)`;
7. no accepted geographic candidate exists and the current latency classes disagree:
   `(unknown, unavailable, conflicting, null)`;
8. otherwise a current latency class exists: `(unknown, latency_distance_class, null, class)`.

Additional invariants:

- An on-globe row is exactly a row whose `region` is not `unknown`. Because rule 7 emits
  `region == 'unknown'`, `unknown` and non-geographic latency classes are always off-globe.
- Source precedence is total. When an owner declaration and reviewed relay metadata agree on
  one subject, the row reports `owner_declared`; when they disagree it is `conflicting`.
- Withdrawal is a signed successor statement: the withdrawn record leaves the accepted
  authority set and its row reduces under rule 2. Evidence records are immutable.
- The reduction is pure, deterministic, sorted by `node`, bounded by the projection's node
  bound, JSON-serializable, and never mutates its inputs.
- The reduction consumes and emits vocabulary codes only. It emits no coordinate, cell,
  distance, provider name, or identifier of any kind.
- Unknown peers are never dropped: a row exists for every node, including peers with no
  evidence at all.

## 6. Projection evolution

The reduction is published through the existing closed projection, additively:

1. Node `region` widens from the single value `unknown` to any `mycelium.coarse_region.v1`
   code, and node `region_source` widens from `unavailable` to
   `unavailable`, `owner_declared`, `reviewed_relay_metadata`, or `latency_distance_class`.
2. The projection gains an **optional** `regions` key holding the reduction rows in the exact
   shape of Section 5, sorted by `node`, one row per node, bounded by the node bound. The key
   is present exactly when the snapshot publishes at least one reviewed evidence record; a
   snapshot without evidence yields the A14a projection unchanged, with every region
   `unknown`/`unavailable`.
3. `regions` is optional so that A14a-era projections remain valid unchanged. When present,
   every row must name a node of the projection, and `row.region`/`row.region_source` must
   equal that node's `region`/`region_source`; a mismatch is a contract rejection.
4. The product snapshot may carry a bounded, closed `coarse_region_evidence` list of reviewed
   records. The projection is a pure function of the snapshot: the published evidence set
   supplies the accepted authority digests, so `explorer == project_route_explorer(snapshot)`
   still holds at `mycelium_product_spine/contracts.py:709`-`714`. A caller may apply a
   narrower accepted set (for example after a withdrawal or before review), which is how the
   `unauthorized` blocker is reached; the ordinary product path publishes reviewed records
   only.
5. Registration follows the A14a pattern: registry, manifest, Python and TypeScript
   validators, and the generated conformance fixture move together, and the manifest digest
   is refreshed by regeneration rather than hand-editing. A14b may not add the two protocols
   by editing a manifest alone. Because the node enum and the snapshot field change owner
   sources, A14b must also regenerate the manifest entries for `mycelium.route_explorer.v1`,
   `mycelium.product_snapshot.v1`, and the projector.
6. `overlays` remains exactly `'unavailable'` until a qualified A5–A7/A11 overlay authority
   is bound. A14b creates no overlay.
7. **Declared A14a supersession.** The A14a boundary case `test_semantic_rejection[region]`
   in `tests/a14_acceptance/test_route_projection_boundaries.py` asserts the A14a-era freeze
   by requiring `nodes[0]['region'] = 'europe'` to be rejected. Widening the enum supersedes
   that exact case and nothing else: A14b must replace its value with a non-vocabulary code
   such as `us-east-1` so the rejection still tests a forbidden value, and it must record the
   supersession in its acceptance record. The frozen inventory (`inventory.v1.json` and
   `test_inventory.py`), every other A14a case, and the A14a browser behaviour are not
   amended. A widening that silently deletes or weakens that case is not A14b acceptance.

## 7. Presentation and equivalence

The globe is a synchronized, optional projection of the same rows the table already shows.
It is `non-normative` in the frozen inventory sense: it owns no fact.

- The A14a table and unknown-region tray remain normative and complete. Disabling, failing,
  or omitting the globe loses no fact, filter, action explanation, or history.
- A non-unknown code may be associated with a fixed product-owned schematic display cell.
  The mapping is a closed product constant mirrored by the renderer; it carries no
  coordinate, and a cell is never asserted location:

  | Region code | Display cell |
  |---|---|
  | `africa` | `cell_africa` |
  | `americas_north` | `cell_americas_north` |
  | `americas_south` | `cell_americas_south` |
  | `asia` | `cell_asia` |
  | `europe` | `cell_europe` |
  | `oceania` | `cell_oceania` |
  | `polar` | `cell_polar` |
  | `unknown` | none; the peer stays off-globe in the tray |

  The cell vocabulary is schematic geometry only: it contains no digit, no coordinate, no
  geography, and no member data, and it is not part of any contract payload.
- Neither evidence nor projection carries a display cell or a coordinate, so the renderer
  cannot mint a position the table does not state.
- The globe uses no motion to carry meaning, and reduced-motion/low-power behaviour is
  unchanged from A14a. Map tiles or assets receive no member identifier, path state, seed
  origin, or telemetry; an offline or self-hosted asset path is preferred for release
  qualification.
- Selecting on the globe or the table selects the same row; a globe action cannot mutate a
  route, plan, evidence, or readiness.

## 8. Acceptance boundary

A14b remains `design_only` until its own deterministic, browser, physical-positive,
physical-negative, privacy, and accessibility acceptance executes, and until the completion
checklist row for A14b is executed rather than assumed. `a14b_before_a14a_rejected` binds the
slice order: this document and its RED suite are specification artifacts only and prove
nothing about A14b behavior, A14a acceptance, or A14 completion.

A14b closes only after: the vocabulary and evidence contracts are frozen and registered; the
reduction is implemented and its blockers, precedence, and off-globe behavior are exercised;
A14a's complete semantics survive a disabled globe (deterministic and browser); no
coordinate, cell, provider name, or raw identity appears in projection or rendering; region
evidence cannot be derived from network identity; and the A14b row of the completion
checklist is satisfied by executed evidence.

## 9. Machine-checked RED suite

`tests/a14b_acceptance/` holds the frozen specification suite for this design. At `b90edaf`
every test in it fails, because no A14b behavior exists: `mycelium_product_spine.coarse_region`
does not exist, `mycelium.route_explorer.v1` freezes `region` to `unknown`, and the projector
has no region evidence input. The suite is a specification of the target behavior, not a
claim that anything works; it may only be promoted to a passing gate when A14b executes.

| Test module | Binds |
|---|---|
| `test_coarse_region_contract.py` | Sections 3 and 4: vocabulary, normalization, evidence closure, privacy, digest |
| `test_route_explorer_regions.py` | Sections 5 and 6: reduction, blockers, off-globe, projection, product binding, equivalence |

Run from the repository root:

```sh
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/bin/python3 -B \
  -m pytest -q -p no:cacheprovider tests/a14b_acceptance
```
