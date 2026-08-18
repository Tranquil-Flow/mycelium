# Mycelium A14 Privacy-Preserving Route Explorer Specification

**Status:** `design_only`; split route-explorer acceptance boundary
**Gate:** A14
**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`
**Direct prerequisite:** A8 direct/relay observations; A1 live/sealed separation is
already transitive
**Slice order:** A14a accessible table/logical graph, then A14b coarse globe/region
visualization; relevant A5–A7 and A11 authorities gate only their optional overlays

## 1. Outcome and claim boundary

A14 adds an understandable geographic/logical route explorer to the existing Network
workspace. It shows where the operator has chosen to place a peer at coarse resolution,
which privacy-safe relay region was observed, or that region is unknown. It never
geolocates an IP address, reveals exact coordinates, or creates planning, topology,
qualification, liveness, or inference authority.

The explorer is a read-only projection of the same live authorities used by Network,
Plans, Nodes, Readiness, Incidents, and Inference. Fixture, replay, modeled, proposed,
selected, observed, failed, recovered, historical, direct, relay, and unknown states are
independent and visibly labelled. An animation, rendered line, map position, or latency
estimate cannot satisfy a physical claim.

### 1.1 Delivery split

**A14a — Accessible route table and logical graph.** A14a owns the normative route
table/timeline and a synchronized logical graph. It includes every pseudonymous peer,
unknown-region tray, path generation, direction, phase, transport class, provenance,
freshness, transition, and qualified optional overlay without requiring map assets,
coordinates, spatial reasoning, pointer input, canvas, WebGL, or animation. A14a can be
accepted independently after A8 live authority exists. It does not wait for geographic
evidence and cannot emit a globe or inferred position.

**A14b — Coarse globe/region visualization.** A14b begins only after A14a is accepted and
the shared region vocabulary below is frozen. It adds a synchronized, optional coarse
globe/region view for explicitly authorized geographic region evidence. The A14a table
remains normative and complete; disabling or failing the globe loses no fact, filter,
action explanation, or history. A14b consumes the same read-only projection and cannot
create region, path, transport, planning, qualification, liveness, or evidence authority.

A14a completion does not promote A14b or the combined A14 gate. Both slices remain
`design_only` until their own implementation, accessibility, privacy, browser, and
physical acceptance executes.

## 2. Coarse-region authority and privacy budget

`mycelium.coarse_region_evidence.v1` accepts exactly one reviewed source:

- an owner-declared coarse region selected from a versioned bounded region vocabulary;
- an A8 privacy-safe relay reference with reviewed coarse relay region; or
- a labelled non-geographic latency-distance class derived from current bounded
  measurements and its declared model.

The evidence binds pseudonymous member/relay reference, source kind, vocabulary/model
version, issue/expiry, signer/owner authority, uncertainty, and canonical digest. It
never accepts IP-geolocation output, GPS, Wi-Fi/cell location, street/city address,
precise latitude/longitude, hostname, raw EndpointID, relay URL, or private address.

The minimum geographic cell/region size and retention period are frozen before physical
testing. A declared region is not verified physical location. Relay region locates the
relay only, never either peer. A latency-distance class is not geography and renders in
the logical/unknown tray rather than on a geographic coordinate.

Missing, expired, conflicting, withdrawn, or unauthorized evidence yields `unknown`.
Unknown peers remain fully inspectable in an explicit tray and logical topology; they are
never hidden or assigned a default coordinate. Zero values never substitute for missing
measurements.

### 2.1 Shared coarse-region vocabulary

`mycelium.coarse_region.v1` is the only geographic vocabulary shared by owner
declaration, reviewed A8 relay metadata, the read-only projection, and A14b rendering:

- `africa`;
- `americas_north`;
- `americas_south`;
- `asia`;
- `europe`;
- `oceania`;
- `polar`; and
- `unknown`.

The vocabulary is deliberately macro-regional. It contains no country, subdivision,
city, site, availability zone, latitude, longitude, geohash, network prefix, provider
region name, or free-form location. Central America and the Caribbean normalize to
`americas_north`; Antarctica and Arctic-only declarations normalize to `polar`.
Unmapped, ambiguous, conflicting, stale, or unauthorized values normalize to `unknown`,
never to the nearest or default region.

An owner selects a value explicitly through separate authenticated authority. A relay
region enters only through a reviewed versioned mapping from private relay metadata to
this vocabulary and locates the relay, not a peer. No IP address, DNS name, hostname,
EndpointID, RTT, jitter, loss, goodput, route shape, time zone, locale, or device setting
may derive or refine a geographic value. A14b may associate each non-unknown code with a
fixed product-owned schematic display cell, but neither evidence nor projection carries
coordinates and the cell is not asserted location. `unknown` and non-geographic
latency-distance classes remain off-globe in the A14a logical/unknown presentation.

## 3. Path and generation model

The explorer consumes immutable path records and never joins them by display name. Every
segment binds deployment, request/attempt where applicable, path/track generation,
ordered pseudonymous endpoints, directed edge, transport observation, phase, evidence
source, issue/expiry, and authority digest.

The following dimensions remain independently filterable:

- physical membership edge versus logical execution edge;
- prefill forward, decode forward, decode closure, and control/probe traffic;
- proposed, selected, observed, failed, recovered, and historical generation;
- primary, alternative, replica, recovery predecessor/successor, and optional draft;
- direct, relay, and unknown transport; and
- live, stale, disconnected, sealed replay, and fixture provenance.

Observed activation requires current bound Router/transport evidence. Proposed and
selected paths never animate as traffic. Direct/relay transitions create new observed
segments; they do not rewrite the earlier observation. Recovery cutover keeps the old
route ghosted/historical and the successor separately active. Replica traffic shows the
exact request track, not every theoretically legal combination.

## 4. Visual and accessible presentations

The Network workspace offers three synchronized presentations:

1. **Coarse globe/map:** only operator-declared or relay-region geographic evidence.
2. **Logical route graph:** all peers and paths, including unknown-region members.
3. **Accessible route table/timeline:** the complete semantic authority, usable without
   color, pointer, animation, canvas, WebGL, or spatial reasoning.

The table is the normative accessible representation. It exposes path status, direction,
phase, source/destination pseudonyms, transport class, coarse-region source, nullable
metrics, freshness, request/track binding, generation, and evidence provenance.

Color is never the only state signal. Keyboard navigation, screen-reader names, focus
order, high contrast, text scaling, reduced motion, pause, and touch targets pass the
declared accessibility budgets. Reduced motion disables automatic animation. Low-power
mode uses static markers and bounded refresh. Large fleets use deterministic clustering
without changing counts or authority; expanding a cluster reveals its exact bounded
members in the table.

## 5. Closed projection and rendering safety

`mycelium.route_explorer.v1` is a closed, bounded, privacy-reduced projection containing
only pseudonymous nodes, coarse-region references, immutable path/segment facts,
nullable measurements, current/historical state, filters, and source digests. Rendering
order is deterministic so refresh/reconnect does not imply a route change.

The contract rejects exact coordinates, overly precise region values, raw network
identity, unbounded labels/traces, private paths, credentials, prompts, outputs, token
IDs, logits, tensors, activations, or KV content. Display labels are fixed product copy
or bounded pseudonyms, never unsanitized peer input. Map tiles/assets cannot receive
member identifiers, path state, seed origin, or telemetry; an offline/self-hosted asset
path is preferred for release qualification.

The UI cannot mutate planner input, region evidence, selection, route, membership,
qualification, or incident state. Owner region declaration lives in Settings through a
separate authenticated action and produces new signed evidence.

## 6. Eight-workspace convergence

- **Inference:** links a request to its exact observed route timeline without exposing
  private payloads.
- **Device Lab:** explains coarse-region consent and shows unknown as a valid state.
- **Network:** globe/map, logical graph, accessible table, filters, trace, transitions,
  and provenance.
- **Nodes:** shows the same coarse-region source/status, path class, and freshness.
- **Plans:** proposed/selected topology and costs remain modeled/planned, distinct from
  observed route segments.
- **Readiness:** verifies region privacy, transport observation, trace binding, and
  freshness separately; region is never serving readiness.
- **Incidents:** retains route transition, failure, recovery, and evidence withdrawal on
  a bounded generation timeline.
- **Settings:** owner region declaration/withdrawal, privacy/retention, reduced motion,
  low power, map asset, and redacted export policy.

All views reconstruct from the same current public generation after direct navigation,
refresh, Back/Forward, workspace switching, reconnect, stale/degraded authority, and a
clean second session.

## 7. Machine-checked acceptance inventory

The inventory freezes these specification decisions:

- `coarse_region_authority` and `unknown_region_handling` bind Section 2;
- `direct_relay_transition_evidence` and `independent_path_transport_dimensions` bind
  Section 3;
- `accessible_alternatives` and `reduced_motion_low_power` bind Section 4; and
- `privacy_no_ip_coordinates` and `read_only_projection_authority` bind Section 5.

`tests/a14_acceptance/inventory.v1.json` is the closed machine-readable inventory for
the A14a/A14b dependency split, `mycelium.coarse_region.v1` vocabulary, projection
authority, privacy restrictions, independent path/transport dimensions, accessible
alternatives, and reduced-motion/low-power behavior. Its protocol is
`mycelium.a14_acceptance_inventory.v1`; its claim boundary is frozen design and future
acceptance input only. It contains no production UI, map asset, coordinate, network
observation, membership record, evidence, readiness, or completion claim.
Repository-local tests enforce exact slice order, vocabulary, decision/case sets,
privacy vocabulary, accessible equivalence, read-only behavior, specification binding,
and `design_only` state.

## 8. Verification and completion

Contract/deterministic tests cover closed shape, canonical digest, privacy, region
vocabulary, precision rejection, source precedence, expiry/conflict/withdrawal, unknown
tray, path generation, transition history, filter independence, clustering stability,
render ordering, bounded size, and hostile label rejection.

Browser tests cover accessible table equivalence, keyboard and screen-reader flow,
high contrast, text scaling, reduced motion, pause, low-power mode, responsive layouts,
large-fleet performance, refresh/reconnect/history, all route filters, unknown region,
and clean-session privacy.

Physical positive: from bound live evidence, show one proposed path, actual direct path,
forced-relay path, observed direct/relay transition, replica or recovery overlay when its
own gate is qualified, and terminal route history surviving refresh. Frame and transport
counters—not animation—prove movement.

Physical negative: absent/conflicting/expired region and link evidence remain unknown;
raw address/coordinate injection is rejected; historical/sealed evidence cannot render
live; a globe action cannot alter a route or readiness; reduced-motion/table-only use
retains all information.

A14a may close only after its complete table/logical-graph semantics, live
direct/relay/transition evidence, read-only/privacy negatives, and accessible browser
checks pass without a globe dependency. A14b may close only after A14a, the frozen region
vocabulary, optional-globe equivalence, unknown-region, no-coordinate, reduced-motion,
low-power, and privacy gates pass. The combined A14 gate completes only after both slices,
all-eight-workspace checks, full regressions, slice commits, and one atomic A14
integration commit that binds both accepted slices.
Until then A14, A14a, and A14b remain `design_only`.
